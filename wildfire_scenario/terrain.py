"""Terrain IO, public Copernicus COG access and screening. Runs off the UI thread."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()


def spatial_ref(definition):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(definition)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def transformed_geometry(wkt, source_crs, target_crs):
    geom = ogr.CreateGeometryFromWkt(wkt)
    if geom is None or geom.IsEmpty() or not geom.IsValid():
        raise ValueError('Выделите корректный полигон участка.')
    src, dst = spatial_ref(source_crs), spatial_ref(target_crs)
    geom.Transform(osr.CoordinateTransformation(src, dst))
    return geom


def tile_url(lat, lon):
    ns, ew = ('N' if lat >= 0 else 'S'), ('E' if lon >= 0 else 'W')
    name = f'Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM'
    return f'https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif'


@contextmanager
def network_options():
    options = {'GDAL_HTTP_TIMEOUT': '30', 'GDAL_HTTP_CONNECTTIMEOUT': '10',
               'GDAL_HTTP_MAX_RETRY': '2', 'GDAL_HTTP_RETRY_DELAY': '1',
               'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
               'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif,.vrt'}
    previous = {k: gdal.GetThreadLocalConfigOption(k) for k in options}
    try:
        for k, v in options.items():
            gdal.SetThreadLocalConfigOption(k, v)
        yield
    finally:
        for k, v in previous.items():
            gdal.SetThreadLocalConfigOption(k, v)


def rasterize_geometry(geom, shape, transform, projection, all_touched=False):
    mem = gdal.GetDriverByName('MEM').Create('', shape[1], shape[0], 1, gdal.GDT_Byte)
    mem.SetGeoTransform(transform)
    mem.SetProjection(projection)
    vectors = ogr.GetDriverByName('Memory').CreateDataSource('')
    layer = vectors.CreateLayer('mask', spatial_ref(projection), geom_type=geom.GetGeometryType())
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(geom)
    layer.CreateFeature(feature)
    gdal.RasterizeLayer(mem, [1], layer, burn_values=[1],
                        options=['ALL_TOUCHED=TRUE'] if all_touched else [])
    return mem.ReadAsArray().astype(bool)


def prepare_terrain(area_wkt, area_crs, directory, local_dem='', buffer_m=3000,
                    resolution=30, notify=None, cancelled=None):
    if not math.isfinite(buffer_m) or not math.isfinite(resolution) or buffer_m < 0 or resolution <= 0:
        raise ValueError('Буфер должен быть неотрицательным, а шаг сетки — положительным.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    geographic = transformed_geometry(area_wkt, area_crs, 'EPSG:4326')
    lon, lat, _ = geographic.Centroid().GetPoint()
    if abs(lat) > 80:
        raise ValueError('Автоматический режим поддерживает широты от 80° ю.ш. до 80° с.ш.')
    if geographic.GetEnvelope()[1]-geographic.GetEnvelope()[0] > 6:
        raise ValueError('Участок слишком широк или пересекает линию смены дат. Разбейте его.')
    utm = f'EPSG:{32600+min(60, max(1, int((lon+180)/6)+1)) if lat >= 0 else 32700+min(60, max(1, int((lon+180)/6)+1))}'
    area = transformed_geometry(area_wkt, area_crs, utm)
    if area.GetArea() <= 0 or area.GetArea() > 2500e6:
        raise ValueError('Площадь выделения должна быть больше нуля и не более 2500 км².')
    region = area.Buffer(buffer_m)
    xmin, xmax, ymin, ymax = region.GetEnvelope()
    if (xmax-xmin)*(ymax-ymin)/resolution**2 > 2000000:
        raise ValueError('Слишком большая сетка (>2 млн ячеек). Уменьшите участок или буфер.')
    sources, datasets = [], []
    if local_dem:
        source = gdal.Open(local_dem)
        if source is None or not source.GetProjection():
            raise ValueError('ЦМР не читается или у неё не задана система координат.')
        if source.RasterCount != 1:
            raise ValueError('ЦМР должна быть одноканальным растром высот, а не цветным снимком или картой.')
        units = source.GetRasterBand(1).GetUnitType().lower()
        if units and units not in ('m', 'meter', 'metre', 'meters', 'metres'):
            raise ValueError('Высоты ЦМР должны быть в метрах. Преобразуйте вертикальные единицы перед расчётом.')
        sources = [local_dem]
        datasets.append(source)
        source_note = {'kind': 'local_dem', 'path': str(local_dem)}
        saved_source = source.GetMetadataItem('WILDFIRE_TERRAIN_SOURCE') or source.GetMetadataItem('LANDSLIDE_TERRAIN_SOURCE') or source.GetMetadataItem('AVALANCHE_TERRAIN_SOURCE')
        if saved_source:
            try:
                saved_source = json.loads(saved_source)
                if isinstance(saved_source, dict) and saved_source.get('kind'):
                    source_note = dict(saved_source, cached_path=str(local_dem))
            except (ValueError, TypeError):
                pass
    else:
        bounds = transformed_geometry(region.ExportToWkt(), utm, 'EPSG:4326').GetEnvelope()
        tiles = [(la, lo) for la in range(math.floor(bounds[2]), math.ceil(bounds[3]))
                 for lo in range(math.floor(bounds[0]), math.ceil(bounds[1]))]
        if not tiles or len(tiles) > 16:
            raise ValueError('Нужно более 16 плиток рельефа. Разбейте участок на части.')
        sources = ['/vsicurl/'+tile_url(la, lo) for la, lo in tiles]
        source_note = {'kind': 'Copernicus GLO-30 Public DSM, 2021',
                       'urls': [s.replace('/vsicurl/', '') for s in sources],
                       'credit': '© DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018, provided under COPERNICUS by the European Union and ESA; all rights reserved.',
                       'license': 'https://copernicus-dem-30m.s3.amazonaws.com/readme.html'}
    destination = str(directory/'terrain.tif')
    def callback(completion, message, data):
        if notify:
            notify(5+completion*20, 'Загрузка и подготовка рельефа…')
        return 0 if cancelled and cancelled() else 1
    with network_options():
        try:
            if not local_dem:
                for i, path in enumerate(sources):
                    if cancelled and cancelled():
                        raise InterruptedError('Расчёт отменён.')
                    if notify:
                        notify(5, f'Открытие плитки рельефа {i+1}/{len(sources)}…')
                    datasets.append(gdal.Open(path))
            mosaic = gdal.BuildVRT('', datasets)
            ds = gdal.Warp(destination, mosaic, dstSRS=utm,
                           outputBounds=(xmin, ymin, xmax, ymax),
                           xRes=resolution, yRes=resolution, targetAlignedPixels=True,
                           resampleAlg='bilinear', dstNodata=-9999,
                           outputType=gdal.GDT_Float32,
                           creationOptions=['COMPRESS=DEFLATE', 'TILED=YES'],
                           callback=callback)
        except RuntimeError as exc:
            if cancelled and cancelled():
                raise InterruptedError('Расчёт отменён.') from exc
            raise ValueError('Не удалось получить ЦМР. Проверьте интернет, покрытие Copernicus '
                             'или выберите локальный GeoTIFF. Подробности: '+str(exc)) from exc
    if ds is None:
        raise ValueError('Не удалось подготовить ЦМР.')
    transform, projection = ds.GetGeoTransform(), ds.GetProjection()
    data = ds.ReadAsArray().astype(float)
    data[data == -9999] = np.nan
    shape = data.shape
    region_mask = rasterize_geometry(region, shape, transform, projection)
    target_mask = rasterize_geometry(area, shape, transform, projection)
    if not np.any(target_mask):
        raise ValueError('Выделение меньше ячейки ЦМР. Увеличьте участок или уменьшите разрешение.')
    coverage = np.count_nonzero(np.isfinite(data) & region_mask)/max(1, np.count_nonzero(region_mask))
    if coverage < .99:
        raise ValueError(f'Покрытие рельефом {coverage:.1%}; нужно не менее 99%. '
                         'Выберите ЦМР, которая покрывает участок вместе с буфером.')
    data[~region_mask] = np.nan
    ds.SetMetadataItem('WILDFIRE_TERRAIN_SOURCE', json.dumps(source_note, ensure_ascii=False))
    ds.FlushCache()
    ds = None
    return dict(dem=data, transform=transform, projection=projection,
                area_mask=target_mask, region=region, crs=utm, source=source_note,
                terrain_path=destination, coverage=coverage)


def write_raster(path, array, transform, projection, categorical=False):
    dtype, nodata = (gdal.GDT_Byte, 255) if categorical else (gdal.GDT_Float32, -9999)
    ds = gdal.GetDriverByName('GTiff').Create(str(path), array.shape[1], array.shape[0], 1,
                                            dtype, ['COMPRESS=DEFLATE', 'TILED=YES'])
    ds.SetGeoTransform(transform)
    ds.SetProjection(projection)
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(np.where(np.isfinite(array), array, nodata))
    band.FlushCache()
    ds = None
