"""
Client for the Evalueserve document parser.

The service is asynchronous: submit a job, poll for completion, then read the JSON
it wrote to the destination blob. Both source and destination live in this project's
own storage account.

The parser reports which engine it used per document ("AzureDocumentIntelligence" or
"Aspose"); it is chosen dynamically, so the engine is recorded on the Document rather
than assumed.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from . import locales
from .models import Cell, Document, Row, Table, parse_number
from .storage import store

logger = logging.getLogger(__name__)


class IDPError(RuntimeError):
    """The parser could not process the document."""


def _post(url: str, payload: Any, timeout: int = 120) -> str:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace").strip()


def _get(url: str, timeout: int = 60) -> str:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def build_payload(source_blob: str, dest_prefix: str) -> List[Dict[str, Any]]:
    """
    Assemble the parseDocument payload. Every field is configurable via .env so the
    parser's contract can change without touching code.
    """
    locator_type, locator_subtype, locator_id = settings.idp_locator
    endpoints = {
        "ConnectionString": settings.blob_connection_string,
        "ConnectionSASString": None,
        "ContainerName": settings.blob_container,
    }
    item: Dict[str, Any] = {
        "AiraRequestId": settings.idp_aira_request_id,
        "ProductClientId": settings.idp_product_client_id,
        "ProductProjectId": settings.idp_product_project_id,
        "LocatorType": locator_type,
        "LocatorSubType": locator_subtype,
        "LocatorId": locator_id,
        "Key": settings.idp_key,
        "SourceFile": {**endpoints, "FilePath": source_blob},
        "DestinationFile": {**endpoints, "FilePath": dest_prefix},
    }
    advance = settings.idp_advance_parsing
    if advance is not None:
        item["IsAdvanceParsing"] = advance
    return [item]


def submit(source_blob: str, dest_prefix: str) -> str:
    """Queue a parse job. Returns the parser's request id."""
    if not store.configured:
        raise IDPError("Blob storage must be configured; the parser reads and writes blobs")

    payload = build_payload(source_blob, dest_prefix)
    url = f"{settings.idp_base_url}/{settings.idp_parse_path}"

    try:
        raw = _post(url, payload, timeout=settings.idp_request_timeout)
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace")
        raise IDPError(f"{settings.idp_parse_path} returned {e.code}: {detail[:300]}") from e
    except Exception as e:
        raise IDPError(f"could not reach the document parser at {url}: {e}") from e

    request_id = raw.strip().strip('"')
    if not request_id:
        raise IDPError("parseDocument returned an empty request id")
    logger.info(f"IDP job {request_id} queued for {source_blob}")
    return request_id


def wait(request_id: str) -> Dict[str, Any]:
    """Poll until every file in the job is processed or failed."""
    url = f"{settings.idp_base_url}/{settings.idp_status_path}?requestId={request_id}"
    deadline = time.time() + settings.idp_poll_timeout
    status: Dict[str, Any] = {}

    while time.time() < deadline:
        try:
            status = json.loads(_get(url))
        except Exception as e:
            logger.warning(f"status poll failed for {request_id}: {e}")
            time.sleep(settings.idp_poll_interval)
            continue

        total = max(int(status.get("totalCount") or 1), 1)
        finished = int(status.get("processedCount") or 0) + int(status.get("failedCount") or 0)
        if finished >= total:
            if int(status.get("failedCount") or 0):
                raise IDPError(f"parser reported a failure: {status}")
            return status
        time.sleep(settings.idp_poll_interval)

    raise IDPError(
        f"parser did not finish within {settings.idp_poll_timeout}s (last status: {status})"
    )


def fetch_output(dest_prefix: str) -> Tuple[str, Dict[str, Any]]:
    """Read the JSON the parser wrote. Returns (blob name, parsed payload)."""
    container = store.service.get_container_client(settings.blob_container)
    candidates = [b.name for b in container.list_blobs(name_starts_with=dest_prefix)
                  if b.name.lower().endswith(".json")]
    if not candidates:
        raise IDPError(f"no parser output found under {dest_prefix}")
    blob_name = sorted(candidates)[-1]
    raw = container.download_blob(blob_name).readall()
    return blob_name, json.loads(raw.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# JSON -> Document
# --------------------------------------------------------------------------

def _rows_from_table_block(block: List[Dict[str, Any]], page: int) -> Tuple[str, List[str], List[Row]]:
    """
    Convert the parser's table shape into rows.

    The parser emits [{"row1": [{"cell1": "..."}, ...]}, ...]. The first cell of each
    row is the row label; the rest are data columns. The first row is the header.
    """
    grid: List[List[str]] = []
    for row_obj in block:
        if not isinstance(row_obj, dict) or not row_obj:
            continue
        (_, cells), = row_obj.items()
        values: List[str] = []
        for cell_obj in cells or []:
            if isinstance(cell_obj, dict) and cell_obj:
                (_, v), = cell_obj.items()
                values.append("" if v is None else str(v))
            else:
                values.append("")
        grid.append(values)

    if not grid:
        return "", [], []

    header = grid[0]
    label_header = header[0] if header else ""
    column_names = [h.strip() or f"Column {i + 1}" for i, h in enumerate(header[1:])]

    rows: List[Row] = []
    for i, raw_row in enumerate(grid[1:]):
        label = (raw_row[0] if raw_row else "").strip()
        cells: List[Cell] = []
        for j, raw_value in enumerate(raw_row[1:]):
            value = parse_number(raw_value)
            if value is not None or (raw_value or "").strip():
                cells.append(Cell(column=j, raw=raw_value, value=value))
        rows.append(Row(index=i, page=page, label=label, cells=cells))

    return label_header, column_names, rows


def to_document(file_name: str, payload: Dict[str, Any],
                locale: Optional[str] = None) -> Document:
    """Build a Document from the parser's JSON."""
    doc = Document(file_name=payload.get("fileName") or file_name)

    for page_obj in payload.get("content") or []:
        page_no = int(page_obj.get("pageNo") or 1)
        for block in page_obj.get("data") or []:
            if not isinstance(block, dict):
                continue
            if "heading" in block:
                heading = (block.get("heading") or "").strip()
                if heading:
                    doc.headings.append(heading)
            elif "table" in block:
                label_header, column_names, rows = _rows_from_table_block(
                    block["table"] or [], page_no
                )
                if rows:
                    doc.tables.append(Table(
                        page=page_no,
                        label_header=label_header,
                        column_names=column_names,
                        rows=rows,
                    ))

    if not doc.tables:
        raise IDPError(f"parser returned no tables for {file_name}")

    apply_locale(doc, locale)
    return doc


def apply_locale(doc: Document, declared: Optional[str] = None) -> Dict[str, Any]:
    """
    Read every cell using the declared locale, and check the document agrees.

    The analyst knows where the document came from, so the declared locale is the source
    of truth. Detection runs alongside it purely to disagree: a document containing
    31/12/2023 cannot be month-first, whatever was selected, and that is worth saying
    out loud. It is the case that bites in practice - a US team receiving one property's
    rent roll from the UK, or a pack that mixes regions - where sorting by date silently
    puts part of the portfolio in the wrong order.

    Cells are parsed twice on purpose: the first pass only gathers raw text, because the
    convention cannot be checked until the whole document has been seen.
    """
    declared = (declared or settings.document_locale or "AUTO").upper()
    declared_numbers, declared_dates = locales.preset(declared)

    raw_values: List[str] = []
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.raw:
                    raw_values.append(cell.raw)

    found_numbers = locales.detect_number_format(raw_values)
    found_dates = locales.detect_date_order(raw_values)

    conflicts: List[str] = []

    number_format = declared_numbers or found_numbers
    if declared_numbers and found_numbers.confident and (
        found_numbers.decimal != declared_numbers.decimal
    ):
        conflicts.append(
            f"{declared} was selected, but the figures are written with '"
            f"{found_numbers.decimal}' as the decimal mark ({found_numbers.evidence}). "
            f"The document's own convention was used."
        )
        number_format = found_numbers

    date_format = declared_dates or found_dates
    if declared_dates and found_dates.confident and found_dates.order != declared_dates.order:
        conflicts.append(
            f"{declared} implies {declared_dates.order} dates, but this document "
            f"contains a value that can only be {found_dates.order} "
            f"({found_dates.evidence}). The document's own convention was used, "
            f"otherwise those dates would be wrong by up to 11 months."
        )
        date_format = found_dates

    numbers = dates = 0
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell.value = parse_number(cell.raw, number_format)
                cell.date_value = (
                    locales.parse_date(cell.raw, date_format)
                    if cell.value is None else None
                )
                numbers += cell.value is not None
                dates += cell.date_value is not None

    detected = {
        "declared": declared,
        "numbers": number_format.describe(),
        "dates": date_format.describe(),
        "date_order": date_format.order,
        "detected_numbers": found_numbers.describe(),
        "detected_dates": found_dates.describe(),
        "conflicts": conflicts,
        "figures_read": numbers,
        "dates_read": dates,
    }
    doc.meta["locale"] = detected

    for conflict in conflicts:
        logger.warning(f"locale conflict in {doc.file_name}: {conflict}")
    logger.info(
        f"locale for {doc.file_name}: declared {declared}; "
        f"{numbers} figures, {dates} dates"
    )
    return detected


def submit_document(pdf_bytes: bytes, file_name: str, job_id: str) -> Dict[str, Any]:
    """
    Upload a PDF and queue it, without waiting.

    Split from collect_document so a batch can hand the parser every document before
    waiting on any of them. The service is asynchronous, so submitting twelve documents
    and then collecting them takes about as long as the slowest one rather than the sum.
    """
    prefix = f"{settings.idp_work_prefix}/{job_id}"
    source_blob = store.put_bytes(
        f"{prefix}/source/{store.safe_name(file_name)}", pdf_bytes, "application/pdf"
    )
    request_id = submit(source_blob, f"{prefix}/parsed")
    return {
        "job_id": job_id,
        "file_name": file_name,
        "request_id": request_id,
        "source_blob": source_blob,
        "dest_prefix": f"{prefix}/parsed",
    }


def collect_document(submission: Dict[str, Any], locale: Optional[str] = None) -> Document:
    """Wait for a queued document and turn the parser's response into a Document."""
    status = wait(submission["request_id"])
    output_blob, payload = fetch_output(submission["dest_prefix"])

    document = to_document(submission["file_name"], payload, locale=locale)
    document.parsing_engine = status.get("ParsingEngine")
    document.source_blob = submission["source_blob"]
    document.parser_output_blob = output_blob
    logger.info(
        f"parsed {submission['file_name']} with {document.parsing_engine}: "
        f"{document.row_count()} rows, {document.figure_count()} figures"
    )
    return document


def parse_pdf(pdf_bytes: bytes, file_name: str, job_id: str,
              locale: Optional[str] = None) -> Document:
    """
    Full round trip for a single document: upload, queue, wait, read.

    The PDF and the parser's JSON are both retained in blob storage, so any figure can
    be traced back to the exact source document and the exact parser response.
    """
    return collect_document(submit_document(pdf_bytes, file_name, job_id), locale=locale)
