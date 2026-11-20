"""Bounded child process: untrusted raster/JSON/CSV never parsed in the HTTP loop."""
import asyncio
import json
import os
from pathlib import Path
import sys

from .geojson import load_geojson


async def validate_upload(path):
    env = {**os.environ, 'GDAL_DISABLE_READDIR_ON_OPEN':'EMPTY_DIR', 'GDAL_PAM_ENABLED':'NO'}
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-B', '-m', 'webapp.backend.upload_validation', str(path),
        cwd=Path(__file__).resolve().parents[2], env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except asyncio.TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise ValueError('Проверка файла превысила 20 секунд.') from None
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        try:
            detail = json.loads(stdout).get('detail')
        except (ValueError, AttributeError):
            detail = None
        raise ValueError(detail[:1000] if isinstance(detail,str) and detail else 'Не удалось безопасно прочитать файл. Проверьте его формат.')


def validate_file(path):
    if path.suffix in ('.tif', '.tiff'):
        from osgeo import gdal
        gdal.UseExceptions()
        try:
            ds = gdal.OpenEx(str(path), allowed_drivers=['GTiff'])
            if ds.RasterCount != 1 or not ds.GetProjection():
                raise ValueError('Нужен одноканальный GeoTIFF высот с системой координат.')
            if not 0 < ds.RasterXSize*ds.RasterYSize <= 100_000_000:
                raise ValueError('Размер GeoTIFF превышает 100 млн пикселей. Обрежьте исходный растр.')
            ds = None
        except RuntimeError as exc:
            raise ValueError('Не удалось прочитать GeoTIFF.') from exc
    elif path.suffix == '.geojson':
        items = load_geojson(path)
        from osgeo import ogr
        for item in items:
            geometry = ogr.CreateGeometryFromJson(json.dumps(item['geometry']))
            if geometry is None or geometry.IsEmpty() or not geometry.IsValid():
                raise ValueError('Некорректная геометрия GeoJSON.')
    elif path.suffix == '.csv':
        from wildfire_scenario.sources import load_weather_csv
        load_weather_csv(path)


if __name__ == '__main__':
    try:
        validate_file(Path(sys.argv[1]))
    except ValueError as error:
        print(json.dumps({'detail': str(error)}, ensure_ascii=True))
        raise SystemExit(1)
    except Exception:
        raise SystemExit(1)
