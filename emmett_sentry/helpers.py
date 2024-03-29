import urllib
import weakref

from functools import wraps

from emmett import current
from emmett.asgi.wrappers import Request as ASGIRequest, Websocket as ASGIWebsocket
from emmett.http import HTTPResponse
from emmett.rsgi.wrappers import Request as RSGIRequest, Websocket as RSGIWebsocket
from sentry_sdk.hub import Hub, _should_send_default_pii
from sentry_sdk.integrations._wsgi_common import _filter_headers
from sentry_sdk.tracing import Transaction, TRANSACTION_SOURCE_ROUTE
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception


def _capture_exception(hub, exception):
    with capture_internal_exceptions():
        event, hint = event_from_exception(
            exception,
            client_options=hub.client.options,
            mechanism={"type": "emmett", "handled": False},
        )
        hub.capture_event(event, hint=hint)


def _capture_message(hub, message, level = None):
    with capture_internal_exceptions():
        hub.capture_message(message, level=level)


def _configure_transaction(scope, wrapper):
    scope.clear_breadcrumbs()
    scope.set_transaction_name(wrapper.name, source=TRANSACTION_SOURCE_ROUTE)


def _continue_transaction(scope, wrapper, wrapper_type):
    scope.clear_breadcrumbs()
    proto = (
        "rsgi" if hasattr(wrapper._scope, "rsgi_version") else
        "asgi"
    )
    txn = Transaction.continue_from_headers(
        wrapper.headers,
        op=f"{wrapper_type}.server",
        name=wrapper.name,
        source=TRANSACTION_SOURCE_ROUTE
    )
    txn.set_tag(f"{proto}.type", wrapper_type)
    return txn


def _process_common_asgi(data, wrapper):
    data["query_string"] = urllib.parse.unquote(
        wrapper._scope["query_string"].decode("latin-1")
    )

def _process_common_rsgi(data, wrapper):
    data["query_string"] = urllib.parse.unquote(wrapper._scope.query_string)


def _process_common(data, wrapper):
    data["url"] = "%s://%s%s" % (
        wrapper.scheme,
        wrapper.host,
        wrapper.path
    )
    data["env"] = {}
    data["headers"] = _filter_headers(dict(wrapper.headers.items()))

    if _should_send_default_pii():
        data["env"]["REMOTE_ADDR"] = wrapper.client

    if isinstance(wrapper, (ASGIRequest, ASGIWebsocket)):
        _process_common_asgi(data, wrapper)
    elif isinstance(wrapper, (RSGIRequest, RSGIWebsocket)):
        _process_common_rsgi(data, wrapper)


def _process_http(weak_wrapper):
    def processor(event, hint):
        wrapper = weak_wrapper()
        if wrapper is None:
            return event

        with capture_internal_exceptions():
            data = event.setdefault("request", {})
            _process_common(data, wrapper)
            data["method"] = wrapper.method
            data["content_length"] = wrapper.content_length

        return event

    return processor


def _process_ws(weak_wrapper):
    def processor(event, hint):
        wrapper = weak_wrapper()
        if wrapper is None:
            return event

        with capture_internal_exceptions():
            data = event.setdefault("request", {})
            _process_common(data, wrapper)

        return event

    return processor


def _build_http_dispatcher_wrapper_err(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        hub = Hub.current
        weak_request = weakref.ref(current.request)

        with Hub(hub) as hub:
            with hub.configure_scope() as scope:
                _configure_transaction(scope, current.request)
                scope.add_event_processor(_process_http(weak_request))
                for key, builder in ext._scopes.items():
                    scope.set_context(key, await builder())
                try:
                    return await dispatch_method(*args, **kwargs)
                except HTTPResponse:
                    raise
                except Exception as exc:
                    scope.set_context(
                        "body_params",
                        await current.request.body_params
                    )
                    _capture_exception(hub, exc)
                    raise

    return wrap


def _build_http_dispatcher_wrapper_txn(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        hub = Hub.current
        weak_request = weakref.ref(current.request)

        with Hub(hub) as hub:
            with hub.configure_scope() as scope:
                txn = _continue_transaction(scope, current.request, "http")
                scope.add_event_processor(_process_http(weak_request))
                for key, builder in ext._scopes.items():
                    scope.set_context(key, await builder())
                with hub.start_transaction(txn):
                    try:
                        return await dispatch_method(*args, **kwargs)
                    except HTTPResponse:
                        raise
                    except Exception as exc:
                        scope.set_context(
                            "body_params",
                            await current.request.body_params
                        )
                        _capture_exception(hub, exc)
                        raise
    return wrap


def _build_ws_dispatcher_wrapper_err(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        hub = Hub.current
        weak_websocket = weakref.ref(current.websocket)

        with hub.configure_scope() as scope:
            _configure_transaction(scope, current.websocket)
            scope.add_event_processor(_process_ws(weak_websocket))
            for key, builder in ext._scopes.items():
                scope.set_context(key, await builder())
            try:
                return await dispatch_method(*args, **kwargs)
            except Exception as exc:
                _capture_exception(hub, exc)
                raise
    return wrap


def _build_ws_dispatcher_wrapper_txn(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        hub = Hub.current
        weak_websocket = weakref.ref(current.websocket)

        with Hub(hub) as hub:
            with hub.configure_scope() as scope:
                txn = _continue_transaction(scope, current.request, "websocket")
                scope.add_event_processor(_process_ws(weak_websocket))
                for key, builder in ext._scopes.items():
                    scope.set_context(key, await builder())

                with hub.start_transaction(txn):
                    try:
                        return await dispatch_method(*args, **kwargs)
                    except Exception as exc:
                        _capture_exception(hub, exc)
                        raise
    return wrap


def _build_routing_rec_http(ext, rec_cls):
    def _routing_rec_http(router, name, match, dispatch):
        wrapper = (
            _build_http_dispatcher_wrapper_txn if (
                ext.config.enable_tracing and
                name not in ext._tracing_excluded_routes
            ) else _build_http_dispatcher_wrapper_err
        )

        return rec_cls(
            name=name,
            match=match,
            dispatch=wrapper(ext, dispatch)
        )

    return _routing_rec_http


def _build_routing_rec_ws(ext, rec_cls):
    def _routing_rec_ws(router, name, match, dispatch, flow_recv, flow_send):
        wrapper = (
            _build_ws_dispatcher_wrapper_txn if (
                ext.config.enable_tracing and
                ext.config.trace_websockets and
                name not in ext._tracing_excluded_routes
            ) else _build_ws_dispatcher_wrapper_err
        )

        return rec_cls(
            name=name,
            match=match,
            dispatch=wrapper(ext, dispatch),
            flow_recv=flow_recv,
            flow_send=flow_send
        )

    return _routing_rec_ws


def patch_routers(ext):
    ext.app._router_http.__class__._routing_rec_builder = _build_routing_rec_http(
        ext, ext.app._router_http.__class__._routing_rec_builder
    )
    ext.app._router_ws.__class__._routing_rec_builder = _build_routing_rec_ws(
        ext, ext.app._router_ws.__class__._routing_rec_builder
    )
