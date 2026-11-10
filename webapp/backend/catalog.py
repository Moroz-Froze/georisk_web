from dataclasses import asdict
from avalanche_scenario.core import Parameters as Avalanche
from landslide_scenario.core import Parameters as Landslide
from wildfire_scenario.core import Parameters as Wildfire, FUELS

MODELS = {'avalanche': Avalanche, 'landslide': Landslide, 'wildfire': Wildfire}
LABELS = {
    'mu': ('Сухое трение μ', ''), 'xi': ('Турбулентное сопротивление ξ', 'м/с²'),
    'flow_depth': ('Высота движущегося потока', 'м'), 'release_depth': ('Толщина отрыва снега', 'м'),
    'density': ('Плотность снега', 'кг/м³'), 'duration': ('Длительность', 'с'),
    'time_step': ('Шаг по времени', 'с'), 'pressure_coefficient': ('Коэффициент давления', ''),
    'low_pressure': ('Нижний порог давления', 'кПа'), 'high_pressure': ('Верхний порог давления', 'кПа'),
    'max_particles': ('Максимум трассеров', ''),
    'c_kpa': ('Сцепление грунта', 'кПа'), 'root_kpa': ('Сцепление за счёт корней', 'кПа'),
    'phi_deg': ('Угол внутреннего трения', '°'), 'gamma_knm3': ('Удельный вес грунта', 'кН/м³'),
    'depth_m': ('Вертикальная глубина скольжения', 'м'), 'm_ratio': ('Обводнение: высота воды / глубина слоя', '0–1'),
    'min_slope': ('Минимальный уклон анализа', '°'), 'max_slope': ('Максимальный уклон анализа', '°'),
    'reserve_threshold': ('Граница отображения запаса FS', ''),
    'fuel_type': ('Сценарный тип горючего', ''), 'duration_min': ('Длительность распространения', 'мин'),
    'ffmc_initial': ('Начальный FFMC', ''), 'dmc_initial': ('Начальный DMC', ''), 'dc_initial': ('Начальный DC', ''),
    'grass_curing': ('Доля высохшей травы', '%'), 'grass_load': ('Запас травы', 'кг/м²'),
    'foliar_moisture': ('Влажность листвы', '%'), 'crown_base': ('Высота основания крон', 'м'),
    'crown_load': ('Запас крон', 'кг/м²'), 'intensity_low': ('Нижний порог интенсивности', 'кВт/м'),
    'intensity_high': ('Верхний порог интенсивности', 'кВт/м'), 'max_slope_percent': ('Предельный уклон FBP', '%'),
}
DETAILS = {
    'avalanche': ('Лавины', 'Сход снега и воздействие на железную дорогу', 'Трассеры Фёлльми', 3000,
                  'Сценарная зона воздействия. Состояние снежного покрова и вероятность схода автоматически не определяются.'),
    'landslide': ('Оползни', 'Устойчивость склонов при заданном обводнении', 'Бесконечный склон · Мора–Кулона', 500,
                  'Свойства грунта и обводнение — ваши исходные данные. Значения по умолчанию являются сценарными допущениями.'),
    'wildfire': ('Лесные пожары', 'Пожарная интенсивность и распространение огня', 'FWI 1987 · FBP 1992', 500,
                 'NDVI и NDMI описывают растительность. Они не превращаются в вероятность пожара. Тип горючего — выбранный аналог.'),
}


def catalog():
    result = []
    for key, model in MODELS.items():
        title, subtitle, method, buffer, note = DETAILS[key]
        fields = [{'key': name, 'label': LABELS[name][0], 'unit': LABELS[name][1], 'default': value,
                   'type': 'select' if isinstance(value, str) else 'number',
                   'options': {k: v for k, v in FUELS.items() if k not in ('NF', 'WA')} if name == 'fuel_type' else None}
                  for name, value in asdict(model()).items()]
        result.append(dict(id=key, title=title, subtitle=subtitle, method=method, buffer=buffer, note=note, fields=fields))
    return result


def validated_parameters(hazard, values):
    defaults = asdict(MODELS[hazard]())
    if set(values) - set(defaults):
        raise ValueError('Неизвестные параметры модели: ' + ', '.join(set(values)-set(defaults)))
    converted = {}
    for key, value in values.items():
        if isinstance(defaults[key], str):
            converted[key] = str(value)
        elif isinstance(value, bool):
            raise ValueError('Ожидалось число: ' + key)
        else:
            number = float(value)
            if isinstance(defaults[key], int):
                if not number.is_integer():
                    raise ValueError('Нужно целое число: ' + key)
                number = int(number)
            converted[key] = number
    params = MODELS[hazard](**converted)
    params.validate()
    return asdict(params)
