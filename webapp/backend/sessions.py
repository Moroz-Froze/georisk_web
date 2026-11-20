"""Ephemeral private workspaces, shared calculation queue, and safe cleanup."""
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import secrets
import shutil
from threading import BoundedSemaphore, Event, Lock, RLock, Thread
import time
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from .jobs import CalculationQueue, JobManager

COOKIE = 'georisk_session'


class DataDirectoryLock:
    """Prevent a second server from purging workspaces owned by a live one."""
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root/'.session.lock').open('a+b')
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise RuntimeError('Папка данных уже используется другим сервером ГеоРиск. '
                               'Остановите другой экземпляр или выберите отдельную HAZARD_DATA_DIR.') from error

    def close(self):
        # Closing the descriptor releases the lock on both supported platforms.
        self.stream.close()


@dataclass
class Session:
    token: str
    workspace_id: str
    csrf: str
    root: Path
    jobs: JobManager
    tabs: dict = field(default_factory=dict)
    close_at: float | None = None
    closing: bool = False
    active_requests: int = 0
    upload_bytes: int = 0
    upload_count: int = 0
    disposal_lock: Lock = field(default_factory=Lock)


class SessionStore:
    idle_seconds = 600
    grace_seconds = 15

    def __init__(self, root, clock=time.monotonic):
        self.directory_lock = DataDirectoryLock(root)
        self.root = (Path(root)/'sessions').resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            self.root.chmod(0o700)
        self.clock = clock
        self.lock = RLock()
        self.sessions = {}
        self.pending = {}
        self.queue = CalculationQueue(self.root)
        self.upload_slots = BoundedSemaphore(2)
        self.upload_bytes = 0
        self.session_starts = {}
        self.all_starts = []
        self.stop_event = Event()
        # Only this namespace is ephemeral; pre-existing shared history is not
        # assigned to a new user and is never removed by migration.
        for directory in self.root.iterdir():
            if self._owned_directory(directory):
                shutil.rmtree(directory)
        self.thread = Thread(target=self._sweep, name='session-cleanup', daemon=True)
        self.thread.start()

    def _owned_directory(self, path):
        try:
            UUID(path.name)
        except ValueError:
            return False
        return (path.is_dir() and not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)()
                and path.resolve().parent == self.root)

    def _expired(self, session):
        now = self.clock()
        if session.close_at is not None and now >= session.close_at:
            return True
        return not any(now-seen < self.idle_seconds for seen in session.tabs.values()) and session.close_at is None

    def begin(self, token, tab, peer='local'):
        self.collect()
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                self._limit_new_session(peer)
                if len(self.sessions)+len(self.pending) >= 64:
                    raise HTTPException(429, 'Сервер занят. Повторите подключение позже.')
                token = secrets.token_urlsafe(32)
                workspace_id = str(uuid4())
                root = self.root/workspace_id
                session = Session(token, workspace_id, secrets.token_urlsafe(32), root,
                                  JobManager(root, pool=self.queue))
                self.sessions[token] = session
            self._touch_tab(session, tab)
            session.close_at = None
            return session

    def _limit_new_session(self, peer):
        now = self.clock()
        self.session_starts = {key:[t for t in times if now-t < 60] for key,times in self.session_starts.items()
                               if any(now-t < 60 for t in times)}
        self.all_starts = [t for t in self.all_starts if now-t < 60]
        times = self.session_starts.get(peer, [])
        if len(times) >= 12 or len(self.all_starts) >= 120:
            raise HTTPException(429, 'Слишком много новых подключений. Повторите через минуту.')
        self.session_starts[peer] = times+[now]
        self.all_starts.append(now)

    def lookup(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session is None or session.closing or self._expired(session):
                raise HTTPException(401, 'Сессия завершена. Начните новую работу.')
            return session

    def acquire(self, token):
        with self.lock:
            session = self.lookup(token)
            session.active_requests += 1
            return session

    def release(self, session):
        with self.lock:
            session.active_requests -= 1

    def heartbeat(self, token, tab):
        with self.lock:
            session = self.lookup(token)
            self._touch_tab(session, tab)
            session.close_at = None
            return session

    def _touch_tab(self, session, tab):
        now = self.clock()
        session.tabs = {key: seen for key, seen in session.tabs.items() if now-seen < self.idle_seconds}
        if str(tab) not in session.tabs and len(session.tabs) >= 16:
            raise HTTPException(429, 'Открыто слишком много вкладок. Закройте лишние.')
        session.tabs[str(tab)] = now

    def begin_upload(self, session):
        if not self.upload_slots.acquire(blocking=False):
            raise HTTPException(429, 'Сервер принимает другие файлы. Повторите загрузку позже.')
        with self.lock:
            if session.closing or session.upload_count >= 32:
                self.upload_slots.release()
                raise HTTPException(429, 'Лимит 32 файла за сессию. Начните новую работу.')
            session.upload_count += 1

    def add_upload_bytes(self, session, size):
        with self.lock:
            if session.upload_bytes+size > 512*1024*1024 or self.upload_bytes+size > 2*1024**3:
                raise HTTPException(413, 'Превышена общая квота загрузок: 512 МиБ за сессию и 2 ГиБ на сервер.')
            session.upload_bytes += size
            self.upload_bytes += size

    def finish_upload(self, session, size, success):
        with self.lock:
            if not success:
                session.upload_count -= 1
                session.upload_bytes -= size
                self.upload_bytes -= size
        self.upload_slots.release()

    def leave(self, token, tab):
        with self.lock:
            session = self.lookup(token)
            session.tabs.pop(str(tab), None)
            now = self.clock()
            if not any(now-seen < self.idle_seconds for seen in session.tabs.values()):
                session.close_at = now+self.grace_seconds

    def _retire(self, session):
        session.closing = True
        self.sessions.pop(session.token, None)
        self.pending[session.token] = session

    def _dispose(self, session):
        if not session.disposal_lock.acquire(blocking=False):
            return False
        try:
            session.jobs.close()  # cancel queued work; stop owned workers first
            with self.lock:
                if session.active_requests:
                    return False  # finish/abort uploads and downloads before removal
            if session.root.exists():
                if not self._owned_directory(session.root):
                    raise RuntimeError('Unsafe session directory; cleanup refused.')
                shutil.rmtree(session.root)
            with self.lock:
                self.pending.pop(session.token, None)
                self.upload_bytes -= session.upload_bytes
                session.upload_bytes = 0
            return True
        except OSError:
            logging.getLogger('uvicorn.error').exception('Session file cleanup will be retried')
            return False
        finally:
            session.disposal_lock.release()

    def end(self, token):
        with self.lock:
            session = self.sessions.get(token) or self.pending.get(token)
            if session is None:
                return True
            self._retire(session)
        return self._dispose(session)

    def collect(self):
        with self.lock:
            for session in list(self.sessions.values()):
                if self._expired(session):
                    self._retire(session)
            pending = list(self.pending.values())
        for session in pending:
            self._dispose(session)

    def _sweep(self):
        while not self.stop_event.wait(5):
            try:
                self.collect()
            except Exception:
                logging.getLogger('uvicorn.error').exception('Session cleanup failed')

    def close(self):
        self.stop_event.set()
        self.thread.join()
        with self.lock:
            for session in list(self.sessions.values()):
                self._retire(session)
        self.collect()
        try:
            self.queue.shutdown(wait=True, cancel_futures=True)
        finally:
            self.directory_lock.close()


def check_csrf(session, value):
    if not isinstance(value, str) or not secrets.compare_digest(session.csrf, value):
        raise HTTPException(403, 'Недействительный запрос сессии.')


class SessionMiddleware:
    """Keep the session leased through the entire response body/file transfer."""
    PUBLIC = {'/api/health', '/api/catalog', '/api/session', '/api/session/heartbeat',
              '/api/session/leave', '/api/session/end'}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path']
        api = path.startswith('/api/')
        request = Request(scope)
        session = None
        store = scope['app'].state.sessions if api and path not in self.PUBLIC else None
        try:
            if store is not None:
                session = store.acquire(request.cookies.get(COOKIE))
                if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                    check_csrf(session, request.headers.get('x-georisk-csrf'))
                scope.setdefault('state', {})['user'] = session
        except HTTPException as error:
            if session is not None:
                store.release(session)
            response = JSONResponse({'detail': error.detail}, status_code=error.status_code,
                                    headers={'Cache-Control': 'no-store'})
            logging.getLogger('uvicorn.error').warning('session_rejection status=%s request_id=%s',
                error.status_code, scope.get('state', {}).get('request_id', '-'))
            return await response(scope, receive, send)

        async def uncached(message):
            if message['type'] == 'http.response.start' and (api or path in ('/', '/index.html')):
                headers = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'cache-control']
                message['headers'] = headers+[(b'cache-control', b'no-store')]
            await send(message)
        try:
            await self.app(scope, receive, uncached)
        finally:
            if session is not None:
                store.release(session)
