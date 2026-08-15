"""
Repair strategies.

Each strategy inspects the failing reconciliation checks, proposes one concrete change,
and records what it did. The agent applies a strategy, re-runs reconciliation, and keeps
the change only if the number of exceptions actually fell - so every repair is judged
against objective arithmetic rather than trusted because it sounded reasonable.

All strategies are deterministic. No model is involved: the model never touches a
figure or a structural decision anywhere in this pipeline.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional

from .models import Row, Table, normalize_label, subtotal_subject
from .reconcile import Check

logger = logging.getLogger(__name__)


@dataclass
class Repair:
    strategy: str
    description: str
    evidence: str
    row_index: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# strategy: rejoin a wrapped label the parser split across two rows
# --------------------------------------------------------------------------

def rejoin_wrapped_labels(table: Table, failures: List[Check]) -> List[Repair]:
    """
    A label-only row directly after a subtotal is usually the tail of a wrapped label.

    Self-validating: the merge is accepted only if the joined subject then matches a
    block header that genuinely exists in the table. "Total for Other Job Related" +
    "Costs" merges because "Other Job Related Costs" is a real row; "Total for Income"
    followed by the heading "Cost of Goods Sold" does not, because the join matches
    nothing.

    This is the fix for the parser truncating labels such as "Total for Auto and Truck",
    which otherwise leaves an orphan "Expenses" row that later gets mistaken for the
    start of the Expenses block.
    """
    existing = {normalize_label(r.label) for r in table.rows if r.label}
    repairs: List[Repair] = []
    drop: List[int] = []

    for position in range(len(table.rows) - 1):
        row, nxt = table.rows[position], table.rows[position + 1]
        if not row.label or not nxt.label or nxt.has_figures:
            continue
        if nxt.index in drop:
            continue
        subject = subtotal_subject(row.label)
        if not subject:
            continue
        joined = normalize_label(f"{subject} {nxt.label}")
        if joined in existing and normalize_label(subject) not in existing:
            merged_label = f"{row.label} {nxt.label}".strip()
            repairs.append(Repair(
                strategy="rejoin_wrapped_labels",
                description=f'joined "{row.label}" + "{nxt.label}" -> "{merged_label}"',
                evidence=f'"{subject} {nxt.label}" matches an existing block header',
                row_index=row.index,
            ))
            row.label = merged_label
            row.repairs.append(f'rejoined wrapped label fragment "{nxt.label}"')
            drop.append(nxt.index)

    if drop:
        table.rows = [r for r in table.rows if r.index not in drop]
    return repairs


# --------------------------------------------------------------------------
# strategy: a child's sign is inverted
# --------------------------------------------------------------------------

def fix_inverted_sign(table: Table, failures: List[Check]) -> List[Repair]:
    """
    If a subtotal is out by exactly twice one of its children, that child's sign is
    inverted relative to how the subtotal treats it.

    Only fires on an exact match (within tolerance) against a single candidate, so it
    cannot silently reshape figures to force a balance.
    """
    from .config import settings

    total_column = table.total_column
    if total_column < 0:
        return []

    repairs: List[Repair] = []
    for check in failures:
        if check.kind != "subtotal" or check.delta is None or check.row_index is None:
            continue
        row = table.row_by_index(check.row_index)
        if row is None or not row.children:
            continue

        candidates = []
        for child_index in row.children:
            child = table.row_by_index(child_index)
            if child is None:
                continue
            value = child.value_at(total_column)
            if value is None or value == 0:
                continue
            if abs(abs(check.delta) - abs(2 * value)) <= settings.reconcile_tolerance:
                candidates.append(child)

        if len(candidates) != 1:
            continue

        child = candidates[0]
        for cell in child.cells:
            if cell.column == total_column and cell.value is not None:
                before = cell.value
                cell.value = -before
                child.repairs.append(
                    f"inverted sign on the total column ({before} -> {cell.value})"
                )
                repairs.append(Repair(
                    strategy="fix_inverted_sign",
                    description=f'inverted the sign of "{child.label}" ({before} -> {cell.value})',
                    evidence=(
                        f'"{row.label}" was out by {check.delta:,.2f}, exactly twice '
                        f"this row's value"
                    ),
                    row_index=child.index,
                ))
                break
    return repairs


# --------------------------------------------------------------------------
# strategy: the block start was resolved to the wrong row
# --------------------------------------------------------------------------

def retarget_block_start(table: Table, failures: List[Check]) -> List[Repair]:
    """
    Re-anchor a failing subtotal to whichever earlier row makes its block balance.

    Only accepted when exactly one candidate start position produces an exact tie, so
    it repairs a genuinely misplaced anchor without fishing for a coincidence.
    """
    from .config import settings

    total_column = table.total_column
    if total_column < 0:
        return []

    positions = {row.index: i for i, row in enumerate(table.rows)}
    repairs: List[Repair] = []

    for check in failures:
        if check.kind != "subtotal" or check.row_index is None:
            continue
        row = table.row_by_index(check.row_index)
        if row is None:
            continue
        printed = row.value_at(total_column)
        if printed is None:
            continue
        end = positions.get(row.index)
        if end is None:
            continue

        winners = []
        for start in range(end):
            total = 0.0
            for candidate in table.rows[start:end]:
                value = candidate.value_at(total_column)
                if value is not None:
                    total += value
            if abs(total - printed) <= settings.reconcile_tolerance:
                winners.append(start)

        if len(winners) != 1:
            continue
        start = winners[0]
        if start == positions.get(row.block_start, -1):
            continue

        old_label = None
        if row.block_start is not None:
            previous = table.row_by_index(row.block_start)
            old_label = previous.label if previous else None

        row.block_start = table.rows[start].index
        row.children = [r.index for r in table.rows[start:end]]
        row.repairs.append(f'block start moved to "{table.rows[start].label}"')
        repairs.append(Repair(
            strategy="retarget_block_start",
            description=(
                f'"{row.label}" now sums from "{table.rows[start].label}"'
                + (f' (was "{old_label}")' if old_label else "")
            ),
            evidence=f"this is the only start position that ties to {printed:,.2f}",
            row_index=row.index,
        ))
    return repairs


# The agent tries these in order. Cheapest and most certain first.
STRATEGIES: List[Callable[[Table, List[Check]], List[Repair]]] = [
    rejoin_wrapped_labels,
    retarget_block_start,
    fix_inverted_sign,
]

STRATEGY_NAMES = [s.__name__ for s in STRATEGIES]
