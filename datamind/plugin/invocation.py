"""Kernel-unified plugin handler invocation: one call site, one attribution vocabulary.

A parser handler is third-party code, and a caller that invokes it in several places ends up
with several answers to the same two questions: which HTTP error does a broken handler
become, and which plugin does the log line name? :func:`invoke_plugin` answers both once,
which is why ``ingestion.upload_pipeline`` never calls a loader directly any more.

Two rules shape it:

*the kernel stays standard library only (G2)*
    ``fastapi`` is imported inside the exception path only, never at module top, so the
    kernel stays importable without the application's web stack and a listing endpoint
    cannot pull a web framework through a plugin import.

*passthrough is not a failure*
    A plugin that raises ``HTTPException`` has deliberately refused the input and already
    named its own stable code (G12); that exception is re-raised unchanged, so the contract
    error the caller sees is exactly the one the plugin raised. Everything else is mapped
    onto ``PARSE_ADAPTER_UNSUPPORTED``: an ``ImportError`` (including
    ``ModuleNotFoundError``) means an optional parser library is absent and is reported as
    ``reason='library_missing'``, any other ``Exception`` as ``reason='handler_failed'``.
    The original error stays attached as ``__cause__``. ``KeyboardInterrupt`` and
    ``SystemExit`` are not the plugin's failure to report, so only ``Exception`` is caught.

The log record is the attribution channel: every invocation emits ``plugin``,
``media_type``, ``reason`` and ``duration_ms``, and the HTTP body never carries exception
text - a message that quotes a str(error) can leak paths, connection secrets or SQL.

Example::

    from datamind.plugin.invocation import invoke_plugin

    result, extra = invoke_plugin(manifest, manifest.resolve(), path, document_id, sha)
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from .manifest import PARSE_ADAPTER_UNSUPPORTED, ParserPluginManifest

__all__ = ['invoke_plugin']

log = logging.getLogger('datamind.plugin')

#: The status a handler failure is reported as. 422 matches the contract's own use of
#: ``PARSE_ADAPTER_UNSUPPORTED``: the upload is well formed, nothing can parse it.
UNSUPPORTED_STATUS = 422


def invoke_plugin(manifest: ParserPluginManifest, loader: Callable[..., Any],
                  *args: Any, **kwargs: Any) -> Any:
    """Call ``loader(*args, **kwargs)`` and translate anything it raises.

    :param manifest: the declaration that owns ``loader``; it names the plugin in the
        error body and in the log record, and its first media type is logged.
    :param loader: the resolved handler. Resolution (and therefore the import) is the
        caller's job, so a caller that caches entry points still imports lazily (G7).
    :returns: whatever ``loader`` returned, untouched.
    :raises HTTPException: the plugin's own ``HTTPException``, unchanged; or one carrying
        ``{'error_code': 'PARSE_ADAPTER_UNSUPPORTED', 'details': {...}}`` when the handler
        raised ``ImportError`` (``reason='library_missing'``) or anything else
        (``reason='handler_failed'``), with the original error as ``__cause__``.
    """
    started = time.perf_counter()
    try:
        value = loader(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 - translated below, never swallowed
        duration_ms = _milliseconds(started)
        if _is_http_exception(error):
            # A deliberate refusal: the plugin's own stable code already reaches the
            # caller, so it is neither a success nor a crash of the handler.
            _log('passed_through', manifest, duration_ms)
            raise
        reason = 'library_missing' if isinstance(error, ImportError) else 'handler_failed'
        _log(reason, manifest, duration_ms, failed=True)
        raise _unsupported(manifest, reason) from error
    _log('ok', manifest, _milliseconds(started))
    return value


def _milliseconds(started: float) -> float:
    """Whole-ish milliseconds elapsed since ``started`` (a ``perf_counter`` reading)."""
    return round((time.perf_counter() - started) * 1000, 3)


def _is_http_exception(error: Exception) -> bool:
    """Whether ``error`` is a ``fastapi.HTTPException`` - imported here, never at top.

    The import stays lazy on purpose: this module is part of the kernel, which must remain
    importable with the standard library alone (G2). An environment without fastapi cannot
    have produced an ``HTTPException`` either, so a missing import answers ``False``.
    """
    try:
        from fastapi import HTTPException
    except ImportError:  # pragma: no cover - the application always ships fastapi
        return False
    return isinstance(error, HTTPException)


def _unsupported(manifest: ParserPluginManifest, reason: str) -> Exception:
    """The mapped error: the contract's stable code, attributed to the plugin."""
    from fastapi import HTTPException

    return HTTPException(UNSUPPORTED_STATUS, detail={
        'error_code': PARSE_ADAPTER_UNSUPPORTED,
        'details': {'subject': 'plugin', 'reason': reason,
                    'plugin': manifest.name, 'adapter': manifest.name}})


def _log(reason: str, manifest: ParserPluginManifest, duration_ms: float,
         *, failed: bool = False) -> None:
    """Emit the structured attribution record: one line, four fields, no exception text.

    A failure is a ``WARNING`` with ``exc_info=True`` so the traceback is in the log where
    an operator looks for it, never in the response a client receives.
    """
    extra = {'plugin': manifest.name, 'media_type': manifest.media_types[0],
             'reason': reason, 'duration_ms': duration_ms}
    if failed:
        log.warning('plugin %s failed on %s: %s', manifest.name, manifest.media_types[0],
                    reason, extra=extra, exc_info=True)
    else:
        log.info('plugin %s handled %s: %s', manifest.name, manifest.media_types[0],
                 reason, extra=extra)
