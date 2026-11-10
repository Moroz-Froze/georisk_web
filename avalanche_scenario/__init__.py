__version__ = '0.1.1'


def classFactory(iface):
    from .plugin import AvalanchePlugin
    return AvalanchePlugin(iface)
