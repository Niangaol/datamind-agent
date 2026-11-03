"""Restore the bundled clean demonstration data into dedicated, persistent volumes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import time

import psycopg2
from psycopg2 import sql
import requests


SEED = Path(os.environ.get("DATAMIND_DEMO_SEED_DIR", "/seed"))
APP = Path("/opt/app")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pg(database="postgres"):
    return psycopg2.connect(host=os.environ.get("PGHOST", "postgres"),
                           port=int(os.environ.get("PGPORT", "5432")), dbname=database,
                           user=os.environ.get("PGUSER", "postgres"),
                           password=os.environ.get("PGPASSWORD", "datamind_dev"),
                           connect_timeout=5)


def wait_services():
    http = requests.Session()
    http.trust_env = False
    for attempt in range(60):
        try:
            with pg() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            response = http.get(os.environ["DATAMIND_QDRANT_URL"] + "/readyz", timeout=3)
            response.raise_for_status()
            return http
        except (psycopg2.OperationalError, requests.RequestException):
            if attempt == 59:
                raise RuntimeError("Demonstration databases did not become ready") from None
            time.sleep(2)


def restore_postgres(admin):
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(714202619)")
        cur.execute("""CREATE TABLE IF NOT EXISTS public.datamind_demo_seed_log (
                       name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                       restored_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute((SEED / "postgres/roles.sql").read_text(encoding="utf-8"))
    for database in ("datamind_meta", "demo_retail", "chinook"):
        path = SEED / "postgres" / (database + ".sql")
        expected = digest(path)
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0")
                            .format(sql.Identifier(database)))
        with pg(database) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.datamind_demo_seed_marker')")
            if cur.fetchone()[0]:
                cur.execute("SELECT sha256 FROM public.datamind_demo_seed_marker WHERE singleton")
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"Incomplete restore marker in {database}")
                print(f"Reusing {database}" if row[0] == expected else
                      f"Keeping existing {database}; bundled seed has changed", flush=True)
                # The marker is committed with the restored tables. A crash between
                # the target transaction and the admin log is therefore recoverable.
                with admin.cursor() as log:
                    log.execute("""INSERT INTO public.datamind_demo_seed_log(name,sha256) VALUES (%s,%s)
                                   ON CONFLICT(name) DO UPDATE SET sha256=EXCLUDED.sha256""", (database, row[0]))
                continue
            cur.execute("""SELECT count(*) FROM pg_tables WHERE schemaname
                           NOT IN ('pg_catalog','information_schema')""")
            if cur.fetchone()[0]:
                raise RuntimeError(f"Refusing to replace unmarked existing database {database}")
            content = path.read_text(encoding="utf-8")
            if re.search(r"(?m)^COPY .* FROM stdin;", content):
                raise RuntimeError("Seed SQL must contain INSERT statements, without psql commands")
            print(f"Restoring {database}", flush=True)
            cur.execute(content)
            cur.execute("""CREATE TABLE public.datamind_demo_seed_marker (
                           singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
                           sha256 TEXT NOT NULL, restored_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("INSERT INTO public.datamind_demo_seed_marker(singleton,sha256) VALUES (true,%s)",
                        (expected,))
        with admin.cursor() as cur:
            cur.execute("""INSERT INTO public.datamind_demo_seed_log(name,sha256) VALUES (%s,%s)
                           ON CONFLICT(name) DO UPDATE SET sha256=EXCLUDED.sha256""", (database, expected))


def restore_files(admin):
    archive = SEED / "object_store.tar.gz"
    if not archive.is_file():
        raise RuntimeError("The bundled document previews and CAD sources are missing")
    target = APP / "data/object_store"
    target.mkdir(parents=True, exist_ok=True)
    expected = json.loads((SEED / "manifest.json").read_text(encoding="utf-8"))["object_store_files"]
    restored = 0
    with tarfile.open(archive, "r:gz") as tar:
        for item in expected:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Invalid bundled document path")
            path = target / relative
            if path.is_file() and digest(path) == item["sha256"]:
                continue
            if path.exists():
                raise RuntimeError("Bundled immutable document has changed: " + item["path"])
            # Recover missing baseline files even when only the file volume was lost.
            # Existing uploaded files and updated user records are never overwritten.
            tar.extract(item["path"], target, filter="data")
            if digest(path) != item["sha256"]:
                raise RuntimeError("Restored document checksum mismatch")
            restored += 1
    print(f"Document files ready; recovered {restored} files", flush=True)
    with admin.cursor() as cur:
        cur.execute("""INSERT INTO public.datamind_demo_seed_log(name,sha256) VALUES (%s,%s)
                       ON CONFLICT(name) DO UPDATE SET sha256=EXCLUDED.sha256""", ("object_store", digest(archive)))


def restore_vectors(http):
    registry = json.loads((SEED / "manifest.json").read_text(encoding="utf-8"))
    base = os.environ["DATAMIND_QDRANT_URL"].rstrip("/")
    for item in registry["qdrant"]:
        name = item["collection"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RuntimeError("Invalid bundled collection name")
        url = f"{base}/collections/{name}"
        response = http.get(url, timeout=10)
        if response.status_code == 404:
            path = SEED / item["path"]
            if digest(path) != item["sha256"]:
                raise RuntimeError("Vector snapshot checksum mismatch")
            print(f"Restoring vector collection {name}", flush=True)
            with path.open("rb") as stream:
                restored = http.post(url + "/snapshots/upload", params={"priority": "snapshot", "wait": "true"},
                                     files={"snapshot": (path.name, stream, "application/octet-stream")},
                                     timeout=180)
            restored.raise_for_status()
            response = http.get(url, timeout=10)
        response.raise_for_status()
        count = response.json()["result"]["points_count"]
        if count < item["points"]:
            raise RuntimeError(f"Vector collection {name} is incomplete: {count} points")
        print(f"Vector collection {name}: {count} points", flush=True)


def main():
    manifest = json.loads((SEED / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = SEED / item["path"]
        if not path.is_file() or digest(path) != item["sha256"]:
            raise RuntimeError("Bundled demonstration data is incomplete: " + item["path"])
    http = wait_services()
    admin = pg()
    try:
        restore_postgres(admin)
        restore_files(admin)
        restore_vectors(http)
        with pg("datamind_meta") as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks")
            chunks = cur.fetchone()[0]
            if chunks < 50:
                raise RuntimeError("Bundled knowledge base has too few chunks")
        print(f"Demonstration data ready: {chunks} knowledge chunks", flush=True)
    finally:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(714202619)")
        admin.close()
        http.close()


if __name__ == "__main__":
    main()
