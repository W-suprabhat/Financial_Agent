"""
FastAPI application.

Serves the single-page UI at / and the API under /api. Source PDFs, the parser's raw
JSON, and every generated file are retained in Azure Blob Storage so any figure in a
deliverable can be traced back to its origin.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Body, Cookie, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from datetime import datetime, timezone

from . import __version__
from . import agent as agent_module
from . import auth
from . import batch as batch_module
from . import formatters, idp, locales, provenance, reconcile, rules, structure, workbook
from .auth import User, current_user
from .config import STATIC_DIR, settings
from .models import Document
from .storage import CONTENT_TYPES, EXTENSIONS, store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The Azure storage SDK logs full request/response headers at INFO, which buries
# our own logs on every blob call.
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.storage").setLevel(logging.WARNING)

app = FastAPI(
    title="Financial Statement Agent",
    description=(
        "Extracts financial statements in full and proves its own work: every figure "
        "is reconciled against the document's own arithmetic."
    ),
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_FORMATS = ("json", "csv", "excel")


def _require_format(fmt: str) -> None:
    if fmt not in VALID_FORMATS:
        raise HTTPException(status_code=400, detail="format must be json, csv, or excel")


def _run_agent(file_name: str, pdf_bytes: bytes, locale: Optional[str] = None) -> tuple:
    job_id = uuid.uuid4().hex[:16]
    state = agent_module.run(pdf_bytes, file_name, job_id, locale=locale)

    if state.get("error") or state.get("document") is None:
        raise HTTPException(status_code=502, detail=state.get("error") or "extraction failed")

    document: Document = state["document"]
    checks: List[reconcile.Check] = state.get("checks") or []
    return job_id, document, checks, state


def _persist_record(job_id: str, document: Document, checks, state,
                     locale: Optional[str] = None, rerun_of: Optional[str] = None) -> bool:
    """
    Store the extraction so approvals and exports can find it later.

    Returns whether it worked. A failure here used to be logged as a warning and
    otherwise ignored, so extraction returned 200 while approving a fix later failed
    with "no stored extraction" - and since the approval never completed, the same fix
    was proposed again on every upload. The caller now surfaces this.

    locale is kept so /api/rerun can reprocess the document the way it was originally
    asked to be read, rather than falling back to the account default. rerun_of names
    the job this one replays with the current rules, if any - the original stays
    exactly as produced, at its own job_id; nothing here overwrites it.
    """
    if not store.configured:
        return False
    try:
        store.put_record(job_id, {
            "job_id": job_id,
            "locale": locale,
            "rerun_of": rerun_of,
            "document": document.to_dict(),
            "checks": [c.to_dict() for c in checks],
            "reconciliation": provenance.reconciliation_summary(document, checks),
            "agent": {
                "rounds": state.get("rounds", 0),
                "repairs": state.get("repairs") or [],
                "applied_rules": state.get("applied_rules") or [],
                "proposals": state.get("proposals") or [],
                "findings": state.get("findings") or [],
                "fingerprints": state.get("fingerprints") or [],
                "trace": state.get("trace") or [],
            },
        })
        return True
    except Exception as e:
        logger.error(
            f"could not persist record for {job_id}: {e}. Approvals and exports for this "
            f"job will not work until this is fixed."
        )
        return False


def _extraction_payload(job_id: str, file_name: str, document: Document, checks, state,
                         saved: bool, rerun_of: Optional[str] = None) -> Dict[str, Any]:
    """
    The JSON body /extract and /api/rerun both return: one document's result plus what
    the agent did to it. Shared so the two routes cannot drift into describing the same
    shape two different ways.
    """
    summary = provenance.reconciliation_summary(document, checks)
    table = document.primary_table
    body = {
        "status": "success",
        "document": file_name,
        "job_id": job_id,
        "parsing_engine": document.parsing_engine,
        "extracted": {
            "tables": len(document.tables),
            "rows": document.row_count(),
            "figures": document.figure_count(),
            "columns": table.column_names if table else [],
        },
        "reconciliation": summary,
        "agent": {
            "repair_rounds": state.get("rounds", 0),
            "repairs": state.get("repairs") or [],
            # fixes reused from a previous approval - no human needed
            "applied_rules": state.get("applied_rules") or [],
            # computed but unapproved: one click makes them permanent
            "proposals": state.get("proposals") or [],
            # too risky to change; described so the analyst knows what to distrust
            "findings": state.get("findings") or [],
            "trace": state.get("trace") or [],
        },
        # False means approvals and exports for this job cannot work
        "record_saved": saved,
        "interpretation": document.meta,
        "hierarchy_preview": structure.tree_lines(table, 60) if table else [],
    }
    if rerun_of:
        body["rerun_of"] = rerun_of
    return body


def _build(document: Document, checks, state, fmt: str) -> bytes:
    if fmt == "excel":
        return workbook.build(document, checks, state)
    if fmt == "csv":
        return formatters.to_csv(document).encode("utf-8")
    return formatters.to_json(document, checks, state).encode("utf-8")


def _file_response(content: bytes, fmt: str, basename: str) -> Response:
    blob_name = None
    if store.configured:
        try:
            blob_name = store.put_output(fmt, basename, content)
        except Exception as e:
            logger.warning(f"could not archive {fmt} output: {e}")
    headers = {"Content-Disposition": f'attachment; filename="{basename}.{EXTENSIONS[fmt]}"'}
    if blob_name:
        headers["X-Blob-Name"] = blob_name
    return Response(content=content, media_type=CONTENT_TYPES[fmt], headers=headers)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index(fa_session: Optional[str] = Cookie(default=None)):
    # A page, so an unknown visitor is sent to sign in rather than handed a 401 body.
    # The API answers 401 instead - see auth.current_user.
    if auth.user_from_cookie(fa_session) is None:
        return RedirectResponse("/login", status_code=303)
    path = STATIC_DIR / "index.html"
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return JSONResponse({"detail": "UI not found; see /api/info"}, status_code=404)


@app.get("/login", include_in_schema=False)
async def login_page():
    path = STATIC_DIR / "login.html"
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return JSONResponse({"detail": "login page not found"}, status_code=404)


# --------------------------------------------------------------------------
# access
# --------------------------------------------------------------------------
# Not authentication - a typed name and nothing else. It exists so an approval can
# name a person; see app/auth.py for the limits of that claim.

@app.post("/auth/login", include_in_schema=False)
async def sign_in(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="your name is required")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="that name is too long")

    response = JSONResponse({"name": name})
    response.set_cookie(
        auth.COOKIE,
        auth.issue(name),
        max_age=auth.MAX_AGE,
        httponly=True,      # the page never needs to read it, so script cannot either
        samesite="lax",     # CORS here allows any origin; Lax keeps the cookie off
                            # cross-site requests regardless
        path="/",
    )
    return response


@app.post("/auth/logout", include_in_schema=False)
async def sign_out():
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/auth/me", include_in_schema=False)
async def whoami(user: User = Depends(current_user)):
    return user.to_dict()


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "Financial Statement Agent",
        "model_deployment": settings.openai_deployment,
        "azure_openai_configured": settings.openai_configured,
        "blob_configured": store.configured,
        "idp_base_url": settings.idp_base_url,
    }


@app.get("/api/info")
async def api_info(_user: User = Depends(current_user)):
    return {
        "service": "Financial Statement Agent",
        "version": __version__,
        "approach": (
            "The document is extracted in full by the Evalueserve IDP parser. Hierarchy "
            "is recovered from row labels without coordinates, then every figure is "
            "reconciled against the document's own arithmetic. Exceptions are repaired "
            "and re-verified. A model is used only to name the statement type and "
            "period; it never reads a figure."
        ),
        "agent": {
            "framework": "LangGraph",
            "nodes": ["parse_document", "infer_structure", "verify", "repair",
                      "describe", "finalize"],
            "repair_strategies": [s.__name__ for s in __import__(
                "app.repair", fromlist=["STRATEGIES"]).STRATEGIES],
            "max_repair_rounds": settings.max_repair_rounds,
            "reconcile_tolerance": settings.reconcile_tolerance,
        },
        "parser": {
            "base_url": settings.idp_base_url,
            "parse_path": settings.idp_parse_path,
            "status_path": settings.idp_status_path,
            "work_prefix": settings.idp_work_prefix,
        },
        "locale": {
            "default": settings.document_locale,
            "options": {k: v.get("label") for k, v in locales.PRESETS.items()},
            "note": ("The analyst declares this; detection runs alongside only to flag a "
                     "document that disagrees."),
        },
        "storage": {
            "configured": store.configured,
            "container": store.container_name,
            "source_prefix": store.upload_prefix,
            "output_prefix": store.output_prefix,
        },
    }


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

@app.post("/extract")
async def extract(
    _user: User = Depends(current_user),
    file: UploadFile = File(...),
    output_format: str = Query("json", description="json|csv|excel"),
    locale: Optional[str] = Query(
        None,
        description="How this document writes figures and dates: US|UK|IN|EU|CH|ISO|AUTO. "
                    "Defaults to DOCUMENT_LOCALE.",
    ),
):
    """
    Extract one statement in full, reconcile it, and return the result.

    output_format=json returns the structured payload plus the reconciliation report
    and the job_id needed by /api/export. csv and excel stream a file.
    """
    _require_format(output_format)
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="file must be a PDF")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(pdf_bytes) > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"file is larger than {settings.max_file_size_bytes // (1024*1024)}MB",
        )

    job_id, document, checks, state = _run_agent(file.filename, pdf_bytes, locale)
    saved = _persist_record(job_id, document, checks, state, locale=locale)

    if output_format == "json":
        body = _extraction_payload(job_id, file.filename, document, checks, state, saved)
        body["data"] = json.loads(formatters.to_json(document, checks, state))
        return body

    basename = (file.filename or "statement").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _file_response(_build(document, checks, state, output_format),
                          output_format, basename)


@app.post("/api/rerun")
async def rerun(payload: dict = Body(...), user: User = Depends(current_user)):
    """
    Re-process a document from its original raw parse, picking up the rules approved
    right now rather than the ones that were approved when it first ran.

    Nothing about the earlier result changes - it stays exactly as produced, at its own
    job_id, downloadable and citable as it always was. A new job is created instead,
    the same way a withdrawn rule is moved aside rather than deleted: what already
    happened stays explainable.

    This is also the only step withdrawing a rule takes on its own: apply_learned_fixes
    reads rules.rule_store.rules_for(fp) fresh on every run, so a rule withdrawn a
    moment ago is already excluded here - no exclusion list to pass in.

    Body: {"job_id": "the job to redo"}
    """
    old_job_id = payload.get("job_id")
    if not old_job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    old = _record_or_404(old_job_id)

    old_document = old.get("document") or {}
    blob_name = old_document.get("parser_output_blob")
    if not blob_name:
        raise HTTPException(
            status_code=409,
            detail="no raw parse was retained for this job, so it cannot be re-run "
                   "(it predates re-run support, or storage was off when it ran)",
        )

    try:
        raw = store.get_by_blob_name(blob_name)
        parser_payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"could not re-read the original parse: {e}"
        )

    file_name = old_document.get("file_name") or "document.pdf"
    locale = old.get("locale")

    document = idp.to_document(file_name, parser_payload, locale=locale)
    document.parsing_engine = old_document.get("parsing_engine")
    document.source_blob = old_document.get("source_blob")
    document.parser_output_blob = blob_name

    new_job_id = uuid.uuid4().hex[:16]
    state = agent_module.run(None, file_name, new_job_id, locale=locale, document=document)
    if state.get("error") or state.get("document") is None:
        raise HTTPException(status_code=502, detail=state.get("error") or "re-run failed")

    document = state["document"]
    checks: List[reconcile.Check] = state.get("checks") or []
    saved = _persist_record(new_job_id, document, checks, state,
                             locale=locale, rerun_of=old_job_id)

    return _extraction_payload(new_job_id, file_name, document, checks, state,
                                saved, rerun_of=old_job_id)


@app.post("/api/batch")
async def start_batch(
    _user: User = Depends(current_user),
    files: List[UploadFile] = File(...),
    locale: Optional[str] = Query(None, description="US|UK|IN|EU|CH|ISO|AUTO"),
):
    """
    Queue a pack of documents and return immediately.

    Every file is handed to the parser before any of them is waited on, so a pack takes
    about as long as its slowest document rather than the sum. Poll /api/batch/{id} for
    progress; the work continues even if the browser is closed.
    """
    if not files:
        raise HTTPException(status_code=400, detail="no files supplied")
    if len(files) > settings.batch_max_files:
        raise HTTPException(
            status_code=400,
            detail=f"at most {settings.batch_max_files} files per batch",
        )

    payload: List[tuple] = []
    rejected: List[Dict[str, str]] = []
    for upload in files:
        if upload.content_type != "application/pdf":
            rejected.append({"file": upload.filename, "reason": "not a PDF"})
            continue
        data = await upload.read()
        if not data:
            rejected.append({"file": upload.filename, "reason": "empty"})
            continue
        if len(data) > settings.max_file_size_bytes:
            rejected.append({"file": upload.filename, "reason": "too large"})
            continue
        payload.append((upload.filename, data))

    if not payload:
        raise HTTPException(status_code=400, detail=f"no usable PDFs: {rejected}")

    started = batch_module.start(payload, locale)
    return {
        "batch_id": started.batch_id,
        "accepted": len(payload),
        "rejected": rejected,
        "poll": f"/api/batch/{started.batch_id}",
    }


@app.get("/api/batch/{batch_id}")
async def batch_status(batch_id: str, _user: User = Depends(current_user)):
    """Progress for a batch, including each document's reconciliation once it lands."""
    state = batch_module.get(batch_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no batch {batch_id}")
    return state


@app.post("/api/export")
async def export(payload: dict = Body(...), _user: User = Depends(current_user)):
    """
    Rebuild a file from a finished job.

    Body: {"job_id": "...", "format": "excel"}
    """
    job_id = payload.get("job_id")
    fmt = payload.get("format", "excel")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    _require_format(fmt)

    record = _record_or_404(job_id)
    document = _document_from_record(record)
    checks = _checks_from_record(record)
    state = record.get("agent") or {}

    basename = (document.file_name or "statement").rsplit(".", 1)[0]
    return _file_response(_build(document, checks, state, fmt), fmt, basename)


def _cell_from_record(data: Dict[str, Any]):
    """Rebuild a Cell, turning the stored ISO date back into a real date."""
    from datetime import date as _date
    from .models import Cell

    raw_date = data.get("date_value")
    parsed = None
    if isinstance(raw_date, str) and raw_date:
        try:
            parsed = _date.fromisoformat(raw_date[:10])
        except ValueError:
            parsed = None
    elif isinstance(raw_date, _date):
        parsed = raw_date

    return Cell(
        column=data.get("column", 0),
        raw=data.get("raw", ""),
        value=data.get("value"),
        date_value=parsed,
    )


def _document_from_record(record: Dict[str, Any]) -> Document:
    """Rebuild a Document from a persisted record."""
    from .models import Cell, Row, Table

    data = record.get("document") or {}
    document = Document(
        file_name=data.get("file_name") or "document.pdf",
        parsing_engine=data.get("parsing_engine"),
        source_blob=data.get("source_blob"),
        parser_output_blob=data.get("parser_output_blob"),
        headings=data.get("headings") or [],
        meta=data.get("meta") or {},
    )
    for t in data.get("tables") or []:
        table = Table(
            page=t.get("page", 1),
            label_header=t.get("label_header", ""),
            column_names=t.get("column_names") or [],
        )
        for r in t.get("rows") or []:
            table.rows.append(Row(
                index=r.get("index", 0),
                page=r.get("page", table.page),
                label=r.get("label", ""),
                cells=[_cell_from_record(c) for c in r.get("cells") or []],
                depth=r.get("depth", 0),
                role=r.get("role", "line_item"),
                block_start=r.get("block_start"),
                children=r.get("children") or [],
                repairs=r.get("repairs") or [],
            ))
        document.tables.append(table)
    return document


def _record_or_404(job_id: str) -> Dict[str, Any]:
    """Load a persisted job, or say plainly that it is gone."""
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is not configured")
    try:
        return store.get_record(job_id)
    except Exception as e:
        logger.error(f"could not read record {job_id}: {e}")
        raise HTTPException(
            status_code=404,
            detail=f"no stored extraction for job_id {job_id}; it may have expired",
        )


def _checks_from_record(record: Dict[str, Any]) -> List[reconcile.Check]:
    return [reconcile.Check(**{k: v for k, v in c.items() if k != "delta"})
            for c in record.get("checks") or []]


@app.get("/api/provenance/{job_id}")
async def job_provenance(job_id: str, status: Optional[str] = None,
                         _user: User = Depends(current_user)):
    """
    Where every figure in a finished job came from, and what proves it.

    One entry per figure: the page and row it was printed on, the text the document
    actually used, the value after locale parsing, and the reconciliation checks that
    cover it. `status=exception` narrows the list to the figures that need a human.

    The source PDF and the parser's own response are both named in the response, so a
    citation can be followed all the way back rather than only as far as this service.
    """
    record = _record_or_404(job_id)
    document = _document_from_record(record)
    checks = _checks_from_record(record)

    citations = provenance.build(document, checks,
                                 sheet_of_page=workbook.sheet_map(document))
    if status:
        wanted = status.strip().lower()
        if wanted not in (provenance.TIES, provenance.EXCEPTION, provenance.UNCHECKED):
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {provenance.TIES}, "
                       f"{provenance.EXCEPTION}, {provenance.UNCHECKED}",
            )
        citations = [c for c in citations if c.status == wanted]

    return {
        "job_id": job_id,
        "file_name": document.file_name,
        "parsing_engine": document.parsing_engine,
        "source_blob": document.source_blob,
        "parser_output_blob": document.parser_output_blob,
        # summary always describes the whole job, not the filtered slice, so a UI
        # showing only exceptions can still say how many figures there were
        "summary": provenance.summarize(
            provenance.build(document, checks) if status else citations
        ),
        "citations": [c.to_dict() for c in citations],
    }


@app.post("/api/rules/approve")
async def approve_fix(payload: dict = Body(...), user: User = Depends(current_user)):
    """
    Approve a proposed fix so it applies automatically from now on.

    Body: {"job_id": "...", "proposal_id": "..."} - the approver is the signed-in user.

    This is the point of the whole design: the analyst approves once, and every future
    document with the same layout is corrected without anyone being asked again.
    """
    job_id = payload.get("job_id")
    proposal_id = payload.get("proposal_id")
    approved_by = user.name   # not payload-supplied: the caller cannot approve as someone else
    # some proposals are a question rather than a yes/no, e.g. day-first or month-first
    choice = payload.get("choice")

    if not job_id or not proposal_id:
        raise HTTPException(status_code=400, detail="job_id and proposal_id are required")
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is required to remember fixes")

    try:
        record = store.get_record(job_id)
    except Exception as e:
        logger.error(f"could not read record {job_id}: {e}")
        raise HTTPException(status_code=404, detail=f"no stored extraction for job_id {job_id}")

    proposals = (record.get("agent") or {}).get("proposals") or []
    proposal = next((p for p in proposals if p.get("id") == proposal_id), None)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id} on this job")

    if proposal.get("choices"):
        valid = {c.get("value") for c in proposal["choices"]}
        if choice not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"this proposal needs a choice, one of {sorted(valid)}",
            )
        proposal = dict(proposal)
        proposal["params"] = {**(proposal.get("params") or {}), "order": choice}
        proposal["choice"] = choice

    fingerprint = proposal.get("fingerprint")
    if not fingerprint:
        raise HTTPException(status_code=400, detail="proposal has no layout fingerprint")

    try:
        rules.rule_store.approve(
            fingerprint, proposal, approved_by, layout=proposal.get("layout")
        )
    except Exception as e:
        logger.error(f"could not save approval: {e}")
        raise HTTPException(status_code=502, detail="could not save the approval")

    # Apply it to THIS job as well and re-store, so the download reflects the approval
    # immediately. Saving the rule alone only helped future documents, which meant
    # approving appeared to do nothing.
    rows_changed = 0
    try:
        document = _document_from_record(record)
        for table in document.tables:
            rows_changed += rules.apply_proposal(table, proposal)

        if rows_changed:
            try:
                from .describe import name_merged_columns
                renames = []
                for table in document.tables:
                    renames.extend(name_merged_columns(table))
                if renames:
                    document.meta.setdefault("column_headings_split", []).extend(renames)
            except Exception as e:
                logger.warning(f"heading split unavailable after approval: {e}")

            for table in document.tables:
                structure.infer(table)
            checks = reconcile.run_document(document)

            agent = record.get("agent") or {}
            entry = dict(proposal)
            entry["rows_changed"] = rows_changed
            entry["approved_by"] = approved_by
            agent["applied_rules"] = (agent.get("applied_rules") or []) + [entry]
            agent["proposals"] = [
                p for p in (agent.get("proposals") or []) if p.get("id") != proposal_id
            ]
            # recompute, so a structural warning the fix has resolved stops being shown
            agent["findings"] = rules.detect_report_only(document.tables)
            record["agent"] = agent
            record["document"] = document.to_dict()
            record["checks"] = [c.to_dict() for c in checks]
            record["reconciliation"] = provenance.reconciliation_summary(document, checks)
            store.put_record(job_id, record)
    except Exception as e:
        logger.error(f"approved but could not update job {job_id}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"the fix was saved for future documents but could not be applied to "
                   f"this job: {e}",
        )

    return {
        "status": "approved",
        "fingerprint": fingerprint,
        "proposal_id": proposal_id,
        "approved_by": approved_by,
        "affected_rows": proposal.get("affected_rows"),
        "rows_changed": rows_changed,
        "effect": (
            f"Applied to {rows_changed} rows in this document - download it again to see "
            f"the corrected columns. The fix is also saved against this layout, so future "
            f"documents with the same columns are corrected automatically."
        ),
    }


@app.post("/api/rules/correct")
async def correct(payload: dict = Body(...), user: User = Depends(current_user)):
    """
    Record a correction an analyst made by hand, so it applies from now on.

    Body: {"job_id", "kind": "relabel_row"|"rename_column", "before", "after"}
    The corrector is the signed-in user.

    The approval flow runs the other way round: the agent finds something, a person says
    yes. This is the direction a person actually works in - they see a label the parser
    mangled, they fix it, and they expect not to fix it again next month. Without this
    the correction lives in one downloaded file and is retyped every time.
    """
    job_id = payload.get("job_id")
    kind = (payload.get("kind") or "").strip()
    before = (payload.get("before") or "").strip()
    after = (payload.get("after") or "").strip()
    corrected_by = user.name

    if not job_id or not before or not after:
        raise HTTPException(status_code=400, detail="job_id, before and after are required")
    if kind not in rules.CORRECTABLE:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {list(rules.CORRECTABLE)}. A single figure is "
                   f"corrected for this document only and is never learned, because next "
                   f"month's document carries a different figure under the same label.",
        )
    if before == after:
        raise HTTPException(status_code=400, detail="the correction changes nothing")
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is required to remember fixes")

    try:
        record = store.get_record(job_id)
    except Exception as e:
        logger.error(f"could not read record {job_id}: {e}")
        raise HTTPException(status_code=404, detail=f"no stored extraction for job_id {job_id}")

    document = _document_from_record(record)

    # Save against the layouts the correction actually matches, not against the document
    # as a whole. A pack of pages can carry more than one layout, and a rule filed under
    # a layout it never matches is a rule that silently never runs.
    rows_changed, applied = 0, []
    for table in document.tables:
        fp = rules.fingerprint(table)
        entry = rules.correction(kind, fp, before, after).to_dict()
        changed = rules.apply_proposal(table, entry)
        if not changed:
            continue
        rows_changed += changed
        entry["affected_rows"] = changed
        if fp not in [a["fingerprint"] for a in applied]:
            rules.rule_store.approve(fp, entry, corrected_by)
            applied.append({"fingerprint": fp, "rule_id": entry["id"]})

    if not rows_changed:
        raise HTTPException(
            status_code=404,
            detail=f'nothing in this document reads "{before}"',
        )

    try:
        for table in document.tables:
            structure.infer(table)
        checks = reconcile.run_document(document)
        agent = record.get("agent") or {}
        agent["applied_rules"] = (agent.get("applied_rules") or []) + [{
            "kind": kind,
            "description": f'{kind.replace("_", " ")}: "{before}" reads "{after}"',
            "rows_changed": rows_changed,
            "authority": rules.PROPOSE,
            "proof": "corrected by an analyst",
            "approved_by": corrected_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }]
        record["agent"] = agent
        record["document"] = document.to_dict()
        record["checks"] = [c.to_dict() for c in checks]
        record["reconciliation"] = provenance.reconciliation_summary(document, checks)
        store.put_record(job_id, record)
    except Exception as e:
        logger.error(f"correction saved but could not update job {job_id}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"the correction was saved for future documents but could not be "
                   f"applied to this job: {e}",
        )

    return {
        "status": "corrected",
        "rows_changed": rows_changed,
        "learned_on": applied,
        "effect": (
            f'Changed {rows_changed} place(s) in this document - download it again to see '
            f'them. Saved against {len(applied)} layout(s), so "{before}" is corrected '
            f"automatically from now on."
        ),
    }


@app.delete("/api/rules/{fingerprint}/{rule_id}")
async def revoke_rule(fingerprint: str, rule_id: str,
                      user: User = Depends(current_user)):
    """
    Stop a saved fix from applying.

    Approve-once is only safe if it can be undone. A rule that turns out to be wrong
    would otherwise keep correcting every future document silently, and the person who
    approved it may not be the person who discovers it.
    """
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is not configured")
    try:
        rule = rules.rule_store.revoke(fingerprint, rule_id, user.name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"could not revoke {rule_id}: {e}")
        raise HTTPException(status_code=502, detail="could not revoke the fix")

    return {
        "status": "revoked",
        "fingerprint": fingerprint,
        "rule_id": rule_id,
        "revoked_by": user.name,
        "was_applied": rule.get("times_applied", 0),
        "effect": (
            "It no longer applies to new documents. Documents already produced are "
            "unchanged, and the withdrawal is recorded against the layout."
        ),
    }


@app.get("/api/rules")
async def list_rules(limit: int = Query(50, ge=1, le=200),
                     _user: User = Depends(current_user)):
    """Every fix an analyst has approved, and how often each has since been reused."""
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is not configured")
    try:
        learned = rules.rule_store.list_all(limit)
    except Exception as e:
        logger.error(f"could not list rules: {e}")
        raise HTTPException(status_code=502, detail="could not list learned fixes")
    reuses = sum(r.get("times_applied", 0) for entry in learned for r in entry["rules"])
    return {
        "layouts": len(learned),
        "rules": sum(len(entry["rules"]) for entry in learned),
        "total_reuses": reuses,
        "learned": learned,
    }


@app.get("/api/outputs")
async def outputs(
    fmt: Optional[str] = Query(None, description="json|csv|excel"),
    limit: int = Query(25, ge=1, le=100),
    _user: User = Depends(current_user),
):
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is not configured")
    if fmt:
        _require_format(fmt)
    try:
        return {"container": store.container_name, "outputs": store.list_outputs(fmt, limit)}
    except Exception as e:
        logger.error(f"could not list blobs: {e}")
        raise HTTPException(status_code=502, detail="could not list blob storage")


@app.get("/api/download")
async def download(blob: str = Query(..., description="blob name under the output prefix"),
                   _user: User = Depends(current_user)):
    if not store.configured:
        raise HTTPException(status_code=503, detail="blob storage is not configured")
    try:
        content = store.get_output(blob)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"could not read blob {blob}: {e}")
        raise HTTPException(status_code=404, detail="file not found")

    name = blob.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower()
    media = {"json": CONTENT_TYPES["json"], "csv": CONTENT_TYPES["csv"],
             "xlsx": CONTENT_TYPES["excel"]}.get(ext, "application/octet-stream")
    return Response(content=content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
