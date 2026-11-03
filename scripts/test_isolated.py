"""Run regression against a disposable metadata clone, leaving demo state intact.

Requires the existing datamind-postgres container and initialized demo data.
Only datamind_meta connections in this process are redirected; retail/industrial
SQL stays read-only. Model settings, private PDF registry rows and private
knowledge publication/outbox rows are excluded from the database copy.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
import uuid

import psycopg2
from psycopg2.extensions import parse_dsn

ROOT = Path(__file__).resolve().parents[1]


def _git_commit() -> str | None:
    """The revision this run verified; None outside a checkout (exported snapshot)."""
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(ROOT),
                              check=True, capture_output=True, text=True).stdout.strip() or None
    except Exception:
        return None


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--pattern',default='test_*.py')
    ap.add_argument('--container',default='datamind-postgres');args=ap.parse_args()
    database='datamind_test_'+uuid.uuid4().hex[:12]
    original_connect=psycopg2.connect
    docker=['docker','exec',args.container]
    subprocess.run(docker+['createdb','-U','postgres','-O','datamind_app',database],check=True,capture_output=True)
    try:
        dump=subprocess.run(docker+['pg_dump','-U','postgres','-Fc','--exclude-table-data=app_settings',
                                   '--exclude-table-data=auth_users','--exclude-table-data=auth_sessions',
                                   '--exclude-table-data=auth_login_limits',
                                   # chat_sessions.user_id FK-references auth_users, whose DATA is
                                   # excluded above; keeping session rows would make pg_restore fail
                                   # when it re-adds the FK (owner missing), aborting the whole run.
                                   '--exclude-table-data=chat_sessions',
                                   # Same rule, second family: every wiki row hangs off
                                   # wiki_entries (owner_id -> auth_users) or off a wiki
                                   # revision. Excluding only the three heads left
                                   # wiki_provenance_links.revision_id dangling and the
                                   # restore still aborted on the re-added FK. The wiki
                                   # tests build their own entries, so no fixture is lost.
                                   '--exclude-table-data=wiki_entries',
                                   '--exclude-table-data=wiki_revisions',
                                   '--exclude-table-data=wiki_assertions',
                                   '--exclude-table-data=wiki_evidence_refs',
                                   '--exclude-table-data=wiki_provenance_links',
                                   '--exclude-table-data=wiki_relations',
                                   '--exclude-table-data=wiki_review_events',
                                   '--exclude-table-data=wiki_build_jobs',
                                   '--exclude-table-data=source_authority_rules','--exclude-table-data=source_authority_events',
                                   '--exclude-table-data=cad_assets','--exclude-table-data=cad_versions',
                                   '--exclude-table-data=cad_association_reviews',
                                   '--exclude-table-data=dimension_measurements','--exclude-table-data=dimension_measurement_versions',
                                   '--exclude-table-data=dimension_bindings','--exclude-table-data=dimension_binding_versions',
                                   '--exclude-table-data=dimension_approval_events','--exclude-table-data=dimension_comparisons',
                                   '--exclude-table-data=file_jobs','--exclude-table-data=file_job_items',
                                   '--exclude-table-data=pdf_snapshot_assets','--exclude-table-data=pdf_snapshot_versions',
                                   '--exclude-table-data=pdf_knowledge_requests','--exclude-table-data=pdf_knowledge_heads',
                                   'datamind_meta'],check=True,capture_output=True).stdout
        subprocess.run(['docker','exec','-i',args.container,'pg_restore','-U','postgres','-d',database],
                       input=dump,check=True,capture_output=True)

        def isolated_connect(dsn=None,*connect_args,**kwargs):
            options=parse_dsn(dsn) if dsn else {}
            options.update(kwargs)
            if options.get('dbname')=='datamind_meta':options['dbname']=database
            return original_connect(*connect_args,**options)

        psycopg2.connect=isolated_connect
        # Runtime APIs only accept repository-owned files. Adapt paths in the
        # clone when this command runs in a different checkout/worktree.
        from datamind.agent import _write_dsn
        with psycopg2.connect(_write_dsn()) as conn,conn.cursor() as cur:
            cur.execute('SELECT document_id,object_uri FROM document_assets')
            for document_id,uri in cur.fetchall():
                # Stored object_uri keeps the checkout's native separator; normalise so the
                # '/data/' split also matches a Windows path (D:\...\data\knowledge_base\x.pdf).
                _,marker,suffix=(uri or '').replace('\\','/').partition('/data/')
                source=(ROOT/'data'/suffix).resolve() if marker else None
                if not source or not source.is_relative_to(ROOT/'data') or not source.is_file():
                    raise RuntimeError('Clone source unavailable: '+document_id)
                cur.execute('UPDATE document_assets SET object_uri=%s WHERE document_id=%s',(str(source),document_id))
            cur.execute("SELECT document_id,object_uri FROM document_assets WHERE file_type='pdf'")
            pdf_sources=cur.fetchall()
        import pymupdf
        for document_id,uri in pdf_sources:
            directory=ROOT/'data/object_store/page_images'/document_id;directory.mkdir(parents=True,exist_ok=True)
            with pymupdf.open(uri) as pdf:
                for i,page in enumerate(pdf):
                    (directory/f'page_{i+1:03d}.png').write_bytes(page.get_pixmap(dpi=110).tobytes('png'))
        # ORT's optional telemetry HTTP thread can race interpreter shutdown on
        # macOS. Keep regression local; this does not change model inference.
        try:
            import onnxruntime
        except ImportError:
            pass
        else:
            onnxruntime.disable_telemetry_events()
        runpy.run_path(str(ROOT/'db/seed_industrial.py'),run_name='__main__')
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
                     metadata_isolated=True,model_settings_copied=False,
                     private_pdf_snapshot_rows_copied=False,private_pdf_knowledge_rows_copied=False,
                     source_database_modified=False)
        # Record the run so the "current" numbers in PROGRESS.md / README.md can reference one
        # source instead of being hand-edited (that drift was reported three times). The file is
        # committed on purpose: it is the evidence of the last verified run, and
        # `tests/test_status_docs_sync.py` fails when the documents disagree with it.
        # Which tests failed, not just how many: `status_sync.py` distinguishes "the only red
        # tests are the status-drift ones" (publishing the numbers *is* the repair) from a real
        # regression (never publish). Without this the mechanism deadlocks: stale docs make the
        # drift test fail, and a red run would then refuse to publish the numbers that fix it.
        summary.update(commit=_git_commit(), ran_at=datetime.now(timezone.utc).isoformat(),
                       failed_tests=sorted(test.id() for test, _ in
                                           list(result.failures) + list(result.errors)))
        # `reports/` is entirely ignored as a run-artifact directory (it may not exist in a
        # fresh checkout or after `git clean`); create it instead of losing a finished run's
        # numbers to a FileNotFoundError.
        (ROOT/'reports').mkdir(parents=True, exist_ok=True)
        (ROOT/'reports/latest_regression.json').write_text(
            json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(summary))
        return 0 if result.wasSuccessful() else 1
    finally:
        psycopg2.connect=original_connect
        subprocess.run(docker+['dropdb','-U','postgres','--force',database],check=True,capture_output=True)


if __name__=='__main__':
    sys.path.insert(0,str(ROOT))
    raise SystemExit(main())
