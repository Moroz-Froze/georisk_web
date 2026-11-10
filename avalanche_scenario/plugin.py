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
from .provider import AvalancheProvider
from .workflow import run_workflow, create_demo
from .terrain import prepare_terrain


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
        super().__init__('Лавина: расчёт для железной дороги', QgsTask.Flag.CanCancel)
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
        super().__init__('Лавина: загрузка реального рельефа Copernicus', QgsTask.Flag.CanCancel)
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


class ScenarioDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin, self.iface, self.task = plugin, plugin.iface, None
        self.downloaded_terrain = None
        self.setWindowTitle('Лавина · участок железной дороги')
        self.resize(790, 770)
        layout = QVBoxLayout(self)
        title = QLabel('Предварительная оценка лавинного воздействия')
        title.setStyleSheet('font-size:20px;font-weight:600;color:#17465b;padding:8px 0')
        layout.addWidget(title)
        lead = QLabel('Выделите участок → получите рельеф и кандидатные очаги → проверьте трассу ЖД.')
        lead.setWordWrap(True)
        layout.addWidget(lead)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        main = QWidget(); form = QFormLayout(main)
        self.tabs.addTab(main, 'Участок и данные')
        self.area = self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer)
        form.addRow('Участок обследования', self.area)
        row = QHBoxLayout()
        draw = QPushButton('Выделить прямоугольник на карте')
        draw.clicked.connect(plugin.draw_area)
        row.addWidget(draw)
        extent = QPushButton('Взять текущий вид карты')
        extent.clicked.connect(plugin.area_from_extent)
        row.addWidget(extent)
        form.addRow(row)
        row = QHBoxLayout()
        base = QPushButton('Добавить карту OpenStreetMap')
        base.clicked.connect(plugin.add_basemap)
        row.addWidget(base)
        form.addRow(row)
        self.coordinates = QLineEdit('86.5, 50.0')
        self.coordinates.setPlaceholderText('долгота, широта, например 86.5, 50.0')
        row = QHBoxLayout(); row.addWidget(self.coordinates)
        go = QPushButton('Перейти'); go.clicked.connect(self.go_to); row.addWidget(go)
        form.addRow('Координаты для навигации', row)
        self.terrain_source = QComboBox()
        self.terrain_source.addItems(['Интернет — Copernicus GLO-30', 'Свой файл рельефа'])
        form.addRow('Источник реального рельефа', self.terrain_source)
        self.download_button = QPushButton('Загрузить реальный рельеф выделенного участка')
        self.download_button.clicked.connect(self.download_terrain)
        form.addRow(self.download_button)
        self.dem = self.combo(QgsMapLayerProxyModel.Filter.RasterLayer)
        row = QHBoxLayout(); row.addWidget(self.dem)
        load = QPushButton('Открыть ЦМР…'); load.clicked.connect(self.open_dem); row.addWidget(load)
        form.addRow('Локальная ЦМР (необязательно)', row)
        self.dem.setEnabled(False)
        self.terrain_source.currentIndexChanged.connect(lambda index: self.dem.setEnabled(index == 1))
        note = QLabel('Нужен интернет, ключ и регистрация не требуются. Реальный рельеф появится на карте.\n'
                      'Можно сразу нажать «Рассчитать участок» — рельеф загрузится автоматически.')
        note.setWordWrap(True); form.addRow(note)
        self.rail = self.combo(QgsMapLayerProxyModel.Filter.LineLayer)
        form.addRow('Слой оси проектируемой ЖД', self.rail)
        row = QHBoxLayout()
        load_rail = QPushButton('Открыть трассу ЖД…'); load_rail.clicked.connect(self.open_rail)
        row.addWidget(load_rail); form.addRow(row)
        self.release = self.combo(QgsMapLayerProxyModel.Filter.PolygonLayer)
        form.addRow('Известные очаги (необязательно)', self.release)
        note = QLabel('Без известных очагов используется поиск склонов 30–50°. '
                      'Поиск включает буфер: лавина может прийти с соседней горы.')
        note.setWordWrap(True); form.addRow(note)
        self.output = QLineEdit(str(Path(QgsProject.instance().homePath() or str(Path.home()/'Documents'))/'Avalanche_results'))
        row = QHBoxLayout(); row.addWidget(self.output)
        choose = QPushButton('Выбрать…'); choose.clicked.connect(self.choose_output); row.addWidget(choose)
        form.addRow('Папка результатов', row)
        self.inputs = {}
        advanced = QWidget(); advanced_form = QFormLayout(advanced)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(advanced)
        self.tabs.addTab(scroll, 'Рельеф и сценарий')
        specifications = [
            ('buffer','Буфер поиска вокруг участка, м',3000,0,20000),
            ('resolution','Шаг расчётной сетки, м',30,1,1000),
            ('min_slope','Уклон очага от, °',30,1,80),
            ('max_slope','Уклон очага до, °',50,2,89),
            ('min_relief','Минимальный перепад высот, м',100,0,5000),
            ('min_area','Минимальная связная площадь очага, м²',3600,1,1e8),
            ('mu','Сухое трение mu',.25,0,2),
            ('xi','Коэффициент xi, м/с²',1000,1,10000),
            ('flow_depth','Заданная высота потока, м',1,.01,20),
            ('release_depth','Толщина отрыва (для объёма), м',1,.01,20),
            ('density','Плотность снега, кг/м³',300,1,917),
            ('duration','Время моделирования, с',240,1,3600),
            ('time_step','Максимальный шаг по времени, с',.1,.001,1),
            ('pressure_coefficient','Коэффициент давления Cp',1,.01,10),
            ('low_pressure','Нижний порог интенсивности, кПа',3,.01,1000),
            ('high_pressure','Верхний порог интенсивности, кПа',30,.02,10000)]
        info = QLabel('Значения снега и трения ниже — допущения сценария. Они не загружаются из ЦМР. '
                      'Изменение шага сетки не улучшает исходные данные.')
        info.setWordWrap(True); advanced_form.addRow(info)
        for key,label,default,minimum,maximum in specifications:
            field = self.spin(default,minimum,maximum)
            self.inputs[key] = field; advanced_form.addRow(label,field)
        risk = QWidget(); risk_form = QFormLayout(risk)
        self.tabs.addTab(risk, 'Риск для ЖД')
        info = QLabel('Без данных о частоте и повреждаемости рассчитываются карты воздействия и '
                      'затронутая длина трассы. Денежный риск требует обоснованных исходных данных. '
                      'Частота относится ко всему сценарию, а не к каждой кандидатной ячейке.')
        info.setWordWrap(True); risk_form.addRow(info)
        self.risk = QCheckBox('Рассчитать ожидаемый годовой ущерб')
        risk_form.addRow(self.risk)
        self.frequency = self.spin(.01,0,100,5)
        self.cost = self.spin(0,0,1e10,2)
        self.vulnerability = self.spin(.5,0,1,3)
        self.corridor = self.spin(15,1,1000)
        risk_form.addRow('Частота сценария, событий/год',self.frequency)
        risk_form.addRow('Восстановление пути, руб./м',self.cost)
        risk_form.addRow('Доля ущерба (уязвимость), 0–1',self.vulnerability)
        risk_form.addRow('Полуширина коридора давления, м',self.corridor)
        for w in (self.frequency,self.cost,self.vulnerability):
            w.setEnabled(False); self.risk.toggled.connect(w.setEnabled)
        formula = QLabel('Ущерб за событие = затронутая длина × руб./м × уязвимость.\n'
                         'Ожидаемый ущерб за год = частота × ущерб за событие.\n'
                         'Простои поездов и человеческие потери не включены.')
        formula.setWordWrap(True); risk_form.addRow(formula)
        help_view = QTextBrowser()
        help_view.setOpenExternalLinks(True)
        help_view.setSource(QUrl.fromLocalFile(str(Path(__file__).with_name('help.html'))))
        self.tabs.addTab(help_view, 'Метод и ограничения')
        limit = QLabel('Предварительный анализ для планирования изысканий. '
                       'Не заменяет инженерные изыскания и обоснование защиты железной дороги.')
        limit.setWordWrap(True)
        limit.setStyleSheet('background:#fff1d9;color:#654b1d;padding:10px;border-radius:6px')
        layout.addWidget(limit)
        self.state = QLabel('Готов к выбору участка.')
        self.state.setWordWrap(True); layout.addWidget(self.state)
        self.progress = QProgressBar(); self.progress.setValue(0); layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.report = QPushButton('Открыть отчёт'); self.report.setEnabled(False)
        self.report.clicked.connect(self.open_report); row.addWidget(self.report)
        self.cancel = QPushButton('Отменить расчёт'); self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_task); row.addWidget(self.cancel)
        self.run_button = QPushButton('Рассчитать участок')
        self.run_button.setStyleSheet('background:#146579;color:white;padding:9px 16px;font-weight:bold')
        self.run_button.clicked.connect(self.start); row.addWidget(self.run_button)
        layout.addLayout(row)
        self.report_path = ''

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

    def demo(self):
        try:
            folder = Path(self.output.text())/('DEMO_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
            config = create_demo(folder)
            project = QgsProject.instance()
            dem = QgsRasterLayer(config['dem'],'УЧЕБНЫЙ рельеф (синтетический)')
            area = QgsVectorLayer(config['inputs']+'|layername=study_area','УЧЕБНЫЙ участок','ogr')
            rail = QgsVectorLayer(config['inputs']+'|layername=railway','УЧЕБНАЯ ось ЖД','ogr')
            for layer in (dem,area,rail): project.addMapLayer(layer)
            self.area.setLayer(area); self.dem.setLayer(dem); self.rail.setLayer(rail)
            self.terrain_source.setCurrentIndex(1)
            self.release.setLayer(None); self.inputs['buffer'].setValue(0)
            self.inputs['resolution'].setValue(30); self.inputs['duration'].setValue(240)
            self.risk.setChecked(False)
            self.plugin.style_area(area)
            canvas = self.iface.mapCanvas(); canvas.setDestinationCrs(dem.crs())
            canvas.setExtent(dem.extent()); canvas.refresh()
            self.state.setText('Загружен синтетический пример. Нажмите «Рассчитать участок».')
        except Exception as exc:
            self.state.setText(str(exc))

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

    def configuration(self):
        terrain = self.terrain_request()
        rail, release = self.rail.currentLayer(), self.release.currentLayer()
        snap = lambda layer: [dict(id=str(f.id()),wkt=f.geometry().asWkt()) for f in layer.getFeatures()]
        if self.risk.isChecked() and not rail: raise ValueError('Для денежного риска укажите ось ЖД.')
        dem_path = ''
        if self.terrain_source.currentIndex() == 1:
            dem = self.dem.currentLayer()
            if not dem: raise ValueError('Выберите свой файл рельефа или переключите источник на Интернет.')
            dem_path = dem.source()
        elif (self.downloaded_terrain and self.downloaded_terrain['key'] == self.terrain_key(terrain)
              and Path(self.downloaded_terrain['path']).is_file()):
            dem_path = self.downloaded_terrain['path']
        vals = {k:w.value() for k,w in self.inputs.items()}
        return dict(area_wkt=terrain['area_wkt'],area_crs=terrain['area_crs'],
                    dem=dem_path,
                    rails=snap(rail) if rail else [],rail_crs=rail.crs().toWkt() if rail else '',
                    release_wkts=[x['wkt'] for x in snap(release)] if release else [],
                    release_crs=release.crs().toWkt() if release else '',
                    directory=str(Path(self.output.text().strip())/('avalanche_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))),
                    buffer=vals.pop('buffer'),resolution=vals.pop('resolution'),
                    screening={k:vals.pop(k) for k in ('min_slope','max_slope','min_relief','min_area')},
                    parameters=vals,frequency=self.frequency.value() if self.risk.isChecked() else None,
                    repair_cost=self.cost.value(),vulnerability=self.vulnerability.value(),
                    corridor=self.corridor.value())

    def start(self):
        if self.task is not None: return
        try: config = self.configuration()
        except ValueError as exc:
            self.state.setText(str(exc)); return
        self.set_busy(True)
        self.report.setEnabled(False)
        self.state.setText('Получение рельефа и расчёт… Прогресс также виден в диспетчере задач QGIS.')
        self.task = ScenarioTask(config,self.completed)
        self.task.progressChanged.connect(lambda value:self.progress.setValue(int(value)))
        QgsApplication.taskManager().addTask(self.task)

    def cancel_task(self):
        if self.task: self.task.cancel(); self.state.setText('Отмена операции…')

    def completed(self,manifest,error):
        self.set_busy(False)
        self.task = None
        if manifest is None:
            self.state.setText(error or 'Расчёт прерван.'); return
        self.progress.setValue(100)
        self.report_path = manifest['outputs']['report']; self.report.setEnabled(True)
        self.plugin.load_results(manifest)
        if manifest['status'] != 'completed':
            self.state.setText('Критерии поиска горных очагов не выполнены. Проверьте выделение и буфер. Подробности в отчёте.')
        else:
            s = manifest['simulation']
            self.state.setText(f"Сценарий рассчитан: {s['particles']} трассеров; максимум {s['max_pressure_kpa']:.1f} кПа. Результаты добавлены на карту. Проверьте ограничения в отчёте.")

    def open_report(self):
        if self.report_path: QDesktopServices.openUrl(QUrl.fromLocalFile(self.report_path))

    def closeEvent(self,event):
        # Keep the dialog alive while its task owns callbacks; closing hides it.
        event.ignore(); self.hide()


class AvalanchePlugin:
    def __init__(self,iface):
        self.iface,self.provider,self.dialog,self.area_tool = iface,None,None,None

    def initProcessing(self):
        if self.provider is None:
            self.provider = AvalancheProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        self.action = QAction(QIcon(str(Path(__file__).with_name('icon.svg'))),
                              'Лавина · оценка участка ЖД',self.iface.mainWindow())
        self.action.triggered.connect(self.show)
        self.iface.addPluginToMenu('Лавина — сценарии и риск',self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.dialog and self.dialog.task: self.dialog.task.cancel()
        if self.area_tool:
            self.iface.mapCanvas().unsetMapTool(self.area_tool)
        if hasattr(self,'action'):
            self.iface.removePluginMenu('Лавина — сценарии и риск',self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider); self.provider = None
        if self.dialog: self.dialog.hide()

    def show(self):
        if self.dialog is None: self.dialog = ScenarioDialog(self)
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
        self.dialog.release.setLayer(None)
        if self.area_tool: canvas.unsetMapTool(self.area_tool)
        self.show(); self.dialog.state.setText('Участок выделен. Можно запустить автоматический расчёт.')

    def draw_area(self):
        self.dialog.hide()
        self.area_tool = AreaTool(self.iface.mapCanvas(),self.add_area)
        self.iface.mapCanvas().setMapTool(self.area_tool)
        self.iface.messageBar().pushMessage('Лавина','Зажмите левую кнопку и растяните прямоугольник участка.',level=Qgis.MessageLevel.Info,duration=8)

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
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0,'Лавина · '+datetime.now().strftime('%H:%M:%S'))
        outputs = manifest['outputs']
        for key,name in [('terrain','Рельеф участка'),('slope','Уклон, градусы'),('candidates','Кандидатные очаги'),
                         ('speed_m_s','Скорость, м/с'),('pressure_kpa','Давление, кПа'),
                         ('intensity','Интенсивность сценария')]:
            if key not in outputs: continue
            layer = QgsRasterLayer(outputs[key],name)
            if not layer.isValid(): continue
            if key in ('intensity','candidates'):
                items = [(0,QColor(0,0,0,0),'Нет воздействия в сценарии' if key=='intensity' else 'Вне кандидатов'),
                         (1,QColor('#f8d875'),'Низкая' if key=='intensity' else 'Кандидатный очаг'),
                         (2,QColor('#ef8e41'),'Средняя'),(3,QColor('#c32f3c'),'Высокая')]
                classes = [QgsPalettedRasterRenderer.Class(v,c,label) for v,c,label in items]
                layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(),1,classes))
                layer.renderer().setOpacity(.7)
            project.addMapLayer(layer,False); group.insertLayer(0,layer)
        if 'vectors' in outputs:
            for key,name in [('railway_risk','ЖД · воздействие и риск')]:
                layer = QgsVectorLayer(outputs['vectors']+'|layername='+key,name,'ogr')
                if layer.isValid():
                    layer.renderer().setSymbol(QgsLineSymbol.createSimple({'line_color':'15,56,80','line_width':'1.2'}))
                    project.addMapLayer(layer,False); group.insertLayer(0,layer)
