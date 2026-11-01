"""Synchronous event bus with Cordis' five dispatch styles.

A handler is any callable and receives exactly the arguments the emitter passes: there
is no implicit context argument and no implicit ``self``. :class:`Context` decides what
to pass, so a kernel user never has to remember a hidden first parameter.

Every style is synchronous and runs on the caller's thread. The exact contract of each
one is the docstring of its method; the short version:

===============  ===============================================================
``emit``         call all handlers, discard results, let the first exception escape
``parallel``     call all handlers on a thread pool, return results in listener order
``serial``       call in order, return the first non-``None`` result, else ``None``
``waterfall``    thread the first argument through the chain, return the last value
``bail``         like ``serial``, but a missing result raises :class:`BailError`
===============  ===============================================================
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

__all__ = ['BailError', 'EventBus', 'EventError']

Handler = Callable[..., Any]


class EventError(Exception):
    """Base class for event dispatch failures."""


class BailError(EventError):
    """Raised by :meth:`EventBus.bail` when no handler produced a result."""


class EventBus:
    """Name -> ordered handler list, with snapshot reads so dispatch is lock-free.

    Registration and removal are guarded by a re-entrant lock; dispatch reads a tuple
    snapshot, so a handler may register or remove handlers (including itself) while
    the current dispatch is running without corrupting the iteration order.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._lock = threading.RLock()

    def on(self, name: str, handler: Handler) -> Callable[[], None]:
        """Append ``handler`` to ``name`` and return the disposer that removes it."""
        if not isinstance(name, str) or not name:
            raise TypeError(f'event name must be a non-empty string, got {name!r}')
        if not callable(handler):
            raise TypeError(f'event handler must be callable, got {type(handler).__name__}')
        with self._lock:
            self._handlers.setdefault(name, []).append(handler)

        def dispose() -> None:
            self.off(name, handler)

        return dispose

    def off(self, name: str, handler: Handler) -> None:
        """Remove the first registration of ``handler``; a missing one is a no-op."""
        with self._lock:
            handlers = self._handlers.get(name)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                del self._handlers[name]

    def has(self, name: str) -> bool:
        """True when at least one handler is registered for ``name``."""
        with self._lock:
            return bool(self._handlers.get(name))

    def listeners(self, name: str) -> tuple[Handler, ...]:
        """The handlers registered for ``name``, in registration order."""
        with self._lock:
            return tuple(self._handlers.get(name, ()))

    def emit(self, name: str, *args: Any) -> None:
        """Call every handler in registration order and discard the results.

        Exceptions are not caught: the first failing handler aborts the dispatch and
        later handlers do not run. Return values are ignored on purpose - use
        :meth:`serial` or :meth:`waterfall` when handlers must produce a value.
        """
        for handler in self.listeners(name):
            handler(*args)

    def parallel(self, name: str, *args: Any, max_workers: int | None = None) -> list[Any]:
        """Call every handler concurrently and return results in registration order.

        Each handler runs on a ``ThreadPoolExecutor`` worker. Every handler runs to
        completion before this returns; if any raised, the first exception in
        (registration, not completion) order is re-raised afterwards, so the failure a
        caller sees does not depend on thread scheduling. The handler that raised is
        the only one whose result is missing from the returned list.
        """
        handlers = self.listeners(name)
        if not handlers:
            return []
        workers = max_workers or min(32, len(handlers))
        errors: list[BaseException] = []
        results: list[Any] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f'event:{name}') as pool:
            futures = [pool.submit(handler, *args) for handler in handlers]
            for future in futures:
                try:
                    results.append(future.result())
                except BaseException as exc:  # collected so every handler still joins
                    errors.append(exc)
        if errors:
            raise errors[0]
        return results

    def serial(self, name: str, *args: Any) -> Any:
        """Call handlers in order and return the first result that is not ``None``.

        ``None`` (the default return of a handler without an explicit ``return``) means
        "no opinion, keep going"; any other value - including ``0``, ``False`` and
        ``''`` - stops the chain and is returned. With no handler left, the result is
        ``None``. Exceptions propagate immediately.
        """
        for handler in self.listeners(name):
            result = handler(*args)
            if result is not None:
                return result
        return None

    def waterfall(self, name: str, *args: Any) -> Any:
        """Thread the first argument through the handlers and return the final value.

        The first handler receives ``args`` unchanged; every later handler receives the
        previous non-``None`` result followed by the remaining original arguments, so a
        handler may refine the value and may still receive side-channel parameters. A
        handler returning ``None`` leaves the value untouched. With no handlers the
        original first argument is returned.
        """
        handlers = self.listeners(name)
        if not args:
            for handler in handlers:
                handler()
            return None
        current = args[0]
        rest: Sequence[Any] = args[1:]
        for handler in handlers:
            result = handler(current, *rest)
            if result is not None:
                current = result
        return current

    def bail(self, name: str, *args: Any) -> Any:
        """Like :meth:`serial`, but requires a result.

        Raises :class:`BailError` when no handler returned a non-``None`` value, i.e.
        when the event has no handler that can answer it. Use this for events where
        silence is a bug (a pipeline stage nobody handled) rather than a valid outcome.
        """
        for handler in self.listeners(name):
            result = handler(*args)
            if result is not None:
                return result
        raise BailError(f'no handler for event {name!r} returned a result')
