"""
Provenance: where each figure came from, and what proves it.

Every other tool in this space asks you to trust a number because a model produced it.
Nothing here produces a number - the figures are moved by deterministic code from the
parser's grid to the workbook - so each one can still name the page it was printed on,
the row and column it sat in, and the characters the document actually used. That last
part matters more than it sounds: a reviewer sent to check "-1234.56" against a page
that reads "(1,234.56)" will not find it.

The second half of a citation is the arithmetic. reconcile.py already decides whether a
figure is consistent with the ones around it; this module attaches those verdicts to the
individual figures so the question "how do you know this is right?" has an answer per
cell rather than per document.

Three statuses, and the distinction between the last two is the honest one:

    ties        a check covers this figure and it holds
    exception   a check covers this figure and it does not - with the amount it is out by
    unchecked   nothing cross-foots this figure

An unchecked figure is not a passing figure. Reporting it as one would claim proof this
project does not have, on exactly the documents - rent rolls, schedules with no totals -
where columns are not parts of a whole and reconcile.py deliberately withholds checks.

No model is involved anywhere in this module.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import reconcile
from .models import Document
from .reconcile import Check

TIES = "ties"
EXCEPTION = "exception"
UNCHECKED = "unchecked"

# Every Check built by reconcile.py opens its location with "page {n}" - see the four
# construction sites there. Read rather than assumed, so a check whose location does not
# say applies to the document rather than silently to page 1.
_PAGE = re.compile(r"page\s+(\d+)", re.IGNORECASE)


def page_of(check: Check) -> Optional[int]:
    """The page a check refers to, or None if it does not name one."""
    match = _PAGE.search(check.location or "")
    return int(match.group(1)) if match else None


@dataclass
class Citation:
    """One figure, and everything known about where it came from."""

    page: int
    sheet: str                      # worksheet the figure lands on, if known
    row_index: int                  # 0-based position within the table
    row_label: str                  # label exactly as printed
    column: str                     # column heading as printed
    printed: str                    # the figure exactly as the document wrote it
    value: Optional[float]          # after locale parsing and sign normalization
    status: str = UNCHECKED
    out_by: Optional[float] = None  # signed gap on the worst failing check
    checks: List[str] = field(default_factory=list)
    repairs: List[str] = field(default_factory=list)

    @property
    def is_exception(self) -> bool:
        return self.status == EXCEPTION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _checks_by_page(checks: List[Check]):
    """
    Split checks into the ones that name a row and the ones that cover a whole page.

    identities() reports against a page - "assets = liabilities + equity" is not a
    statement about one row - and that is still proof for the figures on it.
    """
    by_row: Dict[tuple, List[Check]] = {}
    by_page: Dict[int, List[Check]] = {}
    for check in checks:
        page = page_of(check)
        if page is None:
            continue
        if check.row_index is None:
            by_page.setdefault(page, []).append(check)
        else:
            by_row.setdefault((page, check.row_index), []).append(check)
    return by_row, by_page


def build(document: Document, checks: List[Check],
          sheet_of_page: Optional[Dict[int, str]] = None) -> List[Citation]:
    """
    One Citation per figure, exceptions first.

    Only cells holding a number are cited. A resident name or a lease date is carried
    through to the workbook, but it is not a figure and reconciliation says nothing
    about it - listing it here would pad the coverage claim with rows nothing was ever
    going to check.
    """
    sheets = sheet_of_page or {}
    by_row, by_page = _checks_by_page(checks or [])

    citations: List[Citation] = []
    for table in document.tables:
        for row in table.rows:
            covering = by_row.get((row.page, row.index), []) + by_page.get(row.page, [])
            failing = [c for c in covering if not c.passed]

            if failing:
                status = EXCEPTION
                worst = max(failing, key=lambda c: abs(c.delta or 0.0))
                out_by = worst.delta
            elif covering:
                status, out_by = TIES, None
            else:
                status, out_by = UNCHECKED, None

            for cell in row.cells:
                if cell.value is None:
                    continue  # not a figure
                column = (
                    table.column_names[cell.column]
                    if 0 <= cell.column < len(table.column_names)
                    else f"Column {cell.column + 1}"
                )
                citations.append(Citation(
                    page=row.page,
                    sheet=sheets.get(row.page, ""),
                    row_index=row.index,
                    row_label=row.label,
                    column=column,
                    printed=cell.raw,
                    value=cell.value,
                    status=status,
                    out_by=out_by,
                    checks=[c.description for c in covering],
                    repairs=list(row.repairs),
                ))

    # Exceptions first - the sheet opens as a worklist - and document order within each
    # status, so a reviewer reading down the page follows the document.
    citations.sort(key=lambda c: 0 if c.status == EXCEPTION else 1)
    return citations


def reconciliation_summary(document: Document, checks: List[Check]) -> Dict[str, Any]:
    """
    The reconciliation summary every caller should report: status *and* its coverage.

    reconcile.summarize answers "did the checks pass"; it cannot answer "how much did the
    checks reach", because reaching that answer needs the document and reconcile is
    deliberately not given it. Reported alone the status misleads - "1 exception" over
    nine verified figures out of 846 reads like a clean document - so the pair is built
    here, once, instead of being left to four call sites to remember.
    """
    summary = reconcile.summarize(checks)
    summary["coverage"] = coverage(document, checks)
    return summary


def coverage(document: Document, checks: List[Check]) -> Dict[str, Any]:
    """
    What fraction of the figures a check actually covers.

    Derived from the citations rather than counted separately, so this can never disagree
    with the Provenance worksheet - the two would otherwise drift and leave nobody able to
    say which number was the true one.
    """
    counts = summarize(build(document, checks))
    figures = counts["figures"]
    verified = counts["ties"] + counts["exceptions"]
    return {
        "figures": figures,
        "verified": verified,
        "unchecked": counts["unchecked"],
        "coverage_pct": round(100.0 * verified / figures, 1) if figures else 0.0,
    }


def summarize(citations: List[Citation]) -> Dict[str, Any]:
    """Counts for the header of the sheet and the API response."""
    return {
        "figures": len(citations),
        "cited": sum(1 for c in citations if c.printed and c.row_label),
        "ties": sum(1 for c in citations if c.status == TIES),
        "exceptions": sum(1 for c in citations if c.status == EXCEPTION),
        "unchecked": sum(1 for c in citations if c.status == UNCHECKED),
    }
