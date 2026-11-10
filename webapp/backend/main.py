from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from osgeo import gdal
from .catalog import catalog
from .inputs import config_for
from .jobs import JobManager
from .schemas import JobRequest
from .storage import write_json

ROOT = Path(__file__).resolve().parents[2]


def create_app(data_root=None):
    root = Path(data_root or os.environ.get('HAZARD_DATA_DIR', ROOT/'webapp'/'data')).resolve()
    @asynccontextmanager
    async def lifespan(app):
        app.state.jobs = JobManager(root)
        yield
        app.state.jobs.close()
    app = FastAPI(title='ГеоРиск · моделирование природных опасностей', version='1.0.0', lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, exc):
        errors = [{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()]
        return JSONResponse(status_code=422, content={'detail': errors})

    @app.exception_handler(FileNotFoundError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={'detail': 'Файл или расчёт не найден.'})

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={'detail': str(exc)})

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'gdal': gdal.VersionInfo('--version')}

    @app.get('/api/catalog')
    def get_catalog():
        return catalog()

    @app.post('/api/uploads', status_code=201)
    async def upload(file: UploadFile = File(...)):
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in ('.tif', '.tiff', '.geojson', '.csv'):
            raise ValueError('Поддерживаются GeoTIFF, GeoJSON и CSV.')
        asset_id = str(uuid4())
        directory = root/'uploads'/asset_id
        directory.mkdir(parents=True)
        path = directory/('data'+suffix)
        size = 0
        try:
            with path.open('wb') as stream:
                while chunk := await file.read(1024*1024):
                    size += len(chunk)
                    if size > (200 if suffix in ('.tif', '.tiff') else 10)*1024*1024:
                        raise ValueError('Слишком большой файл: до 200 МБ для GeoTIFF, до 10 МБ для GeoJSON/CSV.')
                    stream.write(chunk)
            if not size:
                raise ValueError('Файл пуст.')
            if suffix in ('.tif', '.tiff'):
                try:
                    ds = gdal.OpenEx(str(path), allowed_drivers=['GTiff'])
                    if ds is None or ds.RasterCount != 1 or not ds.GetProjection():
                        raise ValueError('Нужен одноканальный GeoTIFF высот с системой координат.')
                    ds = None
                except RuntimeError as exc:
                    raise ValueError('Не удалось прочитать GeoTIFF.') from exc
            elif suffix == '.geojson':
                try:
                    payload = json.loads(path.read_text(encoding='utf-8-sig'))
                except (ValueError, UnicodeError) as exc:
                    raise ValueError('Не удалось прочитать GeoJSON UTF-8.') from exc
                if not isinstance(payload, dict) or payload.get('type') not in ('Feature', 'FeatureCollection'):
                    raise ValueError('Нужен GeoJSON Feature или FeatureCollection в WGS 84.')
            meta = dict(id=asset_id, name=Path(file.filename).name, suffix=suffix, size=size)
            write_json(directory/'asset.json', meta)
            return meta
        except Exception:
            shutil.rmtree(directory)
            raise
        finally:
            await file.close()

    @app.post('/api/jobs', status_code=202)
    def submit(request: JobRequest):
        payload = request.model_dump(mode='json')
        # Validate asset ownership/format and geometry before occupying the queue.
        config_for(payload, root, root/'unused')
        return app.state.jobs.submit(payload)

    @app.get('/api/jobs')
    def list_jobs():
        return app.state.jobs.list()

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        return app.state.jobs.read(job_id)

    @app.post('/api/jobs/{job_id}/cancel', status_code=202)
    def cancel(job_id: str):
        return app.state.jobs.cancel(job_id)

    def completed_folder(job_id):
        if app.state.jobs.read(job_id, False)['state'] != 'completed':
            raise HTTPException(409, 'Результат ещё не готов.')
        return app.state.jobs.folder(job_id)

    @app.get('/api/jobs/{job_id}/previews/{filename}')
    def image(job_id: str, filename: str):
        folder = completed_folder(job_id)
        if Path(filename).name != filename or not filename.endswith('.png'):
            raise FileNotFoundError()
        path = folder/'previews'/filename
        if not path.is_file():
            raise FileNotFoundError()
        return FileResponse(path, media_type='image/png')

    @app.get('/api/jobs/{job_id}/files/{filename:path}')
    def file(job_id: str, filename: str):
        folder = (completed_folder(job_id)/'result').resolve()
        path = (folder/filename).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise FileNotFoundError()
        response = FileResponse(path, filename=path.name, content_disposition_type='inline' if path.suffix=='.html' else 'attachment')
        if path.suffix == '.html':
            response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox allow-downloads"
        return response

    @app.get('/api/jobs/{job_id}/archive')
    def archive(job_id: str):
        return FileResponse(completed_folder(job_id)/'results.zip', filename=f'georisk-{job_id[:8]}.zip')

    frontend = ROOT/'webapp'/'frontend'/'dist'
    if frontend.exists():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
    return app


app = create_app()
