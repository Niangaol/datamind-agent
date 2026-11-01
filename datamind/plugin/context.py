"""Cordis-style plugin context: services, injection, reversible effects, disposal.

This is a Python-native kernel with Cordis' *semantics*, not its API: there is no npm
``cordis``, no Node process and no sidecar (G1), and nothing here imports anything
outside the standard library (G2). The differences from npm cordis are deliberate:

* everything is synchronous; ``await``-based cordis dispatchers map onto the
  :class:`~datamind.plugin.events.EventBus` methods (``emit``/``parallel``/``serial``/
  ``waterfall``/``bail``);
* services are reached with ``ctx.get(name)`` or attribute access (``ctx.parser``),
  not through JavaScript property proxies;
* ``inject`` is explicit and never silently postponed: a plugin whose declared
  dependencies are missing fails its load with :class:`InjectError` instead of waiting.

The three ideas that matter downstream:

*reversible effects*
    Every mutation (service registration, event listener, arbitrary cleanup) is applied
    together with a disposer and recorded through :meth:`Context.effect`. Disposal runs
    the disposers in reverse order, so a context is a stack of undo actions.

*scoped loading*
    :meth:`Context.plugin` applies a plugin on a fresh child context. A successful load
    leaves the child attached to its parent; a failed load disposes the child, which
    rolls back exactly the effects that load applied and nothing else.

*shared surface, per-plugin ownership*
    Services and events are stored on the root context and therefore visible to
    everyone, while the *disposers* that remove them belong to the loading context. That
    is what makes unloading a plugin remove its services and listeners while the rest of
    the application keeps running.
"""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .events import EventBus

__all__ = [
    'ConfigError',
    'Context',
    'InjectError',
    'PluginError',
    'ServiceConflictError',
    'ServiceNotFoundError',
    'validate_config',
]

_MISSING = object()


class PluginError(Exception):
    """Base class for plugin kernel failures."""


class ServiceConflictError(PluginError):
    """A service name is already taken; the existing service must be disposed first."""


class ServiceNotFoundError(PluginError):
    """A service was requested that no context has registered."""


class InjectError(PluginError):
    """A plugin load or an injection needs services that are not registered."""

    def __init__(self, missing: Sequence[str], *, detail: str = '') -> None:
        self.missing = tuple(missing)
        message = f'missing dependencies: {", ".join(self.missing)}'
        super().__init__(f'{message}; {detail}' if detail else message)


class ConfigError(PluginError):
    """A plugin config violates its declarative schema; ``path`` names the field."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f'{path}: {reason}')


@dataclass(eq=False)
class _PendingInjection:
    owner: 'Context'
    deps: tuple[str, ...]
    callback: Callable[['Context'], Any]


class Context:
    """A plugin scope: services, events, reversible effects and child plugins.

    A context with no parent is the root: it owns the service table, the event bus and
    the pending-injection list. Child contexts (one per loaded plugin) share those
    three objects but own their own disposer stack, which is what makes loading and
    unloading a plugin locally reversible.

    Handlers registered through :meth:`on` are called with exactly the arguments given
    to :meth:`emit` (and friends); the context is *not* injected into the call.
    """

    def __init__(self, parent: 'Context | None' = None) -> None:
        """Create a root context, or a child scope owned by ``parent``.

        A child shares the parent's services, event bus and pending injections, and is
        registered as one of the parent's plugins, so disposing the parent unwinds it.
        Prefer :meth:`plugin`, which also applies a plugin on the child.
        """
        self._parent = parent
        self._lock = threading.RLock()
        self._disposers: list[Callable[[], Any]] = []
        self._plugins: list['Context'] = []
        self._disposed = False
        if parent is None:
            self._root: Context = self
            self._services: dict[str, Any] = {}
            self._bus = EventBus()
            self._pending: list[_PendingInjection] = []
            self._ready = True
        else:
            self._root = parent._root
            self._services = parent._services
            self._bus = parent._bus
            self._pending = parent._pending
            self._ready = False
            with parent._lock:
                parent._plugins.append(self)

    # -- introspection ---------------------------------------------------------

    @property
    def parent(self) -> 'Context | None':
        """The context this one was loaded from, or ``None`` for the root."""
        return self._parent

    @property
    def ready(self) -> bool:
        """True once the plugin applied on this context finished loading."""
        return self._ready

    @property
    def disposed(self) -> bool:
        """True once :meth:`dispose` started; effects from then on run immediately."""
        return self._disposed

    @property
    def service_names(self) -> tuple[str, ...]:
        """Names of the services currently registered, in registration order."""
        with self._lock:
            return tuple(self._services)

    def plugins(self) -> tuple['Context', ...]:
        """The plugin contexts currently loaded under this one, in load order."""
        with self._lock:
            return tuple(self._plugins)

    # -- services --------------------------------------------------------------

    def set(self, name: str, value: Any) -> Callable[[], None]:
        """Register ``value`` as service ``name``; returns the disposer that removes it.

        The registration is recorded as an effect of *this* context, so disposing this
        context (or the plugin that owns it) frees the name again. Reusing a name that
        is still taken raises :class:`ServiceConflictError` instead of silently
        replacing another plugin's service - a replacement must be an explicit
        dispose-then-set by whoever owns the first registration.
        """
        _require_service_name(name)
        with self._lock:
            existing = self._services.get(name, _MISSING)
            if existing is not _MISSING:
                raise ServiceConflictError(
                    f'service {name!r} is already registered as {existing!r}; '
                    'dispose the existing registration before replacing it')
            self._services[name] = value

        def dispose() -> None:
            with self._lock:
                if self._services.get(name, _MISSING) is value:
                    del self._services[name]

        self.effect(dispose)
        self._resolve_pending()
        return dispose

    def get(self, name: str) -> Any:
        """Return the service registered as ``name``.

        Raises :class:`ServiceNotFoundError` naming the services that *do* exist, so a
        misspelled dependency is diagnosable from the message alone.
        """
        with self._lock:
            try:
                return self._services[name]
            except KeyError:
                known = ', '.join(sorted(self._services)) or 'none'
                raise ServiceNotFoundError(
                    f'service {name!r} is not registered; registered services: {known}') from None

    def has(self, name: str) -> bool:
        """True when a service called ``name`` is registered."""
        with self._lock:
            return name in self._services

    # -- effects ---------------------------------------------------------------

    def effect(self, disposer: Callable[[], Any]) -> Callable[[], Any]:
        """Record ``disposer`` as a reversible effect of this context and return it.

        Disposers run in reverse order on :meth:`dispose`. A context that is already
        disposed applies nothing: the disposer runs immediately and the effect is
        therefore a no-op, which keeps late registrations from leaking.
        """
        if not callable(disposer):
            raise TypeError(f'effect() expects a callable disposer, got {type(disposer).__name__}')
        with self._lock:
            disposed = self._disposed
            if not disposed:
                self._disposers.append(disposer)
        if disposed:
            disposer()
        return disposer

    def dispose(self) -> None:
        """Unwind this context: loaded plugins first, then its own effects in reverse.

        Idempotent - the second call is a no-op. Every disposer gets to run even when
        one raises; the first error is re-raised afterwards so a broken cleanup cannot
        strand the remaining ones. A ``dispose`` event is emitted once, with this
        context, before anything is unwound; a listener that raises joins the collected
        errors instead of stopping the unwind. The context also removes itself from its
        parent's plugin list.
        """
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            children = list(self._plugins)
            self._plugins.clear()
            disposers = list(self._disposers)
            self._disposers.clear()

        parent = self._parent
        if parent is not None:
            with parent._lock:
                if self in parent._plugins:
                    parent._plugins.remove(self)

        errors: list[BaseException] = []
        try:
            self._bus.emit('dispose', self)
        except BaseException as exc:
            errors.append(exc)
        for child in reversed(children):
            try:
                child.dispose()
            except BaseException as exc:
                errors.append(exc)
        for disposer in reversed(disposers):
            try:
                disposer()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    # -- injection -------------------------------------------------------------

    def inject(self, deps: Sequence[str], callback: Callable[['Context'], Any]) -> Callable[[], None]:
        """Run ``callback(self)`` as soon as every service in ``deps`` exists.

        If the dependencies are already registered the callback runs immediately (before
        ``inject`` returns); otherwise it is queued and runs when the last missing one is
        registered, in registration order among the callbacks waiting for that service.
        The canonical use is a plugin that wants to react to a service it cannot require:
        a plugin that *cannot* work without it should declare ``inject`` instead, so the
        load fails loudly (see :class:`InjectError`).

        Returns a disposer that cancels a still-pending callback; disposing this context
        also cancels it. Cancelling an already-run callback does nothing. An exception
        raised by a deferred callback propagates out of the ``set`` that satisfied it.
        """
        names = _normalize_dependencies(deps)
        if not callable(callback):
            raise TypeError(f'inject() expects a callable, got {type(callback).__name__}')
        pending = _PendingInjection(owner=self, deps=names, callback=callback)
        with self._lock:
            satisfied = not self._disposed and all(dep in self._services for dep in names)
            if not satisfied and not self._disposed:
                self._pending.append(pending)

        def cancel() -> None:
            with self._lock:
                if pending in self._pending:
                    self._pending.remove(pending)

        self.effect(cancel)
        if satisfied:
            callback(self)
        return cancel

    def _resolve_pending(self) -> None:
        """Run every deferred injection whose dependencies are now all registered.

        Called after a successful :meth:`set`. Callbacks run outside the lock and in
        registration order; a callback that registers more services re-enters here.
        """
        while True:
            with self._lock:
                if self._disposed or not self._pending:
                    return
                ready = [p for p in self._pending
                         if all(dep in self._services for dep in p.deps)]
                if not ready:
                    return
                for entry in ready:
                    self._pending.remove(entry)
            for entry in ready:
                if entry.owner.disposed:
                    continue
                entry.callback(entry.owner)

    # -- plugins ---------------------------------------------------------------

    def plugin(self, plugin: Any, config: Any = None, *, schema: Mapping[str, Any] | None = None) -> 'Context':
        """Load ``plugin`` on a fresh child context and return that context.

        ``plugin`` may be a function ``apply(ctx, config)``, an object exposing
        ``apply(ctx, config)``, or a class; a class is instantiated without arguments and
        then treated as an object. ``config`` is validated first, against ``schema`` if
        given, else against the plugin's own ``config_schema`` attribute (if any); see
        :func:`validate_config`. An ``inject`` attribute on the plugin lists service
        names that must already exist - a load that cannot resolve them raises
        :class:`InjectError` before anything is applied.

        The returned context is the plugin's handle: its ``ready`` is True, its
        ``parent`` is this context, and ``handle.dispose()`` unloads the plugin, undoing
        its services, listeners and effects in reverse order. If the plugin raises, the
        child context is disposed (rolling back only that load's effects) and the
        original exception is re-raised unchanged.
        """
        if self._disposed:
            raise PluginError('this context is disposed; a plugin can no longer be loaded into it')
        applier, declared_schema, dependencies = _resolve_plugin(plugin)
        validated = validate_config(schema if schema is not None else declared_schema, config)
        missing = [dep for dep in dependencies if not self.has(dep)]
        if missing:
            raise InjectError(missing, detail=f'plugin {_plugin_name(plugin)!r} declares them as inject')

        child = Context(parent=self)
        try:
            applier(child, validated)
        except BaseException:
            # Roll back this load only; a rollback error must not hide the real one.
            _dispose_quietly(child)
            raise

        with self._lock:
            parent_disposed = self._disposed
            if not parent_disposed:
                child._ready = True
        if parent_disposed:
            _dispose_quietly(child)
            raise PluginError('the parent context was disposed while the plugin was loading')
        try:
            self._bus.emit('ready', child)
        except BaseException:
            # A ready listener is part of the load: if one rejects the plugin, undo the
            # load rather than hand back a context nobody can reach.
            _dispose_quietly(child)
            raise
        return child

    # -- events ----------------------------------------------------------------

    def on(self, name: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """Register an event handler as an effect of this context; returns its disposer."""
        disposer = self._bus.on(name, handler)
        self.effect(disposer)
        return disposer

    def off(self, name: str, handler: Callable[..., Any]) -> None:
        """Remove an event handler registered for ``name`` (a missing one is a no-op)."""
        self._bus.off(name, handler)

    def emit(self, name: str, *args: Any) -> None:
        """Call every handler for ``name`` in order, discarding results; errors escape."""
        self._bus.emit(name, *args)

    def parallel(self, name: str, *args: Any, max_workers: int | None = None) -> list[Any]:
        """Call every handler for ``name`` concurrently; results follow listener order."""
        return self._bus.parallel(name, *args, max_workers=max_workers)

    def serial(self, name: str, *args: Any) -> Any:
        """Call handlers in order; return the first non-``None`` result, else ``None``."""
        return self._bus.serial(name, *args)

    def waterfall(self, name: str, *args: Any) -> Any:
        """Thread the first argument through the handlers; return the final value."""
        return self._bus.waterfall(name, *args)

    def bail(self, name: str, *args: Any) -> Any:
        """Like :meth:`serial` but raise :class:`BailError` when nothing answers."""
        return self._bus.bail(name, *args)

    # -- attribute access ------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Expose services as attributes, so ``ctx.parser`` means ``ctx.get('parser')``.

        Only reached for attributes the context does not define. Private names are not
        routed to services, and an unknown service surfaces as ``AttributeError`` (not
        :class:`ServiceNotFoundError`) so ``hasattr`` and IDE tooling behave normally.
        """
        if name.startswith('_'):
            raise AttributeError(name)
        try:
            return self.get(name)
        except ServiceNotFoundError as exc:
            raise AttributeError(str(exc)) from None


# -- plugin resolution ---------------------------------------------------------


def _dispose_quietly(ctx: 'Context') -> None:
    """Roll a failed load back without letting the rollback hide the load error."""
    try:
        ctx.dispose()
    except BaseException:  # noqa: BLE001 - the load error is the one that matters
        pass


def _plugin_name(plugin: Any) -> str:
    return getattr(plugin, '__name__', None) or type(plugin).__name__


def _resolve_plugin(plugin: Any) -> tuple[Callable[..., Any], Mapping[str, Any] | None, tuple[str, ...]]:
    """Normalize the three accepted plugin shapes into (applier, schema, dependencies)."""
    if inspect.isclass(plugin):
        try:
            plugin = plugin()
        except TypeError as exc:
            raise PluginError(
                f'plugin class {plugin.__name__} must be instantiable without arguments: {exc}') from exc
    applier = getattr(plugin, 'apply', None)
    if not callable(applier):
        if callable(plugin):
            applier = plugin
        else:
            raise PluginError(
                f'plugin {plugin!r} must be a callable apply(ctx, config), an object with '
                'apply(ctx, config), or a class exposing apply')
    schema = getattr(plugin, 'config_schema', None)
    if schema is not None and not isinstance(schema, Mapping):
        raise PluginError(
            f'plugin {_plugin_name(plugin)!r} declares config_schema as '
            f'{type(schema).__name__}; it must be a mapping')
    return applier, schema, _normalize_dependencies(getattr(plugin, 'inject', ()))


def _normalize_dependencies(deps: Any) -> tuple[str, ...]:
    if deps is None:
        return ()
    if isinstance(deps, str):
        raise TypeError('dependencies must be a sequence of service names, not a single string')
    names = tuple(deps)
    for name in names:
        _require_service_name(name)
    return names


def _require_service_name(name: Any) -> None:
    if not isinstance(name, str) or not name:
        raise TypeError(f'service name must be a non-empty string, got {name!r}')


# -- config validation ---------------------------------------------------------


def validate_config(
    schema: Mapping[str, Any] | None,
    config: Any = None,
    *,
    path: str = 'config',
) -> dict[str, Any]:
    """Validate ``config`` against a declarative ``schema`` and return the resolved dict.

    This is the single config entry point of the kernel (G-schema, plan Task 2 builds on
    it). ``schema`` maps a field name to a spec dict with any of:

    ``type``
        A type (or tuple of types) the value must be an instance of. ``bool`` is *not*
        accepted where ``int`` is asked for, because Python's ``bool`` is an ``int``
        subclass and "``retries: true``" is a config bug, not a nice thing to accept.
    ``required``
        When true the field must be present unless a ``default`` exists.
    ``default``
        Value used when the field is absent.
    ``choices``
        The accepted values, tested with ``in``.

    Absent optional fields without a default are left out of the result rather than set
    to ``None``, so "was it configured?" stays answerable. Unknown fields are rejected:
    a typo in a plugin config must not be silently ignored. Every failure raises
    :class:`ConfigError` whose ``path`` names the offending field, e.g. ``config.retries``
    (pass ``path='plugins.pdf.config'`` to prefix it with the plugin).

    With ``schema=None`` the config passes through as a shallow copy (or ``{}`` for
    ``None``), which is what a plugin without a schema expects. Nested schemas are out of
    scope for the kernel: Task 2 composes flat schemas per plugin.
    """
    if schema is None:
        if config is None:
            return {}
        if not isinstance(config, Mapping):
            raise ConfigError(path, f'expects an object, got {type(config).__name__}')
        return dict(config)
    if not isinstance(schema, Mapping):
        raise ConfigError(path, f'schema must be a mapping, got {type(schema).__name__}')
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise ConfigError(path, f'expects an object, got {type(config).__name__}')

    resolved: dict[str, Any] = {}
    for field, spec in schema.items():
        field_path = f'{path}.{field}'
        if spec is None:
            spec = {}
        if not isinstance(spec, Mapping):
            raise ConfigError(field_path, f'field schema must be a mapping, got {type(spec).__name__}')
        if field in config:
            value = config[field]
        elif 'default' in spec:
            value = spec['default']
        elif spec.get('required'):
            raise ConfigError(field_path, 'is required')
        else:
            continue
        expected = spec.get('type')
        if expected is not None and not _matches_type(value, expected):
            raise ConfigError(
                field_path, f'expected {_type_label(expected)}, got {type(value).__name__}')
        choices = spec.get('choices')
        if choices is not None and value not in choices:
            raise ConfigError(field_path, f'expected one of {list(choices)!r}, got {value!r}')
        resolved[field] = value

    for field in config:
        if field not in schema:
            raise ConfigError(f'{path}.{field}', 'is not a known field')
    return resolved


def _matches_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, tuple):
        return any(_matches_type(value, candidate) for candidate in expected)
    if not isinstance(expected, type):
        return False
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def _type_label(expected: Any) -> str:
    if isinstance(expected, tuple):
        return ' or '.join(_type_label(candidate) for candidate in expected)
    return getattr(expected, '__name__', repr(expected))
