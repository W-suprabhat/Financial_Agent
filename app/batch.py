"""
Batch processing.

Analysts do not receive one document, they receive a deal pack: twelve monthly rent
rolls, a trailing-twelve P&L, a handful of statements. Processing those one at a time
means waiting out the parser once per file, which for a pack of twelve is close to
twenty minutes of sitting and watching.

The parser is asynchronous, so the wait is avoidable: hand it every document first, then
collect them. Twelve documents then take about as long as the slowest one rather than
the sum of all of them.

    submit  file 1..N  ->  the parser works on all of them at once
    collect file 1..N  ->  each is reconciled as soon as its response lands

Progress is written to blob after every state change, so the browser can be closed and
the batch still finishes.
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import agent as agent_module
from . import idp, provenance
from .config import settings
from .storage import store

logger = logging.getLogger(__name__)

BATCH_PREFIX = "financial-agent/batches"

QUEUED = "queued"
SUBMITTED = "submitted"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Batch:
    """One pack of documents, processed together."""

    def __init__(self, batch_id: str, files: List[Tuple[str, bytes]],
                 locale: Optional[str] = None):
        self.batch_id = batch_id
        self.locale = locale
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {
            "batch_id": batch_id,
            "status": QUEUED,
            "created_at": _now(),
            "finished_at": None,
            "total": len(files),
            "documents": [
                {
                    "index": i,
                    "file_name": name,
                    "size": len(data),
                    "status": QUEUED,
                    "job_id": None,
                    "error": None,
                }
                for i, (name, data) in enumerate(files)
            ],
        }
        self._files = files

    # --- state ------------------------------------------------------------

    def _update(self, index: int, **fields) -> None:
        with self.lock:
            self.state["documents"][index].update(fields)
            self._save()

    def _set_status(self, status: str) -> None:
        with self.lock:
            self.state["status"] = status
            if status in (DONE, FAILED):
                self.state["finished_at"] = _now()
            self._save()

    def _save(self) -> None:
        """Persist progress so a closed browser does not lose the batch."""
        if not store.configured:
            return
        try:
            import json
            store.put_bytes(
                f"{BATCH_PREFIX}/{self.batch_id}.json",
                json.dumps(self.state, indent=2, default=str).encode("utf-8"),
                "application/json",
            )
        except Exception as e:
            logger.warning(f"could not save batch {self.batch_id}: {e}")

    @property
    def summary(self) -> Dict[str, Any]:
        with self.lock:
            docs = self.state["documents"]
            return {
                **self.state,
                "completed": sum(1 for d in docs if d["status"] in (DONE, FAILED)),
                "succeeded": sum(1 for d in docs if d["status"] == DONE),
                "failed": sum(1 for d in docs if d["status"] == FAILED),
            }

    # --- work -------------------------------------------------------------

    def run(self) -> None:
        """Submit everything, then collect and reconcile each as it lands."""
        self._set_status(SUBMITTED)

        submissions: Dict[int, Dict[str, Any]] = {}
        for i, (name, data) in enumerate(self._files):
            job_id = uuid.uuid4().hex[:16]
            try:
                submissions[i] = idp.submit_document(data, name, job_id)
                self._update(i, status=SUBMITTED, job_id=job_id)
            except Exception as e:
                logger.error(f"could not submit {name}: {e}")
                self._update(i, status=FAILED, error=str(e))

        if not submissions:
            self._set_status(FAILED)
            return

        self._set_status(RUNNING)
        logger.info(
            f"batch {self.batch_id}: {len(submissions)} document(s) queued with the "
            f"parser, collecting"
        )

        # Collect concurrently. The parser is already working on all of them; this only
        # bounds how many responses are turned into workbooks at once.
        workers = min(len(submissions), settings.batch_workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._collect_one, i, submissions[i]): i
                for i in submissions
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"document {index} failed: {e}")
                    self._update(index, status=FAILED, error=str(e))

        self._set_status(DONE)
        done = self.summary
        logger.info(
            f"batch {self.batch_id} finished: {done['succeeded']} succeeded, "
            f"{done['failed']} failed"
        )

    def _collect_one(self, index: int, submission: Dict[str, Any]) -> None:
        name = submission["file_name"]
        self._update(index, status=RUNNING)

        document = idp.collect_document(submission, locale=self.locale)
        state = agent_module.run(
            None, name, submission["job_id"], locale=self.locale, document=document
        )

        if state.get("error") or state.get("document") is None:
            self._update(index, status=FAILED, error=state.get("error") or "extraction failed")
            return

        checks = state.get("checks") or []
        # reconciliation_summary rather than reconcile.summarize, and passed through whole:
        # the four pass/fail keys this used to hand-pick left out coverage, so a batched
        # document reached the browser with no way to say how far the checks reached - and
        # the batch is the path the UI actually uses.
        summary = provenance.reconciliation_summary(state["document"], checks)
        saved = _persist(submission["job_id"], state["document"], checks, state,
                          locale=self.locale)

        self._update(
            index,
            status=DONE,
            record_saved=saved,
            parsing_engine=state["document"].parsing_engine,
            rows=state["document"].row_count(),
            figures=state["document"].figure_count(),
            reconciliation=summary,
            # The full list, not a count: a withdrawn rule is only detectable client-side
            # by checking whether its id appears here, the same way a single /extract
            # result is checked.
            applied_rules=state.get("applied_rules") or [],
            proposals=state.get("proposals") or [],
            findings=state.get("findings") or [],
        )


def _persist(job_id, document, checks, state, locale=None) -> bool:
    from .main import _persist_record  # imported here to avoid a circular import
    return _persist_record(job_id, document, checks, state, locale=locale)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

_batches: Dict[str, Batch] = {}
_registry_lock = threading.Lock()


def start(files: List[Tuple[str, bytes]], locale: Optional[str] = None) -> Batch:
    """Queue a pack and start work in the background."""
    batch_id = uuid.uuid4().hex[:16]
    batch = Batch(batch_id, files, locale)
    with _registry_lock:
        _batches[batch_id] = batch

    thread = threading.Thread(target=batch.run, name=f"batch-{batch_id}", daemon=True)
    thread.start()
    return batch


def get(batch_id: str) -> Optional[Dict[str, Any]]:
    """Live state if the batch is still in this process, otherwise the saved copy."""
    with _registry_lock:
        batch = _batches.get(batch_id)
    if batch is not None:
        return batch.summary

    if not store.configured:
        return None
    try:
        import json
        raw = store.service.get_container_client(store.container_name) \
            .download_blob(f"{BATCH_PREFIX}/{batch_id}.json").readall()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None
