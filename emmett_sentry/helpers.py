# -*- coding: utf-8 -*-
"""
    emmett_sentry.helpers
    ---------------------

    Provides Sentry extension helpers

    :copyright: 2020 Giovanni Barillari
    :license: BSD-3-Clause
"""

import urllib

from functools import wraps

from emmett import current
from emmett.asgi.wrappers import Request as ASGIRequest, Websocket as ASGIWebsocket
from emmett.http import HTTPResponse
from emmett.rsgi.wrappers import Request as RSGIRequest, Websocket as RSGIWebsocket
from sentry_sdk.hub import Hub, _should_send_default_pii
from sentry_sdk.integrations._wsgi_common import _filter_headers
from sentry_sdk.tracing import Transaction
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
    hub.capture_message(
        message,
        level=level
    )


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


def _process_http(event, hint):
    if not hasattr(current, "request"):
        return event

    wrapper = current.request

    with capture_internal_exceptions():
        data = event.setdefault("request", {})
        _process_common(data, wrapper)
        data["method"] = wrapper.method
        data["content_length"] = wrapper.content_length

    event["transaction"] = wrapper.name

    return event


def _process_ws(event, hint):
    if not hasattr(current, "websocket"):
        return event

    wrapper = current.websocket

    with capture_internal_exceptions():
        data = event.setdefault("request", {})
        _process_common(data, wrapper)

    event["transaction"] = wrapper.name

    return event


def _build_http_dispatcher_wrapper_err(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        with Hub(Hub.current) as hub:
            with hub.push_scope() as scope:
                scope.add_event_processor(_process_http)
                for key, builder in ext._scopes.items():
                    scope.set_extra(key, await builder())
                try:
                    return await dispatch_method(*args, **kwargs)
                except HTTPResponse:
                    raise
                except Exception as exc:
                    scope.set_extra(
                        "body_params",
                        await current.request.body_params
                    )
                    _capture_exception(hub, exc)
                    raise
    return wrap


def _build_http_dispatcher_wrapper_txn(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        with Hub(Hub.current) as hub:
            with hub.push_scope() as scope:
                scope.add_event_processor(_process_http)
                for key, builder in ext._scopes.items():
                    scope.set_extra(key, await builder())

                proto = (
                    "rsgi" if hasattr(current.request._scope, "rsgi_version") else
                    "asgi"
                )
                txn = Transaction.continue_from_headers(
                    current.request.headers,
                    op="http.server"
                )
                txn.set_tag(f"{proto}.type", "http")

                with hub.start_transaction(txn):
                    try:
                        return await dispatch_method(*args, **kwargs)
                    except HTTPResponse:
                        raise
                    except Exception as exc:
                        scope.set_extra(
                            "body_params",
                            await current.request.body_params
                        )
                        _capture_exception(hub, exc)
                        raise
    return wrap


def _build_ws_dispatcher_wrapper_err(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        with Hub(Hub.current) as hub:
            with hub.push_scope() as scope:
                scope.add_event_processor(_process_ws)
                for key, builder in ext._scopes.items():
                    scope.set_extra(key, await builder())
                try:
                    return await dispatch_method(*args, **kwargs)
                except Exception as exc:
                    _capture_exception(hub, exc)
                    raise
    return wrap


def _build_ws_dispatcher_wrapper_txn(ext, dispatch_method):
    @wraps(dispatch_method)
    async def wrap(*args, **kwargs):
        with Hub(Hub.current) as hub:
            with hub.push_scope() as scope:
                scope.add_event_processor(_process_ws)
                for key, builder in ext._scopes.items():
                    scope.set_extra(key, await builder())

                proto = (
                    "rsgi" if hasattr(current.websocket._scope, "rsgi_version") else
                    "asgi"
                )
                txn = Transaction.continue_from_headers(
                    current.websocket.headers,
                    op="websocket.server"
                )
                txn.set_tag(f"{proto}.type", "websocket")

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
