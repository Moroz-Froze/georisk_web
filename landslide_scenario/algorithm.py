from pathlib import Path
from datetime import datetime
from qgis.core import (Qgis,QgsProcessingAlgorithm,QgsProcessingException,
    QgsProcessingParameterFeatureSource,QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,QgsProcessingParameterFolderDestination,
    QgsProcessingOutputRasterLayer,QgsProcessingOutputHtml,QgsProcessingOutputFile)
from .workflow import run_workflow
from .core import SOIL_FIELDS

PARAMETERS=[
    ('C','c_kpa','Эффективное сцепление грунта, кПа',5,0,10000),
    ('ROOT','root_kpa','Дополнительное корневое сцепление, кПа',0,0,10000),
    ('PHI','phi_deg','Эффективный угол трения, °',30,0,60),
    ('GAMMA','gamma_knm3','Удельный вес грунта, кН/м³',20,9.82,40),
    ('DEPTH','depth_m','Вертикальная глубина слоя скольжения, м',2,.01,100),
    ('M','m_ratio','Обводнение m: высота воды / глубина слоя',.5,0,1),
    ('MIN_SLOPE','min_slope','Минимальный уклон анализа, °',5,.1,89),
    ('MAX_SLOPE','max_slope','Максимальный уклон применимости, °',60,.2,89.9),
    ('RESERVE','reserve_threshold','Порог отображения запаса FS (не норматив)',1.3,1.001,10),
]


def snapshot(source,soil=False):
    features=[]
    for feature in source.getFeatures():
        if not feature.hasGeometry() or not feature.geometry().isGeosValid():
            raise ValueError(f'Объект {feature.id()} имеет некорректную геометрию.')
        item=dict(id=str(feature.id()),wkt=feature.geometry().asWkt())
        if soil:
            missing=[key for key in SOIL_FIELDS if source.fields().indexFromName(key)<0]
            if missing:raise ValueError('В слое грунтов не хватает полей: '+', '.join(missing))
            try:item['values']={key:float(feature[key]) for key in SOIL_FIELDS}
            except (ValueError,TypeError) as exc:raise ValueError('Свойства грунтов должны быть числами, без NULL.') from exc
        features.append(item)
    if soil and not features:raise ValueError('Слой грунтовых зон пуст. Добавьте зоны или отключите слой.')
    return features


class StabilityAlgorithm(QgsProcessingAlgorithm):
    def name(self):return 'infinite_slope'
    def displayName(self):return 'Оползни: коэффициент устойчивости склонов'
    def group(self):return 'Предварительная оценка'
    def groupId(self):return 'screening'
    def createInstance(self):return StabilityAlgorithm()
    def shortHelpString(self):
        return ('Статическая модель бесконечного склона. ЦМР загружается из Copernicus, '
                'если не указан свой растр. Свойства грунта задаются числами или '
                'полигональными зонами с полями c_kpa, root_kpa, phi_deg, gamma_knm3, '
                'depth_m, m_ratio. Глубина вертикальная; m — доля высоты воды в слое. '
                'Три карты FS: сухо, заданное обводнение, полное обводнение. '
                'Не прогноз вероятности, времени и дальности движения оползня.')
    def initAlgorithm(self,config=None):
        self.addParameter(QgsProcessingParameterFeatureSource('AREA','Один полигон участка',[Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterRasterLayer('DEM','Своя ЦМР; пусто = реальный Copernicus',optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource('SOIL','Инженерно-геологические зоны (необязательно)',[Qgis.ProcessingSourceType.VectorPolygon],optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource('RAIL','Ось ЖД (необязательно)',[Qgis.ProcessingSourceType.VectorLine],optional=True))
        for name,key,label,default,low,high in PARAMETERS:
            self.addParameter(QgsProcessingParameterNumber(name,label,QgsProcessingParameterNumber.Type.Double,default,False,low,high))
        for name,label,default,low,high in [('BUFFER','Буфер анализа вокруг участка, м',500,0,20000),('RESOLUTION','Шаг сетки, м',30,1,1000),('CORRIDOR','Полуширина выборки FS вдоль ЖД, м',15,1,1000)]:
            self.addParameter(QgsProcessingParameterNumber(name,label,QgsProcessingParameterNumber.Type.Double,default,False,low,high))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT','Папка результатов'))
        self.addOutput(QgsProcessingOutputRasterLayer('FS','FS при заданном обводнении'))
        self.addOutput(QgsProcessingOutputRasterLayer('CLASSES','Классы устойчивости'))
        self.addOutput(QgsProcessingOutputHtml('REPORT','Отчёт'))
        self.addOutput(QgsProcessingOutputFile('MANIFEST','Конфигурация JSON'))
    def processAlgorithm(self,parameters,context,feedback):
        try:
            area=self.parameterAsSource(parameters,'AREA',context)
            if area is None:raise ValueError('Выберите один полигон участка.')
            areas=snapshot(area)
            if len(areas)!=1:raise ValueError('Нужен ровно один полигон участка.')
            soil=self.parameterAsSource(parameters,'SOIL',context)
            rail=self.parameterAsSource(parameters,'RAIL',context)
            dem=self.parameterAsRasterLayer(parameters,'DEM',context)
            number=lambda name:self.parameterAsDouble(parameters,name,context)
            folder=Path(self.parameterAsString(parameters,'OUTPUT',context))/('landslide_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            config=dict(area_wkt=areas[0]['wkt'],area_crs=area.sourceCrs().toWkt(),dem=dem.source() if dem else '',
                soil_zones=snapshot(soil,True) if soil else [],soil_crs=soil.sourceCrs().toWkt() if soil else '',
                rails=snapshot(rail) if rail else [],rail_crs=rail.sourceCrs().toWkt() if rail else '',
                parameters={key:number(name) for name,key,*_ in PARAMETERS},buffer=number('BUFFER'),
                resolution=number('RESOLUTION'),corridor=number('CORRIDOR'),directory=str(folder))
            def notify(pct,message):feedback.setProgress(pct);feedback.setProgressText(message)
            result=run_workflow(config,notify,feedback.isCanceled)
            for warning in result['warnings']:feedback.pushWarning(warning)
            out=result['outputs']
            return dict(OUTPUT=str(folder),FS=out['fs_selected'],CLASSES=out['stability_class'],REPORT=out['report'],MANIFEST=out['manifest'])
        except (ValueError,RuntimeError,InterruptedError) as exc:raise QgsProcessingException(str(exc)) from exc
