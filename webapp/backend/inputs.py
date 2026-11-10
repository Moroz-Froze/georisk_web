import json
from osgeo import ogr
from .storage import asset_path


def features(root, asset_id, allowed, required=()):
    if not asset_id:
        return []
    path = asset_path(root, asset_id, {'.geojson'})
    payload = json.loads(path.read_text(encoding='utf-8-sig'))
    items = payload.get('features', []) if payload.get('type') == 'FeatureCollection' else [payload]
    if not 0 < len(items) <= 10000:
        raise ValueError('GeoJSON должен содержать от 1 до 10 000 объектов.')
    records = []
    for i, feature in enumerate(items):
        if not isinstance(feature, dict):
            raise ValueError('GeoJSON содержит некорректный объект Feature.')
        geometry = feature.get('geometry')
        if not isinstance(geometry, dict) or geometry.get('type') not in allowed:
            raise ValueError('Неверная геометрия в GeoJSON: нужны ' + ', '.join(allowed))
        geom = ogr.CreateGeometryFromJson(json.dumps(geometry))
        if geom is None or geom.IsEmpty() or not geom.IsValid():
            raise ValueError('Некорректная геометрия GeoJSON.')
        x1, x2, y1, y2 = geom.GetEnvelope()
        if not (-180 <= x1 <= x2 <= 180 and -80 <= y1 <= y2 <= 80):
            raise ValueError('GeoJSON должен содержать долготу и широту WGS 84 (EPSG:4326).')
        props = feature.get('properties') or {}
        if any(key not in props for key in required):
            raise ValueError('Нет обязательных полей: ' + ', '.join(required))
        records.append({'id': str(feature.get('id', i)), 'wkt': geom.ExportToWkt(), 'values': props})
    return records


def config_for(request, root, directory):
    west, south, east, north = request['bbox']
    config = dict(area_wkt=f'POLYGON (({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))',
                  area_crs='EPSG:4326', directory=str(directory), parameters=request['parameters'],
                  buffer=request['buffer'] if request['buffer'] is not None else (3000 if request['hazard']=='avalanche' else 500),
                  resolution=request['resolution'], corridor=request['corridor'])
    if request.get('dem_id'):
        config['dem'] = str(asset_path(root, request['dem_id'], {'.tif', '.tiff'}))
    rails = features(root, request.get('rail_id'), {'LineString', 'MultiLineString'})
    if rails:
        config.update(rails=rails, rail_crs='EPSG:4326')
    if request['hazard'] == 'landslide':
        from landslide_scenario.core import SOIL_FIELDS, validate_soil
        zones = features(root, request.get('soil_id'), {'Polygon', 'MultiPolygon'}, SOIL_FIELDS)
        for zone in zones:
            zone['values'] = {k: float(zone['values'][k]) for k in SOIL_FIELDS}
            validate_soil(zone['values'])
        if zones:
            config.update(soil_zones=zones, soil_crs='EPSG:4326')
    elif request['hazard'] == 'avalanche':
        release = features(root, request.get('release_id'), {'Polygon', 'MultiPolygon'})
        if release:
            config.update(release_wkts=[x['wkt'] for x in release], release_crs='EPSG:4326')
        config.update(frequency=request['frequency'], vulnerability=request['vulnerability'], repair_cost=request['repair_cost'],
                      screening=dict(min_slope=request['release_min_slope'], max_slope=request['release_max_slope'],
                                     min_relief=request['release_min_relief'], min_area=request['release_min_area']))
    else:
        zones = features(root, request.get('fuel_id'), {'Polygon', 'MultiPolygon'}, ['fuel'])
        if zones:
            config.update(fuel_zones=[dict(wkt=x['wkt'], fuel=x['values']['fuel']) for x in zones], fuel_crs='EPSG:4326')
        config.update(scenario_date=request['scenario_date'], satellite=request['satellite'],
                      lookback_days=request['lookback_days'], warmup_days=request['warmup_days'],
                      ignitions=[f'POINT ({lon} {lat})' for lon, lat in request['ignitions']], ignition_crs='EPSG:4326')
        if request.get('weather_id'):
            config['weather_csv'] = str(asset_path(root, request['weather_id'], {'.csv'}))
    return config
