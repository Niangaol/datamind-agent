"""Built-in parser plugins and the process-wide parser context.

``builtin_manifests()`` is the declaration inventory. ``default_context()`` is the
bootstrap seam: the parser registry plus every builtin manifest, created once per process
under a module lock and reused afterwards, so the parsing child process bootstraps the
same set on its own (G8) with no external setup step.

Importing this package imports only the manifest declarations (stdlib plus
``datamind.plugin``); each loader stays a ``"module:function"`` reference until a caller
resolves it, which is what keeps a plugin listing free of ``pymupdf`` and peers (G7).
"""
from __future__ import annotations

import threading

from datamind.plugin import Context
from datamind.plugin.manifest import ParserPluginManifest, ParserRegistry

from . import (dxf_plugin, measurement_json_plugin, office_plugin, ohr_json_plugin,
               pdf_plugin)

__all__ = ['builtin_manifests', 'default_context']

_lock = threading.Lock()
_context: Context | None = None


def builtin_manifests() -> tuple[ParserPluginManifest, ...]:
    """Every built-in parser manifest, in registration order."""
    return (pdf_plugin.MANIFEST, ohr_json_plugin.MANIFEST, *office_plugin.MANIFESTS,
            dxf_plugin.MANIFEST, measurement_json_plugin.MANIFEST)


def default_context() -> Context:
    """The process-wide parser context, created on first use and shared afterwards.

    The registry is the context's ``parser`` service and each manifest is registered as
    one of the context's effects, so disposing the context frees the claims again. The
    manifests already hold their declarations, so concurrent callers hold the lock only
    for the registration itself.
    """
    global _context
    if _context is not None:
        return _context
    with _lock:
        if _context is None:
            context = Context()
            context.set('parser', ParserRegistry())
            for manifest in builtin_manifests():
                context.effect(context.parser.register(manifest))
            _context = context
    return _context
