import sys
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import sentry_sdk

from ._imports import Extension, Signals, _is_emmett, listen_signal
from .helpers import _capture_exception, _capture_message, patch_routers


T = TypeVar("T")


class Sentry(Extension):
    default_config = {
        "dsn": "",
        "environment": "development",
        "release": None,
        "auto_load": True,
        "enable_tracing": False,
        "sample_rate": 1.0,
        "tracing_sample_rate": None,
        "tracing_exclude_routes": [],
        "trace_websockets": False,
        "trace_orm": True,
        "trace_templates": True,
        "trace_sessions": True,
        "trace_cache": True,
        "trace_pipes": False,
        "integrations": [],
        "sdk_opts": {},
    }
    _initialized = False
    _errmsg = "You need to configure Sentry extension before using its methods"

    def on_load(self):
        self._scopes = {}
        self._before_send_callbacks = []
        if not self.config.dsn:
            return
        self._tracing_excluded_routes = set(self.config.tracing_exclude_routes)
        sdk_config = {
            "dsn": self.config.dsn,
            "environment": self.config.environment,
            "release": self.config.release,
            "sample_rate": self.config.sample_rate,
            "traces_sample_rate": self.config.tracing_sample_rate,
            "before_send": self._before_send,
            "integrations": self.config.integrations,
        }
        sdk_config = {**sdk_config, **self.config.sdk_opts}
        sentry_sdk.init(**sdk_config)
        if self.config.auto_load:
            patch_routers(self)
        self._instrument()
        self._initialized = True

    def _instrument(self):
        if self.config.enable_tracing:
            if self.config.trace_templates and _is_emmett:
                from .instrument import instrument_templater

                instrument_templater(self.app)
            if self.config.trace_sessions:
                from .instrument import instrument_sessions

                instrument_sessions()
            if self.config.trace_cache:
                from .instrument import instrument_cache

                instrument_cache()
            if self.config.trace_pipes:
                from .instrument import instrument_pipes

                instrument_pipes()

    @listen_signal(Signals.after_database)
    def _signal_db(self, database):
        if self.config.enable_tracing and self.config.trace_orm and _is_emmett:
            from .instrument import instrument_orm

            instrument_orm(database)

    def _before_send(self, event, hint):
        for callback in self._before_send_callbacks:
            event = callback(event, hint)
            if not event:
                break
        return event

    def exception(self, exc_info: Any = None, **kwargs: Dict[str, Any]):
        assert self._initialized, self._errmsg
        capture_exception(exc_info or sys.exc_info(), **kwargs)

    def message(self, msg: str, level: Optional[str] = None, **kwargs: Dict[str, Any]):
        assert self._initialized, self._errmsg
        capture_message(msg, level, **kwargs)

    def extra_scope(self, name: str, builder: Callable[[], Awaitable[Any]]):
        self._scopes[name] = builder

    def before_send(self, f: T) -> T:
        self._before_send_callbacks.append(f)
        return f


def capture_exception(exception, **contexts):
    with sentry_sdk.get_current_scope() as scope:
        for key, val in contexts.items():
            scope.set_context(key, val)
        _capture_exception(exception)


def capture_message(message, level, **contexts):
    with sentry_sdk.get_current_scope() as scope:
        for key, val in contexts.items():
            scope.set_context(key, val)
        _capture_message(message, level=level)
