from pathlib import Path
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .algorithm import ScenarioAlgorithm


class AvalancheProvider(QgsProcessingProvider):
    def id(self):
        return 'avalanche_scenario'

    def name(self):
        return 'Лавина — сценарии и риск'

    def longName(self):
        return self.name()

    def icon(self):
        return QIcon(str(Path(__file__).with_name('icon.svg')))

    def loadAlgorithms(self):
        self.addAlgorithm(ScenarioAlgorithm())
