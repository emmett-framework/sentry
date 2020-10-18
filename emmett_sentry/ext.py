# -*- coding: utf-8 -*-
"""
    emmett_sentry.ext
    -----------------

    Provides Sentry extension

    :copyright: 2020 Giovanni Barillari
    :license: BSD-3-Clause
"""

import sys

from typing import Any, Awaitable, Callable, Optional

import sentry_sdk

from emmett.extensions import Extension
from sentry_sdk.hub import Hub

from .helpers import _capture_exception, _capture_message, patch_routers


class Sentry(Extension):
    default_config = dict(
        dsn="",
        environment="development",
        release=None,
        auto_load=True,
        enable_tracing=False
    )
    _initialized = False
    _errmsg = "You need to configure Sentry extension before using its methods"

    def on_load(self):
        self._scopes = {}
        if not self.config.dsn:
            return
        sentry_sdk.init(
            dsn=self.config.dsn,
            environment=self.config.environment,
            release=self.config.release
        )
        patch_routers(self)
        self._initialized = True

    def exception(self, exc_info: Any = None, **kwargs: Any):
        assert self._initialized, self._errmsg
        capture_exception(exc_info or sys.exc_info())

    def message(self, msg: str, level: Optional[str] = None, **kwargs: Any):
        assert self._initialized, self._errmsg
        capture_message(msg, level)

    def extra_scope(self, name: str, builder: Callable[[], Awaitable[Any]]):
        self._scopes[name] = builder


def capture_exception(exception):
    with Hub(Hub.current) as hub:
        _capture_exception(hub, exception)


def capture_message(message, level):
    with Hub(Hub.current) as hub:
        _capture_message(hub, message, level=level)
