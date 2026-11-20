from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .catalog import catalog
from .inputs import validate_assets
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from .sessions import COOKIE, SessionMiddleware, SessionStore, check_csrf
from .schemas import AreaRequest, JobRequest
from .area_limits import check_area
from .storage import write_json
from .identity import APPLICATION, data_directory, instance_id
from .security import SecurityMiddleware
from .deployment import public_base_path
from .upload_validation import validate_upload

ROOT = Path(__file__).resolve().parents[2]


class TabRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tab_id: UUID
    csrf: str | None = Field(default=None, max_length=128)


def create_app(data_root=None):
    root = Path(data_root).resolve() if data_root is not None else data_directory()
    @asynccontextmanager
    async def lifespan(app):
        app.state.sessions = SessionStore(root)
        try:
            yield
        finally:
            app.state.sessions.close()
    cookie_path = public_base_path()+'/'
    docs = os.environ.get('GEORISK_DOCS') == '1'
    app = FastAPI(title='ГеоРиск · моделирование природных опасностей', version='1.5.0', lifespan=lifespan,
                  docs_url='/docs' if docs else None, redoc_url=None, openapi_url='/openapi.json' if docs else None)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(SecurityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        errors = [{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()]
        return JSONResponse(status_code=422, content={'detail': errors})

    @app.exception_handler(FileNotFoundError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={'detail': 'Файл или расчёт не найден.'})

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={'detail': str(exc)[:1000]})

    def current_session(request):
        return app.state.sessions.lookup(request.cookies.get(COOKIE))

    @app.post('/api/session')
    def begin_session(payload: TabRequest, request: Request, response: Response):
        session = app.state.sessions.begin(request.cookies.get(COOKIE), payload.tab_id,
                                          request.client.host if request.client else 'unknown')
        response.set_cookie(COOKIE, session.token, httponly=True, samesite='strict',
                            secure=(request.url.scheme == 'https' or os.environ.get('GEORISK_COOKIE_SECURE') == '1'
                                    or os.environ.get('GEORISK_PUBLIC_ORIGIN', '').startswith('https://')), path=cookie_path)
        return dict(workspace_id=session.workspace_id, csrf=session.csrf,
                    idle_seconds=app.state.sessions.idle_seconds,
                    grace_seconds=app.state.sessions.grace_seconds)

    @app.post('/api/session/heartbeat')
    def heartbeat(payload: TabRequest, request: Request):
        session = current_session(request)
        check_csrf(session, request.headers.get('x-georisk-csrf') or payload.csrf)
        app.state.sessions.heartbeat(session.token, payload.tab_id)
        return {'active': True}

    @app.post('/api/session/leave')
    def leave(payload: TabRequest, request: Request):
        session = current_session(request)
        check_csrf(session, request.headers.get('x-georisk-csrf') or payload.csrf)
        app.state.sessions.leave(session.token, payload.tab_id)
        return {'closing': True}

    @app.post('/api/session/end')
    def end_session(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        try:
            session = current_session(request)
        except HTTPException as error:
            if error.status_code != 401:
                raise
        else:
            check_csrf(session, request.headers.get('x-georisk-csrf'))
        deleted = app.state.sessions.end(token)
        response.delete_cookie(COOKIE, path=cookie_path)
        return {'deleted': deleted}

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'application': APPLICATION, 'instance': instance_id(root)}

    @app.get('/api/catalog')
    def get_catalog():
        return catalog()

    @app.post('/api/uploads', status_code=201)
    async def upload(request: Request):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'multipart/form-data':
            raise HTTPException(415, 'Загрузка требует multipart/form-data.')
        user = request.state.user
        store = app.state.sessions
        store.begin_upload(user)
        directory = None
        size = 0
        success = False
        try:
            async with request.form(max_files=1, max_fields=0, max_part_size=1024) as form:
                file = form.get('file')
                if len(form) != 1 or not isinstance(file, UploadFile):
                    raise HTTPException(400, 'Передайте один файл в поле file.')
                suffix = Path(file.filename or '').suffix.lower()
                if suffix not in ('.tif', '.tiff', '.geojson', '.csv'):
                    raise ValueError('Поддерживаются GeoTIFF, GeoJSON и CSV.')
                asset_id = str(uuid4())
                directory = user.root/'uploads'/asset_id
                directory.mkdir(parents=True)
                path = directory/('data'+suffix)
                with path.open('wb') as stream:
                    while chunk := await file.read(1024*1024):
                        if user.closing:
                            raise ValueError('Сессия завершена.')
                        if size+len(chunk) > (200 if suffix in ('.tif', '.tiff') else 10)*1024*1024:
                            raise HTTPException(413, 'До 200 МиБ для GeoTIFF, до 10 МиБ для GeoJSON/CSV.')
                        store.add_upload_bytes(user, len(chunk))
                        size += len(chunk)
                        await run_in_threadpool(stream.write, chunk)
                if not size:
                    raise ValueError('Файл пуст.')
                await validate_upload(path)
                meta = dict(id=asset_id, name=Path(file.filename or '').name[:255], suffix=suffix, size=size)
                write_json(directory/'asset.json', meta)
                success = True
                return meta
        except BaseException:
            if directory is not None:
                shutil.rmtree(directory)
            raise
        finally:
            store.finish_upload(user, size, success)

    @app.post('/api/area-check')
    def area_check(request: AreaRequest):
        return check_area(request)

    @app.post('/api/jobs', status_code=202)
    def submit(request: JobRequest, http: Request):
        user = http.state.user
        assessment = check_area(request)
        if not assessment['valid']:
            raise ValueError(assessment['message'])
        payload = request.model_dump(mode='json')
        # Validate asset ownership/format and geometry before occupying the queue.
        validate_assets(payload, user.root)
        return user.jobs.submit(payload)

    @app.get('/api/jobs')
    def list_jobs(request: Request):
        return request.state.user.jobs.list()

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str, request: Request):
        return request.state.user.jobs.read(job_id)

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id: str, request: Request):
        try:
            return request.state.user.jobs.delete(job_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/jobs/{job_id}/cancel', status_code=202)
    def cancel(job_id: str, request: Request):
        return request.state.user.jobs.cancel(job_id)

    def completed_folder(job_id, user):
        if user.jobs.read(job_id, False)['state'] != 'completed':
            raise HTTPException(409, 'Результат ещё не готов.')
        return user.jobs.folder(job_id)

    @app.get('/api/jobs/{job_id}/previews/{filename}')
    def image(job_id: str, filename: str, request: Request):
        folder = completed_folder(job_id, request.state.user)
        if Path(filename).name != filename or not filename.endswith('.png'):
            raise FileNotFoundError()
        path = folder/'previews'/filename
        if not path.is_file():
            raise FileNotFoundError()
        return FileResponse(path, media_type='image/png')

    @app.get('/api/jobs/{job_id}/files/{filename:path}')
    def file(job_id: str, filename: str, request: Request):
        folder = (completed_folder(job_id, request.state.user)/'result').resolve()
        path = (folder/filename).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise FileNotFoundError()
        response = FileResponse(path, filename=path.name, content_disposition_type='inline' if path.suffix=='.html' else 'attachment')
        if path.suffix == '.html':
            response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox allow-downloads"
        return response

    @app.get('/api/jobs/{job_id}/archive')
    def archive(job_id: str, request: Request):
        return FileResponse(completed_folder(job_id, request.state.user)/'results.zip', filename=f'georisk-{job_id[:8]}.zip')

    frontend = ROOT/'webapp'/'frontend'/'dist'
    if frontend.exists():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
    return app


app = create_app()
