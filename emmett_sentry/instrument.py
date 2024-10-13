import asyncio
from functools import wraps

import sentry_sdk
from sentry_sdk.consts import OP, SPANDATA
from sentry_sdk.tracing_utils import record_sql_queries

from ._imports import _is_emmett
from .helpers import _SPAN_ORIGIN, _SPAN_ORIGIN_DB


def _orm_tracer(adapter):
    _original_method = adapter.execute

    @wraps(_original_method)
    def wrapped(*args, **kwargs):
        with record_sql_queries(
            cursor=adapter.cursor,
            query=args[0],
            params_list=[],
            paramstyle=None,
            executemany=False,
            span_origin=_SPAN_ORIGIN_DB,
        ) as span:
            span.set_data(SPANDATA.DB_SYSTEM, adapter.dbengine)
            return _original_method(*args, **kwargs)

    return wrapped


def _templater_ctx_filter(ctx):
    return {key: ctx[key] for key in set(ctx.keys()) - {"__builtins__", "__writer__"}}


def _templater_tracer(render):
    @wraps(render)
    def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(
            op=OP.TEMPLATE_RENDER,
            name=args[0],
            origin=_SPAN_ORIGIN,
        ) as span:
            span.set_data("context", _templater_ctx_filter(kwargs.get("context") or args[1]))
            return render(*args, **kwargs)

    return wrapped


def _session_tracer(pipe, method):
    _original_method = getattr(pipe, method)
    span_name = {"_load_session": "load", "_pack_session": "save"}[method]

    @wraps(_original_method)
    def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(op=f"session.{span_name}", origin=_SPAN_ORIGIN):
            return _original_method(*args, **kwargs)

    return wrapped


def _cache_tracer(handler, method, op):
    _original_method = getattr(handler, method)

    @wraps(_original_method)
    def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(
            op=op,
            origin=_SPAN_ORIGIN,
        ) as span:
            span.set_data(SPANDATA.CACHE_KEY, args[1])
            return _original_method(*args, **kwargs)

    return wrapped


def _pipe_edge_tracer(original, mode):
    name = original.__self__.__class__.__name__

    @wraps(original)
    async def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(
            op=f"middleware.emmett.{mode}",
            name=name,
            origin=_SPAN_ORIGIN,
        ) as span:
            span.set_tag("emmett.pipe", name)
            return await original(*args, **kwargs)

    return wrapped


def _pipe_flow_tracer(original, pipe):
    name = pipe.__class__.__name__

    @wraps(original)
    async def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(
            op="middleware.emmett",
            name=name,
            origin=_SPAN_ORIGIN,
        ) as span:
            span.set_tag("emmett.pipe", name)
            return await original(*args, **kwargs)

    return wrapped


def _flow_target_tracer(pipeline, original):
    if not asyncio.iscoroutinefunction(original):
        original = pipeline._awaitable_wrap(original)

    @wraps(original)
    async def wrapped(*args, **kwargs):
        with sentry_sdk.start_span(
            op=OP.FUNCTION,
            name=original.__qualname__,
            origin=_SPAN_ORIGIN,
        ):
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


def _pipeline_target_instrument(original_method):
    @wraps(original_method)
    def wrapped(pipeline, f):
        return original_method(pipeline, _flow_target_tracer(pipeline, f))

    return wrapped


def instrument_orm(db):
    from emmett.orm.adapters import SQLAdapter

    if not isinstance(db._adapter, SQLAdapter):
        return
    db._adapter.execute = _orm_tracer(db._adapter)


def instrument_templater(app):
    app.templater.render = _templater_tracer(app.templater.render)


def instrument_sessions():
    from emmett_core.sessions import CookieSessionPipe, FileSessionPipe, RedisSessionPipe

    CookieSessionPipe._load_session = _session_tracer(CookieSessionPipe, "_load_session")
    CookieSessionPipe._pack_session = _session_tracer(CookieSessionPipe, "_pack_session")
    FileSessionPipe._load_session = _session_tracer(FileSessionPipe, "_load_session")
    FileSessionPipe._pack_session = _session_tracer(FileSessionPipe, "_pack_session")
    RedisSessionPipe._load_session = _session_tracer(RedisSessionPipe, "_load_session")
    RedisSessionPipe._pack_session = _session_tracer(RedisSessionPipe, "_pack_session")


def instrument_cache():
    from emmett_core.cache.handlers import RamCache, RedisCache

    RamCache.get = _cache_tracer(RamCache, "get", "get")
    RamCache.set = _cache_tracer(RamCache, "set", "put")
    RedisCache.get = _cache_tracer(RedisCache, "get", "get")
    RedisCache.set = _cache_tracer(RedisCache, "set", "put")

    if _is_emmett:
        from emmett.cache import DiskCache

        DiskCache.get = _cache_tracer(DiskCache, "get", "get")
        DiskCache.set = _cache_tracer(DiskCache, "set", "put")


def instrument_pipes():
    from emmett_core.pipeline import RequestPipeline

    RequestPipeline._flow_open = _pipeline_edges_instrument(RequestPipeline._flow_open, "open")
    RequestPipeline._flow_close = _pipeline_edges_instrument(RequestPipeline._flow_close, "close")
    RequestPipeline._get_proper_wrapper = _pipeline_flow_instrument(RequestPipeline._get_proper_wrapper)
    RequestPipeline.__call__ = _pipeline_target_instrument(RequestPipeline.__call__)
