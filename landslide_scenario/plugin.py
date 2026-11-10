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
from .provider import LandslideProvider
from .workflow import run_workflow
from .terrain import prepare_terrain
from .algorithm import PARAMETERS,snapshot


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
        super().__init__('Оползни: расчёт устойчивости склонов', QgsTask.Flag.CanCancel)
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


class TerrainTask(QgsTask):
    statusChanged = pyqtSignal(str)

    def __init__(self, config, callback):
        super().__init__('Оползни: загрузка реального рельефа Copernicus', QgsTask.Flag.CanCancel)
        self.config, self.callback, self.result, self.error = config, callback, None, ''

    def run(self):
        try:
            def notify(pct, message):
                self.setProgress(min(99, pct*4))
                self.statusChanged.emit(message)
            if self.isCanceled():
                raise InterruptedError('Загрузка отменена.')
            result = prepare_terrain(self.config['area_wkt'], self.config['area_crs'],
                self.config['directory'], buffer_m=self.config['buffer'],
                resolution=self.config['resolution'], notify=notify, cancelled=self.isCanceled)
            if self.isCanceled():
                raise InterruptedError('Загрузка отменена.')
            # Keep only plain metadata for the main-thread completion callback.
            self.result = {key: result[key] for key in ('terrain_path', 'source', 'crs', 'coverage')}
            self.setProgress(100)
            return True
        except Exception as exc:
            self.error = str(exc)
            self.details = traceback.format_exc()
            return False

    def finished(self, success):
        self.callback(self.result if success else None, self.error)


class LandslideDialog(QDialog):
    def __init__(self,plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin,self.iface,self.task=plugin,plugin.iface,None
        self.downloaded_terrain=None;self.report_path=''
        self.setWindowTitle('Оползни · устойчивость склонов')
        self.resize(850,800)
        layout=QVBoxLayout(self)
        title=QLabel('Оползневая опасность · устойчивость склонов')
        title.setStyleSheet('font-size:20px;font-weight:600;color:#245b49;padding:8px 0')
        layout.addWidget(title)
        lead=QLabel('Выделите участок → загрузите реальный рельеф → задайте грунт и обводнение.')
        lead.setWordWrap(True);layout.addWidget(lead)
        self.tabs=QTabWidget();layout.addWidget(self.tabs)
        main=QWidget();form=QFormLayout(main);self.tabs.addTab(main,'Участок и рельеф')
        self.area=self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer)
        form.addRow('Участок обследования',self.area)
        row=QHBoxLayout()
        draw=QPushButton('Выделить прямоугольник на карте');draw.clicked.connect(plugin.draw_area);row.addWidget(draw)
        extent=QPushButton('Взять текущий вид карты');extent.clicked.connect(plugin.area_from_extent);row.addWidget(extent)
        form.addRow(row)
        base=QPushButton('Добавить карту OpenStreetMap');base.clicked.connect(plugin.add_basemap);form.addRow(base)
        self.coordinates=QLineEdit('86.5, 50.0');row=QHBoxLayout();row.addWidget(self.coordinates)
        go=QPushButton('Перейти');go.clicked.connect(self.go_to);row.addWidget(go)
        form.addRow('Долгота, широта',row)
        self.terrain_source=QComboBox();self.terrain_source.addItems(['Интернет — Copernicus GLO-30','Свой файл рельефа'])
        form.addRow('Источник рельефа',self.terrain_source)
        self.download_button=QPushButton('Загрузить реальный рельеф выделенного участка')
        self.download_button.clicked.connect(self.download_terrain);form.addRow(self.download_button)
        self.dem=self.combo(QgsMapLayerProxyModel.Filter.RasterLayer);self.dem.setEnabled(False)
        row=QHBoxLayout();row.addWidget(self.dem)
        load=QPushButton('Открыть ЦМР…');load.clicked.connect(self.open_dem);row.addWidget(load)
        form.addRow('Своя ЦМР',row)
        self.terrain_source.currentIndexChanged.connect(lambda index:self.dem.setEnabled(index==1))
        note=QLabel('Реальный рельеф загружается без ключа и регистрации. Можно сразу запустить расчёт.\n'
                    'Для анализа откосов ЖД нужна своя детальная модель рельефа.')
        note.setWordWrap(True);form.addRow(note)
        self.rail=self.combo(QgsMapLayerProxyModel.Filter.LineLayer)
        form.addRow('Ось ЖД (необязательно)',self.rail)
        load=QPushButton('Открыть трассу ЖД…');load.clicked.connect(self.open_rail);form.addRow(load)
        self.output=QLineEdit(str(Path(QgsProject.instance().homePath() or str(Path.home()/'Documents'))/'Landslide_results'))
        row=QHBoxLayout();row.addWidget(self.output)
        choose=QPushButton('Выбрать…');choose.clicked.connect(self.choose_output);row.addWidget(choose)
        form.addRow('Папка результатов',row)
        geology=QWidget();geo=QFormLayout(geology);self.tabs.addTab(geology,'Грунт и вода')
        self.soil=self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer)
        geo.addRow('Зоны грунтов изысканий',self.soil)
        load=QPushButton('Открыть полигональные зоны грунтов…');load.clicked.connect(self.open_soil);geo.addRow(load)
        note=QLabel('Без слоя зон применяются единые сценарные значения ниже. '
                    'Они не определяются из ЦМР и не являются данными изысканий.\n'
                    'Поля зон: c_kpa, root_kpa, phi_deg, gamma_knm3, depth_m, m_ratio. '
                    'Зоны должны покрывать все анализируемые склоны без перекрытий.')
        note.setWordWrap(True);geo.addRow(note)
        advanced=QWidget();advanced_form=QFormLayout(advanced);self.tabs.addTab(advanced,'Сетка и границы модели')
        self.inputs={}
        for name,key,label,default,low,high in PARAMETERS:
            field=self.spin(default,low,high);self.inputs[key]=field
            (geo if key in ('c_kpa','root_kpa','phi_deg','gamma_knm3','depth_m','m_ratio') else advanced_form).addRow(label,field)
        for key,label,default,low,high in [('buffer','Буфер анализа вокруг участка, м',500,0,20000),
            ('resolution','Шаг расчётной сетки, м',30,1,1000),('corridor','Полуширина выборки FS вдоль ЖД, м',15,1,1000)]:
            field=self.spin(default,low,high);self.inputs[key]=field;advanced_form.addRow(label,field)
        note=QLabel('m — доля вертикальной высоты воды в слое (0 = нет напора; 1 = вода до поверхности). '
                    'Это не влажность грунта и не осадки. Глубина слоя измеряется вертикально. '
                    'Всегда рассчитываются три состояния: m=0, заданное m, m=1 при одинаковом удельном весе грунта.')
        note.setWordWrap(True);geo.addRow(note)
        self.soil.layerChanged.connect(self.soil_selected)
        note=QLabel('Порог FS=1 — граница равновесия в модели. Порог отображения запаса задаёте вы. '
                    'Он не является нормативом для ЖД. Склоны круче верхнего предела отмечаются как '
                    'неоценённые, а пологие — как находящиеся ниже порога анализа.')
        note.setWordWrap(True);advanced_form.addRow(note)
        help_view=QTextBrowser();help_view.setOpenExternalLinks(True)
        help_view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name('help.html'))))
        self.tabs.addTab(help_view,'Метод и источники')
        limit=QLabel('Предварительная оценка зон зарождения неглубоких оползней. '
                     'Не моделирует дальность движения и не заменяет изыскания для строительства ЖД.')
        limit.setWordWrap(True);limit.setStyleSheet('background:#fff1d9;color:#654b1d;padding:10px;border-radius:6px')
        layout.addWidget(limit)
        self.state=QLabel('Выделите участок и проверьте сценарные свойства грунта.');self.state.setWordWrap(True);layout.addWidget(self.state)
        self.progress=QProgressBar();self.progress.setValue(0);layout.addWidget(self.progress)
        row=QHBoxLayout()
        self.report=QPushButton('Открыть отчёт');self.report.setEnabled(False);self.report.clicked.connect(self.open_report);row.addWidget(self.report)
        self.cancel=QPushButton('Отменить');self.cancel.setEnabled(False);self.cancel.clicked.connect(self.cancel_task);row.addWidget(self.cancel)
        self.run_button=QPushButton('Рассчитать устойчивость')
        self.run_button.setStyleSheet('background:#246b55;color:white;padding:9px 16px;font-weight:bold')
        self.run_button.clicked.connect(self.start);row.addWidget(self.run_button);layout.addLayout(row)

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


    def terrain_request(self):
        area = self.area.currentLayer()
        if not area: raise ValueError('Выделите участок на карте или выберите один полигон.')
        areas = list(area.getSelectedFeatures() if area.selectedFeatureCount() else area.getFeatures())
        if len(areas) != 1 or not areas[0].hasGeometry() or not areas[0].geometry().isGeosValid():
            raise ValueError('Участок должен содержать один корректный полигон. В многополигонном слое выберите один объект.')
        folder = self.output.text().strip()
        if not folder: raise ValueError('Укажите папку результатов.')
        if not area.crs().isValid(): raise ValueError('У выделенного участка не задана система координат.')
        return dict(area_wkt=areas[0].geometry().asWkt(), area_crs=area.crs().toWkt(),
                    directory=str(Path(folder)/('terrain_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))),
                    buffer=self.inputs['buffer'].value(), resolution=self.inputs['resolution'].value())

    @staticmethod
    def terrain_key(config):
        return tuple(config[key] for key in ('area_wkt', 'area_crs', 'buffer', 'resolution'))

    def set_busy(self, busy):
        self.tabs.setEnabled(not busy); self.run_button.setEnabled(not busy)
        self.cancel.setEnabled(busy)
        if busy: self.progress.setValue(0)

    def download_terrain(self):
        if self.task is not None: return
        try: config = self.terrain_request()
        except ValueError as exc:
            self.state.setText(str(exc)); return
        self.terrain_source.setCurrentIndex(0)
        self.set_busy(True)
        self.state.setText('Загрузка реального рельефа Copernicus для выделенного участка…')
        key = self.terrain_key(config)
        self.task = TerrainTask(config, lambda result, error: self.terrain_downloaded(key, result, error))
        self.task.progressChanged.connect(lambda value: self.progress.setValue(int(value)))
        self.task.statusChanged.connect(self.state.setText)
        QgsApplication.taskManager().addTask(self.task)

    def terrain_downloaded(self, key, result, error):
        self.task = None; self.set_busy(False)
        if result is None:
            self.state.setText(error or 'Загрузка прервана.'); return
        layer = QgsRasterLayer(result['terrain_path'], 'Реальный рельеф · Copernicus GLO-30')
        if not layer.isValid():
            self.state.setText('Рельеф получен, но QGIS не смог открыть GeoTIFF.'); return
        layer.setAttribution(result['source'].get('credit', 'Copernicus GLO-30'))
        layer.setAttributionUrl('https://registry.opendata.aws/copernicus-dem/')
        project = QgsProject.instance()
        project.addMapLayer(layer, False)
        # Place terrain beneath the highlighted area and above map backgrounds.
        root = project.layerTreeRoot()
        area = self.area.currentLayer()
        area_node = root.findLayer(area.id()) if area else None
        parent = area_node.parent() if area_node else root
        index = parent.children().index(area_node)+1 if area_node else 0
        parent.insertLayer(index, layer)
        self.dem.setLayer(layer)
        self.downloaded_terrain = dict(key=key, path=result['terrain_path'])
        canvas = self.iface.mapCanvas()
        tr = QgsCoordinateTransform(layer.crs(),canvas.mapSettings().destinationCrs(),project)
        canvas.setExtent(tr.transformBoundingBox(layer.extent())); canvas.refresh()
        self.progress.setValue(100)
        self.state.setText('Реальный рельеф загружен и добавлен на карту. Можно нажать «Рассчитать участок».')


    def soil_selected(self,layer):
        for key in ('c_kpa','root_kpa','phi_deg','gamma_knm3','depth_m','m_ratio'):
            self.inputs[key].setEnabled(layer is None)

    def open_soil(self):
        path,_=QFileDialog.getOpenFileName(self,'Зоны грунтов','','Вектор (*.gpkg *.geojson *.shp);;Все файлы (*)')
        if path:
            layer=QgsVectorLayer(path,Path(path).stem,'ogr')
            if layer.isValid() and layer.geometryType()==Qgis.GeometryType.Polygon:
                QgsProject.instance().addMapLayer(layer);self.soil.setLayer(layer)
            else:self.state.setText('Нужен полигональный слой грунтов с указанными полями.')

    def configuration(self):
        terrain=self.terrain_request();dem_path=''
        if self.terrain_source.currentIndex()==1:
            dem=self.dem.currentLayer()
            if not dem:raise ValueError('Выберите свою ЦМР или переключите источник на Интернет.')
            dem_path=dem.source()
        elif (self.downloaded_terrain and self.downloaded_terrain['key']==self.terrain_key(terrain)
              and Path(self.downloaded_terrain['path']).is_file()):dem_path=self.downloaded_terrain['path']
        values={key:widget.value() for key,widget in self.inputs.items()}
        soil,rail=self.soil.currentLayer(),self.rail.currentLayer()
        return dict(area_wkt=terrain['area_wkt'],area_crs=terrain['area_crs'],dem=dem_path,
            directory=str(Path(self.output.text().strip())/('landslide_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))),
            buffer=values.pop('buffer'),resolution=values.pop('resolution'),corridor=values.pop('corridor'),parameters=values,
            soil_zones=snapshot(soil,True) if soil else [],soil_crs=soil.crs().toWkt() if soil else '',
            rails=snapshot(rail) if rail else [],rail_crs=rail.crs().toWkt() if rail else '')

    def start(self):
        if self.task is not None:return
        try:config=self.configuration()
        except ValueError as exc:self.state.setText(str(exc));return
        self.set_busy(True);self.report.setEnabled(False)
        self.state.setText('Получение рельефа и расчёт трёх состояний обводнения…')
        self.task=ScenarioTask(config,self.completed)
        self.task.progressChanged.connect(lambda value:self.progress.setValue(int(value)))
        QgsApplication.taskManager().addTask(self.task)

    def cancel_task(self):
        if self.task:self.task.cancel();self.state.setText('Отмена операции…')

    def completed(self,manifest,error):
        self.set_busy(False);self.task=None
        if manifest is None:self.state.setText(error or 'Расчёт прерван.');return
        self.progress.setValue(100);self.report_path=manifest['outputs']['report'];self.report.setEnabled(True)
        self.plugin.load_results(manifest)
        if manifest['status']!='completed':
            self.state.setText('Нет склонов в заданном диапазоне анализа. Проверьте участок и границы модели; это не заключение о безопасности.')
        else:
            s=manifest['summary']
            self.state.setText(f"Готово. Минимальный FS: {s['min_fs']:.3f}. Площадь FS < 1 в выделении: {s['target_unstable_area_m2']:.0f} м². Смотрите карты и отчёт.")

    def open_report(self):
        if self.report_path:QDesktopServices.openUrl(QUrl.fromLocalFile(self.report_path))

    def closeEvent(self,event):event.ignore();self.hide()


class LandslidePlugin:
    def __init__(self,iface):
        self.iface,self.provider,self.dialog,self.area_tool = iface,None,None,None

    def initProcessing(self):
        if self.provider is None:
            self.provider = LandslideProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self.action = QAction(QIcon(str(Path(__file__).with_name('icon.svg'))),
                              'Оползни · устойчивость склонов',self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu('Оползни — устойчивость склонов',self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.dialog and self.dialog.task: self.dialog.task.cancel()
        if self.area_tool:
            self.iface.mapCanvas().unsetMapTool(self.area_tool)
        if hasattr(self,'action'):
            self.iface.removePluginMenu('Оползни — устойчивость склонов',self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider); self.provider = None
        if self.dialog: self.dialog.hide()

    def show(self):
        if self.dialog is None: self.dialog = LandslideDialog(self)
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
        self.iface.messageBar().pushMessage('Оползни','Зажмите левую кнопку и растяните прямоугольник участка.',level=Qgis.MessageLevel.Info,duration=8)

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

    def load_results(self,manifest):
        from qgis.core import QgsSingleBandPseudoColorRenderer,QgsRasterShader,QgsColorRampShader
        project=QgsProject.instance()
        group=project.layerTreeRoot().insertGroup(0,'Оползни · '+datetime.now().strftime('%H:%M:%S'))
        out=manifest['outputs'];threshold=manifest['parameters']['reserve_threshold']
        entries=[('terrain','Рельеф участка',True),('slope_deg','Уклон, градусы',False),
            ('fs_dry','FS · без напора воды (m=0)',False),('fs_saturated','FS · полное обводнение (m=1)',False),
            ('critical_m','Критическое обводнение m при FS=1',False),
            ('fs_selected','FS · заданное обводнение',False),('stability_class','Устойчивость · заданное обводнение',True)]
        for key,name,visible in entries:
            layer=QgsRasterLayer(out[key],name)
            if not layer.isValid():continue
            if key=='stability_class':
                items=[(0,QColor('#b5c5c3'),'Пологий: ниже порога анализа'),(1,QColor('#c73240'),'FS < 1 · неустойчив в сценарии'),
                    (2,QColor('#f2a13b'),f'1 ≤ FS < {threshold:g} · малый запас'),(3,QColor('#4b9b6c'),f'FS ≥ {threshold:g} · больший запас'),
                    (4,QColor('#8460a5'),'Крутой: вне применимости модели')]
                layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(),1,
                    [QgsPalettedRasterRenderer.Class(value,color,label) for value,color,label in items]))
                layer.renderer().setOpacity(.72)
            elif key.startswith('fs_'):
                ramp=QgsColorRampShader();ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
                ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(value,QColor(color),label) for value,color,label in
                    [(0,'#bf2639','0'),(1,'#ed8b36','1'),(threshold,'#73ac72',str(threshold)),(max(3,threshold+1),'#2a7294','Больший запас')]])
                shader=QgsRasterShader();shader.setRasterShaderFunction(ramp)
                layer.setRenderer(QgsSingleBandPseudoColorRenderer(layer.dataProvider(),1,shader))
            if key=='terrain':
                layer.setAttribution(manifest['source'].get('credit',manifest['source']['kind']))
            project.addMapLayer(layer,False);node=group.insertLayer(0,layer)
            node.setItemVisibilityChecked(visible)
        if manifest.get('railway'):
            layer=QgsVectorLayer(out['vectors']+'|layername=railway_screening','ЖД · пересечение с зонами срыва','ogr')
            if layer.isValid():
                layer.renderer().setSymbol(QgsLineSymbol.createSimple({'line_color':'15,50,72','line_width':'1.1'}))
                project.addMapLayer(layer,False);group.insertLayer(0,layer)

