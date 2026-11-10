from pathlib import Path
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .algorithm import StabilityAlgorithm


class LandslideProvider(QgsProcessingProvider):
    def id(self):return 'landslide_scenario'
    def name(self):return 'Оползни — устойчивость склонов'
    def longName(self):return self.name()
    def icon(self):return QIcon(str(Path(__file__).with_name('icon.svg')))
    def loadAlgorithms(self):self.addAlgorithm(StabilityAlgorithm())
