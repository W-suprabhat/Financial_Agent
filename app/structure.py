"""
Structure inference: recover the account hierarchy without coordinates.

The parser returns a flat grid - no indentation, no bounding boxes. Hierarchy is
recovered from the labels alone, because financial statements are self-describing in
this respect: a row reading "Total for X" is a closing bracket for a block that opened
at the row labelled "X". Nesting those brackets yields the tree, including multi-level
nesting.

Nothing here is a guess that goes unchecked. Every block this module infers is then
verified arithmetically in reconcile.py - a wrong tree does not balance.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from .models import (
    ROLE_BLOCK_HEADER, ROLE_LINE_ITEM, ROLE_SECTION, ROLE_SUBTOTAL,
    Row, Table, normalize_label, subtotal_subject,
)

logger = logging.getLogger(__name__)

TOTAL_HINT = re.compile(r"\btotals?\b", re.I)


def has_unrecognised_totals(tables: List[Table]) -> bool:
    """
    Did the document print totals this module then failed to make sense of?

    The distinction this draws is the one that decides whether low arithmetic coverage is
    worth reporting. A rent roll with no printed total has nothing to recognise, and
    saying so would be crying wolf on a document handled correctly. A payroll journal
    prints "Grand totals" and a "Total" on every block, and still yields no hierarchy -
    there the silence is a failure, and an analyst needs to know the figures went out
    unverified.

    Deliberately looks in the cells as well as the labels: the layouts that defeat
    inference are exactly the ones that put the label somewhere this code did not expect.

    Judged per table, never per document. A pack is routinely one statement the pipeline
    understands beside forty pages it does not, and asking whether *any* table yielded a
    block lets the one success hide all of the failures - which is the shape of the
    document that prompted this in the first place.
    """
    return any(_table_has_unrecognised_totals(table) for table in tables)


def _table_has_unrecognised_totals(table: Table) -> bool:
    if any(row.block_start is not None for row in table.rows):
        return False

    for row in table.rows:
        if TOTAL_HINT.search(row.label or ""):
            return True
        for cell in row.cells:
            if cell.value is None and TOTAL_HINT.search(cell.raw or ""):
                return True
    return False


def _label_index(rows: List[Row]) -> Dict[str, List[int]]:
    index: Dict[str, List[int]] = {}
    for position, row in enumerate(rows):
        if row.label:
            index.setdefault(normalize_label(row.label), []).append(position)
    return index


def _find_block_start(
    rows: List[Row], subtotal_position: int, subject: str
) -> Tuple[Optional[int], Optional[str]]:
    """
    Locate the row that opens the block this subtotal closes.

    Returns (position, repair note). An exact label match is preferred. Failing that,
    the subject is treated as a prefix, because the parser truncates some labels that
    the PDF wrapped over two lines - "Total for Auto and Truck" should close the
    "Auto and Truck Expenses" block.
    """
    target = normalize_label(subject)
    if not target:
        return None, None

    index = _label_index(rows)
    for candidate in reversed(index.get(target, [])):
        if candidate < subtotal_position:
            return candidate, None

    best: Optional[int] = None
    for position, row in enumerate(rows[:subtotal_position]):
        if subtotal_subject(row.label):
            continue  # another subtotal cannot open a block
        normalized = normalize_label(row.label)
        if normalized == target or normalized.startswith(target + " "):
            best = position
    if best is not None:
        return best, (
            f'label truncated by the parser; matched by prefix to "{rows[best].label}"'
        )
    return None, None


def infer(table: Table) -> List[str]:
    """
    Assign role, depth and children to every row. Returns notes about any repairs.

    Called before reconciliation and again after each repair round.
    """
    rows = table.rows
    total_column = table.total_column
    notes: List[str] = []

    for row in rows:
        row.role = ROLE_LINE_ITEM
        row.depth = 0
        row.block_start = None
        row.children = []

    # 1. Pair every subtotal with the block it closes.
    spans: Dict[int, int] = {}   # subtotal position -> block start position
    for position, row in enumerate(rows):
        subject = subtotal_subject(row.label)
        if not subject:
            continue
        if total_column >= 0 and row.value_at(total_column) is None and not row.has_figures:
            continue
        start, note = _find_block_start(rows, position, subject)
        if start is None:
            continue
        spans[position] = start
        row.role = ROLE_SUBTOTAL
        row.block_start = rows[start].index
        if note:
            row.repairs.append(note)
            notes.append(f'"{row.label}": {note}')

    starts = {start: end for end, start in spans.items()}

    # 2. Depth is how many blocks strictly contain the row.
    intervals = [(start, end) for end, start in spans.items()]
    for position, row in enumerate(rows):
        row.depth = sum(1 for a, b in intervals if a < position < b)
        if position in spans:
            row.role = ROLE_SUBTOTAL
        elif position in starts:
            row.role = ROLE_BLOCK_HEADER
        elif not row.has_figures and row.label:
            row.role = ROLE_SECTION

    # 3. Direct children of each block. On reaching a nested block, take that block's
    #    subtotal and skip its internals - counting both would double the money.
    for subtotal_position, start_position in spans.items():
        children: List[int] = []
        cursor = start_position
        while cursor < subtotal_position:
            nested_end = starts.get(cursor)
            if nested_end is not None and nested_end < subtotal_position and cursor != start_position:
                children.append(rows[nested_end].index)
                cursor = nested_end + 1
                continue
            children.append(rows[cursor].index)
            cursor += 1
        rows[subtotal_position].children = children

    return notes


def tree_lines(table: Table, max_rows: int = 200) -> List[str]:
    """Readable rendering of the inferred hierarchy, for logs and the UI."""
    marker = {
        ROLE_SUBTOTAL: "=",
        ROLE_BLOCK_HEADER: "+",
        ROLE_SECTION: "#",
        ROLE_LINE_ITEM: " ",
    }
    total_column = table.total_column
    out: List[str] = []
    for row in table.rows[:max_rows]:
        if not row.label and not row.has_figures:
            continue
        value = row.value_at(total_column) if total_column >= 0 else None
        rendered = "" if value is None else f"{value:>15,.2f}"
        out.append(
            f"{marker.get(row.role, ' ')} {'    ' * row.depth}{row.label[:46]:46s}{rendered}"
        )
    return out


def orphan_fragments(table: Table) -> List[int]:
    """
    Label-only rows that look like the tail of a wrapped label.

    A row with no figures, whose label appended to the previous row's label matches a
    real block header, is almost certainly a continuation the parser split. Reported
    rather than repaired here; the repair strategies decide what to do.
    """
    rows = table.rows
    existing = {normalize_label(r.label) for r in rows if r.label}
    found: List[int] = []
    for position in range(1, len(rows)):
        row, previous = rows[position], rows[position - 1]
        if row.has_figures or not row.label or not previous.label:
            continue
        subject = subtotal_subject(previous.label)
        if not subject:
            continue
        if normalize_label(f"{subject} {row.label}") in existing:
            found.append(row.index)
    return found
