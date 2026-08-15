"""
JSON and CSV output.

Both preserve the full table: every row, every column, hierarchy and role. Excel lives
in workbook.py because it carries the reconciliation and audit sheets too.
"""

import csv
import json
from datetime import date, datetime
from io import StringIO
from typing import Any, Dict, List

from .models import Document
from .reconcile import Check, summarize


def _json_default(value: Any) -> Any:
    """Dates reach the workbook as real dates; JSON and CSV get ISO strings."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _flat(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def to_json(document: Document, checks: List[Check], state: Dict[str, Any]) -> str:
    """Complete structured payload, including verification and audit trail."""
    summary = summarize(checks)
    table = document.primary_table

    payload: Dict[str, Any] = {
        "document": {
            "file_name": document.file_name,
            "parsing_engine": document.parsing_engine,
            "source_blob": document.source_blob,
            "parser_output_blob": document.parser_output_blob,
            "headings": document.headings,
            "row_count": document.row_count(),
            "figure_count": document.figure_count(),
        },
        # every value below this point was read deterministically
        "interpretation": document.meta,
        "reconciliation": summary,
        "checks": [c.to_dict() for c in checks],
        "agent": {
            "repair_rounds": state.get("rounds", 0),
            "repairs": state.get("repairs") or [],
            "structure_notes": state.get("structure_notes") or [],
            "trace": state.get("trace") or [],
        },
        "tables": [],
    }

    for t in document.tables:
        payload["tables"].append({
            "page": t.page,
            "label_header": t.label_header,
            "column_names": t.column_names,
            "total_column": t.total_column,
            "rows": [
                {
                    "row": r.index + 1,
                    "label": r.label,
                    "depth": r.depth,
                    "role": r.role,
                    "is_subtotal": r.block_start is not None,
                    "values": r.display(t.ncols),
                    "repairs": r.repairs,
                }
                for r in t.rows
                if r.label or r.has_figures
            ],
        })

    # display() yields real dates so the workbook can sort them; JSON needs ISO strings
    return json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)


def to_csv(document: Document) -> str:
    """
    Flat CSV of the primary table, one line per row, one column per period/segment.

    Hierarchy is preserved in two machine-readable columns (depth, role) rather than as
    indentation, so the file stays usable in a spreadsheet or a dataframe.
    """
    table = document.primary_table
    output = StringIO()
    if table is None:
        return ""

    writer = csv.writer(output)
    writer.writerow(
        ["page", "row", "depth", "role", table.label_header or "line_item"]
        + list(table.column_names)
    )
    for row in table.rows:
        if not row.label and not row.has_figures:
            continue
        writer.writerow(
            [row.page, row.index + 1, row.depth, row.role, row.label]
            + ["" if v is None else _flat(v) for v in row.display(table.ncols)]
        )
    return output.getvalue()


def reconciliation_csv(checks: List[Check]) -> str:
    """The exception report on its own, for pasting into a QA log."""
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["check", "description", "document_states", "figures_add_to",
                     "difference", "result", "location"])
    for c in sorted(checks, key=lambda x: (x.passed, x.kind)):
        writer.writerow([
            c.kind, c.description,
            "" if c.printed is None else c.printed,
            "" if c.computed is None else c.computed,
            "" if c.delta is None else c.delta,
            "ties" if c.passed else "EXCEPTION",
            c.location,
        ])
    return output.getvalue()
