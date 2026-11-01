"""Standalone worker for the durable `/v1/uploads` parse queue (review 5.1).

`GET /v1/uploads/{upload_id}` used to call `work_once`, so a browser poll *was* the queue's
clock: closing the page stopped every parse, and N concurrent HTTP requests could start N
parses (`workers=1..4` only bounded a single call). The GET is now a pure read; this process
is the clock. It adds no new claim logic — it drives `work_once`/`drain` and publishes a
heartbeat so `GET /v1/uploads` (and `/health`) can honestly report whether a processor is
running.

    python -m ingestion.upload_worker --watch                 # long-running, no HTTP needed
    python -m ingestion.upload_worker --once                  # drain what is claimable, exit
    python -m ingestion.upload_worker --watch --interval 5 --workers 2 --deadline 60

* `--once` runs one `drain()` under a cross-process parse slot: every claimable job is
  finished, then it exits (cron/one-shot use). It is the deterministic path for tests.
* `--watch` claims and finishes one bounded wave at a time (`work_once`), sleeping
  `--interval` seconds only when nothing was claimable.
* `--workers` (1..4) bounds how many jobs a single wave claims; that wave's claims run
  **sequentially**, and the wave itself holds one advisory slot, so the sum of parses
  running across every process is bounded by `DATAMIND_TASK_SLOTS` (default 2) rather than
  by the number of HTTP requests or worker processes.
* `--deadline` overrides `upload_pipeline.wave_deadline()` for one wave (validated by
  `datamind.task_budget`; never unbounded).

Signals (SIGINT/SIGTERM) stop the loop **after** the wave in flight: the bounded parse child
runs in its own session, so it is never abandoned half-way by a Ctrl-C.

Before anything else — after argument validation, before the DSN is even resolved — the
worker bootstraps plugin discovery (`ingestion.plugins.ensure_loaded()`). That applies
`DATAMIND_PLUGIN_CONFIG` and imports the modules named by `DATAMIND_PLUGIN_PATH`, which is
what lets this parent process attribute a wave timeout to an external plugin and refuse to
start on a broken configuration rather than failing later, once a claim arrives.

Progress is one `[upload_worker] <timestamp> ...` line per event on stdout, so a supervisor
or a human can see what is happening without reading JSON. Exit codes:

===  ==================================================================================
0    ran to completion — the queue was drained/empty, or a signal stopped the loop
1    unexpected error (including a heartbeat that could not be registered, or a plugin
     configuration `ConfigError` that stopped startup)
2    invalid arguments (argparse)
3    every cross-process parse slot is held by another worker; nothing was attempted
===  ==================================================================================
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from pathlib import Path
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from datamind import task_budget
from datamind.agent import _write_dsn
from datamind.plugin.context import ConfigError
from ingestion import upload_pipeline
from ingestion.plugins import ensure_loaded

MARKER = '[upload_worker]'
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_SLOTS_BUSY = 3
DEFAULT_INTERVAL_SECONDS = 2.0
MIN_INTERVAL_SECONDS = 0.1
#: Keys worth printing from one `_execute` outcome, in a stable order.
OUTCOME_FIELDS = ('job_id', 'attempt', 'outcome', 'error_code', 'wave_timeouts', 'elapsed_ms')


def emit(message, stream=None):
    """One human-readable, timestamped progress line."""
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f'{MARKER} {stamp} {message}', file=stream or sys.stdout, flush=True)


def report(outcomes):
    """Print one line per finished/failed/claim-lost outcome."""
    for outcome in outcomes or []:
        parts = [f'{key}={outcome[key]}' for key in OUTCOME_FIELDS
                 if isinstance(outcome, dict) and outcome.get(key) is not None]
        emit(' '.join(parts) or 'outcome=unknown')


def error_code(error):
    detail = getattr(error, 'detail', None)
    return detail.get('error_code') if isinstance(detail, dict) else None


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m ingestion.upload_worker',
        description='Drive the durable upload parse queue without an HTTP poller.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true',
                      help='Drain every claimable job (one bounded drain), then exit.')
    mode.add_argument('--watch', action='store_true',
                      help='Poll forever: one bounded wave per pass, then sleep --interval.')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL_SECONDS,
                        metavar='SECONDS',
                        help=f'Idle sleep in --watch mode (default {DEFAULT_INTERVAL_SECONDS:g}).')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=1,
                        help='Jobs claimed per wave, executed sequentially (default 1).')
    parser.add_argument('--deadline', type=float, metavar='SECONDS',
                        help='Wall-clock budget of one wave; default wave_deadline().')
    return parser


def run_once(dsn, *, workers, deadline_s):
    """One `drain()` under the cross-process slot; the whole claimable queue is finished."""
    with upload_pipeline.wave_slot(dsn):
        return upload_pipeline.drain(dsn, workers=workers, deadline_s=deadline_s)


def run_watch(dsn, *, workers, deadline_s, interval, stopping, heartbeat):
    """Poll forever until `stopping` is set; a busy slot is a retry, not a fatal error."""
    while not stopping.is_set():
        with contextlib.suppress(Exception):
            # Best effort: the heartbeat thread renews anyway, and a lapsed lease is
            # re-acquired on the next successful beat. A transient outage must not kill a
            # long-running worker (nor claim it is alive while it cannot reach the store).
            heartbeat.beat()
        try:
            with upload_pipeline.wave_slot(dsn):
                wave = upload_pipeline.work_once(dsn, workers=workers, deadline_s=deadline_s)
        except HTTPException as error:
            if error_code(error) == 'TASK_BUDGET_BUSY':
                emit(f'slots busy (another worker holds the parse slots); retry in {interval:g}s')
                stopping.wait(interval)
                continue
            raise
        except Exception as error:  # transient database/budget failure: keep the loop alive
            emit(f'wave error={error.__class__.__name__}', stream=sys.stderr)
            stopping.wait(interval)
            continue
        report(wave['outcomes'])
        if not wave['claimed']:
            stopping.wait(interval)
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval < MIN_INTERVAL_SECONDS:
        parser.error(f'--interval must be >= {MIN_INTERVAL_SECONDS:g}')
    deadline_s = None
    if args.deadline is not None:
        try:
            deadline_s = task_budget.validate_deadline(args.deadline)
        except ValueError as error:
            parser.error(str(error))

    # Discovery runs here, before the first claim and before the heartbeat writes anything:
    # this process is the only writer of `_record_wave_timeout`/`_adapter_for`, so it must
    # know the external plugins a claim's suffix may name, and a bad plugin config must
    # stop the process instead of failing later as an unattributed error.
    try:
        ensure_loaded()
    except ConfigError as error:
        emit(f'plugin config error path={error.path} reason={error.reason}; refusing to start',
             stream=sys.stderr)
        return EXIT_ERROR

    dsn = _write_dsn()
    stopping = threading.Event()

    def on_signal(signum, _frame):
        if not stopping.is_set():
            stopping.set()
            emit(f'signal={signal.Signals(signum).name} 不打断已领取的解析，当前波次结束后退出')

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    heartbeat = upload_pipeline.WorkerHeartbeat(dsn)
    try:
        heartbeat.start()
    except Exception as error:
        # Running without a heartbeat would make every client claim "no processor is
        # running" while jobs do advance — refuse to start instead of lying in either
        # direction.
        emit(f'heartbeat unavailable ({error.__class__.__name__}); refusing to start',
             stream=sys.stderr)
        return EXIT_ERROR

    emit(f'heartbeat lease={heartbeat.name} alive_seconds={heartbeat.alive_seconds:g}')
    emit(f"start mode={'once' if args.once else 'watch'} workers={args.workers} "
         f'deadline={deadline_s if deadline_s is not None else upload_pipeline.wave_deadline():g} '
         f'interval={args.interval:g}')
    try:
        if args.once:
            try:
                result = run_once(dsn, workers=args.workers, deadline_s=deadline_s)
            except HTTPException as error:
                if error_code(error) == 'TASK_BUDGET_BUSY':
                    emit('slots busy: every cross-process parse slot is held; nothing claimed')
                    return EXIT_SLOTS_BUSY
                raise
            report(result['outcomes'])
            emit(f"done mode=once waves={result['waves']} claimed={result['claimed']} "
                 f"outcomes={len(result['outcomes'])}")
            return EXIT_OK
        return run_watch(dsn, workers=args.workers, deadline_s=deadline_s,
                         interval=args.interval, stopping=stopping, heartbeat=heartbeat)
    except KeyboardInterrupt:  # pragma: no cover - defensive; the signal handler wins
        return EXIT_OK
    except Exception as error:
        emit(f'fatal error={error.__class__.__name__}', stream=sys.stderr)
        return EXIT_ERROR
    finally:
        heartbeat.stop()
        emit('stop heartbeat released')


if __name__ == '__main__':
    sys.exit(main())
