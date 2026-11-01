"""Plugin kernel: Cordis-style context, services, injection, effects and events.

The kernel is exercised through its public surface only (``Context`` and
``validate_config``); nothing here imports the application, so a failure means the
kernel itself is wrong, not the surrounding app.
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamind.plugin import (
    BailError,
    ConfigError,
    Context,
    InjectError,
    PluginError,
    ServiceConflictError,
    ServiceNotFoundError,
    validate_config,
)

SCHEMA = {
    'name': {'type': str, 'required': True},
    'retries': {'type': int, 'default': 2},
    'mode': {'type': str, 'choices': ('strict', 'loose'), 'default': 'strict'},
}


class ServiceTest(unittest.TestCase):
    def test_set_get_has_roundtrip(self):
        ctx = Context()
        value = object()

        dispose = ctx.set('store', value)

        self.assertTrue(ctx.has('store'))
        self.assertIs(ctx.get('store'), value)
        self.assertEqual(ctx.service_names, ('store',))
        dispose()
        self.assertFalse(ctx.has('store'))

    def test_duplicate_service_is_rejected_with_its_name(self):
        ctx = Context()
        ctx.set('store', object())

        with self.assertRaises(ServiceConflictError) as caught:
            ctx.set('store', object())

        self.assertIn('store', str(caught.exception))

    def test_missing_service_error_lists_known_names(self):
        ctx = Context()
        ctx.set('registry', object())

        with self.assertRaises(ServiceNotFoundError) as caught:
            ctx.get('parser')

        message = str(caught.exception)
        self.assertIn('parser', message)
        self.assertIn('registry', message)

    def test_service_can_be_registered_again_after_its_disposer_ran(self):
        ctx = Context()
        ctx.set('store', 'first')()

        ctx.set('store', 'second')

        self.assertEqual(ctx.get('store'), 'second')

    def test_attribute_access_reads_services(self):
        ctx = Context()
        parser = object()
        ctx.set('parser', parser)

        self.assertIs(ctx.parser, parser)
        with self.assertRaises(AttributeError):
            ctx.no_such_service  # noqa: B018 - attribute access is the assertion

    def test_service_names_require_a_non_empty_string(self):
        ctx = Context()

        with self.assertRaises(TypeError):
            ctx.set('', 1)
        with self.assertRaises(TypeError):
            ctx.set(None, 1)


class InjectTest(unittest.TestCase):
    def test_inject_runs_immediately_when_dependency_exists(self):
        ctx = Context()
        ctx.set('db', 'D1')
        calls = []

        dispose = ctx.inject(['db'], lambda inner: calls.append(inner.get('db')))

        self.assertEqual(calls, ['D1'])
        dispose()

    def test_inject_defers_then_runs_in_registration_order(self):
        ctx = Context()
        calls = []
        ctx.inject(['db'], lambda inner: calls.append(('first', inner.get('db'))))
        ctx.inject(['db'], lambda inner: calls.append(('second', inner.get('db'))))

        self.assertEqual(calls, [])
        ctx.set('db', 'D1')

        self.assertEqual(calls, [('first', 'D1'), ('second', 'D1')])

    def test_inject_waits_for_every_dependency(self):
        ctx = Context()
        calls = []
        ctx.inject(['db', 'cache'], lambda inner: calls.append('ready'))

        ctx.set('db', 1)
        self.assertEqual(calls, [])

        ctx.set('cache', 2)
        self.assertEqual(calls, ['ready'])

    def test_inject_disposer_cancels_a_pending_callback(self):
        ctx = Context()
        calls = []
        cancel = ctx.inject(['db'], lambda inner: calls.append('ran'))

        cancel()
        ctx.set('db', 1)

        self.assertEqual(calls, [])

    def test_unresolved_injection_is_dropped_when_its_context_disposes(self):
        root = Context()
        calls = []
        inner = root.plugin(lambda ctx, config: ctx.inject(['db'], lambda i: calls.append('ran')))

        inner.dispose()
        root.set('db', 1)

        self.assertEqual(calls, [])

    def test_plugin_declaring_a_missing_dependency_is_rejected(self):
        ctx = Context()
        calls = []

        def plugin(inner, config):
            calls.append('applied')

        plugin.inject = ('registry',)

        with self.assertRaises(InjectError) as caught:
            ctx.plugin(plugin)

        self.assertIn('registry', str(caught.exception))
        self.assertEqual(calls, [])

    def test_plugin_loads_once_the_dependency_exists(self):
        ctx = Context()
        ctx.set('registry', 'R1')

        def plugin(inner, config):
            inner.set('parser', inner.get('registry'))

        plugin.inject = ('registry',)
        ctx.plugin(plugin)

        self.assertEqual(ctx.get('parser'), 'R1')


class EffectTest(unittest.TestCase):
    def test_disposers_run_in_reverse_order(self):
        ctx = Context()
        order = []
        ctx.effect(lambda: order.append('first'))
        ctx.effect(lambda: order.append('second'))
        ctx.effect(lambda: order.append('third'))

        ctx.dispose()

        self.assertEqual(order, ['third', 'second', 'first'])
        self.assertTrue(ctx.disposed)

    def test_dispose_is_idempotent(self):
        ctx = Context()
        order = []
        ctx.effect(lambda: order.append('once'))

        ctx.dispose()
        result = ctx.dispose()

        self.assertIsNone(result)
        self.assertEqual(order, ['once'])

    def test_effect_on_a_disposed_context_runs_immediately(self):
        ctx = Context()
        ctx.dispose()
        order = []

        ctx.effect(lambda: order.append('ran'))

        self.assertEqual(order, ['ran'])

    def test_effect_requires_a_callable(self):
        with self.assertRaises(TypeError):
            Context().effect('not-a-callable')

    def test_dispose_keeps_unwinding_after_a_failing_disposer(self):
        ctx = Context()
        order = []
        ctx.effect(lambda: order.append('first'))

        def boom():
            raise RuntimeError('boom')

        ctx.effect(boom)
        ctx.effect(lambda: order.append('last'))

        with self.assertRaises(RuntimeError):
            ctx.dispose()

        self.assertEqual(order, ['last', 'first'])
        self.assertTrue(ctx.disposed)

    def test_plugin_disposers_run_in_reverse_order(self):
        ctx = Context()
        order = []

        def plugin(inner, config):
            inner.effect(lambda: order.append('first'))
            inner.effect(lambda: order.append('second'))

        handle = ctx.plugin(plugin)
        handle.dispose()

        self.assertEqual(order, ['second', 'first'])


class PluginLoadTest(unittest.TestCase):
    def test_function_plugin_receives_config_and_returns_its_context(self):
        ctx = Context()
        seen = {}

        def plugin(inner, config):
            seen['ctx'] = inner
            seen['config'] = config
            inner.set('svc', config['name'])

        handle = ctx.plugin(plugin, {'name': 'pdf'})

        self.assertEqual(seen['config'], {'name': 'pdf'})
        self.assertIs(seen['ctx'], handle)
        self.assertIs(handle.parent, ctx)
        self.assertTrue(handle.ready)
        self.assertEqual(ctx.get('svc'), 'pdf')

    def test_object_plugin_with_apply(self):
        class Recorder:
            def __init__(self):
                self.calls = []

            def apply(self, inner, config):
                self.calls.append(config)
                inner.set('svc-log', self)

        recorder = Recorder()
        ctx = Context()

        ctx.plugin(recorder, {'level': 'debug'})

        self.assertEqual(recorder.calls, [{'level': 'debug'}])
        self.assertIs(ctx.get('svc-log'), recorder)

    def test_class_plugin_is_instantiated_and_applied(self):
        class ClassPlugin:
            def __init__(self):
                self.loaded = False

            def apply(self, inner, config):
                self.loaded = True
                inner.set('class-svc', self)

        ctx = Context()
        ctx.plugin(ClassPlugin, {'v': 2})

        self.assertTrue(ctx.get('class-svc').loaded)

    def test_invalid_plugin_shape_is_rejected(self):
        ctx = Context()

        for bad in (None, 42, object()):
            with self.assertRaises(PluginError):
                ctx.plugin(bad)

    def test_plugin_on_a_disposed_context_is_rejected(self):
        ctx = Context()
        ctx.dispose()

        with self.assertRaises(PluginError):
            ctx.plugin(lambda inner, config: None)

    def test_failed_load_rolls_back_only_its_own_effects(self):
        ctx = Context()
        events = []

        def good(inner, config):
            inner.set('good', 1)
            inner.on('ping', lambda: events.append('good'))

        def bad(inner, config):
            inner.set('bad', 1)
            inner.on('ping', lambda: events.append('bad'))
            inner.effect(lambda: events.append('bad-effect'))
            raise RuntimeError('boom')

        ctx.plugin(good)
        with self.assertRaises(RuntimeError):
            ctx.plugin(bad)

        self.assertTrue(ctx.has('good'))
        self.assertFalse(ctx.has('bad'))
        self.assertEqual(events, ['bad-effect'])

        events.clear()
        ctx.emit('ping')
        self.assertEqual(events, ['good'])

    def test_ready_and_dispose_events_fire_with_the_plugin_context(self):
        ctx = Context()
        seen = []
        ctx.on('ready', lambda inner: seen.append(('ready', inner)))
        ctx.on('dispose', lambda inner: seen.append(('dispose', inner)))

        handle = ctx.plugin(lambda inner, config: inner.set('svc', 1))

        self.assertEqual(seen[0][0], 'ready')
        self.assertIs(seen[0][1], handle)

        handle.dispose()

        self.assertEqual(seen[1][0], 'dispose')
        self.assertIs(seen[1][1], handle)

    def test_unloading_plugin_removes_services_and_listeners(self):
        ctx = Context()
        calls = []

        def plugin(inner, config):
            inner.set('svc', 1)
            inner.on('evt', lambda: calls.append('x'))

        handle = ctx.plugin(plugin)
        ctx.emit('evt')
        self.assertEqual(calls, ['x'])
        self.assertTrue(ctx.has('svc'))

        handle.dispose()

        self.assertFalse(ctx.has('svc'))
        ctx.emit('evt')
        self.assertEqual(calls, ['x'])
        ctx.set('svc', 2)  # the name is free again, not just hidden
        self.assertEqual(ctx.get('svc'), 2)

    def test_disposing_a_parent_disposes_its_loaded_plugins(self):
        ctx = Context()
        ctx.plugin(lambda inner, config: inner.set('svc', 1))

        ctx.dispose()

        self.assertFalse(ctx.has('svc'))

    def test_failing_ready_listener_rolls_the_load_back(self):
        ctx = Context()

        def reject(inner):
            raise RuntimeError('rejected')

        ctx.on('ready', reject)

        with self.assertRaises(RuntimeError):
            ctx.plugin(lambda inner, config: inner.set('svc', 1))

        self.assertFalse(ctx.has('svc'))
        self.assertEqual(ctx.plugins(), ())

    def test_failing_dispose_listener_does_not_stop_the_unwind(self):
        ctx = Context()
        order = []

        def boom(inner):
            raise RuntimeError('boom')

        ctx.on('dispose', boom)
        ctx.effect(lambda: order.append('unwound'))

        with self.assertRaises(RuntimeError):
            ctx.dispose()

        self.assertEqual(order, ['unwound'])
        self.assertTrue(ctx.disposed)


class ConfigValidationTest(unittest.TestCase):
    def test_defaults_and_required_fields_are_applied(self):
        self.assertEqual(
            validate_config(SCHEMA, {'name': 'pdf'}),
            {'name': 'pdf', 'retries': 2, 'mode': 'strict'},
        )

    def test_missing_required_field_names_the_path(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, {})

        self.assertEqual(caught.exception.path, 'config.name')
        self.assertIn('config.name', str(caught.exception))

    def test_type_mismatch_names_the_path(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, {'name': 'pdf', 'retries': 'two'})

        self.assertEqual(caught.exception.path, 'config.retries')

    def test_choices_are_enforced(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, {'name': 'pdf', 'mode': 'turbo'})

        self.assertEqual(caught.exception.path, 'config.mode')
        self.assertIn('turbo', str(caught.exception))

    def test_unknown_field_is_rejected_with_its_path(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, {'name': 'pdf', 'extra': 1})

        self.assertEqual(caught.exception.path, 'config.extra')

    def test_bool_is_not_accepted_where_an_int_is_expected(self):
        with self.assertRaises(ConfigError):
            validate_config({'retries': {'type': int}}, {'retries': True})

    def test_non_mapping_config_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, ['pdf'])

        self.assertEqual(caught.exception.path, 'config')

    def test_without_a_schema_config_passes_through_as_a_copy(self):
        config = {'a': 1}

        result = validate_config(None, config)

        self.assertEqual(result, {'a': 1})
        self.assertIsNot(result, config)
        self.assertEqual(validate_config(None, None), {})

    def test_custom_path_prefixes_the_field_path(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config(SCHEMA, {}, path='plugins.pdf.config')

        self.assertEqual(caught.exception.path, 'plugins.pdf.config.name')

    def test_plugin_config_schema_is_enforced_on_load(self):
        def plugin(inner, config):
            inner.set('svc', config['n'])

        plugin.config_schema = {'n': {'type': int, 'required': True}}
        ctx = Context()

        with self.assertRaises(ConfigError):
            ctx.plugin(plugin, {'n': 'not-an-int'})

        ctx.plugin(plugin, {'n': 1})
        self.assertEqual(ctx.get('svc'), 1)

    def test_explicit_schema_overrides_the_declared_one(self):
        def plugin(inner, config):
            inner.set('svc', config['n'])

        plugin.config_schema = {'n': {'type': int}}
        ctx = Context()

        with self.assertRaises(ConfigError):
            ctx.plugin(plugin, {'n': 1}, schema={'n': {'type': str}})


class EventTest(unittest.TestCase):
    def test_emit_calls_handlers_in_registration_order(self):
        ctx = Context()
        seen = []
        ctx.on('evt', lambda value: seen.append(('a', value)))
        ctx.on('evt', lambda value: seen.append(('b', value)))

        self.assertIsNone(ctx.emit('evt', 1))

        self.assertEqual(seen, [('a', 1), ('b', 1)])

    def test_emit_propagates_a_handler_exception(self):
        ctx = Context()

        def boom():
            raise RuntimeError('boom')

        ctx.on('evt', boom)

        with self.assertRaises(RuntimeError):
            ctx.emit('evt')

    def test_emit_ignores_return_values(self):
        ctx = Context()
        ctx.on('evt', lambda: 'ignored')

        self.assertIsNone(ctx.emit('evt'))

    def test_parallel_returns_results_in_registration_order(self):
        ctx = Context()
        ctx.on('evt', lambda: 'a')
        ctx.on('evt', lambda: 'b')

        self.assertEqual(ctx.parallel('evt'), ['a', 'b'])

    def test_parallel_runs_handlers_concurrently(self):
        ctx = Context()
        barrier = threading.Barrier(2, timeout=10)

        def first():
            barrier.wait()
            return 'first'

        def second():
            barrier.wait()
            return 'second'

        ctx.on('evt', first)
        ctx.on('evt', second)

        self.assertEqual(ctx.parallel('evt'), ['first', 'second'])

    def test_parallel_propagates_the_first_error(self):
        ctx = Context()

        def boom():
            raise RuntimeError('boom')

        ctx.on('evt', boom)
        ctx.on('evt', lambda: 'ok')

        with self.assertRaises(RuntimeError):
            ctx.parallel('evt')

    def test_serial_stops_at_the_first_result(self):
        ctx = Context()
        seen = []
        ctx.on('evt', lambda: seen.append('first'))
        ctx.on('evt', lambda: 0)
        ctx.on('evt', lambda: seen.append('never'))

        self.assertEqual(ctx.serial('evt'), 0)
        self.assertEqual(seen, ['first'])

    def test_serial_returns_none_when_nothing_returns_a_value(self):
        ctx = Context()
        ctx.on('evt', lambda: None)

        self.assertIsNone(ctx.serial('evt'))
        self.assertIsNone(ctx.serial('no-handlers'))

    def test_waterfall_threads_the_first_argument(self):
        ctx = Context()
        seen = []

        def increment(value, factor):
            seen.append(value)
            return value + 1

        def scale(value, factor):
            seen.append(value)
            return value * factor

        def passthrough(value, factor):
            seen.append(value)
            return None

        ctx.on('evt', increment)
        ctx.on('evt', scale)
        ctx.on('evt', passthrough)

        self.assertEqual(ctx.waterfall('evt', 1, 10), 20)
        self.assertEqual(seen, [1, 2, 20])

    def test_waterfall_without_handlers_returns_the_original_value(self):
        self.assertEqual(Context().waterfall('evt', 7), 7)

    def test_bail_returns_the_first_result_or_raises(self):
        ctx = Context()

        with self.assertRaises(BailError):
            ctx.bail('evt')

        ctx.on('evt', lambda: None)
        with self.assertRaises(BailError):
            ctx.bail('evt')

        ctx.on('evt', lambda: 'found')
        self.assertEqual(ctx.bail('evt'), 'found')

    def test_off_removes_a_handler(self):
        ctx = Context()
        seen = []
        handler = seen.append
        ctx.on('evt', handler)

        ctx.off('evt', handler)
        ctx.emit('evt', 'x')

        self.assertEqual(seen, [])

    def test_listener_is_removed_when_its_context_disposes(self):
        root = Context()
        seen = []

        def plugin(inner, config):
            inner.on('evt', lambda: seen.append('plugin'))

        handle = root.plugin(plugin)
        root.emit('evt')
        self.assertEqual(seen, ['plugin'])

        handle.dispose()
        root.emit('evt')

        self.assertEqual(seen, ['plugin'])


class SamplePluginRoundTripTest(unittest.TestCase):
    """The demo boundary: load, observe, unload, and the core stays intact."""

    def test_sample_plugin_round_trip(self):
        root = Context()
        root.set('app', 'DataMind')

        def sample(inner, config):
            inner.set('sample', config['value'])
            inner.on('sample.ping', lambda: config['value'])

        handle = root.plugin(sample, {'value': 'ok'})

        self.assertEqual(root.parallel('sample.ping'), ['ok'])
        self.assertEqual(root.get('sample'), 'ok')
        self.assertTrue(handle.ready)

        handle.dispose()

        self.assertFalse(root.has('sample'))
        self.assertEqual(root.parallel('sample.ping'), [])
        self.assertEqual(root.get('app'), 'DataMind')

        root.dispose()
        self.assertFalse(root.has('app'))


if __name__ == '__main__':
    unittest.main()
