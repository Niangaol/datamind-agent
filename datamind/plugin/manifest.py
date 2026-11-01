"""Parser plugin manifests and the media-type/extension registry service.

A manifest is the declarative half of a parser plugin: which file types it claims, whether
it is enabled, how its loader is reached, and what config it accepts. The registry is the
runtime half: it answers "who parses this?" for a media type or a file extension, refuses
two plugins claiming the same thing, and hands every registration a disposer so unloading a
plugin frees the names it took.

Two rules shape the code below:

*nothing is imported early*
    ``loader`` is a callable or a ``"module:function"`` reference that stays unresolved
    until :meth:`ParserPluginManifest.resolve` is called. Registering, listing, looking up
    and asking for capabilities never import a parser dependency (G7), so a listing
    endpoint cannot pull ``pymupdf`` or ``ezdxf`` into the API process.

*an extension is a hint, a media type is an identity*
    Signature sniffing still decides what a file really is
    (``parse_contract.detect_media_type``, G3); an extension only routes a declared hint
    and is therefore optional. A manifest may declare media types alone, which is what the
    OHR-Bench JSON plugin does (G11).

The module is standard library only (G2): it does not import ``datamind.parse_contract``
(which needs fastapi) or anything else from the application, and it raises
:class:`ManifestError` carrying the contract's stable error codes (G12) so an API layer can
map it with :meth:`ManifestError.as_detail`.

Example::

    from datamind.plugin import Context
    from datamind.plugin.manifest import ParserPluginManifest, ParserRegistry

    ctx = Context()
    ctx.set('parser', ParserRegistry())             # registered as the contextual service

    dispose = ctx.parser.register(ParserPluginManifest(
        'pdf', '1.0.0', ['application/pdf'], ['.pdf'],
        capabilities=['native_text'], loader='ingestion.plugins.pdf_plugin:parse'))

    ctx.parser.lookup('application/pdf').resolve()  # imports the loader only now
    dispose()                                       # the media type and extension are free
"""
from __future__ import annotations

import importlib
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .context import PluginError, validate_config as _kernel_validate_config

__all__ = [
    'ManifestError',
    'PARSE_ADAPTER_UNKNOWN',
    'PARSE_ADAPTER_UNSUPPORTED',
    'PARSE_RESULT_INVALID',
    'ParserPluginManifest',
    'ParserRegistry',
]

# The stable codes of the parse contract (G12). A declaration or lookup failure reuses the
# code the HTTP layer already emits for the same condition instead of inventing a new one.
PARSE_RESULT_INVALID = 'PARSE_RESULT_INVALID'
PARSE_ADAPTER_UNKNOWN = 'PARSE_ADAPTER_UNKNOWN'
PARSE_ADAPTER_UNSUPPORTED = 'PARSE_ADAPTER_UNSUPPORTED'

_MEDIA_TYPE = re.compile(r'^[^\s/;,]+/[^\s/;,]+$')
_EXTENSION = re.compile(r'^\.[a-z0-9][a-z0-9._+-]*$')
_LOADER_REFERENCE = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$')
# The kernel's declarative config spec keys; a manifest schema may use these and no others.
_SCHEMA_SPEC_KEYS = frozenset({'type', 'default', 'required', 'choices'})


class ManifestError(PluginError):
    """A manifest declaration or a registry lookup failed.

    ``error_code`` is one of the parse contract's stable codes and ``details`` carries the
    context (``subject``/``reason`` plus the offending value), so the caller maps it to the
    HTTP error it already emits for that condition::

        raise HTTPException(422, detail=error.as_detail())

    :meth:`as_detail` builds exactly the ``{"error_code": ...}`` body ``fail`` in
    ``parse_contract`` produces, without importing it.
    """

    def __init__(self, error_code: str, message: str, **details: Any) -> None:
        self.error_code = error_code
        self.details = details
        super().__init__(message)

    def as_detail(self) -> dict[str, Any]:
        """The ``detail`` payload of the equivalent contract failure."""
        detail: dict[str, Any] = {'error_code': self.error_code}
        if self.details:
            detail['details'] = dict(self.details)
        return detail


@dataclass
class ParserPluginManifest:
    """One parser plugin's declaration: what it claims and how to reach its loader.

    Every field is normalized and validated on construction, so a manifest that exists is
    well formed: ``name``/``version`` are stripped non-empty text, ``media_types`` are
    lowercase ``type/subtype`` strings (at least one, no duplicates), ``extensions`` are
    lowercase with a leading dot (optional, because a signature-only format has none),
    ``capabilities`` are non-empty strings with no central vocabulary to satisfy, and
    ``loader`` is a callable or a ``"module:function"`` reference that is *not* imported
    here.

    ``priority`` is declarative metadata for ordering decisions a later task makes; the
    registry does not act on it, because two plugins claiming one media type is a conflict,
    not a precedence question.

    ``enabled`` defaults to True. It is the seam plugin config flips (plan Task 6): a
    disabled manifest stays registered and known - ``known_extensions`` can still report
    its extensions - while every lookup refuses it.
    """

    name: str
    version: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = field(kw_only=True)
    loader: Callable[..., Any] | str = field(kw_only=True)
    priority: int = field(default=100, kw_only=True)
    config_schema: Mapping[str, Any] | None = field(default=None, kw_only=True)
    enabled: bool = field(default=True, kw_only=True)

    def __post_init__(self) -> None:
        self.name = _declared_text('name', self.name)
        self.version = _declared_text('version', self.version)
        self.media_types = _declared_media_types(self.media_types)
        self.extensions = _declared_extensions(self.extensions)
        self.capabilities = _declared_capabilities(self.capabilities)
        self.loader = _declared_loader(self.loader)
        self.priority = _declared_priority(self.priority)
        self.config_schema = _declared_config_schema(self.config_schema)
        self.enabled = _declared_enabled(self.enabled)

    def validate_config(self, config: Any = None) -> dict[str, Any]:
        """Validate ``config`` against :attr:`config_schema` through the kernel's validator.

        Failures are :class:`~datamind.plugin.ConfigError` and name the field path prefixed
        with the plugin, e.g. ``plugin.pdf.pages``. A manifest without a schema accepts any
        mapping (``validate_config``'s ``schema=None`` behaviour).
        """
        return _kernel_validate_config(self.config_schema, config, path=f'plugin.{self.name}')

    def resolve(self) -> Callable[..., Any]:
        """Return the loader callable, importing a ``"module:function"`` reference now.

        This is the only place a manifest touches :mod:`importlib`; the registry never
        calls it, which is what keeps listing and lookup free of parser imports.
        """
        loader = self.loader
        if callable(loader):
            return loader
        module_name, _, attribute = loader.partition(':')
        try:
            target: Any = importlib.import_module(module_name)
            for part in attribute.split('.'):
                target = getattr(target, part)
        except (ImportError, AttributeError) as error:
            raise ManifestError(
                PARSE_RESULT_INVALID,
                f'plugin {self.name!r} cannot resolve its loader {loader!r}: {error}',
                subject='manifest.loader', reason='unresolvable_loader', loader=loader) from error
        if not callable(target):
            raise ManifestError(
                PARSE_RESULT_INVALID,
                f'plugin {self.name!r} loader {loader!r} resolves to {type(target).__name__}, '
                'which is not callable',
                subject='manifest.loader', reason='loader_not_callable', loader=loader)
        return target


class ParserRegistry:
    """The ``parser`` service: which plugin claims a media type or a file extension.

    The registry is plain data plus a lock. It never imports a loader and never calls one,
    so listing the plugins of a process is inexpensive and needs no parser dependency.

    It is a pure table: it is not bound to a context, so it cannot know which scope called
    it. Register it as the context's service, and let the registering scope own the
    disposer - that is what makes a plugin unload (or a failed load's rollback) free the
    media types it claimed::

        ctx = Context()
        ctx.set('parser', ParserRegistry())

        def pdf_plugin(inner, config):
            inner.effect(inner.parser.register(ParserPluginManifest(...)))

        handle = ctx.plugin(pdf_plugin)
        handle.dispose()          # the manifest is gone with the plugin

    A manifest may claim several media types; each of its media types and extensions must
    be free, and its name must be free as well, because :meth:`list`/:meth:`capabilities`
    are keyed by name. Every declaration is checked before anything is inserted, so a
    refused manifest leaves the registry exactly as it was.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manifests: dict[str, ParserPluginManifest] = {}
        self._media_types: dict[str, str] = {}
        self._extensions: dict[str, str] = {}

    # -- registration ----------------------------------------------------------

    def register(self, manifest: ParserPluginManifest) -> Callable[[], None]:
        """Claim ``manifest``'s media types and extensions; return the disposer that frees them.

        Conflicts (a duplicate plugin name, media type or extension) raise
        :class:`ManifestError` with ``PARSE_RESULT_INVALID`` and insert nothing. The
        disposer removes the manifest and every name it took; running it twice is a no-op
        and a later registration of the same names then succeeds, which is what a plugin
        reload needs.
        """
        if not isinstance(manifest, ParserPluginManifest):
            raise ManifestError(
                PARSE_RESULT_INVALID,
                f'register() expects a ParserPluginManifest, got {type(manifest).__name__}',
                subject='parser', reason='manifest_required', value=repr(manifest))
        media_types, extensions = manifest.media_types, manifest.extensions
        with self._lock:
            self._require_available(manifest)
            self._manifests[manifest.name] = manifest
            for media_type in media_types:
                self._media_types[media_type] = manifest.name
            for extension in extensions:
                self._extensions[extension] = manifest.name

        def dispose() -> None:
            with self._lock:
                # Identity, not just the name: a replacement registered after this disposer
                # was handed out must survive a late second call.
                if self._manifests.get(manifest.name) is not manifest:
                    return
                del self._manifests[manifest.name]
                for media_type in media_types:
                    if self._media_types.get(media_type) == manifest.name:
                        del self._media_types[media_type]
                for extension in extensions:
                    if self._extensions.get(extension) == manifest.name:
                        del self._extensions[extension]

        return dispose

    def _require_available(self, manifest: ParserPluginManifest) -> None:
        """Refuse a manifest whose name, media type or extension is taken; insert nothing."""
        if manifest.name in self._manifests:
            raise ManifestError(
                PARSE_RESULT_INVALID,
                f'plugin {manifest.name!r} is already registered',
                subject='parser', reason='plugin_already_registered', plugin=manifest.name)
        for media_type in manifest.media_types:
            owner = self._media_types.get(media_type)
            if owner is not None:
                raise ManifestError(
                    PARSE_RESULT_INVALID,
                    f'media type {media_type!r} is already registered by plugin {owner!r}',
                    subject='parser', reason='media_type_already_registered',
                    media_type=media_type, plugin=owner)
        for extension in manifest.extensions:
            owner = self._extensions.get(extension)
            if owner is not None:
                raise ManifestError(
                    PARSE_RESULT_INVALID,
                    f'extension {extension!r} is already registered by plugin {owner!r}',
                    subject='parser', reason='extension_already_registered',
                    extension=extension, plugin=owner)

    # -- lookup ----------------------------------------------------------------

    def lookup(self, media_type: str) -> ParserPluginManifest:
        """The enabled plugin claiming ``media_type`` (compared case-insensitively).

        An unknown media type raises ``PARSE_ADAPTER_UNKNOWN``; one declared only by a
        disabled manifest raises ``PARSE_ADAPTER_UNSUPPORTED`` with ``reason='plugin_disabled'``,
        so a caller can tell "nothing handles this" from "this plugin is switched off".
        """
        key = _media_type_key(media_type)
        if key is None:
            raise ManifestError(
                PARSE_ADAPTER_UNKNOWN, f'{media_type!r} is not a media type',
                subject='parser', reason='invalid_media_type', media_type=repr(media_type))
        with self._lock:
            owner = self._media_types.get(key)
            manifest = self._manifests.get(owner) if owner is not None else None
        if manifest is None:
            raise ManifestError(
                PARSE_ADAPTER_UNKNOWN, f'no parser plugin declares media type {key!r}',
                subject='parser', reason='media_type_not_registered', media_type=key)
        self._require_enabled(manifest, media_type=key)
        return manifest

    def lookup_by_extension(self, extension: str) -> ParserPluginManifest:
        """The enabled plugin claiming ``extension``, with or without its leading dot.

        Refusals match :meth:`lookup`: unknown is ``PARSE_ADAPTER_UNKNOWN``, disabled is
        ``PARSE_ADAPTER_UNSUPPORTED``. An extension is only a declared hint (G3) - a caller
        that wants to know what a file really is sniffs its signature instead.
        """
        key = _extension_key(extension)
        if key is None:
            raise ManifestError(
                PARSE_ADAPTER_UNKNOWN, f'{extension!r} is not a file extension',
                subject='parser', reason='invalid_extension', extension=repr(extension))
        with self._lock:
            owner = self._extensions.get(key)
            manifest = self._manifests.get(owner) if owner is not None else None
        if manifest is None:
            raise ManifestError(
                PARSE_ADAPTER_UNKNOWN, f'no parser plugin declares extension {key!r}',
                subject='parser', reason='extension_not_registered', extension=key)
        self._require_enabled(manifest, extension=key)
        return manifest

    def _require_enabled(self, manifest: ParserPluginManifest, **claimed: str) -> None:
        if manifest.enabled:
            return
        claim = ', '.join(f'{name}={value!r}' for name, value in claimed.items())
        raise ManifestError(
            PARSE_ADAPTER_UNSUPPORTED,
            f'plugin {manifest.name!r} claims {claim} but is disabled',
            subject='parser', reason='plugin_disabled', plugin=manifest.name, **claimed)

    # -- introspection ---------------------------------------------------------

    def known_extensions(self, include_disabled: bool = True) -> tuple[str, ...]:
        """Every registered extension, sorted; ``include_disabled=False`` hides disabled ones.

        The default reports what is declared, not what is currently selectable: plugin
        config that disables a manifest still leaves its extensions listed by inventory
        callers while :meth:`lookup_by_extension` refuses them.
        """
        with self._lock:
            known = [extension for extension, owner in self._extensions.items()
                     if include_disabled or self._manifests[owner].enabled]
        return tuple(sorted(known))

    def list(self) -> tuple[ParserPluginManifest, ...]:
        """Registered manifests - enabled and disabled - in registration order."""
        with self._lock:
            return tuple(self._manifests.values())

    def capabilities(self, name: str | None = None) -> tuple[str, ...] | dict[str, tuple[str, ...]]:
        """Capabilities one plugin declares, or a name-to-capabilities snapshot of all.

        Capabilities are declarative and stay reportable for a disabled plugin; ``enabled``
        only governs lookup. A name that is not registered raises ``PARSE_ADAPTER_UNKNOWN``.
        """
        with self._lock:
            if name is None:
                return {manifest.name: manifest.capabilities
                        for manifest in self._manifests.values()}
            key = name.strip() if isinstance(name, str) else name
            manifest = self._manifests.get(key)
        if manifest is None:
            raise ManifestError(
                PARSE_ADAPTER_UNKNOWN, f'no parser plugin is registered as {name!r}',
                subject='parser', reason='plugin_not_registered', plugin=repr(name))
        return manifest.capabilities

    def adapter_name(self, extension: str) -> str:
        """Name of the plugin that parses ``extension`` - the adapter name a record carries."""
        return self.lookup_by_extension(extension).name


# -- declaration validation ----------------------------------------------------


def _declaration_error(subject: str, reason: str, message: str, **details: Any) -> ManifestError:
    return ManifestError(PARSE_RESULT_INVALID, message, subject=subject, reason=reason, **details)


def _declared_text(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _declaration_error(
            f'manifest.{field_name}', 'must_be_text',
            f'manifest {field_name} must be a non-empty string, got {value!r}')
    return value.strip()


def _declared_sequence(field_name: str, value: Any) -> tuple[Any, ...]:
    # A bare string is the common mistake ("media_types='application/pdf'") and would
    # silently iterate its characters, so it is refused instead of unpacked.
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _declaration_error(
            f'manifest.{field_name}', 'must_be_a_sequence',
            f'manifest {field_name} must be a list or tuple, got {type(value).__name__}')
    return tuple(value)


def _declared_media_types(value: Any) -> tuple[str, ...]:
    items = _declared_sequence('media_types', value)
    if not items:
        raise _declaration_error('manifest.media_types', 'must_not_be_empty',
                                 'manifest media_types must declare at least one media type')
    normalized = []
    for item in items:
        key = _media_type_key(item)
        if key is None:
            raise _declaration_error(
                'manifest.media_types', 'invalid_media_type',
                f'invalid media type {item!r}: expected "type/subtype"', value=repr(item))
        normalized.append(key)
    if len(set(normalized)) != len(normalized):
        raise _declaration_error(
            'manifest.media_types', 'duplicate_media_type',
            f'manifest media_types repeats one of {sorted(set(normalized))!r}',
            media_types=tuple(normalized))
    return tuple(normalized)


def _declared_extensions(value: Any) -> tuple[str, ...]:
    items = _declared_sequence('extensions', value)
    normalized = []
    for item in items:
        key = _extension_key(item)
        if key is None:
            raise _declaration_error(
                'manifest.extensions', 'invalid_extension',
                f'invalid extension {item!r}: expected a non-empty string such as ".pdf"',
                value=repr(item))
        normalized.append(key)
    if len(set(normalized)) != len(normalized):
        raise _declaration_error(
            'manifest.extensions', 'duplicate_extension',
            f'manifest extensions repeats one of {sorted(set(normalized))!r}',
            extensions=tuple(normalized))
    return tuple(normalized)


def _declared_capabilities(value: Any) -> tuple[str, ...]:
    items = _declared_sequence('capabilities', value)
    if not items:
        raise _declaration_error('manifest.capabilities', 'must_not_be_empty',
                                 'manifest capabilities must declare at least one capability')
    normalized = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise _declaration_error(
                'manifest.capabilities', 'capability_must_be_text',
                f'invalid capability {item!r}: expected a non-empty string', value=repr(item))
        normalized.append(item.strip())
    return tuple(normalized)


def _declared_loader(value: Any) -> Callable[..., Any] | str:
    if callable(value):
        return value
    if isinstance(value, str) and _LOADER_REFERENCE.match(value.strip()):
        return value.strip()
    raise _declaration_error(
        'manifest.loader', 'invalid_loader',
        f'manifest loader must be a callable or a "module:function" reference, got {value!r}',
        value=repr(value))


def _declared_priority(value: Any) -> int:
    # bool is an int subclass; ``priority=True`` is a declaration bug, not a priority of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _declaration_error(
            'manifest.priority', 'must_be_an_integer',
            f'manifest priority must be an integer, got {type(value).__name__}', value=repr(value))
    return value


def _declared_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _declaration_error(
            'manifest.enabled', 'must_be_a_boolean',
            f'manifest enabled must be a boolean, got {type(value).__name__}', value=repr(value))
    return value


def _declared_config_schema(value: Any) -> Mapping[str, Any] | None:
    """Accept a flat declarative schema, refusing spec keys the kernel would ignore."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _declaration_error(
            'manifest.config_schema', 'must_be_a_mapping',
            f'manifest config_schema must be a mapping, got {type(value).__name__}')
    for field_name, spec in value.items():
        subject = f'manifest.config_schema.{field_name}'
        if spec is None:
            continue
        if not isinstance(spec, Mapping):
            raise _declaration_error(
                subject, 'field_spec_must_be_a_mapping',
                f'{subject} must be a mapping, got {type(spec).__name__}')
        unknown = sorted(set(spec) - _SCHEMA_SPEC_KEYS)
        if unknown:
            raise _declaration_error(
                subject, 'unknown_field_spec_key',
                f'{subject} uses unknown spec key(s) {", ".join(unknown)}; '
                f'expected {", ".join(sorted(_SCHEMA_SPEC_KEYS))}', keys=tuple(unknown))
    return value


# -- key normalization ---------------------------------------------------------


def _media_type_key(value: Any) -> str | None:
    """Lowercase ``type/subtype``, or None when ``value`` is not a media type."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _MEDIA_TYPE.match(candidate) else None


def _extension_key(value: Any) -> str | None:
    """Lowercase extension with a leading dot, or None when ``value`` is not one."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not candidate.startswith('.'):
        candidate = f'.{candidate}'
    return candidate if _EXTENSION.match(candidate) else None
