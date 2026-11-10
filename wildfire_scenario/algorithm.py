from pathlib import Path
from datetime import datetime,timezone
from qgis.core import (Qgis,QgsProcessingAlgorithm,QgsProcessingException,
    QgsProcessingParameterFeatureSource,QgsProcessingParameterRasterLayer,QgsProcessingParameterNumber,
    QgsProcessingParameterFolderDestination,QgsProcessingParameterEnum,QgsProcessingParameterString,
    QgsProcessingParameterFile,QgsProcessingParameterBoolean,QgsProcessingOutputHtml,QgsProcessingOutputFile)
from .core import FUELS
from .workflow import run_workflow

PARAMETERS=[
    ('DURATION','duration_min','Продолжительность сценария, мин',180,1,1440),
    ('FFMC','ffmc_initial','Начальный FFMC до первого дня ряда',85,0,101),
    ('DMC','dmc_initial','Начальный DMC до первого дня ряда',6,0,1000),
    ('DC','dc_initial','Начальный DC до первого дня ряда',15,0,1000),
    ('CURING','grass_curing','Доля высохшей травы, %',80,.1,100),
    ('GFL','grass_load','Запас травы, кг/м²',.35,.001,100),
    ('FMC','foliar_moisture','Влажность хвои/листвы для FBP, % сухой массы',100,.1,120),
    ('CBH','crown_base','Высота основания крон, м',4,.01,100),
    ('CFL','crown_load','Запас горючего в кронах, кг/м²',.8,.001,20),
    ('LOW','intensity_low','Нижний порог отображения интенсивности, кВт/м',2000,.1,1e6),
    ('HIGH','intensity_high','Верхний порог отображения интенсивности, кВт/м',4000,.2,1e7),
    ('MAXSLOPE','max_slope_percent','Предельный уклон FBP, %',200,1,200),
]


def snapshot(source,fuel=False):
    features=[]
    for feature in source.getFeatures():
        if not feature.hasGeometry() or not feature.geometry().isGeosValid():raise ValueError('Некорректная геометрия объекта '+str(feature.id()))
        item=dict(id=str(feature.id()),wkt=feature.geometry().asWkt())
        if fuel:
            if source.fields().indexFromName('fuel')<0:raise ValueError('В карте горючего требуется текстовое поле fuel.')
            item['fuel']=str(feature['fuel']).upper().replace('-','').replace(' ','')
            if item['fuel'] not in FUELS:raise ValueError('Неподдерживаемый тип горючего: '+item['fuel'])
        features.append(item)
    if fuel and not features:raise ValueError('Карта горючего пуста.')
    return features


class WildfireAlgorithm(QgsProcessingAlgorithm):
    def name(self):return 'forest_fire'
    def displayName(self):return 'Лесные пожары: FWI, NDVI/NDMI и сценарий распространения'
    def group(self):return 'Сценарная оценка'
    def groupId(self):return 'scenarios'
    def createInstance(self):return WildfireAlgorithm()
    def shortHelpString(self):
        return ('Реальный Copernicus DEM, Sentinel-2 Collection 1, модельная погода Open-Meteo. '
                'FWI1987 и FBP1992/2009. Без очага строятся карты потенциальной интенсивности; '
                'с очагом — время прихода огня при постоянных погодных условиях. Свой слой '
                'горючего имеет поле fuel: C2,C3,D1,O1A,O1B,S1,NF,WA. NDVI не является вероятностью пожара. '
                'Без спутника обязателен свой слой горючего. CSV погоды: date,temp_c,rh_pct,wind_kmh,wind_from_deg,rain_mm; '
                'непрерывные дни, полдень местного стандартного времени и 24-часовые осадки.')
    def initAlgorithm(self,config=None):
        for name,label,kind,optional in [('AREA','Один полигон участка',Qgis.ProcessingSourceType.VectorPolygon,False),
            ('FUELMAP','Карта горючего (необязательно)',Qgis.ProcessingSourceType.VectorPolygon,True),
            ('IGNITION','Точки условного зажигания',Qgis.ProcessingSourceType.VectorPoint,True),
            ('RAIL','Ось ЖД (необязательно)',Qgis.ProcessingSourceType.VectorLine,True)]:
            self.addParameter(QgsProcessingParameterFeatureSource(name,label,[kind],optional=optional))
        self.addParameter(QgsProcessingParameterRasterLayer('DEM','Своя ЦМР; пусто = Copernicus',optional=True))
        self.addParameter(QgsProcessingParameterBoolean('SATELLITE','Загрузить Sentinel-2 и рассчитать NDVI/NDMI',True))
        self.addParameter(QgsProcessingParameterFile('WEATHER','Свой CSV погоды; пусто = Open-Meteo',extension='csv',optional=True))
        self.addParameter(QgsProcessingParameterString('DATE','Дата сценария YYYY-MM-DD',datetime.now(timezone.utc).date().isoformat()))
        self.addParameter(QgsProcessingParameterEnum('FUEL','Сценарный аналог горючего для растительности',list(FUELS.values())[:6],defaultValue=0))
        for name,key,label,default,low,high in PARAMETERS:
            self.addParameter(QgsProcessingParameterNumber(name,label,QgsProcessingParameterNumber.Type.Double,default,False,low,high))
        for name,label,default,low,high in [('BUFFER','Буфер вокруг участка, м',500,0,20000),('RESOLUTION','Шаг сетки, м',30,10,1000),
            ('LOOKBACK','Период поиска спутника, дней',60,1,180),('WARMUP','Предыдущих дней для расчёта FWI',60,1,90),('CORRIDOR','Полуширина выборки вдоль ЖД, м',15,1,1000)]:
            self.addParameter(QgsProcessingParameterNumber(name,label,QgsProcessingParameterNumber.Type.Double,default,False,low,high))
        self.addParameter(QgsProcessingParameterFolderDestination('OUTPUT','Папка результатов'))
        self.addOutput(QgsProcessingOutputHtml('REPORT','Отчёт'))
        self.addOutput(QgsProcessingOutputFile('MANIFEST','Параметры и результаты JSON'))
        self.addOutput(QgsProcessingOutputFile('VECTORS','Зоны и воздействие на ЖД, GeoPackage'))
    def processAlgorithm(self,parameters,context,feedback):
        try:
            area=self.parameterAsSource(parameters,'AREA',context)
            if area is None:raise ValueError('Выберите один полигон участка.')
            areas=snapshot(area)
            if len(areas)!=1:raise ValueError('Нужен ровно один полигон участка.')
            layers={name:self.parameterAsSource(parameters,name,context) for name in ('FUELMAP','IGNITION','RAIL')}
            dem=self.parameterAsRasterLayer(parameters,'DEM',context);number=lambda key:self.parameterAsDouble(parameters,key,context)
            model={key:number(name) for name,key,*_ in PARAMETERS};model['fuel_type']=list(FUELS)[self.parameterAsEnum(parameters,'FUEL',context)]
            config=dict(area_wkt=areas[0]['wkt'],area_crs=area.sourceCrs().toWkt(),dem=dem.source() if dem else '',
                scenario_date=self.parameterAsString(parameters,'DATE',context),satellite=self.parameterAsBool(parameters,'SATELLITE',context),
                weather_csv=self.parameterAsFile(parameters,'WEATHER',context),lookback_days=int(number('LOOKBACK')),warmup_days=int(number('WARMUP')),
                buffer=number('BUFFER'),resolution=number('RESOLUTION'),corridor=number('CORRIDOR'),parameters=model,
                directory=str(Path(self.parameterAsString(parameters,'OUTPUT',context))/('wildfire_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))))
            for name,key,crskey in [('FUELMAP','fuel_zones','fuel_crs'),('IGNITION','ignitions','ignition_crs'),('RAIL','rails','rail_crs')]:
                source=layers[name];features=snapshot(source,name=='FUELMAP') if source else []
                config[key]=[f['wkt'] for f in features] if name=='IGNITION' else features
                config[crskey]=source.sourceCrs().toWkt() if source else ''
            result=run_workflow(config,lambda pct,message:(feedback.setProgress(pct),feedback.setProgressText(message)),feedback.isCanceled)
            for warning in result['warnings']:feedback.pushWarning(warning)
            return dict(OUTPUT=str(Path(config['directory'])),REPORT=result['outputs']['report'],MANIFEST=result['outputs']['manifest'],VECTORS=result['outputs']['vectors'])
        except (ValueError,RuntimeError,InterruptedError) as exc:raise QgsProcessingException(str(exc)) from exc
