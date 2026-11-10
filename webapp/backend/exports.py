import html
from pathlib import Path
import zipfile
import numpy as np
from osgeo import gdal
from PIL import Image
from .storage import write_json

PALETTES = {
    'stability_class': [(0, '#9daeb3', 'Ниже минимального уклона'), (1, '#d64045', 'FS < 1'),
                        (2, '#f0ae45', 'Малый запас'), (3, '#479776', 'Больший запас'), (4, '#805d9c', 'Выше предела уклона')],
    'intensity': [(0, '#d5e3e5', 'Нет воздействия в сценарии'), (1, '#ffd17a', 'Низкое давление'),
                  (2, '#ee8853', 'Среднее давление'), (3, '#c43645', 'Высокое давление')],
    'intensity_class': [(0, '#94acbb', 'Негорючее / вода'), (1, '#ffd17a', 'Низкая интенсивность'),
                        (2, '#ee8853', 'Средняя интенсивность'), (3, '#c43645', 'Высокая интенсивность'), (4, '#805d9c', 'Выше предела уклона')],
    'release_candidates': [(1, '#ed8953', 'Кандидатный очаг')],
    'reached': [(1, '#ec653b', 'Достигнуто огнём')],
}
TITLES = {
    'stability_class': 'Классы устойчивости', 'fs_selected': 'Коэффициент FS · заданная вода',
    'fs_dry': 'FS · без напора воды', 'fs_saturated': 'FS · полное обводнение',
    'critical_m': 'Критическое обводнение', 'pore_kpa': 'Поровое давление, кПа',
    'intensity': 'Классы лавинного воздействия', 'pressure_kpa': 'Давление лавины, кПа',
    'speed_m_s': 'Скорость лавины, м/с', 'arrival_s': 'Время прихода лавины, с',
    'release_candidates': 'Кандидатные лавинные очаги', 'candidates': 'Кандидатные лавинные очаги',
    'intensity_class': 'Потенциальная пожарная интенсивность', 'intensity_kw_m': 'Интенсивность, кВт/м',
    'head_m_min': 'Скорость фронта огня, м/мин', 'arrival_min': 'Время прихода огня, мин',
    'reached': 'Зона распространения огня', 'ndvi': 'Растительность · NDVI', 'ndmi': 'Водное состояние · NDMI',
    'terrain': 'Высоты поверхности, м', 'slope': 'Уклон, °', 'slope_deg': 'Уклон, °',
}
PREFERRED = {'avalanche': ['intensity', 'pressure_kpa', 'speed_m_s', 'arrival_s', 'candidates', 'slope', 'terrain'],
             'landslide': ['stability_class', 'fs_selected', 'fs_dry', 'fs_saturated', 'critical_m', 'pore_kpa', 'slope_deg', 'terrain'],
             'wildfire': ['reached', 'intensity_class', 'arrival_min', 'head_m_min', 'intensity_kw_m', 'ndvi', 'ndmi', 'terrain']}


def rgb(color):
    return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))


def preview(path, key, destination):
    source = gdal.Open(str(path))
    warped = gdal.AutoCreateWarpedVRT(source, None, 'EPSG:4326')
    scale = min(1, 1200/max(warped.RasterXSize, warped.RasterYSize))
    palette_key = 'release_candidates' if key == 'candidates' else key
    categorical = palette_key in PALETTES
    ds = gdal.Warp('', source, format='MEM', dstSRS='EPSG:4326',
                   width=max(1, round(warped.RasterXSize*scale)), height=max(1, round(warped.RasterYSize*scale)),
                   resampleAlg='near' if categorical else 'bilinear', dstNodata=-9999, outputType=gdal.GDT_Float32)
    values = ds.ReadAsArray()
    valid = np.isfinite(values) & (values != -9999) & (values != 255 if categorical else True)
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    if categorical:
        legend = []
        for number, color, label in PALETTES[palette_key]:
            rgba[valid & (values == number)] = (*rgb(color), 210)
            legend.append(dict(color=color, label=label))
    else:
        finite = values[valid]
        low, high = (float(np.min(finite)), float(np.max(finite))) if finite.size else (0., 1.)
        if key in ('ndvi', 'ndmi'):
            low, high = -1., 1.
        t = np.clip((np.where(valid, values, low)-low)/max(high-low, 1e-9), 0, 1)
        start, end = (rgb('#f0d9a5'), rgb('#24745b')) if key in ('ndvi', 'ndmi', 'fs_selected', 'fs_dry', 'fs_saturated') else (rgb('#c5e8ed'), rgb('#973e58'))
        for channel in range(3):
            rgba[:, :, channel] = start[channel] + t*(end[channel]-start[channel])
        rgba[:, :, 3] = np.where(valid, 215, 0)
        legend = [dict(color='#%02x%02x%02x' % start, label=f'{low:.3g}'), dict(color='#%02x%02x%02x' % end, label=f'{high:.3g}')]
    Image.fromarray(rgba).save(destination)
    transform = ds.GetGeoTransform()
    west, north = transform[0], transform[3]
    east = west+ds.RasterXSize*transform[1]
    south = north+ds.RasterYSize*transform[5]
    return dict(bounds=[[south, west], [north, east]], legend=legend)


def prepare_result(job_id, folder, hazard, manifest):
    outputs = manifest['outputs']
    preview_dir = folder/'previews'
    preview_dir.mkdir()
    layers = []
    base = f'/api/jobs/{job_id}'
    for key in PREFERRED[hazard]:
        if key not in outputs:
            continue
        metadata = preview(outputs[key], key, preview_dir/(key+'.png'))
        layers.append(dict(id=key, title=TITLES[key], url=f'{base}/previews/{key}.png', **metadata))
    files = []
    for key, filename in outputs.items():
        path = Path(filename)
        relative = path.relative_to(folder/'result').as_posix()
        files.append(dict(id=key, name=relative, title=TITLES.get(key, key), url=f'{base}/files/{relative}'))
    # Original plugin reports use desktop file: links; make those portable in the ZIP.
    report = Path(outputs['report'])
    text = report.read_text(encoding='utf-8')
    for value in outputs.values():
        path = Path(value)
        relative = path.relative_to(folder/'result').as_posix()
        text = text.replace(html.escape(path.resolve().as_uri()), relative)
        text = text.replace('href="'+path.name+'"', 'href="'+relative+'"')
    report.write_text(text, encoding='utf-8')
    result = dict(status=manifest['status'], summary=manifest.get('summary', manifest.get('simulation', {})),
                  screening=manifest.get('screening'), railway=manifest.get('railway'), warnings=manifest['warnings'],
                  sources=manifest.get('sources', []), parameters=manifest['parameters'],
                  data=manifest.get('data', {}).get('latest_weather'), layers=layers, files=files,
                  provenance=dict(terrain=manifest.get('source', manifest.get('data', {}).get('dem_source')),
                                  satellite=manifest.get('data', {}).get('satellite_source'),
                                  weather=manifest.get('data', {}).get('weather_source'),
                                  scenario_date=manifest.get('input', {}).get('scenario_date')),
                  report_url=f'{base}/files/report.html', archive_url=f'{base}/archive')
    write_json(folder/'result.json', result)
    with zipfile.ZipFile(folder/'results.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (folder/'result').rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(folder/'result'))
    return result
