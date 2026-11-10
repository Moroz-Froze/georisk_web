from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from threading import RLock
import time
from uuid import uuid4
from .storage import identifier, read_json, write_json


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, root):
        self.root = Path(root)
        (self.root/'jobs').mkdir(parents=True, exist_ok=True)
        (self.root/'uploads').mkdir(exist_ok=True)
        self.lock = RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hazard-queue')
        self.stopping = False
        for path in (self.root/'jobs').glob('*/job.json'):
            job = read_json(path)
            if job['state'] in ('queued', 'running'):
                job.update(state='interrupted', message='Сервер перезапущен. Запустите расчёт заново.')
                write_json(path, job)

    def folder(self, job_id):
        folder = self.root/'jobs'/identifier(job_id)
        if not (folder/'job.json').is_file():
            raise FileNotFoundError('Расчёт не найден.')
        return folder

    def read(self, job_id, full=True):
        folder = self.folder(job_id)
        job = read_json(folder/'job.json')
        if full and (folder/'request.json').exists():
            job['request'] = read_json(folder/'request.json')
        if job['state'] == 'running' and (folder/'progress.json').exists():
            job.update(read_json(folder/'progress.json'))
        if full and job['state'] == 'completed':
            job['result'] = read_json(folder/'result.json')
        return job

    def list(self):
        jobs = [self.read(path.parent.name, full=False) for path in (self.root/'jobs').glob('*/job.json')]
        return sorted(jobs, key=lambda job: job['created_at'], reverse=True)

    def submit(self, request):
        with self.lock:
            if sum(j['state'] in ('queued', 'running') for j in self.list()) >= 8:
                raise ValueError('Очередь заполнена. Дождитесь завершения расчётов.')
            job_id = str(uuid4())
            folder = self.root/'jobs'/job_id
            folder.mkdir()
            write_json(folder/'request.json', request)
            job = dict(id=job_id, hazard=request['hazard'], bbox=request['bbox'], state='queued', progress=0,
                       message='В очереди на расчёт', created_at=timestamp())
            write_json(folder/'job.json', job)
            self.pool.submit(self._execute, folder)
            return job

    def cancel(self, job_id):
        with self.lock:
            folder = self.folder(job_id)
            job = self.read(job_id, False)
            if job['state'] in ('queued', 'running'):
                (folder/'cancel.flag').touch()
            return job

    def _execute(self, folder):
        with self.lock:
            job = read_json(folder/'job.json')
            if self.stopping or (folder/'cancel.flag').exists():
                job.update(state='cancelled', message='Расчёт отменён')
                write_json(folder/'job.json', job)
                return
            job.update(state='running', message='Подготовка исходных данных…')
            write_json(folder/'job.json', job)
        started = time.monotonic()
        try:
            with (folder/'worker.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-m', 'webapp.backend.worker', str(folder)],
                                           cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                while process.poll() is None:
                    if self.stopping or (folder/'cancel.flag').exists() or time.monotonic()-started > 1800:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                    time.sleep(.15)
            if self.stopping or (folder/'cancel.flag').exists():
                job.update(state='cancelled', message='Расчёт отменён')
            elif process.returncode == 0 and (folder/'result.json').exists():
                job.update(state='completed', progress=100, message='Расчёт завершён')
            else:
                error = read_json(folder/'error.json')['message'] if (folder/'error.json').exists() else 'Превышено время расчёта или расчётный процесс остановлен.'
                job.update(state='failed', message=error)
        except Exception as exc:
            job.update(state='failed', message=str(exc))
        job['finished_at'] = timestamp()
        write_json(folder/'job.json', job)

    def close(self):
        self.stopping = True
        self.pool.shutdown(wait=True, cancel_futures=False)
