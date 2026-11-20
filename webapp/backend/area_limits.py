"""Geometry-only preflight; never downloads terrain or starts a calculation."""
import math

from avalanche_scenario.terrain import transformed_geometry

MAX_AREA_KM2 = 2500
CELL_LIMITS = {'avalanche': 2000000, 'landslide': 2000000, 'wildfire': 300000}


def check_area(request):
    west, south, east, north = request.bbox
    lon, lat = (west+east)/2, (south+north)/2
    zone = min(60, max(1, int((lon+180)/6)+1))
    projection = f'EPSG:{(32600 if lat >= 0 else 32700)+zone}'
    polygon = f'POLYGON(({west} {south},{east} {south},{east} {north},{west} {north},{west} {south}))'
    area = transformed_geometry(polygon, 'EPSG:4326', projection)
    area_km2 = area.GetArea()/1e6
    buffer = request.buffer if request.buffer is not None else (3000 if request.hazard == 'avalanche' else 500)
    region = area.Buffer(buffer) if buffer else area
    xmin, xmax, ymin, ymax = region.GetEnvelope()
    step = request.resolution
    columns = math.ceil(xmax/step)-math.floor(xmin/step)
    rows = math.ceil(ymax/step)-math.floor(ymin/step)
    cells = columns*rows
    maximum = CELL_LIMITS[request.hazard]
    message = ''
    if area_km2 > MAX_AREA_KM2:
        message = 'Площадь участка превышает 2500 км². Выделите меньший участок.'
    elif cells > maximum:
        limit = '300 тыс.' if request.hazard == 'wildfire' else '2 млн'
        message = f'Сетка с учётом буфера превышает {limit} ячеек. Уменьшите участок или буфер либо увеличьте шаг сетки.'
    return dict(valid=not message, message=message, area_km2=area_km2, cells=cells,
                max_cells=maximum, max_area_km2=MAX_AREA_KM2)
