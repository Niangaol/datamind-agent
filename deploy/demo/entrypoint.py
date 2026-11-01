"""Run the API and all durable workers, with bounded shutdown and restart."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path("/opt/app")
STOP = False


def request_stop(signum, frame):
    global STOP
    STOP = True


def start(name, arguments):
    print("Starting " + name, flush=True)
    return subprocess.Popen([sys.executable, *arguments], cwd=ROOT, start_new_session=True)


def stop_all(processes):
    processes = list(processes)
    for process in processes:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 65
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def main():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    (ROOT / ".cache/api").mkdir(parents=True, exist_ok=True)
    (ROOT / "data/uploads").mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "db/migrate.py"], cwd=ROOT, check=True, timeout=120)
    commands = {
        "api": ["-m", "uvicorn", "datamind.api:app", "--host", "0.0.0.0", "--port", "8000",
                "--no-proxy-headers"],
        "uploads": ["-m", "ingestion.upload_worker", "--watch", "--interval", "2"],
        "registrations": ["scripts/file_job_worker.py", "--watch", "--workers", "2"],
        "wiki": ["scripts/wiki_build_worker.py", "--watch", "--interval", "2", "--demo-identity"],
        "private-pdf": ["scripts/pdf_index_worker.py", "--watch", "--poll-interval", "3"],
    }
    children = {}
    restarts = {name: 0 for name in commands}
    exit_code = 0
    try:
        for name, args in commands.items():
            children[name] = start(name, args)
        while not STOP:
            for name, process in list(children.items()):
                if process.poll() is None:
                    continue
                if name == "api" or restarts[name] >= 5:
                    raise RuntimeError(f"{name} exited unexpectedly: {process.returncode}")
                restarts[name] += 1
                print(f"Restarting {name}; exit={process.returncode}, attempt={restarts[name]}", flush=True)
                children[name] = start(name, commands[name])
            time.sleep(1)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        # Stop accepting new API jobs before waiting for workers to finish their wave.
        stop_all(children.values())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
