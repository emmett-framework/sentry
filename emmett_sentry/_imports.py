try:
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
