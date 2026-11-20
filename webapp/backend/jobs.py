from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import shutil
import sys
import logging
from threading import BoundedSemaphore, RLock
import time
from uuid import uuid4
from .storage import identifier, read_json, write_json

MAX_JOB_BYTES = 1024**3
MAX_SESSION_BYTES = 2*1024**3
MAX_SERVER_BYTES = 10*1024**3


def directory_size(root):
    total = 0
    for path in Path(root).rglob('*'):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # another expired session may be removed during this scan
    return total


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class CalculationQueue:
    """One bounded executor for all users, including queued calculations."""
    def __init__(self, storage_root=None):
        self.storage_root = Path(storage_root) if storage_root is not None else None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hazard-queue')
        self.slots = BoundedSemaphore(8)

    def submit(self, function, *arguments):
        if not self.slots.acquire(blocking=False):
            raise ValueError('Очередь заполнена. Дождитесь завершения расчётов.')
        try:
            future = self.pool.submit(function, *arguments)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        return future

    def shutdown(self, **options):
        self.pool.shutdown(**options)


class JobManager:
    def __init__(self, root, pool=None):
        self.root = Path(root)
        (self.root/'jobs').mkdir(parents=True, exist_ok=True)
        (self.root/'uploads').mkdir(exist_ok=True)
        self.lock = RLock()
        self.owns_pool = pool is None
        self.pool = pool if pool is not None else CalculationQueue()
        self.futures = set()
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
        with self.lock:
            jobs = [self.read(path.parent.name, full=False) for path in (self.root/'jobs').glob('*/job.json')]
            return sorted(jobs, key=lambda job: job['created_at'], reverse=True)

    def submit(self, request):
        with self.lock:
            if directory_size(self.root) >= MAX_SESSION_BYTES:
                raise ValueError('Достигнута квота 2 ГиБ данных сессии. Удалите старые расчёты.')
            if self.pool.storage_root and directory_size(self.pool.storage_root) >= MAX_SERVER_BYTES:
                raise ValueError('Достигнута общая квота данных сервера. Повторите позднее.')
            if shutil.disk_usage(self.root).free < MAX_JOB_BYTES:
                raise ValueError('Недостаточно свободного места на сервере.')
            jobs = self.list()
            if len(jobs) >= 20:
                raise ValueError('Лимит 20 расчётов за сессию. Удалите ненужные результаты.')
            if sum(j['state'] in ('queued', 'running') for j in jobs) >= 2:
                raise ValueError('Очередь заполнена. Дождитесь завершения расчётов.')
            job_id = str(uuid4())
            folder = self.root/'jobs'/job_id
            folder.mkdir()
            write_json(folder/'request.json', request)
            job = dict(id=job_id, hazard=request['hazard'], bbox=request['bbox'], state='queued', progress=0,
                       message='В очереди на расчёт', created_at=timestamp())
            write_json(folder/'job.json', job)
            if self.stopping:
                shutil.rmtree(folder)
                raise ValueError('Сессия завершена.')
            try:
                future = self.pool.submit(self._execute, folder)
            except Exception:
                shutil.rmtree(folder)
                raise
            self.futures.add(future)
            future.add_done_callback(self._finished)
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
        checked = started
        storage_exceeded = False
        process = None
        try:
            with (folder/'worker.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-m', 'webapp.backend.worker', str(folder)],
                                           cwd=Path(__file__).resolve().parents[2], stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                while process.poll() is None:
                    now = time.monotonic()
                    if now-checked >= 2:
                        checked = now
                        storage_exceeded = self._storage_exceeded(folder)
                    if storage_exceeded or self.stopping or (folder/'cancel.flag').exists() or now-started > 1800:
                        stop_process(process)
                        break
                    time.sleep(.15)
            if storage_exceeded or self._storage_exceeded(folder):
                job.update(state='failed', message='Расчёт остановлен: превышена квота диска. Уменьшите участок или удалите старые результаты.')
            elif self.stopping or (folder/'cancel.flag').exists():
                job.update(state='cancelled', message='Расчёт отменён')
            elif process.returncode == 0 and (folder/'result.json').exists():
                job.update(state='completed', progress=100, message='Расчёт завершён')
            else:
                error = read_json(folder/'error.json') if (folder/'error.json').exists() else {'message': 'Превышено время расчёта или расчётный процесс остановлен.'}
                job.update(state='failed', message=error['message'], error_code=error.get('code'), error_details=error.get('details'))
        except Exception:
            logging.getLogger('uvicorn.error').exception('Calculation failed job_id=%s', folder.name)
            job.update(state='failed', message='Не удалось выполнить расчёт. Сообщите идентификатор расчёта администратору.')
        finally:
            stop_process(process)
        job['finished_at'] = timestamp()
        write_json(folder/'job.json', job)

    def _storage_exceeded(self, folder):
        return (directory_size(folder) > MAX_JOB_BYTES or directory_size(self.root) > MAX_SESSION_BYTES
            or (self.pool.storage_root is not None and directory_size(self.pool.storage_root) > MAX_SERVER_BYTES))

    def delete(self, job_id):
        with self.lock:
            folder = self.folder(job_id)
            job = self.read(job_id, False)
            if job['state'] in ('queued', 'running'):
                raise RuntimeError('Сначала отмените расчёт и дождитесь его остановки.')
            # folder() accepts only UUIDs under this manager's jobs directory.
            # Uploaded assets can be referenced by other jobs and are retained.
            shutil.rmtree(folder)
            return {'deleted': job_id}

    def _finished(self, future):
        with self.lock:
            self.futures.discard(future)

    def close(self):
        with self.lock:
            self.stopping = True
            futures = list(self.futures)
            for future in futures:
                future.cancel()
        wait(futures)
        if self.owns_pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
