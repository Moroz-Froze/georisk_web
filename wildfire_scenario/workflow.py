from dataclasses import asdict
from datetime import datetime,timezone,date
from pathlib import Path
import csv,html,json,math
import numpy as np
from osgeo import gdal,ogr
from . import __version__
from .core import Parameters,FUEL_IDS,ID_FUELS,weather_indices,behavior_grid,arrival_times
from .terrain import prepare_terrain,spatial_ref,transformed_geometry,rasterize_geometry,write_raster
from .sources import download_weather,load_weather_csv,download_sentinel,write_weather

SOURCES=[
    dict(title='NRCan: Canadian Forest Fire Danger Rating System',url='https://natural-resources.canada.ca/forests-forestry/wildland-fires/canadian-forest-fire-danger-rating-system'),
    dict(title='Reference FWI 1987 / FBP 1992 code, cffdrs_py',url='https://github.com/cffdrs/cffdrs_py/tree/7d8931e64fca7c5a0d0f34a14d258215a3b9506d'),
    dict(title='NRCan: FWI2025 / FBP1992 compatibility notice',url='https://github.com/nrcan-cfs-fire/cffdrs-ng'),
    dict(title='USGS: NDVI',url='https://www.usgs.gov/landsat-missions/landsat-normalized-difference-vegetation-index'),
    dict(title='USGS: NDMI',url='https://www.usgs.gov/landsat-missions/normalized-difference-moisture-index'),
    dict(title='Sentinel-2 Collection 1 / Earth Search',url='https://github.com/Element84/earth-search'),
    dict(title='Copernicus DEM',url='https://registry.opendata.aws/copernicus-dem/'),
    dict(title='Open-Meteo weather documentation',url='https://open-meteo.com/en/docs'),
    dict(title='Rothermel alternative evaluated, not implemented here',url='https://research.fs.usda.gov/treesearch/55928'),
]


def check_cancel(cancelled):
    if cancelled and cancelled():raise InterruptedError('Операция отменена.')


def make_fuels(config,terrain,satellite):
    shape=terrain['dem'].shape;valid=np.isfinite(terrain['dem'])
    ids=np.full(shape,255,dtype=np.uint8)
    if config.get('fuel_zones'):
        occupied=np.zeros(shape,dtype=bool)
        for zone in config['fuel_zones']:
            name=str(zone['fuel']).upper().replace('-','').replace(' ','')
            if name not in FUEL_IDS:raise ValueError('Неподдерживаемый тип горючего: '+name)
            geom=transformed_geometry(zone['wkt'],config['fuel_crs'],terrain['projection'])
            mask=rasterize_geometry(geom,shape,terrain['transform'],terrain['projection']) & valid
            if np.any(mask & occupied):raise ValueError('Зоны горючего перекрываются в центрах ячеек.')
            ids[mask]=FUEL_IDS[name];occupied |= mask
        # Partial maps remain explicitly unknown. They are never filled from NDVI.
        if not np.any(occupied):raise ValueError('Зоны горючего не пересекают расчётную область.')
    else:
        if satellite is None:raise ValueError('Без спутника нужен полигональный слой типов горючего с полем fuel.')
        scl=satellite['scl'];p=Parameters(**config.get('parameters',{}))
        ids[valid & (scl==4)]=FUEL_IDS[p.fuel_type]
        ids[valid & (scl==6)]=FUEL_IDS['WA']
        ids[valid & (scl==11)]=FUEL_IDS['NF']
        # Bare/urban class 5 is not assumed to be a reliable firebreak: unknown.
    return ids


def prepare_inputs(config,notify=None,cancelled=None):
    p=Parameters(**config.get('parameters',{}));p.validate()
    day=date.fromisoformat(config['scenario_date'])
    directory=Path(config['directory']);directory.mkdir(parents=True,exist_ok=True)
    check_cancel(cancelled)
    terrain=prepare_terrain(config['area_wkt'],config['area_crs'],directory,
        local_dem=config.get('dem',''),buffer_m=config.get('buffer',500),
        resolution=config.get('resolution',30),notify=notify,cancelled=cancelled)
    if terrain['dem'].size>300000:raise ValueError('Для пожарного сценария максимум 300 тыс. ячеек. Уменьшите участок/буфер или укрупните сетку.')
    geom=transformed_geometry(config['area_wkt'],config['area_crs'],'EPSG:4326')
    longitude,latitude,_=geom.Centroid().GetPoint()
    check_cancel(cancelled)
    satellite=None
    if config.get('satellite',True):
        satellite=download_sentinel(terrain,config['scenario_date'],directory,config.get('lookback_days',60),notify,cancelled)
    if notify:notify(52,'Погода и последовательный расчёт FWI 1987…')
    if config.get('weather_csv'):
        rows,weather_source=load_weather_csv(config['weather_csv'])
        rows=[row for row in rows if row['date']<=config['scenario_date']]
    else:rows,weather_source=download_weather(latitude,longitude,config['scenario_date'],config.get('warmup_days',60))
    if not rows or rows[-1]['date']!=config['scenario_date']:
        raise ValueError('Метеоряд должен заканчиваться выбранной датой сценария и включать её.')
    weather=weather_indices(rows,latitude,p)
    write_weather(directory/'weather_fwi.csv',weather)
    check_cancel(cancelled)
    ids=make_fuels(config,terrain,satellite)
    paths={'terrain':terrain['terrain_path'],'weather_fwi':str(directory/'weather_fwi.csv')}
    for key,values in [('fuel_ids',ids)]+([(key,satellite[key]) for key in ('ndvi','ndmi','scl')] if satellite else []):
        path=directory/(key+'.tif');write_raster(path,values,terrain['transform'],terrain['projection'],key in ('fuel_ids','scl'))
        paths[key]=str(path)
    meta=dict(plugin='wildfire_scenario',version=__version__,generated_utc=datetime.now(timezone.utc).isoformat(),
        input=config,crs=terrain['crs'],dem_source=terrain['source'],satellite_source=satellite['source'] if satellite else None,
        weather_source=weather_source,weather_days=len(weather),latest_weather=weather[-1],
        latitude=latitude,longitude=longitude,outputs=paths)
    path=directory/'inputs.json';path.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    meta['outputs']['inputs']=str(path)
    if notify:notify(60,'Исходные данные получены.')
    return meta


def read_prepared(path,config):
    source_path=Path(path)
    meta=json.loads(source_path.read_text(encoding='utf-8'))
    # Reuse is permitted only for identical acquisition and bookkeeping settings.
    for key in ('area_wkt','area_crs','dem','buffer','resolution','scenario_date','satellite','lookback_days',
                'weather_csv','warmup_days','fuel_zones','fuel_crs'):
        if meta['input'].get(key)!=config.get(key):raise ValueError('Загруженные данные не соответствуют изменённым параметрам. Загрузите их заново.')
    old=Parameters(**meta['input'].get('parameters',{}));new=Parameters(**config.get('parameters',{}))
    for key in ('fuel_type','ffmc_initial','dmc_initial','dc_initial'):
        if getattr(old,key)!=getattr(new,key):raise ValueError('Изменены начальные коды FWI или тип горючего. Загрузите данные заново.')
    for resource in meta['outputs'].values():
        if not Path(resource).is_file():raise ValueError('Один из файлов загруженных данных отсутствует.')
    meta['outputs']['inputs']=str(source_path)
    return meta


def load_grid(path):
    ds=gdal.Open(str(path));array=ds.ReadAsArray().astype(float);nodata=ds.GetRasterBand(1).GetNoDataValue()
    if nodata is not None:array[array==nodata]=np.nan
    return array,ds.GetGeoTransform(),ds.GetProjection()


def polygonize(ds,name,values,transform,projection,valid=None):
    source=gdal.GetDriverByName('MEM').Create('',values.shape[1],values.shape[0],1,gdal.GDT_Byte)
    source.SetGeoTransform(transform);source.SetProjection(projection);band=source.GetRasterBand(1)
    band.WriteArray(values.astype(np.uint8));band.SetNoDataValue(255)
    mask=gdal.GetDriverByName('MEM').Create('',values.shape[1],values.shape[0],1,gdal.GDT_Byte)
    mask.GetRasterBand(1).WriteArray(((values!=255) if valid is None else valid).astype(np.uint8))
    layer=ds.CreateLayer(name,spatial_ref(projection),ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('class_id',ogr.OFTInteger))
    gdal.Polygonize(band,mask.GetRasterBand(1),layer,0,[])
    layer.ResetReading();return layer


def rail_overlay(ds,config,terrain,behavior,spread,footprint):
    layer=ds.CreateLayer('railway_exposure',spatial_ref(terrain['projection']),ogr.wkbUnknown)
    for key,kind in [('source_id',ogr.OFTString),('coverage',ogr.OFTString),('hit_length_m',ogr.OFTReal),
        ('arrival_min',ogr.OFTReal),('hfi_max_kw_m',ogr.OFTReal),('length_m',ogr.OFTReal)]:layer.CreateField(ogr.FieldDefn(key,kind))
    collection=ogr.Geometry(ogr.wkbMultiPolygon)
    if footprint:
        for feature in footprint:collection.AddGeometry(feature.GetGeometryRef())
    region=collection.UnionCascaded() if collection.GetGeometryCount() else None
    h=behavior['arrays']['head_m_min'];summary=dict(features=len(config['rails']),incomplete=0,hit_length_m=0.0)
    origin_x,dx,_,origin_y,_,ndy=terrain['transform']
    extent=ogr.CreateGeometryFromWkt(f'POLYGON (({origin_x} {origin_y},{origin_x+h.shape[1]*dx} {origin_y},{origin_x+h.shape[1]*dx} {origin_y+h.shape[0]*ndy},{origin_x} {origin_y+h.shape[0]*ndy},{origin_x} {origin_y}))')
    for record in config['rails']:
        geom=transformed_geometry(record['wkt'],config['rail_crs'],terrain['projection'])
        if ogr.GT_Flatten(geom.GetGeometryType()) not in (ogr.wkbLineString,ogr.wkbMultiLineString):
            raise ValueError('Ось ЖД должна быть линейным слоем.')
        corridor=geom.Buffer(config.get('corridor',15))
        mask=rasterize_geometry(corridor,h.shape,terrain['transform'],terrain['projection'],True)
        covered=mask & np.isfinite(h);coverage='complete'
        if not np.any(covered):coverage='outside_or_unknown'
        elif not extent.Contains(corridor) or np.any(mask & ~np.isfinite(h)):coverage='partial'
        if spread and (spread['unknown'] or spread['boundary']) and coverage=='complete':coverage='truncated_scenario'
        feature=ogr.Feature(layer.GetLayerDefn());feature.SetGeometry(geom)
        feature.SetField('source_id',str(record['id']));feature.SetField('coverage',coverage);feature.SetField('length_m',geom.Length())
        intensity=behavior['arrays']['intensity_kw_m']
        if np.any(covered):feature.SetField('hfi_max_kw_m',float(np.nanmax(intensity[covered])))
        length=geom.Intersection(region).Length() if region else 0.0
        if spread is not None:
            times=spread['arrival'][mask & np.isfinite(spread['arrival'])]
            if len(times):feature.SetField('arrival_min',float(np.min(times)))
            if coverage=='complete':feature.SetField('hit_length_m',length);summary['hit_length_m']+=length
        if coverage!='complete':summary['incomplete']+=1
        layer.CreateFeature(feature)
    if summary['incomplete'] or spread is None:summary['hit_length_m']=None
    return summary


def run_workflow(config,notify=None,cancelled=None):
    p=Parameters(**config.get('parameters',{}));p.validate()
    folder=Path(config['directory']);folder.mkdir(parents=True,exist_ok=True)
    if (folder/'manifest.json').exists():raise ValueError('Папка уже содержит расчёт. Выберите новую.')
    if config.get('prepared_inputs'):meta=read_prepared(config['prepared_inputs'],config)
    else:meta=prepare_inputs(dict(config,directory=str(folder/'inputs')),notify,cancelled)
    check_cancel(cancelled)
    dem,transform,projection=load_grid(meta['outputs']['terrain'])
    ids,_,_=load_grid(meta['outputs']['fuel_ids']);ids=np.where(np.isfinite(ids),ids,255).astype(np.uint8)
    terrain=dict(dem=dem,transform=transform,projection=projection)
    dx,dy=transform[1],-transform[5]
    behavior=behavior_grid(dem,ids,dx,dy,meta['latest_weather'],config['scenario_date'],meta['latitude'],meta['longitude'],p,
        lambda value:notify(60+.2*value,'Модель FBP: скорость и интенсивность…') if notify else None,cancelled)
    outputs=dict(meta['outputs'])
    for key,value in dict(behavior['arrays'],intensity_class=behavior['intensity_class'],slope_percent=behavior['slope_percent']).items():
        path=folder/(key+'.tif');write_raster(path,value,transform,projection,key=='intensity_class');outputs[key]=str(path)
    valid_count=int(np.count_nonzero(behavior['known']))
    if not valid_count:raise ValueError('Нет ячеек с известным горючим и допустимым уклоном. Уточните карту горючего или спутниковые данные.')
    warnings=[
        'Сценарная оценка, не оперативный прогноз, вероятность возгорания или руководство по тушению.',
        'NDVI показывает спектральный контраст растительности, NDMI — косвенный признак её водного состояния. Они не преобразуются в вероятность пожара или влажность подстилки.',
        'Используются FWI 1987 и FBP 1992 с поправками 2009. FWI2025 не подставляется в FBP1992.',
        'Метеоусловия одной точки в центре участка приняты одинаковыми по всей области и постоянными на время распространения.',
        'Распространение — 8-связная сетка с эллиптическими скоростями FBP и временем первого достижения. Это приближение, не FARSITE/Prometheus.',
        'Нет моделирования переноса головней, тушения, развития погоды, дыма, взаимодействия огня с атмосферой и вероятности зажигания.',
        'Канадские типы горючего применяются как явно заданные аналоги; калибровка для выбранного региона не выполнена.',
        'Начальные FFMC/DMC/DC задаются пользователем. Прогрев ряда не заменяет сезонную инициализацию и зимовку DC; в снежный сезон применимость ограничена.'
    ]
    if not config.get('fuel_zones'):warnings.insert(0,'Класс SCL=4 принят за растительность с единым выбранным типом горючего. Тип леса, запас и строение топлива из NDVI не определяются. SCL=5, облака и тени остаются неизвестными.')
    if meta['satellite_source'] and meta['satellite_source']['age_days']>14:warnings.append('Спутниковый снимок старше 14 дней; это состояние на дату съёмки, а не на дату сценария.')
    if float(meta['latest_weather']['temp_c'])<=0:warnings.append('Температура в дату сценария ≤0°C. Проверьте снег и применимость пожарного сезона; индекс нельзя трактовать как подтверждение возможности пожара.')
    if meta['weather_days']<30:warnings.append('В метеоряде менее 30 дней: чувствительность к начальным кодам FWI может быть большой.')
    spread=None;seeds=[]
    for wkt in config.get('ignitions',[]):
        geom=transformed_geometry(wkt,config['ignition_crs'],projection)
        if ogr.GT_Flatten(geom.GetGeometryType())!=ogr.wkbPoint:raise ValueError('Для очагов нужны одиночные точки, не полигон или MultiPoint.')
        x,y,_=geom.GetPoint();seeds.append((math.floor((transform[3]-y)/dy),math.floor((x-transform[0])/dx)))
    if seeds:
        spread=arrival_times(behavior,seeds,dx,dy,p.duration_min,cancelled,
            lambda value:notify(80+.15*value,'Сценарий прихода огня…') if notify else None)
        path=folder/'arrival_min.tif';write_raster(path,spread['arrival'],transform,projection);outputs['arrival_min']=str(path)
        reached=np.where(behavior['known'],spread['reached'].astype(np.uint8),255)
        path=folder/'reached.tif';write_raster(path,reached,transform,projection,True);outputs['reached']=str(path)
        if spread['boundary'] or spread['unknown']:warnings.append('Огонь достиг границы или неизвестных ячеек: сценарий обрезан, площадь — только известная часть. Полное воздействие на ЖД неизвестно.')
    else:warnings.append('Очаг не задан: рассчитана потенциальная пожарная интенсивность, распространение не моделировалось.')
    ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(folder/'wildfire.gpkg'))
    polygonize(ds,'potential_intensity',behavior['intensity_class'],transform,projection)
    footprint=None
    if spread:
        footprint=polygonize(ds,'scenario_footprint',spread['reached'].astype(np.uint8),transform,projection,spread['reached'])
        # Contours are nested reached areas at three explicit scenario times.
        for index,fraction in enumerate((.333333333,.666666667,1.0),1):
            minute=p.duration_min*fraction
            mask=np.isfinite(spread['arrival']) & (spread['arrival']<=minute)
            polygonize(ds,'footprint_t'+str(index)+'_'+str(round(minute))+'min',mask.astype(np.uint8),transform,projection,mask)
    railway=rail_overlay(ds,config,terrain,behavior,spread,footprint) if config.get('rails') else None
    ds=None;outputs['vectors']=str(folder/'wildfire.gpkg')
    check_cancel(cancelled)
    area_geom=transformed_geometry(config['area_wkt'],config['area_crs'],projection)
    area=rasterize_geometry(area_geom,dem.shape,transform,projection)
    summary=dict(known_cells=valid_count,unknown_area_m2=int(np.count_nonzero(area & ~behavior['known']))*dx*dy,
        fwi=meta['latest_weather']['fwi'],max_ros_m_min=float(np.nanmax(behavior['arrays']['head_m_min'])),
        max_intensity_kw_m=float(np.nanmax(behavior['arrays']['intensity_kw_m'])),
        scenario_area_m2=int(np.count_nonzero(spread['reached']))*dx*dy if spread else None,
        truncated=bool(spread and (spread['boundary'] or spread['unknown'])))
    outputs.update(report=str(folder/'report.html'),manifest=str(folder/'manifest.json'))
    result=dict(plugin='wildfire_scenario',version=__version__,generated_utc=datetime.now(timezone.utc).isoformat(),
        status='spread_completed' if spread else 'potential_only',input=config,parameters=asdict(p),
        data=meta,summary=summary,railway=railway,warnings=warnings,sources=SOURCES,outputs=outputs)
    (folder/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(folder/'report.html',result)
    if notify:notify(100,'Готово.')
    return result


def write_report(path,data):
    esc=html.escape;summary=data['summary'];meta=data['data']
    weather=meta['latest_weather'];scene=meta['satellite_source']
    rows=[('Дата сценария',data['input']['scenario_date']),('Режим','Распространение от очага' if data['status']=='spread_completed' else 'Потенциальная интенсивность, без очага'),
        ('Дата спутникового снимка',scene['date'] if scene else 'Спутник не используется'),
        ('Покрытие без облаков',f"{scene['clear_coverage']:.1%}" if scene else 'Своя карта горючего'),
        ('FWI 1987 в центре участка',f"{weather['fwi']:.2f}"),('Дней последовательного ряда FWI',str(meta['weather_days'])),
        ('Температура / влажность',f"{float(weather['temp_c']):.1f} °C / {float(weather['rh_pct']):.0f}%"),
        ('Ветер 10 м: скорость / откуда',f"{float(weather['wind_kmh']):.1f} км/ч / {float(weather['wind_from_deg']):.0f}°"),
        ('Максимальная потенциальная скорость головы',f"{summary['max_ros_m_min']:.2f} м/мин"),
        ('Максимальная потенциальная интенсивность',f"{summary['max_intensity_kw_m']:.0f} кВт/м"),
        ('Площадь, достигнутая в сценарии','Не рассчитана' if summary['scenario_area_m2'] is None else f"{summary['scenario_area_m2']/10000:.2f} га"),
        ('Неизвестная область внутри выделения',f"{summary['unknown_area_m2']/10000:.2f} га"),
        ('Сценарий обрезан границей/неизвестными ячейками','Да' if summary['truncated'] else 'Нет / очаг не задан')]
    if data['railway']:
        length=data['railway']['hit_length_m'];rows.append(('Длина ЖД внутри достигнутой области','Неизвестно / без очага' if length is None else f'{length:.1f} м'))
    table=lambda vals:'<table>'+''.join('<tr><th>'+esc(str(k))+'</th><td>'+esc(str(v))+'</td></tr>' for k,v in vals)+'</table>'
    warnings=''.join('<li>'+esc(w)+'</li>' for w in data['warnings'])
    sources=''.join('<li><a href="'+esc(s['url'])+'">'+esc(s['title'])+'</a></li>' for s in data['sources'])
    files=''.join('<li><a href="'+esc(str(Path(v).resolve().as_uri()))+'">'+esc(k)+'</a></li>' for k,v in data['outputs'].items())
    path.write_text(f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Лесные пожары · отчёт</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1080px;margin:35px auto;padding:0 25px;color:#4a3329}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #e9ded6}}th{{width:62%}}.note{{padding:18px;background:#fff0d6;border-radius:12px}}a{{color:#9b4d16}}</style>
<small>ЛЕСНЫЕ ПОЖАРЫ · СЦЕНАРНАЯ ОЦЕНКА · v{__version__} · UTC {esc(data['generated_utc'])}</small>
<h1>Пожарная опасность и распространение</h1>{table(rows)}
<div class="note"><b>Границы расчёта</b><ul>{warnings}</ul></div>
<h2>Что рассчитывается</h2><p>NDVI=(NIR−Red)/(NIR+Red), NDMI=(NIR−SWIR)/(NIR+SWIR), Sentinel-2 B08/B04/B11. Используется отражательная способность с масштабом и смещением из STAC Collection 1. Облака, тени, дефектные пиксели и снег для индексов исключены либо явно отмечены в SCL.</p>
<p>Последовательная цепочка FWI 1987: FFMC, DMC, DC → ISI, BUI → FWI. Погода соответствует полудню местного стандартного времени; осадки — сумме за предыдущие 24 часа. FWI безразмерен, не имеет смысла вероятности и не превращается непосредственно в м/мин.</p>
<p>FBP рассчитывает установившиеся скорости головы, тыла, фланга, азимут и интенсивность для выбранного типа горючего с учётом уклона и ветра. Время достижения ячейки вычисляется кратчайшим временем по направленному 8-связному графу: время ребра = половина длины × (1/скорость в исходной ячейке + 1/скорость в конечной). Диагональ через негорючий угол запрещена. Изотропный фронт на такой сетке имеет угловую погрешность; начальный разгон огня не учитывается.</p>
<p>Классы карты относятся к потенциальной интенсивности: 0 — явно заданная негорючая область/вода; 1–3 — пользовательские интервалы кВт/м; 4 — уклон вне выбранной применимости FBP; 255 — неизвестно. Это не российская шкала пожарной опасности и не команда на эвакуацию.</p>
<h2>Параметры</h2>{table(data['parameters'].items())}<h2>Последние индексы</h2>{table((key,weather[key]) for key in ('ffmc','dmc','dc','isi','bui','fwi'))}
<h2>Источники</h2><ul>{sources}</ul><p>Погода: {esc(meta['weather_source']['kind'])}. {esc(meta['weather_source'].get('credit',''))}</p>
<p>Рельеф: {esc(meta['dem_source']['kind'])}. {esc(meta['dem_source'].get('credit',''))}</p>
<p>{esc(scene['credit'] if scene else 'Спутниковый снимок не использовался.')}</p>
<h2>Файлы</h2><ul>{files}</ul><p>Происхождение снимков и каналов, погода, начальные состояния, геометрия и исходные допущения сохранены в inputs.json и manifest.json. Код CFFDRS включён по GPL-2.0 с лицензией и указанием точной ревизии.</p></html>''',encoding='utf-8')
