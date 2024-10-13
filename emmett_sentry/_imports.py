try:
    from emmett.__version__ import __version__

    _major, _minor, _ = __version__.split(".")
    if _major < 2 or (_major == 2 and _minor < 6):
        from .__version__ import __version__ as extver

        raise RuntimeError(f"Emmett-Sentry {extver} requires Emmett >= 2.6.0")

    from emmett import current
    from emmett.extensions import Extension, Signals, listen_signal

    _is_emmett = True
except ImportError:
    _is_emmett = False
    from emmett55 import current
    from emmett_core.datastructures import sdict
    from emmett_core.extensions import Extension

    Signals = sdict()

    def listen_signal(*args, **kwargs):
        def wrapper(f):
            return f

        return wrapper
