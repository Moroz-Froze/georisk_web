from dataclasses import asdict
from datetime import datetime,timezone
import html
import json
from pathlib import Path
import numpy as np
from osgeo import gdal,ogr
from . import __version__
from .core import Parameters,SOIL_FIELDS,CLASS_LABELS,analyze,validate_soil
from .terrain import prepare_terrain,write_raster,rasterize_geometry,transformed_geometry,spatial_ref

SOURCES=[
    dict(title='Pack, Tarboton, Goodwin. SINMAP manual, Part I §2.2, Eq. (1), printed p.5; vertical depth D.',
         url='https://hydrology.usu.edu/sinmap/sinmap.PDF',role='Уравнение равновесия бесконечного склона'),
    dict(title='USGS: TRIGRS',url='https://www.usgs.gov/software/trigrs',
         role='Обзор моделирования дождевой инфильтрации; TRIGRS в плагине не реализован'),
    dict(title='USGS: Scoops3D',url='https://www.usgs.gov/software/scoops3d',
         role='Сопоставление с объёмным поиском поверхностей; Scoops3D не реализован'),
    dict(title='Copernicus DEM, AWS public dataset',url='https://registry.opendata.aws/copernicus-dem/',
         role='Автоматическая загрузка реального рельефа'),
]


def soil_arrays(config,terrain,eligible):
    shape=terrain['dem'].shape
    maps={key:np.full(shape,np.nan) for key in SOIL_FIELDS}
    occupied=np.zeros(shape,dtype=bool)
    for zone in config['soil_zones']:
        try:values={key:float(zone['values'][key]) for key in SOIL_FIELDS}
        except (ValueError,TypeError,KeyError) as exc:
            raise ValueError(f"Зона {zone['id']}: нужны числовые поля {', '.join(SOIL_FIELDS)}.") from exc
        validate_soil(values)
        geom=transformed_geometry(zone['wkt'],config['soil_crs'],terrain['projection'])
        mask=rasterize_geometry(geom,shape,terrain['transform'],terrain['projection'])
        if np.any(mask & occupied & eligible):
            raise ValueError('Инженерно-геологические зоны перекрываются на расчётных склонах.')
        for key in SOIL_FIELDS:maps[key][mask]=values[key]
        occupied |= mask
    if np.any(eligible & ~occupied):
        missing=int(np.count_nonzero(eligible & ~occupied))
        raise ValueError(f'Грунтовые зоны не покрывают {missing} расчётных ячеек. '
                         'Дополните зоны в выделении и буфере либо используйте единые сценарные параметры.')
    # Unanalyzed cells have no mechanical result. Set neutral finite fillers only
    # to allow validation; these cells are masked out of every FS product.
    default=Parameters(**config.get('parameters',{}))
    for key in SOIL_FIELDS:maps[key][~occupied]=getattr(default,key)
    return maps


def polygons(ds,name,classes,terrain):
    height,width=classes.shape
    mem=gdal.GetDriverByName('MEM').Create('',width,height,1,gdal.GDT_Byte)
    mem.SetGeoTransform(terrain['transform']);mem.SetProjection(terrain['projection'])
    band=mem.GetRasterBand(1);band.WriteArray(classes);band.SetNoDataValue(255)
    layer=ds.CreateLayer(name,spatial_ref(terrain['projection']),ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('class_id',ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn('meaning',ogr.OFTString))
    gdal.Polygonize(band,band.GetMaskBand(),layer,0,[])
    for feat in layer:
        feat.SetField('meaning',CLASS_LABELS.get(feat.GetField('class_id'),'Нет данных'))
        layer.SetFeature(feat)
    layer.ResetReading();return layer


def railway_overlay(config,terrain,analysis,ds,zones):
    layer=ds.CreateLayer('railway_screening',spatial_ref(terrain['projection']),ogr.wkbUnknown)
    for key,kind in [('source_id',ogr.OFTString),('coverage',ogr.OFTString),('length_m',ogr.OFTReal),
                     ('unstable_m',ogr.OFTReal),('low_fs_m',ogr.OFTReal),('steep_m',ogr.OFTReal),
                     ('flat_m',ogr.OFTReal),('fs_min',ogr.OFTReal)]:
        layer.CreateField(ogr.FieldDefn(key,kind))
    geometries={k:ogr.Geometry(ogr.wkbMultiPolygon) for k in (0,1,2,4)}
    for feat in zones:
        key=feat.GetField('class_id')
        if key in geometries:geometries[key].AddGeometry(feat.GetGeometryRef())
    dissolved={k:(geom.UnionCascaded() if geom.GetGeometryCount() else None) for k,geom in geometries.items()}
    total=dict(objects=len(config['rails']),incomplete=0,unstable_length_m=0.0,low_reserve_length_m=0.0)
    transform=terrain['transform'];xmin,dx,_,ymax,_,ndy=transform
    xmax=xmin+terrain['dem'].shape[1]*dx;ymin=ymax+terrain['dem'].shape[0]*ndy
    bounds=ogr.CreateGeometryFromWkt(f'POLYGON (({xmin} {ymin},{xmax} {ymin},{xmax} {ymax},{xmin} {ymax},{xmin} {ymin}))')
    for rail in config['rails']:
        geom=transformed_geometry(rail['wkt'],config['rail_crs'],terrain['projection'])
        if ogr.GT_Flatten(geom.GetGeometryType()) not in (ogr.wkbLineString,ogr.wkbMultiLineString):
            raise ValueError('Ось ЖД должна быть линейным слоем.')
        corridor=geom.Buffer(config.get('corridor',15))
        mask=rasterize_geometry(corridor,terrain['dem'].shape,transform,terrain['projection'],True)
        present=mask & analysis['safe']
        coverage='complete'
        if not np.any(present):coverage='outside_or_nodata'
        elif not bounds.Contains(corridor) or np.any(mask & ~analysis['safe']):coverage='partial'
        fs=analysis['arrays']['fs_selected'];known=mask & np.isfinite(fs)
        feat=ogr.Feature(layer.GetLayerDefn());feat.SetGeometry(geom)
        feat.SetField('source_id',str(rail['id']));feat.SetField('coverage',coverage)
        feat.SetField('length_m',geom.Length())
        lengths={}
        for key,field in ((0,'flat_m'),(1,'unstable_m'),(2,'low_fs_m'),(4,'steep_m')):
            lengths[key]=geom.Intersection(dissolved[key]).Length() if dissolved[key] else 0.0
            if coverage=='complete':feat.SetField(field,lengths[key])
        if np.any(known):feat.SetField('fs_min',float(np.min(fs[known])))
        layer.CreateFeature(feat)
        if coverage!='complete':total['incomplete']+=1
        else:
            total['unstable_length_m']+=lengths[1];total['low_reserve_length_m']+=lengths[2]
    if total['incomplete']:
        total['assessed_only_unstable_length_m']=total['unstable_length_m']
        total['unstable_length_m']=None;total['low_reserve_length_m']=None
    return total


def run_workflow(config,notify=None,cancelled=None):
    p=Parameters(**config.get('parameters',{}));p.validate()
    directory=Path(config['directory']);directory.mkdir(parents=True,exist_ok=True)
    if (directory/'manifest.json').exists():raise ValueError('В папке уже есть расчёт. Выберите новую папку.')
    def check():
        if cancelled and cancelled():raise InterruptedError('Расчёт отменён.')
    check()
    terrain=prepare_terrain(config['area_wkt'],config['area_crs'],directory,
        local_dem=config.get('dem',''),buffer_m=config.get('buffer',500),
        resolution=config.get('resolution',30),notify=notify,cancelled=cancelled)
    check()
    if notify:notify(35,'Расчёт устойчивости склонов…')
    dx,dy=terrain['transform'][1],-terrain['transform'][5]
    result=analyze(terrain['dem'],dx,dy,p)
    if config.get('soil_zones'):
        maps=soil_arrays(config,terrain,result['eligible'])
        result=analyze(terrain['dem'],dx,dy,p,maps)
    else:maps={key:np.full(terrain['dem'].shape,getattr(p,key)) for key in SOIL_FIELDS}
    check()
    outputs={'terrain':terrain['terrain_path']}
    for key,array in result['arrays'].items():
        path=directory/(key+'.tif')
        write_raster(path,array,terrain['transform'],terrain['projection'],key.endswith('_class'))
        outputs[key]=str(path)
    for key,array in maps.items():
        path=directory/('input_'+key+'.tif')
        write_raster(path,np.where(result['safe'],array,np.nan),terrain['transform'],terrain['projection'])
        outputs['input_'+key]=str(path)
    if notify:notify(65,'Сохранение зон и пересечение с трассой…')
    vector=directory/'landslide.gpkg';ds=ogr.GetDriverByName('GPKG').CreateDataSource(str(vector))
    zones=polygons(ds,'stability_zones',result['arrays']['stability_class'],terrain)
    summary=result['summary']
    area=terrain['area_mask']
    classes=result['arrays']['stability_class']
    summary.update(target_unstable_area_m2=int(np.count_nonzero(area & (classes==1)))*dx*dy,
                   target_low_reserve_area_m2=int(np.count_nonzero(area & (classes==2)))*dx*dy,
                   target_unknown_area_m2=int(np.count_nonzero(area & (classes==255)))*dx*dy)
    warnings=[
        'Оценивается зарождение неглубоких сдвигов параллельно склону. Дальность движения, обвалы и глубокие круглоцилиндрические оползни не моделируются.',
        'FS — отношение удерживающих и сдвигающих сил. Это не вероятность события и не частота в год.',
        'Порог отображения запаса FS='+str(p.reserve_threshold)+' выбран пользователем, не является нормативом для ЖД.',
        'Обводнение m задаётся как доля вертикальной высоты воды в слое, а не влажность грунта или количество осадков.',
        'Удельный вес грунта одинаков во всех трёх сценариях. Подсос, фильтрационная нестационарность, сейсмика, выемки, подрезка и нагрузка поездов не учтены.',
        'Для проектных решений по ЖД нужны инженерно-геологические изыскания, гидрогеология и проверка модели специалистом.'
    ]
    if not config.get('soil_zones'):
        warnings.insert(0,'Использованы единые заданные пользователем сценарные свойства грунта; из рельефа они не определяются и автоматически не загружаются.')
    if str(terrain['source']['kind']).startswith('Copernicus'):
        warnings.append('Copernicus GLO-30 — модель поверхности с растительностью и сооружениями. Шаг около 30 м недостаточен для малых откосов и деталей пути.')
    railway=None
    if config.get('rails'):
        railway=railway_overlay(config,terrain,result,ds,zones)
        warnings.append('Пересечение с ЖД показывает расположение пути в зонах потенциального срыва, а не попадание оползня с соседнего склона и не денежный риск.')
    ds=None;outputs['vectors']=str(vector)
    check()
    outputs['report']=str(directory/'report.html');outputs['manifest']=str(directory/'manifest.json')
    status='completed' if summary['analyzed_cells'] else 'no_analyzable_slopes'
    if status!='completed':warnings.insert(0,'В выделении и буфере нет склонов в заданном диапазоне анализа. Проверьте участок и параметры; это не заключение о безопасности.')
    manifest=dict(plugin='landslide_scenario',version=__version__,generated_utc=datetime.now(timezone.utc).isoformat(),
        status=status,crs=terrain['crs'],source=terrain['source'],input=config,parameters=asdict(p),
        summary=summary,railway=railway,warnings=warnings,sources=SOURCES,outputs=outputs)
    (directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(directory/'report.html',manifest)
    if notify:notify(100,'Готово.')
    return manifest


def write_report(path,data):
    esc=html.escape;s=data['summary']
    rows=[('Статус','Рассчитано' if data['status']=='completed' else 'Нет склонов в диапазоне анализа'),
        ('Система координат',data['crs']),('Минимальный FS в расчётной области','Нет расчётных ячеек' if s['min_fs'] is None else f"{s['min_fs']:.3f}"),
        ('Площадь FS < 1 в выделении, м²',f"{s['target_unstable_area_m2']:.0f}"),
        ('Площадь малого запаса в выделении, м²',f"{s['target_low_reserve_area_m2']:.0f}"),
        ('Площадь без данных в выделении, м²',f"{s['target_unknown_area_m2']:.0f}"),
        ('Водочувствительные склоны в области, м²',f"{s['water_sensitive_area_m2']:.0f}"),
        ('Источник грунтовых параметров','Полигональные зоны пользователя' if data['input'].get('soil_zones') else 'Единые сценарные значения')]
    if data['railway']:
        rail=data['railway'];length=rail['unstable_length_m']
        rows.append(('Длина оси ЖД в зонах FS < 1, м','Неизвестно: неполное покрытие' if length is None else f'{length:.1f}'))
    table=lambda items: '<table>'+''.join('<tr><th>'+esc(str(k))+'</th><td>'+esc(str(v))+'</td></tr>' for k,v in items)+'</table>'
    sources=''.join('<li><a href="'+esc(x['url'])+'">'+esc(x['title'])+'</a> — '+esc(x['role'])+'</li>' for x in data['sources'])
    files=''.join('<li><a href="'+esc(Path(v).name)+'">'+esc(k)+'</a></li>' for k,v in data['outputs'].items())
    warnings=''.join('<li>'+esc(x)+'</li>' for x in data['warnings'])
    path.write_text(f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Оползни · устойчивость склонов</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;color:#27483f;max-width:1050px;margin:35px auto;padding:0 25px}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #d7e4dc}}th{{width:65%}}.note{{background:#fff2dc;padding:18px;border-radius:10px}}a{{color:#0d7164}}</style>
<small>ОПОЛЗНИ · ПРЕДВАРИТЕЛЬНАЯ ОЦЕНКА · v{__version__} · UTC {esc(data['generated_utc'])}</small>
<h1>Устойчивость склонов</h1>{table(rows)}
<h2>Расчётная модель</h2><p>FS = [c + c<sub>root</sub> + (γ z cos²β − u) tanφ] / (γ z sinβ cosβ), где u = m γ<sub>w</sub> z cos²β.</p>
<p>z — вертикальная глубина поверхности скольжения, м; c и c<sub>root</sub> — эффективное и дополнительное корневое сцепление, кПа; γ — заданный удельный вес грунта, кН/м³; φ — эффективный угол трения, градусы; m = высота воды в слое / z. γ<sub>w</sub> = 9.81 кН/м³. Сравниваются m=0, заданное m и m=1 при одинаковом γ. Критическое m получается из уравнения FS=1: отрицательное означает FS&lt;1 уже без воды, больше 1 — порог не достигается в диапазоне; при φ=0 водный порог не определён.</p>
<p>Классы: 0 — ниже минимального уклона; 1 — FS&lt;1; 2 — от 1 до заданного порога запаса; 3 — от порога; 4 — выше максимального уклона; 255 — нет данных. FS≥1 не доказывает пригодность участка для строительства.</p>
<div class="note"><b>Границы применимости</b><ul>{warnings}</ul></div>
<h2>Единые параметры и фильтры</h2>{table(data['parameters'].items())}
<p>При наличии грунтовых зон фактические значения по ячейкам сохранены в input_*.tif; единые значения не подставляются в непокрытые расчётные зоны.</p>
<h2>Источники метода и данных</h2><ul>{sources}</ul><p>Происхождение ЦМР: {esc(data['source']['kind'])}. {esc(data['source'].get('credit',''))}</p>
<h2>Файлы</h2><ul>{files}</ul><p>Геометрия, полная конфигурация, источники и результаты сохранены в manifest.json. Плагин не является реализацией SINMAP, TRIGRS или Scoops3D.</p></html>''',encoding='utf-8')
