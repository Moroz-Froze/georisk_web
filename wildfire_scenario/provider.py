from pathlib import Path
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .algorithm import WildfireAlgorithm

class WildfireProvider(QgsProcessingProvider):
    def id(self):return 'wildfire_scenario'
    def name(self):return 'Лесные пожары — опасность и распространение'
    def longName(self):return self.name()
    def icon(self):return QIcon(str(Path(__file__).with_name('icon.svg')))
    def loadAlgorithms(self):self.addAlgorithm(WildfireAlgorithm())
