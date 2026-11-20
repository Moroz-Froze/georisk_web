"""Scenario workflow and auditable GIS/report outputs."""
from dataclasses import asdict
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import numpy as np
from osgeo import gdal, ogr
from . import __version__
from .core import Parameters, simulate, risk_values
from .terrain import (prepare_terrain, screen_terrain, write_raster,
                      rasterize_geometry, transformed_geometry, spatial_ref)


def _field(layer, name, kind=ogr.OFTReal):
    layer.CreateField(ogr.FieldDefn(name, kind))


def _polygonize(array, transform, projection, datasource, name):
    ds = gdal.GetDriverByName('MEM').Create('', array.shape[1], array.shape[0], 1, gdal.GDT_Byte)
    ds.SetGeoTransform(transform)
    ds.SetProjection(projection)
    ds.GetRasterBand(1).WriteArray(array.astype(np.uint8))
    mask = gdal.GetDriverByName('MEM').Create('', array.shape[1], array.shape[0], 1, gdal.GDT_Byte)
    mask.GetRasterBand(1).WriteArray(((array > 0) & (array < 255)).astype(np.uint8))
    layer = datasource.CreateLayer(name, spatial_ref(projection), ogr.wkbPolygon)
    _field(layer, 'class', ogr.OFTInteger)
    gdal.Polygonize(ds.GetRasterBand(1), mask.GetRasterBand(1), layer, 0, [])
    layer.ResetReading()
    return layer


def _rail_results(rails, crs, pressure, transform, projection, ds, frequency,
                  vulnerability, repair_cost, corridor, truncated, notify, cancelled):
    layer = ds.CreateLayer('railway_risk', spatial_ref(projection), ogr.wkbUnknown)
    for name, kind in [('source_id', ogr.OFTString), ('coverage', ogr.OFTString),
                       ('length_m', ogr.OFTReal), ('affected_m', ogr.OFTReal),
                       ('pmax_kpa', ogr.OFTReal), ('loss_event', ogr.OFTReal),
                       ('eal', ogr.OFTReal), ('frequency', ogr.OFTReal),
                       ('vuln', ogr.OFTReal), ('cost_per_m', ogr.OFTReal)]:
        _field(layer, name, kind)
    # Dissolve the positive-pressure raster cells to measure actual intersections
    # with a centerline. A buffer is used only for conservative pressure sampling.
    zones = _polygonize((np.nan_to_num(pressure, nan=0) > 0).astype(np.uint8),
                         transform, projection, ds, 'impact_footprint')
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for feature in zones:
        multi.AddGeometry(feature.GetGeometryRef())
    footprint = multi.UnionCascaded() if multi.GetGeometryCount() else None
    summary = dict(features=len(rails), assessed=0, incomplete=0, affected_length_m=0.0,
                   event_loss=0.0 if frequency is not None else None,
                   expected_annual_loss=0.0 if frequency is not None else None,
                   currency='RUB', frequency_per_year=frequency,
                   vulnerability=vulnerability, repair_cost_per_m=repair_cost,
                   pressure_corridor_halfwidth_m=corridor,
                   aggregation='Сумма по неперекрывающимся объектам; дубликаты трассы исключите вручную.')
    for index, rail in enumerate(rails):
        if cancelled and cancelled():
            raise InterruptedError('Расчёт отменён.')
        geom = transformed_geometry(rail['wkt'], crs, projection)
        if ogr.GT_Flatten(geom.GetGeometryType()) not in (ogr.wkbLineString, ogr.wkbMultiLineString):
            raise ValueError('Трасса ЖД должна быть линейным слоем.')
        geom_buffer = geom.Buffer(corridor)
        mask = rasterize_geometry(geom_buffer, pressure.shape, transform, projection, True)
        # Coverage is geometric as well as raster based: features completely
        # outside the model must never acquire a spurious zero risk.
        xmin, dx, _, ymax, _, negdy = transform
        xmax = xmin + pressure.shape[1]*dx
        ymin = ymax + pressure.shape[0]*negdy
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((xmin,ymin),(xmax,ymin),(xmax,ymax),(xmin,ymax),(xmin,ymin)):
            ring.AddPoint_2D(x, y)
        extent = ogr.Geometry(ogr.wkbPolygon)
        extent.AddGeometry(ring)
        finite = mask & np.isfinite(pressure)
        coverage = 'complete'
        if not np.any(finite):
            coverage = 'outside_or_nodata'
        elif not extent.Contains(geom_buffer) or np.any(mask & ~np.isfinite(pressure)):
            coverage = 'partial'
        if truncated and coverage == 'complete':
            coverage = 'truncated_model'
        maximum = float(np.max(pressure[finite])) if np.any(finite) else None
        affected = float(geom.Intersection(footprint).Length()) if footprint and not footprint.IsEmpty() else 0.0
        record = ogr.Feature(layer.GetLayerDefn())
        record.SetGeometry(geom)
        for key, val in dict(source_id=str(rail['id']), coverage=coverage,
                             length_m=geom.Length(), affected_m=affected,
                             pmax_kpa=maximum, frequency=frequency,
                             vuln=vulnerability, cost_per_m=repair_cost).items():
            if val is not None:
                record.SetField(key, val)
        if coverage == 'complete':
            summary['assessed'] += 1
            summary['affected_length_m'] += affected
            if frequency is not None:
                risk = risk_values(maximum or 0, affected*repair_cost, 0,
                                   vulnerability, frequency)
                record.SetField('loss_event', risk['loss_event'])
                record.SetField('eal', risk['eal'])
                summary['event_loss'] += risk['loss_event']
                summary['expected_annual_loss'] += risk['eal']
        else:
            summary['incomplete'] += 1
        layer.CreateFeature(record)
        if notify:
            notify(88+8*(index+1)/max(1, len(rails)), 'Пересечение с трассой железной дороги…')
    if summary['incomplete']:
        summary['expected_annual_loss_assessed_only'] = summary['expected_annual_loss']
        summary['expected_annual_loss'] = None
    return summary


def run_workflow(config, notify=None, cancelled=None, simulator=None):
    directory = Path(config['directory'])
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/'manifest.json').exists():
        raise ValueError('В этой папке уже есть расчёт. Выберите новую папку результатов.')
    p = Parameters(**config.get('parameters', {}))
    p.validate()
    frequency = config.get('frequency')
    if frequency is not None:
        if not config.get('rails'):
            raise ValueError('Для денежного риска укажите трассу железной дороги.')
        if not math.isfinite(frequency) or frequency < 0:
            raise ValueError('Частота должна быть неотрицательной, событий в год.')
        if not 0 <= config.get('vulnerability', .5) <= 1 or config.get('repair_cost', 0) <= 0:
            raise ValueError('Укажите положительную стоимость восстановления и уязвимость от 0 до 1.')
    terrain = prepare_terrain(config['area_wkt'], config['area_crs'], directory,
                               config.get('dem', ''), config.get('buffer', 3000),
                               config.get('resolution', 30), notify, cancelled)
    transform, projection = terrain['transform'], terrain['projection']
    dx, dy = transform[1], -transform[5]
    screening = screen_terrain(terrain['dem'], dx, dy,
                               **config.get('screening', {}))
    release = screening.pop('release')
    slope = screening.pop('slope')
    if config.get('release_wkts'):
        manual = np.zeros_like(release)
        for wkt in config['release_wkts']:
            geom = transformed_geometry(wkt, config['release_crs'], projection)
            manual |= rasterize_geometry(geom, release.shape, transform, projection)
        release = manual & np.isfinite(slope)
        screening['acceptable'] = bool(np.any(release))
        screening['manual_release'] = True
        screening['candidate_area_m2'] = int(np.count_nonzero(release))*dx*dy
    else:
        screening['manual_release'] = False
    write_raster(directory/'slope_deg.tif', slope, transform, projection)
    write_raster(directory/'release_candidates.tif',
                 np.where(np.isfinite(slope), release.astype(np.uint8), 255),
                 transform, projection, True)
    warnings = [
        'Предварительный сценарный расчёт для выбора участков изысканий; не обоснование проектных решений ЖД.',
        'Кандидатные очаги определены по уклону, без сведений о снеге, ветре, лесном покрове и истории лавин.',
        'Огибающая независимых трассеров при заданной постоянной высоте потока. Не SPH и не RAMMS.',
        'Нулевое воздействие в этом сценарии не доказывает безопасность участка.',
        'Очаги за пределами расчётного буфера не учитываются.'
    ]
    if str(terrain['source'].get('kind', '')).startswith('Copernicus'):
        warnings.append('Copernicus GLO-30 — модель поверхности с шагом около 30 м; для проектирования нужны детальные изыскания.')
    manifest = dict(plugin='avalanche_scenario', version=__version__,
                    generated_utc=datetime.now(timezone.utc).isoformat(),
                    status='terrain_not_suitable', crs=terrain['crs'],
                    source=terrain['source'], coverage=terrain['coverage'],
                    input=config, parameters=asdict(p), screening=screening,
                    warnings=warnings, outputs={
                        'terrain': str(directory/'terrain.tif'),
                        'slope': str(directory/'slope_deg.tif'),
                        'candidates': str(directory/'release_candidates.tif')})
    if screening['acceptable']:
        if notify:
            notify(28, 'Моделирование сценарной зоны воздействия…')
        result = (simulator or simulate)(terrain['dem'], release, dx, dy, p,
                          lambda pct: notify(28+.55*pct, 'Моделирование сценарной зоны воздействия…') if notify else None,
                          cancelled)
        manifest['status'] = 'completed'
        manifest['simulation'] = result['summary']
        if result['summary'].get('batches', 1) > 1:
            warnings.append(f"Все {result['summary']['particles']} ячеек очагов обработаны в {result['summary']['batches']} группах. "
                            'Шаг рельефа сохранён; объединены максимумы скорости и давления и минимальные времена прихода. '
                            'Точки экспортируемых линий траекторий прорежены для ограничения размера файлов.')
        if result['summary']['truncated']:
            warnings.append('Трассеры достигли границы или NoData: зона воздействия обрезана. Расширьте буфер; полный годовой риск не рассчитан.')
        if result['summary']['time_limit']:
            warnings.append('Часть трассеров ещё движется: увеличьте время расчёта. Полный годовой риск не рассчитан.')
        for name, data in [('speed_m_s', result['speed']), ('pressure_kpa', result['pressure']),
                           ('arrival_s', result['arrival']), ('intensity', result['intensity'])]:
            path = directory/(name+'.tif')
            write_raster(path, data, transform, projection, name == 'intensity')
            manifest['outputs'][name] = str(path)
        vector_path = directory/'scenario.gpkg'
        ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(vector_path))
        ds.StartTransaction()
        if notify:
            notify(83, 'Сохранение зон воздействия и траекторий…')
        _polygonize(release.astype(np.uint8), transform, projection, ds, 'release_candidates')
        _polygonize(result['intensity'], transform, projection, ds, 'intensity_zones')
        tracks = ds.CreateLayer('trajectories', spatial_ref(projection), ogr.wkbLineString)
        for key, kind in [('tracer_id', ogr.OFTInteger), ('status', ogr.OFTString),
                           ('vmax_m_s', ogr.OFTReal), ('distance_m', ogr.OFTReal)]:
            _field(tracks, key, kind)
        statuses = ['time_limit', 'stopped', 'boundary', 'nodata']
        for i, path in enumerate(result['paths']):
            if i % 512 == 0:
                if cancelled and cancelled():
                    raise InterruptedError('Расчёт отменён.')
                if notify:
                    end = 87 if config.get('rails') else 96
                    notify(84+(end-84)*i/max(1, len(result['paths'])), 'Сохранение траекторий…')
            if len(path) < 2:
                continue
            line = ogr.Geometry(ogr.wkbLineString)
            for x, y, t in path:
                line.AddPoint_2D(transform[0]+x, transform[3]-y)
            feature = ogr.Feature(tracks.GetLayerDefn())
            feature.SetGeometry(line)
            feature.SetField('tracer_id', i)
            feature.SetField('status', statuses[int(result['status'][i])])
            feature.SetField('vmax_m_s', float(result['peak'][i]))
            feature.SetField('distance_m', float(result['distance'][i]))
            tracks.CreateFeature(feature)
        if config.get('rails'):
            manifest['railway'] = _rail_results(
                config['rails'], config['rail_crs'], result['pressure'], transform,
                projection, ds, frequency, config.get('vulnerability', .5),
                config.get('repair_cost', 0), config.get('corridor', 15),
                result['summary']['truncated']+result['summary']['time_limit'], notify, cancelled)
        else:
            warnings.append('Трасса ЖД не указана: воздействие на железную дорогу и денежный риск не рассчитаны.')
        if frequency is None:
            warnings.append('Частота сценария не задана: ожидаемый годовой денежный ущерб не рассчитан.')
        ds.CommitTransaction()
        ds = None
        manifest['outputs']['vectors'] = str(vector_path)
    else:
        warnings.insert(0, 'В выделении и буфере не выполнены критерии поиска горных очагов. '
                        'Проверьте участок, буфер и пороги. Это не заключение об отсутствии лавинной опасности.')
    # json is the complete audit trail; HTML is the human-readable report.
    report = directory/'report.html'
    manifest['outputs']['report'] = str(report)
    manifest['outputs']['manifest'] = str(directory/'manifest.json')
    (directory/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    write_report(report, manifest)
    if notify:
        notify(100, 'Расчёт завершён.')
    return manifest


def write_report(path, data):
    esc = html.escape
    status = 'Сценарий рассчитан' if data['status'] == 'completed' else 'Участок не прошёл проверку рельефа'
    rows = [('Статус', status), ('Система координат', data['crs']),
            ('Перепад высот, м', f"{data['screening']['relief_m']:.1f}"),
            ('Максимальный уклон, °', f"{data['screening']['max_slope_deg']:.1f}"),
            ('Площадь кандидатных очагов, м²', f"{data['screening']['candidate_area_m2']:.0f}")]
    if 'simulation' in data:
        s = data['simulation']
        rows.extend([('Трассеров', str(s['particles'])),
                     ('Максимальная скорость, м/с', f"{s['max_speed_m_s']:.2f}"),
                     ('Максимальное давление, кПа', f"{s['max_pressure_kpa']:.2f}"),
                     ('Оценка начального объёма, м³', f"{s['release_volume_m3']:.0f}"),
                     ('Движущихся в конце расчёта', str(s['time_limit']))])
    if 'railway' in data:
        r = data['railway']
        rows.extend([('Длина трассы под воздействием, м (полностью покрытые объекты)', f"{r['affected_length_m']:.1f}"),
                     ('Объектов с неполным расчётом', str(r['incomplete'])),
                     ('Ожидаемый годовой ущерб, руб./год',
                      'Не рассчитан' if r['expected_annual_loss'] is None else f"{r['expected_annual_loss']:,.2f}")])
    table = ''.join('<tr><th>'+esc(k)+'</th><td>'+esc(v)+'</td></tr>' for k,v in rows)
    warnings = ''.join('<li>'+esc(x)+'</li>' for x in data['warnings'])
    params = ''.join('<tr><th>'+esc(k)+'</th><td>'+esc(str(v))+'</td></tr>' for k,v in data['parameters'].items())
    links = ''.join('<li><a href="'+esc(Path(v).name)+'">'+esc(k)+'</a></li>' for k,v in data['outputs'].items())
    path.write_text(f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Лавина | Отчёт</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1050px;margin:40px auto;padding:0 24px;color:#17364a}}h1{{font-size:32px}}table{{border-collapse:collapse;width:100%;margin:20px 0}}th,td{{border-bottom:1px solid #dbe3e8;padding:10px;text-align:left}}th{{width:65%}}.note{{background:#fff3df;padding:20px;border-radius:12px}}small{{color:#536878}}a{{color:#006b80}}</style>
<small>ЛАВИНА · ПРЕДВАРИТЕЛЬНАЯ ОЦЕНКА ДЛЯ ЖЕЛЕЗНОЙ ДОРОГИ · v{__version__}</small>
<h1>{esc(status)}</h1><p>Время UTC: {esc(data['generated_utc'])}</p><table>{table}</table>
<div class="note"><b>Границы применимости</b><ul>{warnings}</ul></div>
<h2>Что означают результаты</h2><p>Карты показывают условное воздействие при заданном сходе. Классы интенсивности по давлению: 0 — воздействие не рассчитано в данном сценарии; 1 — выше 0 и ниже нижнего порога; 2 — между порогами; 3 — от верхнего порога. Это не шкала лавинной опасности 1–5. NoData — неизвестная область.</p>
<p>Для ЖД: L — длина пересечения оси пути с ячейками положительного давления; D = L × стоимость восстановления метра × уязвимость; EAL = λD. λ — заданная частота всего рассматриваемого сценария, событий в год. Уязвимость задаётся для выбранной конструкции и сценария. Транзит поездов, человеческие потери, простои, защита и вторичные аварии не моделируются. Дублирующие трассы дают двойной учёт.</p>
<h2>Параметры</h2><table>{params}</table><h2>Файлы</h2><ul>{links}</ul>
<h2>Основания метода</h2><p>А. С. Соловьев. «Математическое моделирование чрезвычайных ситуаций, связанных с зарождением и сходом снежных лавин». Воронеж, 2014. Обзор трения Фёлльми: §1.4.3, печатная с.46, формула (1.4). Авторская вязкоупругая модель: §3.3–3.4, с.75–83, формулы (3.10)–(3.17); в этом плагине не воспроизведена.</p>
<p><a href="https://ramms.ch/ramms-avalanche/friction-parameters/">Первичный источник закона трения</a> · <a href="https://registry.opendata.aws/copernicus-dem/">Copernicus DEM</a> · <a href="https://www.slf.ch/en/avalanches/avalanche-science-and-prevention/avalanche-types/">SLF: образование лавин</a></p>
<p>Источник рельефа: {esc(str(data['source'].get('kind')))}. {esc(data['source'].get('credit', ''))}</p>
<p>Полная конфигурация, геометрия выделения и источники сохранены в manifest.json.</p></html>''', encoding='utf-8')


def create_demo(directory):
    """Synthetic mountain and railway; deliberately no claim to real topography."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    shape, cell = (140, 140), 30.0
    xx, yy = np.meshgrid((np.arange(shape[1])+.5)*cell,
                         (np.arange(shape[0])+.5)*cell)
    z = 2500 - .70*np.minimum(yy, 1900) - .03*np.maximum(yy-1900, 0)
    z += .00007*(xx-2100)**2
    transform = (500000, cell, 0, 5600000, 0, -cell)
    projection = spatial_ref('EPSG:32645').ExportToWkt()
    dem = directory/'DEMO_synthetic_dem.tif'
    write_raster(dem, z, transform, projection)
    ds = ogr.GetDriverByName('GPKG').CreateDataSource(str(directory/'DEMO_inputs.gpkg'))
    area = ds.CreateLayer('study_area', spatial_ref(projection), ogr.wkbPolygon)
    geom = ogr.CreateGeometryFromWkt('POLYGON ((500300 5596100,503900 5596100,503900 5599700,500300 5599700,500300 5596100))')
    feature = ogr.Feature(area.GetLayerDefn()); feature.SetGeometry(geom); area.CreateFeature(feature)
    rails = ds.CreateLayer('railway', spatial_ref(projection), ogr.wkbLineString)
    rail = ogr.CreateGeometryFromWkt('LINESTRING (500600 5598100,503400 5598100)')
    feature = ogr.Feature(rails.GetLayerDefn()); feature.SetGeometry(rail); rails.CreateFeature(feature)
    ds = None
    return dict(dem=str(dem), inputs=str(directory/'DEMO_inputs.gpkg'),
                area_wkt=geom.ExportToWkt(), area_crs=projection, rail_crs=projection,
                rails=[{'id': 'DEMO', 'wkt': rail.ExportToWkt()}], buffer=0,
                resolution=30, parameters={'duration': 240},
                frequency=None, directory=str(directory/'result'))
