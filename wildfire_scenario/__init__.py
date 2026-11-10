__version__ = '0.1.0'


def classFactory(iface):
    from .plugin import WildfirePlugin
    return WildfirePlugin(iface)
