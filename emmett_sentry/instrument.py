from functools import wraps

from emmett.orm.adapters import SQLAdapter
from sentry_sdk.consts import OP, SPANDATA
from sentry_sdk.hub import Hub
from sentry_sdk.tracing_utils import record_sql_queries


def _orm_tracer(adapter):
    original_method = adapter.execute

    @wraps(original_method)
    def wrapped(*args, **kwargs):
        hub = Hub.current
        with record_sql_queries(
            hub,
            adapter.cursor,
            args[0],
            [],
            None,
            False,
        ) as span:
            span.set_data(SPANDATA.DB_SYSTEM, adapter.dbengine)
            return original_method(*args, **kwargs)
    return wrapped


def _templater_tracer(render):
    @wraps(render)
    def wrapped(*args, **kwargs):
        hub = Hub.current
        with hub.start_span(
            op=OP.TEMPLATE_RENDER, description=args[0]
        ) as span:
            span.set_tag("renoir.template_name", args[0])
            return render(*args, **kwargs)
    return wrapped


def _session_tracer(pipe, method):
    original_method = getattr(pipe, method)
    span_name = {
        '_load_session': 'load',
        '_pack_session': 'save'
    }[method]

    @wraps(original_method)
    def wrapped(*args, **kwargs):
        hub = Hub.current
        with hub.start_span(op=f"session.{span_name}"):
            return original_method(*args, **kwargs)
    return wrapped


def _cache_tracer(handler, method):
    original_method = getattr(handler, method)

    @wraps(original_method)
    def wrapped(*args, **kwargs):
        hub = Hub.current
        with hub.start_span(
            op=f"cache.{method}", description=args[1]
        ) as span:
            span.set_tag("emmett.cache_key", args[1])
            return original_method(*args, **kwargs)
    return wrapped


def _pipe_edge_tracer(original, mode):
    name = original.__self__.__class__.__name__

    @wraps(original)
    async def wrapped(*args, **kwargs):
        hub = Hub.current
        with hub.start_span(op=f"pipe.{mode}", description=name) as span:
            span.set_tag("emmett.pipe", name)
            return await original(*args, **kwargs)
    return wrapped


def _pipe_flow_tracer(original, pipe):
    name = pipe.__class__.__name__

    @wraps(original)
    async def wrapped(*args, **kwargs):
        hub = Hub.current
        with hub.start_span(op="pipe", description=name) as span:
            span.set_tag("emmett.pipe", name)
            return await original(*args, **kwargs)
    return wrapped


def _pipeline_edges_instrument(original_method, mode):
    @wraps(original_method)
    def wrapped(*args, **kwargs):
        flow = original_method(*args, **kwargs)
        return [_pipe_edge_tracer(item, mode) for item in flow]
    return wrapped


def _pipe_flow_wrapper(original, pipe):
    @wraps(original)
    def wrapped(pipe_method, *args, **kwargs):
        return original(_pipe_flow_tracer(pipe_method, pipe), *args, **kwargs)
    return wrapped


def _pipeline_flow_instrument(original_method):
    @wraps(original_method)
    def wrapped(*args, **kwargs):
        rv = original_method(*args, **kwargs)
        return _pipe_flow_wrapper(rv, args[1])
    return wrapped


def instrument_orm(db):
    if not isinstance(db._adapter, SQLAdapter):
        return
    db._adapter.execute = _orm_tracer(db._adapter)


def instrument_templater(app):
    app.templater.render = _templater_tracer(app.templater.render)


def instrument_sessions():
    from emmett.sessions import CookieSessionPipe, FileSessionPipe, RedisSessionPipe
    setattr(
        CookieSessionPipe,
        '_load_session',
        _session_tracer(CookieSessionPipe, '_load_session')
    )
    setattr(
        CookieSessionPipe,
        '_pack_session',
        _session_tracer(CookieSessionPipe, '_pack_session')
    )
    setattr(
        FileSessionPipe,
        '_load_session',
        _session_tracer(FileSessionPipe, '_load_session')
    )
    setattr(
        FileSessionPipe,
        '_pack_session',
        _session_tracer(FileSessionPipe, '_pack_session')
    )
    setattr(
        RedisSessionPipe,
        '_load_session',
        _session_tracer(RedisSessionPipe, '_load_session')
    )
    setattr(
        RedisSessionPipe,
        '_pack_session',
        _session_tracer(RedisSessionPipe, '_pack_session')
    )


def instrument_cache():
    from emmett.cache import RamCache, DiskCache, RedisCache
    setattr(RamCache, 'get', _cache_tracer(RamCache, 'get'))
    setattr(RamCache, 'set', _cache_tracer(RamCache, 'set'))
    setattr(DiskCache, 'get', _cache_tracer(DiskCache, 'get'))
    setattr(DiskCache, 'set', _cache_tracer(DiskCache, 'set'))
    setattr(RedisCache, 'get', _cache_tracer(RedisCache, 'get'))
    setattr(RedisCache, 'set', _cache_tracer(RedisCache, 'set'))


def instrument_pipes():
    from emmett.pipeline import RequestPipeline
    RequestPipeline._flow_open = _pipeline_edges_instrument(
        RequestPipeline._flow_open, 'open'
    )
    RequestPipeline._flow_close = _pipeline_edges_instrument(
        RequestPipeline._flow_close, 'close'
    )
    RequestPipeline._get_proper_wrapper = _pipeline_flow_instrument(
        RequestPipeline._get_proper_wrapper
    )
