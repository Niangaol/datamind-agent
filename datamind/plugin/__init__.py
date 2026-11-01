"""Cordis-style plugin kernel for DataMind (Python-native, standard library only).

The kernel gives the parser plugins of later tasks the four things they need to be
loadable, unloadable and testable in isolation:

* a :class:`Context` with services (``set``/``get``/``has``), dependency injection
  (``inject``), reversible effects (``effect``) and scoped loading/unloading
  (``plugin``/``dispose``);
* one declarative config entry point, :func:`validate_config`, whose failures name the
  offending field path;
* a synchronous :class:`~datamind.plugin.events.EventBus` with Cordis' ``emit``,
  ``parallel``, ``serial``, ``waterfall`` and ``bail`` dispatch styles;
* explicit errors (:class:`PluginError` and its subclasses) instead of silent behaviour.

It is deliberately small: no registry, no manifests, no file loading. Those belong to
the plugin system built on top of it, so that the kernel's public surface stays stable.

Example::

    from datamind.plugin import Context

    ctx = Context()
    ctx.set('settings', {'pdf': True})

    def pdf_parser(plugin_ctx, config):
        plugin_ctx.on('parse', lambda path: f'parsed {path}')
        plugin_ctx.set('parser', object())

    handle = ctx.plugin(pdf_parser, {'mode': 'strict'})
    ctx.emit('parse', 'a.pdf')     # the plugin's listener runs
    handle.dispose()               # listener and service are gone again
"""
from .context import (
    ConfigError,
    Context,
    InjectError,
    PluginError,
    ServiceConflictError,
    ServiceNotFoundError,
    validate_config,
)
from .events import BailError, EventBus, EventError

__all__ = [
    'BailError',
    'ConfigError',
    'Context',
    'EventBus',
    'EventError',
    'InjectError',
    'PluginError',
    'ServiceConflictError',
    'ServiceNotFoundError',
    'validate_config',
]
