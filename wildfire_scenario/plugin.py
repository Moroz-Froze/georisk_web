from datetime import datetime
import json
from pathlib import Path
import traceback
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFormLayout, QLineEdit, QFileDialog, QDoubleSpinBox, QCheckBox,
    QTabWidget, QWidget, QProgressBar, QMessageBox, QTextBrowser, QScrollArea, QComboBox)
from qgis.core import (Qgis, QgsApplication, QgsProject, QgsTask, QgsGeometry,
    QgsRectangle, QgsVectorLayer, QgsRasterLayer, QgsFeature, QgsMapLayerProxyModel,
    QgsPalettedRasterRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsPointXY, QgsLineSymbol, QgsFillSymbol)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand
from .provider import WildfireProvider
from .workflow import run_workflow,prepare_inputs
from .terrain import prepare_terrain
from .algorithm import PARAMETERS,snapshot
from .core import FUELS
from qgis.PyQt.QtWidgets import QDateEdit
from qgis.PyQt.QtCore import QDate
from qgis.gui import QgsMapToolEmitPoint


class AreaTool(QgsMapTool):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas, self.callback, self.start = canvas, callback, None
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(242, 157, 55, 90))
        self.band.setStrokeColor(QColor('#e59425'))
        self.band.setWidth(2)

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self.toMapCoordinates(event.pos())

    def canvasMoveEvent(self, event):
        if self.start:
            rect = QgsRectangle(self.start, self.toMapCoordinates(event.pos()))
            self.band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, event):
        if not self.start or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = QgsRectangle(self.start, self.toMapCoordinates(event.pos()))
        self.start = None
        self.band.reset(Qgis.GeometryType.Polygon)
        if rect.width() > 0 and rect.height() > 0:
            self.callback(QgsGeometry.fromRect(rect))

    def deactivate(self):
        self.band.reset(Qgis.GeometryType.Polygon)
        super().deactivate()


class ScenarioTask(QgsTask):
    def __init__(self, config, callback):
        super().__init__('Пожар: сценарий распространения', QgsTask.Flag.CanCancel)
        self.config, self.callback, self.manifest, self.error = config, callback, None, ''

    def run(self):
        try:
            self.manifest = run_workflow(self.config,
                lambda pct, message: self.setProgress(pct), self.isCanceled)
            return True
        except Exception as exc:
            self.error = str(exc)
            self.details = traceback.format_exc()
            return False

    def finished(self, success):
        self.callback(self.manifest if success else None, self.error)


class DataTask(QgsTask):
    statusChanged=pyqtSignal(str)
    def __init__(self,config,callback):
        super().__init__('Пожар: загрузка спутника, рельефа и погоды',QgsTask.Flag.CanCancel)
        self.config,self.callback,self.result,self.error=config,callback,None,''
    def run(self):
        try:
            def notify(pct,message):self.setProgress(min(99,pct*100/60));self.statusChanged.emit(message)
            self.result=prepare_inputs(self.config,notify,self.isCanceled);self.setProgress(100)
            return True
        except Exception as exc:self.error=str(exc);self.details=traceback.format_exc();return False
    def finished(self,success):self.callback(self.result if success else None,self.error)


class WildfireDialog(QDialog):
    def __init__(self,plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin,self.iface,self.task=plugin,plugin.iface,None
        self.loaded_data=None;self.report_path=''
        self.setWindowTitle('Лесные пожары · опасность и распространение');self.resize(880,840)
        layout=QVBoxLayout(self)
        title=QLabel('Лесные пожары · опасность и распространение')
        title.setStyleSheet('font-size:20px;font-weight:600;color:#8c471f;padding:8px 0');layout.addWidget(title)
        lead=QLabel('Рельеф + Sentinel-2 NDVI/NDMI + погода → FWI и сценарий прихода огня.')
        lead.setWordWrap(True);layout.addWidget(lead)
        self.tabs=QTabWidget();layout.addWidget(self.tabs)
        main=QWidget();form=QFormLayout(main);self.tabs.addTab(main,'Участок и данные')
        self.area=self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer);form.addRow('Участок обследования',self.area)
        row=QHBoxLayout()
        button=QPushButton('Выделить прямоугольник на карте');button.clicked.connect(plugin.draw_area);row.addWidget(button)
        button=QPushButton('Взять текущий вид');button.clicked.connect(plugin.area_from_extent);row.addWidget(button);form.addRow(row)
        button=QPushButton('Добавить карту OpenStreetMap');button.clicked.connect(plugin.add_basemap);form.addRow(button)
        self.coordinates=QLineEdit('86.5, 50.0');row=QHBoxLayout();row.addWidget(self.coordinates)
        button=QPushButton('Перейти');button.clicked.connect(self.go_to);row.addWidget(button);form.addRow('Долгота, широта',row)
        self.scenario_date=QDateEdit(QDate.currentDate());self.scenario_date.setCalendarPopup(True);self.scenario_date.setDisplayFormat('yyyy-MM-dd')
        form.addRow('Дата погоды и сценария',self.scenario_date)
        self.terrain_source=QComboBox();self.terrain_source.addItems(['Интернет — Copernicus GLO-30','Своя ЦМР'])
        form.addRow('Рельеф',self.terrain_source)
        self.dem=self.combo(QgsMapLayerProxyModel.Filter.RasterLayer);self.dem.setEnabled(False)
        row=QHBoxLayout();row.addWidget(self.dem);button=QPushButton('Открыть ЦМР…');button.clicked.connect(self.open_dem);row.addWidget(button);form.addRow('Своя модель рельефа',row)
        self.terrain_source.currentIndexChanged.connect(lambda i:self.dem.setEnabled(i==1))
        self.download_button=QPushButton('Загрузить реальные данные: рельеф, спутник и погоду')
        self.download_button.clicked.connect(self.download_data);form.addRow(self.download_button)
        note=QLabel('Регистрация и ключи не требуются. Можно сразу запустить расчёт. Дата снимка и облачность будут показаны после загрузки.')
        note.setWordWrap(True);form.addRow(note)
        self.ignition=self.combo(QgsMapLayerProxyModel.Filter.PointLayer);form.addRow('Условные очаги зажигания',self.ignition)
        button=QPushButton('Указать точку зажигания на карте');button.clicked.connect(plugin.draw_ignition);form.addRow(button)
        self.rail=self.combo(QgsMapLayerProxyModel.Filter.LineLayer);form.addRow('Ось ЖД (необязательно)',self.rail)
        button=QPushButton('Открыть трассу ЖД…');button.clicked.connect(self.open_rail);form.addRow(button)
        self.output=QLineEdit(str(Path(QgsProject.instance().homePath() or str(Path.home()/'Documents'))/'Wildfire_results'))
        row=QHBoxLayout();row.addWidget(self.output);button=QPushButton('Выбрать…');button.clicked.connect(self.choose_output);row.addWidget(button);form.addRow('Папка результатов',row)
        fuel_page=QWidget();fuel_form=QFormLayout(fuel_page);self.tabs.addTab(fuel_page,'Спутник и горючее')
        self.satellite=QCheckBox('Загрузить Sentinel-2 и рассчитать NDVI/NDMI');self.satellite.setChecked(True);fuel_form.addRow(self.satellite)
        self.fuelmap=self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer);fuel_form.addRow('Своя карта горючего',self.fuelmap)
        button=QPushButton('Открыть зоны горючего…');button.clicked.connect(self.open_fuel);fuel_form.addRow(button)
        self.fuel=QComboBox()
        for key,label in list(FUELS.items())[:6]:self.fuel.addItem(label,key)
        fuel_form.addRow('Аналог для растительности SCL=4',self.fuel)
        note=QLabel('NDVI не определяет тип леса, запас горючего или вероятность пожара. Без своей карты '
            'вся видимая растительность SCL=4 получает выбранный сценарный аналог. Вода и снег блокируют '
            'распространение в этой схеме; облака, тени и SCL=5 остаются неизвестными. '
            'Своя карта: поле fuel со значениями C2, C3, D1, O1A, O1B, S1, NF, WA. '
            'При отключённом спутнике карта обязательна.')
        note.setWordWrap(True);fuel_form.addRow(note)
        self.weather_csv=QLineEdit();self.weather_csv.setPlaceholderText('Пусто = автоматическая модельная погода Open-Meteo')
        row=QHBoxLayout();row.addWidget(self.weather_csv);button=QPushButton('CSV…');button.clicked.connect(self.open_weather);row.addWidget(button);fuel_form.addRow('Свои ежедневные метеоданные',row)
        self.inputs={}
        advanced=QWidget();advanced_form=QFormLayout(advanced)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(advanced);self.tabs.addTab(scroll,'Сценарий и FWI')
        for name,key,label,default,low,high in PARAMETERS:
            widget=self.spin(default,low,high);self.inputs[key]=widget;advanced_form.addRow(label,widget)
        for key,label,default,low,high in [('buffer','Буфер вокруг участка, м',500,0,20000),('resolution','Шаг сетки, м',30,10,1000),
            ('lookback_days','Период поиска снимка, дней',60,1,180),('warmup_days','Предыдущих дней для FWI',60,1,90),('corridor','Полуширина выборки вдоль ЖД, м',15,1,1000)]:
            widget=self.spin(default,low,high);self.inputs[key]=widget;advanced_form.addRow(label,widget)
        note=QLabel('Скорости FBP установившиеся, погода постоянна. Система FWI1987 используется отдельно от NDVI. '
            'Без точки зажигания сохраняются карты потенциальной интенсивности, но распространение не рассчитывается. '
            'Начальные коды и характеристики крон — сценарные допущения; замените их обоснованными данными.')
        note.setWordWrap(True);advanced_form.addRow(note)
        help_view=QTextBrowser();help_view.setOpenExternalLinks(True)
        help_view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name('help.html'))));self.tabs.addTab(help_view,'Метод и источники')
        note=QLabel('Сценарная оценка. Не оперативный прогноз пожара: неизвестные участки, перенос головней и тушение требуют отдельного анализа.')
        note.setWordWrap(True);note.setStyleSheet('background:#fff0d6;padding:9px;color:#6b4b25;border-radius:6px');layout.addWidget(note)
        self.state=QLabel('Выделите участок. Для распространения укажите условный очаг.');self.state.setWordWrap(True);layout.addWidget(self.state)
        self.progress=QProgressBar();self.progress.setValue(0);layout.addWidget(self.progress)
        row=QHBoxLayout()
        self.report=QPushButton('Открыть отчёт');self.report.setEnabled(False);self.report.clicked.connect(self.open_report);row.addWidget(self.report)
        self.cancel=QPushButton('Отменить');self.cancel.setEnabled(False);self.cancel.clicked.connect(self.cancel_task);row.addWidget(self.cancel)
        self.run_button=QPushButton('Рассчитать пожарный сценарий');self.run_button.clicked.connect(self.start)
        self.run_button.setStyleSheet('background:#a44d20;color:white;padding:9px 15px;font-weight:bold');row.addWidget(self.run_button);layout.addLayout(row)

    @staticmethod
    def combo(filters):
        widget = QgsMapLayerComboBox()
        widget.setFilters(filters)
        widget.setAllowEmptyLayer(True)
        widget.setLayer(None)
        return widget

    @staticmethod
    def spin(value,minimum,maximum,decimals=3):
        widget = QDoubleSpinBox(); widget.setDecimals(decimals)
        widget.setRange(minimum,maximum); widget.setValue(value)
        return widget

    def choose_output(self):
        folder = QFileDialog.getExistingDirectory(self,'Папка результатов',self.output.text())
        if folder: self.output.setText(folder)

    def open_dem(self):
        path,_ = QFileDialog.getOpenFileName(self,'Открыть ЦМР','','Растр (*.tif *.tiff *.vrt *.img);;Все файлы (*)')
        if path:
            layer = QgsRasterLayer(path,Path(path).stem)
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self.terrain_source.setCurrentIndex(1); self.dem.setLayer(layer)
            else: self.state.setText('Растр не читается. Выберите GeoTIFF с высотами в метрах.')

    def open_rail(self):
        path,_ = QFileDialog.getOpenFileName(self,'Ось проектируемой ЖД','','Вектор (*.gpkg *.geojson *.shp);;Все файлы (*)')
        if path:
            layer = QgsVectorLayer(path,Path(path).stem,'ogr')
            if layer.isValid() and layer.geometryType() == Qgis.GeometryType.Line:
                QgsProject.instance().addMapLayer(layer); self.rail.setLayer(layer)
            else: self.state.setText('Выберите линейный слой оси ЖД. Для GeoPackage с несколькими слоями откройте нужный через QGIS.')

    def go_to(self):
        try:
            lon,lat = [float(x.strip()) for x in self.coordinates.text().split(',')]
            if not -180 <= lon <= 180 or not -80 <= lat <= 80: raise ValueError()
            canvas = self.iface.mapCanvas()
            source = QgsCoordinateReferenceSystem('EPSG:4326')
            tr = QgsCoordinateTransform(source,canvas.mapSettings().destinationCrs(),QgsProject.instance())
            extent = tr.transformBoundingBox(QgsRectangle(lon-.08,lat-.05,lon+.08,lat+.05))
            canvas.setExtent(extent); canvas.refresh()
        except (ValueError, RuntimeError):
            self.state.setText('Введите долготу и широту через запятую, например 86.5, 50.0.')



    def open_fuel(self):
        path,_=QFileDialog.getOpenFileName(self,'Карта горючего','','Вектор (*.gpkg *.geojson *.shp);;Все файлы (*)')
        if path:
            layer=QgsVectorLayer(path,Path(path).stem,'ogr')
            if layer.isValid() and layer.geometryType()==Qgis.GeometryType.Polygon:
                QgsProject.instance().addMapLayer(layer);self.fuelmap.setLayer(layer)
            else:self.state.setText('Нужен полигональный слой с текстовым полем fuel.')

    def open_weather(self):
        path,_=QFileDialog.getOpenFileName(self,'Ежедневная погода в полдень стандартного времени','','CSV (*.csv)')
        if path:self.weather_csv.setText(path)

    def configuration(self,use_cache=True):
        area=self.area.currentLayer()
        if not area:raise ValueError('Выделите участок на карте или выберите один полигон.')
        selected=list(area.getSelectedFeatures() if area.selectedFeatureCount() else area.getFeatures())
        if len(selected)!=1 or not selected[0].hasGeometry() or not selected[0].geometry().isGeosValid():raise ValueError('Нужен один корректный полигон участка.')
        if not area.crs().isValid():raise ValueError('У участка не задана система координат.')
        folder=self.output.text().strip()
        if not folder:raise ValueError('Укажите папку результатов.')
        dem=''
        if self.terrain_source.currentIndex()==1:
            raster=self.dem.currentLayer()
            if not raster:raise ValueError('Выберите свою ЦМР либо Интернет.')
            dem=raster.source()
        fuel,rail,ignition=self.fuelmap.currentLayer(),self.rail.currentLayer(),self.ignition.currentLayer()
        if not self.satellite.isChecked() and fuel is None:raise ValueError('Без спутника требуется своя карта горючего.')
        values={key:widget.value() for key,widget in self.inputs.items()};values['fuel_type']=self.fuel.currentData()
        config=dict(area_wkt=selected[0].geometry().asWkt(),area_crs=area.crs().toWkt(),dem=dem,
            scenario_date=self.scenario_date.date().toString('yyyy-MM-dd'),satellite=self.satellite.isChecked(),
            weather_csv=self.weather_csv.text().strip(),fuel_zones=snapshot(fuel,True) if fuel else [],fuel_crs=fuel.crs().toWkt() if fuel else '',
            rails=snapshot(rail) if rail else [],rail_crs=rail.crs().toWkt() if rail else '',
            ignitions=[f['wkt'] for f in snapshot(ignition)] if ignition else [],ignition_crs=ignition.crs().toWkt() if ignition else '',
            directory=str(Path(folder)/('wildfire_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))),
            buffer=values.pop('buffer'),resolution=values.pop('resolution'),corridor=values.pop('corridor'),
            lookback_days=int(values.pop('lookback_days')),warmup_days=int(values.pop('warmup_days')),parameters=values)
        if use_cache and self.loaded_data and self.loaded_data['key']==self.data_key(config) and Path(self.loaded_data['path']).is_file():
            config['prepared_inputs']=self.loaded_data['path']
        return config

    @staticmethod
    def data_key(config):
        keys=('area_wkt','area_crs','dem','buffer','resolution','scenario_date','satellite','lookback_days','weather_csv','warmup_days','fuel_zones','fuel_crs')
        values={key:config.get(key) for key in keys}
        values['parameters']={key:config['parameters'][key] for key in ('fuel_type','ffmc_initial','dmc_initial','dc_initial')}
        return json.dumps(values,sort_keys=True,ensure_ascii=False)

    def set_busy(self,busy):
        self.tabs.setEnabled(not busy);self.run_button.setEnabled(not busy);self.cancel.setEnabled(busy)
        if busy:self.progress.setValue(0)

    def download_data(self):
        if self.task:return
        try:config=self.configuration(False)
        except ValueError as exc:self.state.setText(str(exc));return
        config['directory']=str(Path(self.output.text().strip())/('fire_data_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')))
        key=self.data_key(config);self.set_busy(True);self.state.setText('Загрузка реального рельефа, Sentinel-2 и погоды…')
        self.task=DataTask(config,lambda result,error:self.data_loaded(key,result,error))
        self.task.progressChanged.connect(lambda pct:self.progress.setValue(int(pct)));self.task.statusChanged.connect(self.state.setText)
        QgsApplication.taskManager().addTask(self.task)

    def data_loaded(self,key,result,error):
        self.task=None;self.set_busy(False)
        if result is None:self.state.setText(error or 'Загрузка прервана.');return
        self.loaded_data=dict(key=key,path=result['outputs']['inputs']);self.progress.setValue(100)
        self.plugin.load_data(result)
        scene=result['satellite_source'];weather=result['latest_weather']
        details=f"Снимок {scene['date']}, без облаков {scene['clear_coverage']:.0%}. " if scene else 'Своя карта горючего. '
        self.state.setText(details+f"FWI={weather['fwi']:.2f}. Укажите очаг для распространения или рассчитайте потенциальную интенсивность.")

    def start(self):
        if self.task:return
        try:config=self.configuration()
        except ValueError as exc:self.state.setText(str(exc));return
        self.set_busy(True);self.report.setEnabled(False);self.state.setText('Расчёт FWI/FBP и сценария…')
        self.task=ScenarioTask(config,self.completed);self.task.progressChanged.connect(lambda pct:self.progress.setValue(int(pct)))
        QgsApplication.taskManager().addTask(self.task)

    def completed(self,result,error):
        self.task=None;self.set_busy(False)
        if result is None:self.state.setText(error or 'Расчёт прерван.');return
        self.report_path=result['outputs']['report'];self.report.setEnabled(True);self.progress.setValue(100)
        self.plugin.load_results(result);s=result['summary']
        text=f"Готово. FWI={s['fwi']:.2f}; максимум скорости {s['max_ros_m_min']:.2f} м/мин. "
        if s['scenario_area_m2'] is None:text+='Очаг не задан: рассчитана потенциальная интенсивность.'
        else:text+=f"Достигнуто {s['scenario_area_m2']/10000:.2f} га."+(' Сценарий обрезан неизвестной областью/границей.' if s['truncated'] else '')
        self.state.setText(text)

    def cancel_task(self):
        if self.task:self.task.cancel();self.state.setText('Отмена операции…')
    def open_report(self):
        if self.report_path:QDesktopServices.openUrl(QUrl.fromLocalFile(self.report_path))
    def closeEvent(self,event):event.ignore();self.hide()


class WildfirePlugin:
    def __init__(self,iface):
        self.iface,self.provider,self.dialog,self.area_tool = iface,None,None,None

    def initProcessing(self):
        if self.provider is None:
            self.provider = WildfireProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self.action = QAction(QIcon(str(Path(__file__).with_name('icon.svg'))),
                              'Пожары · опасность и распространение',self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu('Лесные пожары — опасность и распространение',self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.dialog and self.dialog.task: self.dialog.task.cancel()
        if self.area_tool:
            self.iface.mapCanvas().unsetMapTool(self.area_tool)
        if hasattr(self,'ignition_tool'):
            self.iface.mapCanvas().unsetMapTool(self.ignition_tool)
        if hasattr(self,'action'):
            self.iface.removePluginMenu('Лесные пожары — опасность и распространение',self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider); self.provider = None
        if self.dialog: self.dialog.hide()

    def show(self):
        if self.dialog is None: self.dialog = WildfireDialog(self)
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()

    @staticmethod
    def style_area(layer):
        layer.renderer().setSymbol(QgsFillSymbol.createSimple({'color':'0,0,0,0','outline_color':'230,145,30','outline_width':'0.8'}))
        layer.triggerRepaint()

    def add_area(self,geometry):
        canvas = self.iface.mapCanvas()
        layer = QgsVectorLayer('Polygon','Участок обследования','memory')
        layer.setCrs(canvas.mapSettings().destinationCrs())
        feature = QgsFeature(); feature.setGeometry(geometry)
        layer.dataProvider().addFeatures([feature]); layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.style_area(layer)
        self.dialog.area.setLayer(layer)
        if self.area_tool: canvas.unsetMapTool(self.area_tool)
        self.show(); self.dialog.state.setText('Участок выделен. Можно запустить автоматический расчёт.')

    def draw_area(self):
        self.dialog.hide()
        self.area_tool = AreaTool(self.iface.mapCanvas(),self.add_area)
        self.iface.mapCanvas().setMapTool(self.area_tool)
        self.iface.messageBar().pushMessage('Пожары','Зажмите левую кнопку и растяните прямоугольник участка.',level=Qgis.MessageLevel.Info,duration=8)

    def area_from_extent(self):
        self.add_area(QgsGeometry.fromRect(self.iface.mapCanvas().extent()))

    def add_basemap(self):
        layer = QgsRasterLayer('type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19',
                               'OpenStreetMap © OpenStreetMap contributors','wms')
        layer.setAttribution('© OpenStreetMap contributors')
        layer.setAttributionUrl('https://www.openstreetmap.org/copyright')
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self.dialog.dem.setLayer(None)
            self.dialog.go_to()

    def draw_ignition(self):
        self.dialog.hide();self.ignition_tool=QgsMapToolEmitPoint(self.iface.mapCanvas())
        self.ignition_tool.canvasClicked.connect(self.add_ignition)
        self.iface.mapCanvas().setMapTool(self.ignition_tool)
        self.iface.messageBar().pushMessage('Пожары','Укажите точку условного зажигания левой кнопкой.',level=Qgis.MessageLevel.Info,duration=8)

    def add_ignition(self,point,button=Qt.MouseButton.LeftButton):
        if button!=Qt.MouseButton.LeftButton:return
        layer=QgsVectorLayer('Point','Условный очаг пожара','memory')
        layer.setCrs(self.iface.mapCanvas().mapSettings().destinationCrs())
        feature=QgsFeature();feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        layer.dataProvider().addFeatures([feature]);layer.updateExtents()
        from qgis.core import QgsMarkerSymbol
        layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({'name':'star','color':'240,60,20','size':'5','outline_color':'80,20,10'}))
        QgsProject.instance().addMapLayer(layer);self.dialog.ignition.setLayer(layer)
        if hasattr(self,'ignition_tool'):self.iface.mapCanvas().unsetMapTool(self.ignition_tool)
        self.show();self.dialog.state.setText('Условный очаг задан. Можно запустить сценарий распространения.')

    @staticmethod
    def raster_layer(path,name,key,duration=180):
        from qgis.core import QgsSingleBandPseudoColorRenderer,QgsRasterShader,QgsColorRampShader
        layer=QgsRasterLayer(path,name)
        if not layer.isValid():return layer
        if key in ('intensity_class','reached'):
            labels=([(0,QColor(0,0,0,0),'Не достигнуто в сценарии'),(1,QColor('#d94b25'),'Достигнутая область')]
                if key=='reached' else [(0,QColor('#75b5cd'),'Явно задано негорючее / вода'),(1,QColor('#edcf6b'),'Ниже нижнего порога интенсивности'),
                    (2,QColor('#eb8b35'),'Между порогами интенсивности'),(3,QColor('#c63835'),'Выше верхнего порога'),(4,QColor('#8562ad'),'Уклон вне применимости')])
            layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(),1,[QgsPalettedRasterRenderer.Class(v,c,label) for v,c,label in labels]))
            layer.renderer().setOpacity(.7)
        elif key in ('ndvi','ndmi','arrival_min'):
            items=([(-1,'#e4d2a8'),(0,'#eff0cb'),(.4,'#62b56e'),(1,'#175c37')] if key=='ndvi' else
                   [(-1,'#a36b36'),(0,'#e8dbb6'),(.4,'#72bdac'),(1,'#176d86')] if key=='ndmi' else
                   [(0,'#c02c26'),(duration/2,'#f4a13c'),(duration,'#f5e28a')])
            ramp=QgsColorRampShader();ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
            ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(v,QColor(c),str(round(v,3))) for v,c in items])
            shader=QgsRasterShader();shader.setRasterShaderFunction(ramp)
            layer.setRenderer(QgsSingleBandPseudoColorRenderer(layer.dataProvider(),1,shader))
        return layer

    def load_data(self,meta):
        project=QgsProject.instance();group=project.layerTreeRoot().insertGroup(0,'Пожары · исходные данные')
        for key,name,visible in [('terrain','Рельеф · Copernicus / своя ЦМР',True),('ndmi','NDMI · водное состояние растительности',False),('ndvi','NDVI · растительность',True)]:
            if key not in meta['outputs']:continue
            layer=self.raster_layer(meta['outputs'][key],name,key)
            if layer.isValid():
                project.addMapLayer(layer,False);node=group.insertLayer(0,layer);node.setItemVisibilityChecked(visible)

    def load_results(self,result):
        project=QgsProject.instance();group=project.layerTreeRoot().insertGroup(0,'Пожарный сценарий · '+datetime.now().strftime('%H:%M:%S'))
        entries=[('terrain','Рельеф участка',True),('ndvi','NDVI · дата снимка',False),('ndmi','NDMI · дата снимка',False),
            ('head_m_min','Потенциальная скорость головы, м/мин',False),('intensity_kw_m','Потенциальная интенсивность головы, кВт/м',False),
            ('intensity_class','Потенциальная интенсивность · классы',result['status']=='potential_only'),('arrival_min','Сценарий · время прихода огня, мин',True)]
        for key,name,visible in entries:
            if key not in result['outputs']:continue
            layer=self.raster_layer(result['outputs'][key],name,key,result['parameters']['duration_min'])
            if layer.isValid():project.addMapLayer(layer,False);node=group.insertLayer(0,layer);node.setItemVisibilityChecked(visible)
        if result['railway']:
            layer=QgsVectorLayer(result['outputs']['vectors']+'|layername=railway_exposure','ЖД · сценарное воздействие огня','ogr')
            if layer.isValid():
                layer.renderer().setSymbol(QgsLineSymbol.createSimple({'line_color':'15,50,72','line_width':'1.1'}))
                project.addMapLayer(layer,False);group.insertLayer(0,layer)

