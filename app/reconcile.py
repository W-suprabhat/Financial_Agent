"""
Arithmetic reconciliation.

This is the quality signal. It replaces the "extraction confidence" percentage the
earlier version reported, which only ever measured how well a document fitted a fixed
template - a question the document never agreed to, and one whose answer was not
actionable.

A check either ties or states the amount it is out by and where to look. That is
something an analyst can act on, and it converts "verify all 163 figures" into
"review these 3 exceptions".
"""

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .config import settings
from .models import Document, Row, Table, normalize_label, subtotal_subject

CROSS_FOOT = "cross_foot"
COLUMN_FOOT = "column_foot"
SUBTOTAL = "subtotal"
IDENTITY = "identity"


@dataclass
class Check:
    kind: str
    description: str
    printed: Optional[float]      # what the document states
    computed: Optional[float]     # what the figures add up to
    passed: bool
    location: str = ""
    row_index: Optional[int] = None

    @property
    def delta(self) -> Optional[float]:
        if self.printed is None or self.computed is None:
            return None
        return self.computed - self.printed

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["delta"] = self.delta
        return d


def _tol() -> float:
    return settings.reconcile_tolerance


# Share of candidate rows that must already tie before the layout is treated as
# additive. Set below the point where a genuinely segmented report with a handful of
# real errors would be dismissed, and well above what an unrelated set of columns
# reaches by coincidence.
ADDITIVE_AGREEMENT = 0.6
ADDITIVE_MIN_TIES = 2


def _candidates(table: Table, total_column: int):
    """(row, printed, computed) for every row that could be cross-footed."""
    ncols = table.ncols
    for row in table.rows:
        values = row.values(ncols)
        printed = values[total_column]
        parts = [v for i, v in enumerate(values) if i != total_column and v is not None]
        if printed is None or not parts:
            continue
        yield row, printed, sum(parts)


def is_additive(table: Table) -> bool:
    """
    Whether this table's columns are parts of a whole.

    A profit and loss split by class is: BACKFLOWS + SPRINKLERS + ... = TOTAL, and the
    rows say so themselves. A rent roll is not: Sq Ft, Market Rent and Deposit are
    different measures, and adding them together is meaningless no matter how neatly
    they line up.

    Nothing in the parser output distinguishes the two, so the document is asked
    directly - if the columns really do sum to the total, most rows will already prove
    it. On a rent roll almost none will, and the checks are withheld rather than
    reported as exceptions. An analyst who is handed three exceptions on a clean
    document stops reading the exceptions.
    """
    total_column = table.total_column
    if table.ncols < 3 or total_column < 0:
        return False

    tol = _tol()
    ties = seen = 0
    for _, printed, computed in _candidates(table, total_column):
        seen += 1
        if abs(computed - printed) <= tol:
            ties += 1

    return ties >= ADDITIVE_MIN_TIES and ties >= ADDITIVE_AGREEMENT * seen


def cross_foot(table: Table) -> List[Check]:
    """In a segmented report the parts must sum to the total column."""
    if not is_additive(table):
        return []

    total_column = table.total_column
    return [
        Check(
            kind=CROSS_FOOT,
            description=f"{row.label or '(unlabelled)'}: segments sum to the total column",
            printed=printed,
            computed=computed,
            passed=abs(computed - printed) <= _tol(),
            location=f"page {row.page}, row {row.index + 1}",
            row_index=row.index,
        )
        for row, printed, computed in _candidates(table, total_column)
    ]


GRAND_TOTAL_ROW = re.compile(r"^\s*(?:grand\s+|report\s+)?totals?\s*[:.]?\s*$", re.I)


def column_foot(table: Table) -> List[Check]:
    """
    A grand-total row must equal the column it closes, added downwards.

    Cross-footing reads a report across; this reads it down. Summary tables are where
    the two differ: a rent roll's "Totals:" line has nothing to say about its own row
    (Sq Ft plus Deposit is not a quantity) but everything to say about its columns -
    occupied plus vacant units really must equal the unit count, and the rent really
    must equal the rent. That is the arithmetic an analyst checks by hand, so it is the
    arithmetic worth checking for them.
    """
    total_column = table.total_column
    if table.ncols < 1:
        return []

    checks: List[Check] = []
    for total_row in table.rows:
        if not GRAND_TOTAL_ROW.match(total_row.label or ""):
            continue

        above = [r for r in table.rows if r.index < total_row.index and r.has_figures]
        # Where the block above already subtotals itself, add the subtotals only -
        # adding those and their details would count every figure twice.
        blocks = [r for r in above if r.block_start is not None]
        contributors = blocks or above
        if len(contributors) < 2:
            continue

        printed_row = total_row.values(table.ncols)
        found: List[Check] = []
        for column in range(table.ncols):
            printed = printed_row[column]
            parts = [v for r in contributors if (v := r.value_at(column)) is not None]
            if printed is None or len(parts) < 2:
                continue
            computed = sum(parts)
            name = table.column_names[column] if column < len(table.column_names) else f"column {column + 1}"
            found.append(Check(
                kind=COLUMN_FOOT,
                description=f'{total_row.label}: "{name}" adds down ({len(parts)} rows)',
                printed=printed,
                computed=computed,
                passed=abs(computed - printed) <= _tol(),
                location=f"page {total_row.page}, rows {contributors[0].index + 1}-{total_row.index + 1}",
                row_index=total_row.index,
            ))

        # Two ways to know the label really is a grand total. Structure is the stronger
        # one: a row closing a stack of "Total for X" blocks is a grand total whether or
        # not it adds up, and a statement whose grand total disagrees with its own
        # section totals is precisely the exception worth raising.
        #
        # Failing that, the arithmetic has to speak for itself. A single-column table
        # cannot clear that bar - one number tying proves nothing - so it is only
        # checked when the structure vouches for it.
        ties = sum(1 for c in found if c.passed)
        proven = ties >= ADDITIVE_MIN_TIES and ties >= ADDITIVE_AGREEMENT * len(found)
        if blocks or proven:
            checks.extend(found)

    return checks


def subtotals(table: Table) -> List[Check]:
    """Each "Total for X" row must equal the sum of the block it closes."""
    checks: List[Check] = []
    total_column = table.total_column
    if total_column < 0:
        return checks

    for row in table.rows:
        if row.block_start is None or not row.children:
            continue
        printed = row.value_at(total_column)
        if printed is None:
            continue
        parts = []
        for child_index in row.children:
            child = table.row_by_index(child_index)
            if child is None:
                continue
            value = child.value_at(total_column)
            if value is not None:
                parts.append(value)
        if not parts:
            continue
        computed = sum(parts)
        subject = subtotal_subject(row.label) or row.label
        nested = sum(
            1 for i in row.children
            if (c := table.row_by_index(i)) is not None and c.block_start is not None
        )
        detail = f"{len(parts)} rows" + (f", {nested} via a nested subtotal" if nested else "")
        checks.append(Check(
            kind=SUBTOTAL,
            description=f'{row.label}: sums the "{subject}" block ({detail})',
            printed=printed,
            computed=computed,
            passed=abs(computed - printed) <= _tol(),
            location=f"page {row.page}, rows {row.block_start + 1}-{row.index + 1}",
            row_index=row.index,
        ))
    return checks


def _find(table: Table, column: int, *patterns: str) -> Optional[float]:
    """Value in the given column of the first row whose label matches any pattern."""
    if column < 0:
        return None
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for row in table.rows:
            if row.label and rx.search(row.label.strip()):
                value = row.value_at(column)
                if value is not None:
                    return value
    return None


def _locate(table: Table, column: int, *patterns: str):
    """(position, value) of the first row matching any pattern, or None."""
    if column < 0:
        return None
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for position, row in enumerate(table.rows):
            if row.label and rx.search(row.label.strip()):
                value = row.value_at(column)
                if value is not None:
                    return position, value
    return None


def _deduct(base: Optional[float], item: Optional[float]) -> Optional[float]:
    """
    Base less an item, honouring how the document printed it.

    Statements disagree on whether a cost is written 2,918.4 or (2,918.4), and both
    conventions appear on the same page. Subtracting a figure that was already negated
    doubles it, which is how a balance sheet that ties perfectly gets reported as an
    exception. The sign the parser read is the document's own answer, so use it.
    """
    if base is None or item is None:
        return None
    return base + item if item < 0 else base - item


def identities(table: Table) -> List[Check]:
    """
    Accounting identities, matched on the labels the document actually prints.

    Label-driven rather than schema-driven, so it verifies the statement in front of it
    instead of forcing it into predefined fields.

    Every identity here is one that must hold for the statement to be internally
    consistent. Anything weaker does not belong: an identity that only holds for some
    presentations reports clean documents as broken, and an exception list that cannot
    be trusted is not read.
    """
    # A comparative statement carries a column per period and each must stand on its
    # own, so both years are checked rather than whichever happens to be printed last.
    # A segmented report is different - its columns are parts, and only the total is a
    # statement about the business.
    columns = [table.total_column] if is_additive(table) else list(range(table.ncols))
    checks: List[Check] = []

    for column in columns:
        if column < 0:
            continue
        name = table.column_names[column] if column < len(table.column_names) else ""
        suffix = f" ({name})" if name and len(columns) > 1 else ""

        def add(description: str, printed: Optional[float], computed: Optional[float]):
            if printed is None or computed is None:
                return
            checks.append(Check(
                kind=IDENTITY,
                description=description + suffix,
                printed=printed,
                computed=computed,
                passed=abs(computed - printed) <= _tol(),
                location=f"page {table.page}",
            ))

        def find(*patterns: str) -> Optional[float]:
            return _find(table, column, *patterns)

        income = find(r"^total\s+(for\s+)?income$", r"^total\s+revenue", r"^total\s+sales")
        cogs = find(r"^total\s+(for\s+)?cost of goods sold$", r"^total\s+cost of (sales|revenue)")
        gross = find(r"^gross\s+profit$")
        expenses = find(r"^total\s+(for\s+)?expenses$", r"^total\s+operating expenses$")
        operating_at = _locate(table, column, r"^net\s+operating\s+income$", r"^operating\s+(income|profit)$")
        net_at = _locate(table, column, r"^net\s+income$", r"^profit\s+for\s+the\s+(year|period)$")
        operating = operating_at[1] if operating_at else None
        assets = find(r"^total\s+assets$")
        liabilities = find(r"^total\s+liabilities$")
        equity = find(r"^total\s+equity$", r"^net\s+assets")

        add("Gross profit = income less cost of sales", gross, _deduct(income, cogs))
        add("Operating income = gross profit less expenses", operating, _deduct(gross, expenses))

        # Operating income and net income are separated by whatever the statement
        # reports between them - finance costs and tax on one, other income and
        # exceptional items on another, nothing at all on a third. Rather than name
        # those lines, take the ones the document prints in that gap: they are the
        # reconciling items by construction, whatever they are called and in whatever
        # language. Asserting the two are simply equal, as this once did, reports every
        # statement that pays interest as an exception.
        if operating_at and net_at and operating_at[0] < net_at[0]:
            between = [
                r for r in table.rows[operating_at[0] + 1:net_at[0]]
                if r.value_at(column) is not None
            ]
            blocks = [r for r in between if r.block_start is not None]
            items = blocks or between
            bridge = operating_at[1] + sum(r.value_at(column) for r in items)
            add(
                f"Net income = operating income after the {len(items)} item(s) between"
                if items else
                "Net income = operating income (nothing reported between them)",
                net_at[1], bridge,
            )

        if liabilities is not None and liabilities < 0:
            # Liabilities carried as negatives: the balance sheet nets down to equity.
            add("Net assets = total assets less total liabilities", equity, _deduct(assets, liabilities))
        elif liabilities is not None and equity is not None:
            add("Total assets = total liabilities + total equity", assets, liabilities + equity)

    return checks


def run(table: Table) -> List[Check]:
    return cross_foot(table) + column_foot(table) + subtotals(table) + identities(table)


def run_document(document: Document) -> List[Check]:
    checks: List[Check] = []
    for table in document.tables:
        checks.extend(run(table))
    return checks


def summarize(checks: List[Check]) -> Dict[str, Any]:
    failures = [c for c in checks if not c.passed]
    by_kind: Dict[str, Dict[str, int]] = {}
    for c in checks:
        entry = by_kind.setdefault(c.kind, {"passed": 0, "failed": 0})
        entry["passed" if c.passed else "failed"] += 1

    return {
        "total": len(checks),
        "passed": len(checks) - len(failures),
        "failed": len(failures),
        "reconciled": not failures and bool(checks),
        "status": (
            "RECONCILED" if not failures and checks
            else "NO CHECKS AVAILABLE" if not checks
            else f"{len(failures)} exception(s)"
        ),
        "by_kind": by_kind,
        "exceptions": [c.to_dict() for c in failures],
    }
