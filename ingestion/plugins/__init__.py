"""Built-in parser plugins, explicit-path discovery and the process-wide parser context.

``builtin_manifests()`` is the declaration inventory. ``default_context()`` is the
bootstrap seam: the parser registry plus every builtin manifest, created once per process
under a module lock and reused afterwards, so the parsing child process bootstraps the
same set on its own (G8) with no external setup step.

``ensure_loaded()`` is the second, opt-in step: it applies ``DATAMIND_PLUGIN_CONFIG`` to
the builtin manifests and loads the ``*.py`` files named by ``DATAMIND_PLUGIN_PATH`` into
the same context. Discovery is deliberately explicit - the configured directories are
scanned one level deep and nothing else (never site-packages, never the checkout), because
importing arbitrary modules is a security boundary, not a convenience. A single broken
plugin is skipped and logged; a broken *configuration* is fail-fast, so a typo in a plugin
name or an ``enabled`` value cannot silently change what the process parses.

Importing this package imports only the manifest declarations (stdlib plus
``datamind.plugin``); each loader stays a ``"module:function"`` reference until a caller
resolves it, which is what keeps a plugin listing free of ``pymupdf`` and peers (G7).
External modules are imported by :func:`ensure_loaded` only, never at import time.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datamind.plugin import ConfigError, Context, PluginError
from datamind.plugin.manifest import ParserPluginManifest, ParserRegistry

from . import (dxf_plugin, measurement_json_plugin, office_plugin, ohr_json_plugin,
               pdf_plugin)

__all__ = ['LoadedPlugin', 'builtin_manifests', 'default_context', 'ensure_loaded',
           'loaded_plugins']

#: ``os.pathsep``-separated directories scanned for top-level ``*.py`` plugin modules.
PLUGIN_PATH_ENV = 'DATAMIND_PLUGIN_PATH'
#: A JSON file mapping plugin name to ``{"enabled": bool, "config": object|null}``.
PLUGIN_CONFIG_ENV = 'DATAMIND_PLUGIN_CONFIG'
#: The only keys a per-plugin config entry may carry.
_ENTRY_KEYS = frozenset({'enabled', 'config'})

log = logging.getLogger(__name__)

_lock = threading.Lock()
_context: Context | None = None

_load_lock = threading.RLock()
_loaded = False
_loaded_plugins: tuple['LoadedPlugin', ...] = ()


@dataclass(frozen=True)
class LoadedPlugin:
    """One plugin loaded into the process-wide context, and where it came from.

    ``loaded_from`` is ``'builtin'`` for the declarations this package ships and the
    absolute file path of the module for an external plugin. ``config`` is the subset a
    ``DATAMIND_PLUGIN_CONFIG`` entry declared, validated against the manifest schema; it
    is ``None`` when the entry declared no config. Nothing injects it into a parse handler
    yet - it is recorded so a later task can, and so the listing endpoint can report it.
    """

    manifest: ParserPluginManifest
    loaded_from: str
    config: Mapping[str, Any] | None = None


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


def ensure_loaded() -> Context:
    """Apply plugin config and discover external plugins, once per process.

    The first successful call applies ``DATAMIND_PLUGIN_CONFIG`` to every builtin
    manifest, then loads the modules named by ``DATAMIND_PLUGIN_PATH`` into the default
    context. Later calls return immediately, so the cost is paid once and a caller can
    invoke it on every entry point (the upload API process and every parsing child, G8).

    A failing *module* is logged and skipped so one broken plugin cannot disable the rest;
    a failing *configuration* (bad JSON, an unknown plugin name, an invalid ``enabled``
    value or a config the manifest schema rejects) raises :class:`ConfigError` and leaves
    ``_loaded`` unset, because a process that mis-declares what it may parse must not
    start. The flag is set only after a complete pass, so a raised error is retried by the
    next caller instead of being memoized as success.
    """
    global _loaded, _loaded_plugins
    if _loaded:
        return default_context()
    with _load_lock:
        if _loaded:
            return default_context()
        context = default_context()
        entries = _plugin_config()
        records = [_apply_entry(manifest, entries.get(manifest.name), 'builtin')
                   for manifest in builtin_manifests()]
        records.extend(_discover(context, entries))
        _require_known(entries, context)
        _loaded_plugins = tuple(records)
        _loaded = True
    return context


def loaded_plugins() -> tuple[LoadedPlugin, ...]:
    """Every loaded plugin and its provenance, in registration order.

    The builtins come first, followed by the external modules in path order. Calling this
    before any entry point ran loads the plugins, so the answer never depends on which
    caller happened to go first.
    """
    ensure_loaded()
    return _loaded_plugins


# --------------------------------------------------------------------------------------
# Plugin config
# --------------------------------------------------------------------------------------

def _plugin_config() -> dict[str, dict[str, Any]]:
    """The validated ``DATAMIND_PLUGIN_CONFIG`` entries, keyed by plugin name.

    Only the shape of an entry is checked here; whether the plugin exists is checked after
    discovery, and the entry's ``config`` is checked against the manifest schema when the
    manifest is loaded.
    """
    raw_path = os.environ.get(PLUGIN_CONFIG_ENV)
    if raw_path is None or not raw_path.strip():
        return {}
    path = raw_path.strip()
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as error:
        raise ConfigError(path, f'cannot be read: {error.strerror or error}') from error
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ConfigError(path, f'is not valid JSON: {error}') from error
    if not isinstance(data, dict):
        raise ConfigError('plugins', f'expects a JSON object, got {type(data).__name__}')
    return {name: _config_entry(name, value) for name, value in data.items()}


def _config_entry(name: str, value: Any) -> dict[str, Any]:
    """Validate one plugin entry and keep only the keys it declared."""
    subject = f'plugin.{name}'
    if not isinstance(value, Mapping):
        raise ConfigError(subject, f'expects an object, got {type(value).__name__}')
    for key in value:
        if key not in _ENTRY_KEYS:
            raise ConfigError(f'{subject}.{key}', 'is not a known field')
    entry: dict[str, Any] = {}
    if 'enabled' in value:
        enabled = value['enabled']
        if not isinstance(enabled, bool):
            raise ConfigError(f'{subject}.enabled',
                              f'expected bool, got {type(enabled).__name__}')
        entry['enabled'] = enabled
    if 'config' in value:
        config = value['config']
        if config is not None and not isinstance(config, Mapping):
            raise ConfigError(f'{subject}.config',
                              f'expects an object, got {type(config).__name__}')
        entry['config'] = dict(config) if config is not None else None
    return entry


def _apply_entry(manifest: ParserPluginManifest, entry: dict[str, Any] | None,
                 loaded_from: str) -> LoadedPlugin:
    """Apply one config entry to ``manifest`` and describe the result."""
    config = None
    if entry is not None:
        if 'enabled' in entry:
            manifest.enabled = entry['enabled']
        if 'config' in entry:
            config = manifest.validate_config(entry['config'])
    return LoadedPlugin(manifest=manifest, loaded_from=loaded_from, config=config)


def _require_known(entries: dict[str, dict[str, Any]], context: Context) -> None:
    """Refuse a config entry that names no loaded plugin - after discovery, not before."""
    known = {manifest.name for manifest in context.parser.list()}
    for name in entries:
        if name not in known:
            raise ConfigError(f'plugin.{name}', 'is not a loaded plugin')


# --------------------------------------------------------------------------------------
# Explicit-path discovery
# --------------------------------------------------------------------------------------

def _plugin_files(raw_path: str | None) -> tuple[Path, ...]:
    """Every top-level ``*.py`` of the configured directories, deduplicated and resolved.

    A missing directory is a misconfiguration, not an empty plugin path: silently
    discovering nothing would look exactly like "no external plugins configured". Private
    modules (``_helper.py``) are skipped, and subdirectories are not scanned - the plugin
    path names the plugins, not a package tree.
    """
    roots: dict[Path, None] = {}
    for entry in (raw_path or '').split(os.pathsep):
        candidate = entry.strip()
        if not candidate:
            continue
        root = Path(candidate)
        if not root.is_dir():
            raise ConfigError(candidate, 'plugin path is not a directory')
        roots.setdefault(root.resolve(), None)
    files: dict[Path, None] = {}
    for root in roots:
        for path in sorted(root.glob('*.py')):
            if not path.name.startswith('_'):
                files.setdefault(path.resolve(), None)
    return tuple(files)


def _discover(context: Context, entries: dict[str, dict[str, Any]]) -> list[LoadedPlugin]:
    """Load every discovered module, skipping and logging the ones that fail."""
    records: list[LoadedPlugin] = []
    failures: list[tuple[Path, BaseException]] = []
    for path in _plugin_files(os.environ.get(PLUGIN_PATH_ENV)):
        try:
            records.extend(_load_external(context, path, entries))
        except ConfigError:
            raise
        except Exception as error:  # noqa: BLE001 - one plugin must not stop the others
            failures.append((path, error))
            log.warning('external plugin %s failed to load and was skipped: %s', path, error)
    if failures:
        log.warning('%d external plugin module(s) failed: %s', len(failures),
                    ', '.join(str(path) for path, _ in failures))
    return records


def _import_file(path: Path) -> Any:
    """Import one plugin file under a synthetic name derived from its absolute path."""
    name = ('datamind_external_plugin_'
            + hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:16])
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot create an import spec for {path}')
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so a module that imports itself or defines a dataclass
    # resolves; removed again on failure so a retry does not see half a module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _load_external(context: Context, path: Path,
                   entries: dict[str, dict[str, Any]]) -> list[LoadedPlugin]:
    """Load one module's declarations into ``context``; raise rather than half-apply.

    Two declaration shapes are accepted. A callable ``register(ctx)`` owns its own
    registration, so its declarations are whatever new names it left in the registry. A
    module-level ``MANIFEST`` (one manifest or an iterable of them) is registered here,
    which lets a failure dispose the manifests this module already claimed.
    """
    module = _import_file(path)
    registry = context.parser
    loaded_from = str(path)
    register = getattr(module, 'register', None)
    if callable(register):
        before = {manifest.name for manifest in registry.list()}
        register(context)
        manifests = [manifest for manifest in registry.list() if manifest.name not in before]
        if not manifests:
            raise PluginError(f'{path} register(ctx) declared no parser manifest')
        return [_apply_entry(manifest, entries.get(manifest.name), loaded_from)
                for manifest in manifests]

    disposers = []
    records: list[LoadedPlugin] = []
    try:
        for manifest in _declared_manifests(module, path):
            record = _apply_entry(manifest, entries.get(manifest.name), loaded_from)
            disposers.append(registry.register(manifest))
            records.append(record)
    except BaseException:
        for dispose in reversed(disposers):
            dispose()
        raise
    return records


def _declared_manifests(module: Any, path: Path) -> tuple[ParserPluginManifest, ...]:
    """The manifests a module-level ``MANIFEST`` declares, refusing malformed ones."""
    declared = getattr(module, 'MANIFEST', None)
    if declared is None:
        raise PluginError(f'{path} defines neither a callable register(ctx) nor MANIFEST')
    if isinstance(declared, ParserPluginManifest):
        return (declared,)
    try:
        manifests = tuple(declared)
    except TypeError as error:
        raise PluginError(
            f'{path} MANIFEST must be a ParserPluginManifest or an iterable of them, '
            f'got {type(declared).__name__}') from error
    for manifest in manifests:
        if not isinstance(manifest, ParserPluginManifest):
            raise PluginError(
                f'{path} MANIFEST contains {type(manifest).__name__}, not a '
                'ParserPluginManifest')
    if not manifests:
        raise PluginError(f'{path} MANIFEST declares no manifest')
    return manifests
