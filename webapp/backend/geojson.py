"""Bound GeoJSON shape and coordinate count before passing it to native OGR."""
import json
import math

LIMIT = 100_000
DEPTHS = {'Point':0, 'MultiPoint':1, 'LineString':1, 'MultiLineString':2, 'Polygon':2, 'MultiPolygon':3}


def load_geojson(path):
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('Не удалось прочитать GeoJSON UTF-8.') from exc
    if not isinstance(payload, dict) or payload.get('type') not in ('Feature', 'FeatureCollection'):
        raise ValueError('Нужен GeoJSON Feature или FeatureCollection в WGS 84.')
    items = payload.get('features') if payload['type'] == 'FeatureCollection' else [payload]
    if not isinstance(items, list) or len(items) > 10_000:
        raise ValueError('GeoJSON: требуется список не более 10 000 объектов.')
    budget = [LIMIT]
    for feature in items:
        if not isinstance(feature, dict) or feature.get('type') != 'Feature':
            raise ValueError('GeoJSON содержит некорректный Feature.')
        props = feature.get('properties')
        if props is not None and not isinstance(props, dict):
            raise ValueError('Свойства GeoJSON должны быть объектом или null.')
        geometry = feature.get('geometry')
        if not isinstance(geometry, dict) or geometry.get('type') not in DEPTHS:
            raise ValueError('Неподдерживаемая геометрия GeoJSON.')
        kind = geometry['type']
        coordinates = geometry.get('coordinates')
        check_coordinates(coordinates, DEPTHS[kind], budget)
        if kind == 'LineString' and len(coordinates) < 2:
            raise ValueError('Линия должна содержать хотя бы две точки.')
        lines = coordinates if kind == 'MultiLineString' else []
        polygons = [coordinates] if kind == 'Polygon' else coordinates if kind == 'MultiPolygon' else []
        if any(len(line) < 2 for line in lines):
            raise ValueError('Линия должна содержать хотя бы две точки.')
        if any(len(ring) < 4 or ring[0] != ring[-1] for polygon in polygons for ring in polygon):
            raise ValueError('Кольцо полигона должно быть замкнуто и содержать хотя бы четыре точки.')
    return items


def check_coordinates(value, depth, budget):
    if not isinstance(value, list) or not value:
        raise ValueError('Некорректные координаты GeoJSON.')
    if depth:
        for child in value:
            check_coordinates(child, depth-1, budget)
        return
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError('GeoJSON содержит более 100 000 координат. Упростите геометрию.')
    if not 2 <= len(value) <= 3 or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value):
        raise ValueError('Координаты GeoJSON должны быть конечными числами.')
    if not (-180 <= value[0] <= 180 and -80 <= value[1] <= 80):
        raise ValueError('Нужны координаты долготы и широты WGS 84 (EPSG:4326).')
