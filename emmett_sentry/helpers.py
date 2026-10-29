import sys
import urllib
import weakref
from contextlib import contextmanager, nullcontext
from functools import wraps

import sentry_sdk
from emmett_core.http.response import HTTPResponse
from sentry_sdk import traces
from sentry_sdk.integrations._wsgi_common import _filter_headers
from sentry_sdk.sessions import track_session
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception

from ._imports import current


_SDK_MAJOR = int(sentry_sdk.VERSION.split(".")[0])
_SPAN_ORIGIN = "auto.http.emmett"
_SPAN_ORIGIN_DB = "auto.db.emmett"


def _capture_exception(exception, handled=False):
    # SDK 3.x reads client options by itself, SDK 2.x falls back to defaults without them
    opts = {"client_options": sentry_sdk.get_client().options} if _SDK_MAJOR < 3 else {}
    event, hint = event_from_exception(
        exception,
        mechanism={"type": "emmett", "handled": handled},
        **opts,
    )
    sentry_sdk.capture_event(event, hint=hint)


def _capture_message(message, level=None):
    sentry_sdk.capture_message(message, level=level)


@contextmanager
def _http_scope_wrapper(req, proto, with_sess=True, with_txn=False):
    _sentry_scope_gen = sentry_sdk.isolation_scope()
    _sentry_scope = _sentry_scope_gen.__enter__()
    _sentry_session = track_session(_sentry_scope, session_mode="request") if with_sess else nullcontext()
    _sentry_session.__enter__()
    _configure_scope(_sentry_scope, proto, _process_http, req)
    _ctx = _start_server_span(proto, req, "http") if with_txn else nullcontext()
    _span = _ctx.__enter__()
    try:
        yield _sentry_scope
    except HTTPResponse as http:
        _set_http_status(_span, http.status_code)
        _ctx.__exit__(None, None, None)
        _sentry_session.__exit__(None, None, None)
        _sentry_scope_gen.__exit__(None, None, None)
        raise
    except Exception:
        _set_http_status(_span, 500)
        exc = sys.exc_info()
        _ctx.__exit__(*exc)
        _sentry_session.__exit__(*exc)
        _sentry_scope_gen.__exit__(*exc)
        raise
    _set_http_status(_span, current.response.status)
    _ctx.__exit__(None, None, None)
    _sentry_session.__exit__(None, None, None)
    _sentry_scope_gen.__exit__(None, None, None)


@contextmanager
def _ws_scope_wrapper(ws, proto, with_sess=True, with_txn=False):
    _sentry_scope_gen = sentry_sdk.isolation_scope()
    _sentry_scope = _sentry_scope_gen.__enter__()
    _sentry_session = track_session(_sentry_scope, session_mode="request") if with_sess else nullcontext()
    _sentry_session.__enter__()
    _configure_scope(_sentry_scope, proto, _process_ws, ws)
    _ctx = _start_server_span(proto, ws, "websocket") if with_txn else nullcontext()
    _ctx.__enter__()
    try:
        yield _sentry_scope
    except Exception:
        exc = sys.exc_info()
        _ctx.__exit__(*exc)
        _sentry_session.__exit__(*exc)
        _sentry_scope_gen.__exit__(*exc)
        raise
    _ctx.__exit__()
    _sentry_session.__exit__()
    _sentry_scope_gen.__exit__()


def _configure_scope(scope, proto, proc, wrapper):
    scope.clear_breadcrumbs()
    scope._name = proto
    scope.add_event_processor(proc(proto, weakref.ref(wrapper)))


def _start_server_span(proto, wrapper, sgi_proto):
    traces.continue_trace(wrapper.headers)
    return traces.start_span(
        name=wrapper.name,
        parent_span=None,
        attributes={
            "sentry.op": f"{sgi_proto}.server",
            "sentry.origin": _SPAN_ORIGIN,
            "sentry.segment.name.source": "component",
            f"{proto}.type": sgi_proto,
        },
    )


def _set_http_status(span, status_code):
    if span is None:
        return
    span.set_attribute("http.response.status_code", status_code)
    span.status = "error" if status_code >= 400 else "ok"


def _process_common_asgi(data, wrapper):
    data["query_string"] = urllib.parse.unquote(wrapper._scope["query_string"].decode("latin-1"))


def _process_common_rsgi(data, wrapper):
    data["query_string"] = urllib.parse.unquote(wrapper._scope.query_string)


def _process_common(data, proto, wrapper):
    data["url"] = "%s://%s%s" % (wrapper.scheme, wrapper.host, wrapper.path)
    data["env"] = {}
    data["headers"] = _filter_headers(dict(wrapper.headers.items()))

    if sentry_sdk.get_client().options["data_collection"]["user_info"]:
        data["env"]["REMOTE_ADDR"] = wrapper.client

    if proto == "rsgi":
        _process_common_rsgi(data, wrapper)
    elif proto == "asgi":
        _process_common_asgi(data, wrapper)


def _process_http(proto, weak_wrapper):
    def processor(event, hint):
        wrapper = weak_wrapper()
        if wrapper is None:
            return event

        with capture_internal_exceptions():
            data = event.setdefault("request", {})
            _process_common(data, proto, wrapper)
            data["method"] = wrapper.method
            data["content_length"] = wrapper.content_length

        return event

    return processor


def _process_ws(proto, weak_wrapper):
    def processor(event, hint):
        wrapper = weak_wrapper()
        if wrapper is None:
            return event

        with capture_internal_exceptions():
            data = event.setdefault("request", {})
            _process_common(data, proto, wrapper)

        return event

    return processor


def _build_http_dispatcher_wrapper(ext, dispatch_method, use_txn=False):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        proto = "rsgi" if hasattr(current.request._scope, "rsgi_version") else "asgi"

        with _http_scope_wrapper(current.request, proto, with_sess=use_txn, with_txn=use_txn) as sentry_scope:
            for key, builder in ext._scopes.items():
                sentry_scope.set_context(key, await builder())
            try:
                return await dispatch_method(*args, **kwargs)
            except HTTPResponse:
                raise
            except Exception as exc:
                sentry_scope.set_context("body_params", await current.request.body_params)
                _capture_exception(exc)
                raise

    return wrap


def _build_ws_dispatcher_wrapper(ext, dispatch_method, use_txn=False):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        proto = "rsgi" if hasattr(current.websocket._scope, "rsgi_version") else "asgi"

        with _http_scope_wrapper(current.websocket, proto, with_sess=use_txn, with_txn=use_txn) as sentry_scope:
            for key, builder in ext._scopes.items():
                sentry_scope.set_context(key, await builder())
            try:
                return await dispatch_method(*args, **kwargs)
            except Exception as exc:
                _capture_exception(exc)
                raise

    return wrap


def _build_routing_rec_http(ext, rec_cls):
    def _routing_rec_http(router, name, dispatch, flow_stream):
        use_txn = ext.config.enable_tracing and name not in ext._tracing_excluded_routes
        return rec_cls(
            name=name,
            dispatch=_build_http_dispatcher_wrapper(ext, dispatch, use_txn=use_txn),
            flow_stream=flow_stream,
        )

    return _routing_rec_http


def _build_routing_rec_ws(ext, rec_cls):
    def _routing_rec_ws(router, name, dispatch, flow_recv, flow_send):
        use_txn = ext.config.enable_tracing and ext.config.trace_websockets and name not in ext._tracing_excluded_routes

        return rec_cls(
            name=name,
            dispatch=_build_ws_dispatcher_wrapper(ext, dispatch, use_txn=use_txn),
            flow_recv=flow_recv,
            flow_send=flow_send,
        )

    return _routing_rec_ws


def patch_routers(ext):
    ext.app._router_http.__class__._routing_rec_builder = _build_routing_rec_http(
        ext, ext.app._router_http.__class__._routing_rec_builder
    )
    ext.app._router_ws.__class__._routing_rec_builder = _build_routing_rec_ws(
        ext, ext.app._router_ws.__class__._routing_rec_builder
    )
