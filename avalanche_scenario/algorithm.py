"""Processing interface: usable in batch models and the Python console."""
from pathlib import Path
from datetime import datetime
from qgis.core import (Qgis, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination, QgsProcessingOutputHtml,
    QgsProcessingOutputFile, QgsProcessingOutputRasterLayer)
from .workflow import run_workflow


def source_features(source):
    """Copy values so no live project layers are passed to computation."""
    features = []
    for feature in source.getFeatures():
        if not feature.hasGeometry() or feature.geometry().isEmpty():
            raise ValueError(f'Объект {feature.id()} имеет пустую геометрию.')
        features.append({'id': str(feature.id()), 'wkt': feature.geometry().asWkt()})
    return features


class ScenarioAlgorithm(QgsProcessingAlgorithm):
    def name(self):
        return 'railway_scenario'

    def displayName(self):
        return 'Лавинное воздействие и риск для железной дороги'

    def group(self):
        return 'Предварительная оценка'

    def groupId(self):
        return 'screening'

    def createInstance(self):
        return ScenarioAlgorithm()

    def shortHelpString(self):
        return ('Выделите один полигон, включающий участок ЖД. Без локальной ЦМР '
                'загружается Copernicus GLO-30 через интернет. В буфере ищутся '
                'кандидатные очаги по уклону 30–50°. Равнинный участок получает '
                'отчёт о невыполнении критериев. Трасса ЖД необязательна для карты, '
                'обязательна для оценки воздействия на дорогу. Денежный риск '
                'вычисляется только при включённом флаге и заданных частоте, '
                'стоимости восстановления и уязвимости. Огибающая трассеров с '
                'трением Фёлльми, не авторская SPH-модель Соловьева. '
                'Назначение — выбор участков изысканий, не проектное обоснование.')

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource('AREA', 'Участок обследования (один полигон)',
            [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterRasterLayer('DEM', 'Локальная ЦМР; пусто = загрузить Copernicus 30 м', optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource('RAIL', 'Ось проектируемой железной дороги',
            [Qgis.ProcessingSourceType.VectorLine], optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource('RELEASE', 'Известные очаги; пусто = поиск по рельефу',
            [Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterBoolean('RISK', 'Рассчитать денежный риск по заданным данным', False))
        numeric = [
            ('BUFFER', 'Буфер поиска очагов вокруг участка, м', 3000, 0, 20000),
            ('RESOLUTION', 'Шаг расчётной сетки, м (не повышает качество исходной ЦМР)', 30, 1, 1000),
            ('MIN_SLOPE', 'Минимальный уклон кандидатного очага, °', 30, 1, 80),
            ('MAX_SLOPE', 'Максимальный уклон кандидатного очага, °', 50, 2, 89),
            ('MIN_RELIEF', 'Минимальный перепад высот в расчётной области, м', 100, 0, 5000),
            ('MIN_AREA', 'Минимальная связная площадь очага, м²', 3600, 1, 1e8),
            ('MU', 'Сухое трение mu (сценарное допущение)', .25, 0, 2),
            ('XI', 'Коэффициент xi, м/с² (сценарное допущение)', 1000, 1, 10000),
            ('FLOW_DEPTH', 'Заданная высота движущегося потока, м', 1, .01, 20),
            ('RELEASE_DEPTH', 'Толщина отрыва для оценки начального объёма, м', 1, .01, 20),
            ('DENSITY', 'Плотность снега, кг/м³', 300, 1, 917),
            ('DURATION', 'Продолжительность моделирования, с', 240, 1, 3600),
            ('TIME_STEP', 'Максимальный шаг по времени, с', .1, .001, 1),
            ('CP', 'Коэффициент давления Cp в p=Cp*rho*v²', 1, .01, 10),
            ('P_LOW', 'Нижний порог интенсивности, кПа', 3, .01, 1000),
            ('P_HIGH', 'Верхний порог интенсивности, кПа', 30, .02, 10000),
            ('FREQUENCY', 'Частота всего сценария, событий/год', .01, 0, 100),
            ('VULNERABILITY', 'Доля ущерба затронутому пути, 0–1', .5, 0, 1),
            ('REPAIR_COST', 'Стоимость восстановления метра пути, руб./м', 0, 0, 1e10),
            ('CORRIDOR', 'Полуширина коридора для выборки максимального давления, м', 15, 1, 1000),
        ]
        for key, label, default, minimum, maximum in numeric:
            self.addParameter(QgsProcessingParameterNumber(key, label,
                QgsProcessingParameterNumber.Type.Double, default, False, minimum, maximum))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT', 'Папка результатов (внутри будет новая папка расчёта)'))
        self.addOutput(QgsProcessingOutputHtml('REPORT', 'Отчёт'))
        self.addOutput(QgsProcessingOutputFile('MANIFEST', 'Параметры и результаты JSON'))
        self.addOutput(QgsProcessingOutputFile('VECTORS', 'Зоны воздействия и трасса ЖД, GeoPackage'))
        self.addOutput(QgsProcessingOutputRasterLayer('PRESSURE', 'Максимальное сценарное давление, кПа'))

    def processAlgorithm(self, parameters, context, feedback):
        try:
            area = self.parameterAsSource(parameters, 'AREA', context)
            if area is None:
                raise ValueError('Выберите полигон участка.')
            areas = source_features(area)
            if len(areas) != 1:
                raise ValueError('Укажите ровно один полигон участка; можно использовать только выбранный объект.')
            rail = self.parameterAsSource(parameters, 'RAIL', context)
            release = self.parameterAsSource(parameters, 'RELEASE', context)
            dem = self.parameterAsRasterLayer(parameters, 'DEM', context)
            number = lambda name: self.parameterAsDouble(parameters, name, context)
            output = Path(self.parameterAsString(parameters, 'OUTPUT', context))
            directory = output/('avalanche_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            config = dict(area_wkt=areas[0]['wkt'], area_crs=area.sourceCrs().toWkt(),
                          dem=dem.source() if dem else '', directory=str(directory),
                          rails=source_features(rail) if rail else [],
                          rail_crs=rail.sourceCrs().toWkt() if rail else '',
                          release_wkts=[x['wkt'] for x in source_features(release)] if release else [],
                          release_crs=release.sourceCrs().toWkt() if release else '',
                          buffer=number('BUFFER'), resolution=number('RESOLUTION'),
                          screening=dict(min_slope=number('MIN_SLOPE'), max_slope=number('MAX_SLOPE'),
                                         min_relief=number('MIN_RELIEF'), min_area=number('MIN_AREA')),
                          parameters={key: number(param) for key,param in dict(
                              mu='MU', xi='XI', flow_depth='FLOW_DEPTH', release_depth='RELEASE_DEPTH',
                              density='DENSITY', duration='DURATION', time_step='TIME_STEP',
                              pressure_coefficient='CP', low_pressure='P_LOW', high_pressure='P_HIGH').items()},
                          frequency=number('FREQUENCY') if self.parameterAsBool(parameters, 'RISK', context) else None,
                          vulnerability=number('VULNERABILITY'), repair_cost=number('REPAIR_COST'),
                          corridor=number('CORRIDOR'))
            state = {'message': ''}
            def notify(pct, message):
                feedback.setProgress(pct)
                if state['message'] != message:
                    feedback.pushInfo(message)
                    state['message'] = message
            manifest = run_workflow(config, notify, feedback.isCanceled)
            for warning in manifest['warnings']:
                feedback.pushWarning(warning)
            out = manifest['outputs']
            return {'OUTPUT': str(directory), 'REPORT': out['report'],
                    'MANIFEST': out['manifest'], 'VECTORS': out.get('vectors', ''),
                    'PRESSURE': out.get('pressure_kpa', '')}
        except (ValueError, RuntimeError, InterruptedError) as exc:
            raise QgsProcessingException(str(exc)) from exc
