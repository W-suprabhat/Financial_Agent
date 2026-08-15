"""
Learned fixes.

Documents arrive on a cycle: the same rent roll, from the same property manager, with
the same broken columns, every month. Without memory an analyst repairs it every time.

This module gives the agent that memory. A fix an analyst approves once is stored
against the document's layout fingerprint, and applied automatically the next time a
document with that layout appears. The agent therefore gets quieter over time and only
asks about layouts it has not seen.

Two things are deliberately kept apart:

  proposals  computed fixes that NOBODY has approved yet. Never applied.
  rules      proposals an analyst approved. Applied automatically on sight.
"""

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Table, normalize_label, parse_number
from .storage import store

logger = logging.getLogger(__name__)

RULES_PREFIX = "financial-agent/rules"

# Kinds of fix
SPLIT_MERGED_COLUMN = "split_merged_column"   # duplicated heading: text written into both slots
SPLIT_SINGLE_COLUMN = "split_single_column"   # column dropped: two values in one column
DATE_ORDER = "date_order"                     # the document cannot say day-first or month-first

# Corrections an analyst made by hand. The engine finds what arithmetic can prove;
# these are the things only a person knows - that a truncated label should read in
# full, or that a heading the parser garbled means something else. Recording them as
# rules is what stops the same correction being retyped every month.
RELABEL_ROW = "relabel_row"                   # a row label, corrected by hand
RENAME_COLUMN = "rename_column"               # a column heading, corrected by hand

# Corrections are about words, never figures. A label can be restated because next
# month's document says the same word; a figure cannot, because next month's document
# says a different number. Persisting a corrected figure would overwrite good data with
# stale data, so single figures are fixed for this document only and never learned.
CORRECTABLE = (RELABEL_ROW, RENAME_COLUMN)

# Risk tiers
AUTO = "auto"          # arithmetic proves it; applied without asking
PROPOSE = "propose"    # computable but unprovable; needs one human approval
REPORT = "report"      # too risky to change; described only


@dataclass
class Proposal:
    kind: str
    column: int
    description: str
    evidence: str
    affected_rows: int
    risk: str
    params: Dict[str, Any] = field(default_factory=dict)
    examples: List[Dict[str, str]] = field(default_factory=list)
    # The layout this was found on. Set at detection, because it cannot be reliably
    # worked out afterwards: a dropped-column fix applies to the NARROW pages, while
    # the duplicated-heading evidence that identifies it sits on the WIDE ones. Deriving
    # it later stored the rule against a layout it would never match, so the same fix
    # was proposed again on every upload.
    fingerprint: Optional[str] = None

    @property
    def id(self) -> str:
        raw = f"{self.kind}:{self.column}:{json.dumps(self.params, sort_keys=True)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["id"] = self.id
        return d


def fingerprint(table: Table) -> str:
    """
    Identify a layout.

    Built from the column headings and the row-label heading, so the same monthly report
    fingerprints identically while a different report does not. Pages of one document
    that have genuinely different shapes fingerprint separately, which is correct: a fix
    approved for a 14-column layout must not be applied to a 13-column one.
    """
    parts = [normalize_label(table.label_header)] + [
        normalize_label(c) for c in table.column_names
    ]
    raw = f"{len(table.column_names)}|" + "|".join(parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _looks_like_code(value: str) -> bool:
    """
    A compact identifier such as M994X2A.

    Pure digits are rejected: learning the vocabulary from a whole document otherwise
    picks up figures from summary tables (square-footage totals like 313872) and would
    split a cell at the wrong place.
    """
    v = (value or "").strip()
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-/]{2,15}", v)) and not v.isdigit()


def _cell(row, column: int):
    return next((c for c in row.cells if c.column == column), None)


def detect_merged_columns(tables: List[Table]) -> List[Proposal]:
    """
    Find columns the parser merged, where the merge is safely reversible.

    Fires only when two adjacent columns share an identical heading. That is a structural
    impossibility in a real report and is the signature of the parser flattening a cell
    that spans two columns: the spanning cell's text is written into both slots.

    Detection is document-wide, grouped by layout fingerprint, so a report whose pages
    share a layout yields ONE proposal rather than one per page. That matters twice: the
    analyst approves once, and the vocabulary is pooled across every page instead of
    being limited to whatever happened to appear on one.

    Guards, each preventing a specific way this could corrupt data:
      - identical headings required, so legitimate equal values elsewhere are untouched
        (this rent roll has 278 rows where Actual Rent legitimately equals Potential Rent)
      - numeric cells are never touched, so no figure can be rewritten
      - the split point must be a code observed in this document's own unambiguous rows
      - longest match wins, so a code that prefixes another cannot mis-split
      - anything failing those tests is left alone, never guessed at
    """
    groups: Dict[str, List[Table]] = {}
    for table in tables:
        groups.setdefault(fingerprint(table), []).append(table)

    proposals: List[Proposal] = []

    for fp, group in groups.items():
        reference = group[0]
        if reference.ncols < 2:
            continue

        for left in range(reference.ncols - 1):
            right = left + 1
            a = normalize_label(reference.column_names[left])
            b = normalize_label(reference.column_names[right])
            if not a or a != b:
                continue

            # pool the vocabulary across every page sharing this layout
            vocabulary = set()
            for table in group:
                for row in table.rows:
                    lc, rc = _cell(row, left), _cell(row, right)
                    if lc is None or rc is None:
                        continue
                    lv, rv = (lc.raw or "").strip(), (rc.raw or "").strip()
                    if lv and rv and lv != rv and _looks_like_code(lv):
                        vocabulary.add(lv)

            if not vocabulary:
                continue

            ordered = sorted(vocabulary, key=len, reverse=True)  # longest match first
            affected, pages, examples = 0, set(), []
            for table in group:
                for row in table.rows:
                    lc, rc = _cell(row, left), _cell(row, right)
                    if lc is None or rc is None:
                        continue
                    lv, rv = (lc.raw or "").strip(), (rc.raw or "").strip()
                    if not lv or lv != rv:
                        continue
                    if lc.value is not None or rc.value is not None:
                        continue  # never touch a figure
                    code = next((c for c in ordered if lv.startswith(c + " ")), None)
                    if not code:
                        continue
                    affected += 1
                    pages.add(table.page)
                    if len(examples) < 6:
                        examples.append({
                            "page": str(table.page),
                            "row": str(row.index + 1),
                            "original": lv,
                            "left": code,
                            "right": lv[len(code):].strip(),
                        })

            if affected:
                heading = reference.column_names[left]
                proposals.append(Proposal(
                    fingerprint=fp,
                    kind=SPLIT_MERGED_COLUMN,
                    column=left,
                    description=(
                        f'Split "{heading}" into two columns. The parser merged them and '
                        f"wrote the same text into both."
                    ),
                    evidence=(
                        f"Two adjacent columns carry the identical heading "
                        f'"{heading}", which cannot happen in a real report. '
                        f"{affected} rows on page(s) {', '.join(map(str, sorted(pages)))} "
                        f"begin with one of {len(ordered)} codes seen in unmerged rows of "
                        f"this same document ({', '.join(ordered[:5])}"
                        f"{'…' if len(ordered) > 5 else ''}). No numeric cell is touched."
                    ),
                    affected_rows=affected,
                    risk=PROPOSE,
                    params={"left": left, "right": right, "vocabulary": ordered},
                    examples=examples,
                ))

    return proposals


def detect_dropped_columns(tables: List[Table]) -> List[Proposal]:
    """
    Find a column the parser collapsed two columns into, and split it back out.

    Evidence comes from the document comparing itself. On some pages the parser writes a
    spanning cell's text into BOTH slots, so its heading appears twice adjacently. On
    other pages of the same document it drops one slot entirely, so the same heading
    appears once. A heading that is duplicated on one layout and single on another is
    therefore a column the parser dropped.

    This was originally reported as too risky to change, on the assumption that the later
    columns had shifted out from under their headings. That was wrong: on the narrow
    pages each heading still sits over its own data ("Sq Ft" over 1,095.00). Splitting is
    safe because the heading and its column are moved together, so alignment is preserved
    by construction rather than by hope - and the test suite asserts it.
    """
    # headings the parser duplicated somewhere in this document
    duplicated = set()
    for table in tables:
        for i in range(table.ncols - 1):
            a = normalize_label(table.column_names[i])
            if a and a == normalize_label(table.column_names[i + 1]):
                duplicated.add(a)
    if not duplicated:
        return []

    # vocabulary pooled across the whole document, including pages that are already correct
    vocabulary = set()
    for table in tables:
        for i in range(table.ncols):
            for row in table.rows:
                cell = _cell(row, i)
                if cell is None or cell.value is not None:
                    continue
                v = (cell.raw or "").strip()
                if _looks_like_code(v):
                    vocabulary.add(v)
    if not vocabulary:
        return []
    ordered = sorted(vocabulary, key=len, reverse=True)

    # group by layout so the reported count covers every page the fix will touch
    groups: Dict[str, List[Table]] = {}
    for table in tables:
        groups.setdefault(fingerprint(table), []).append(table)

    proposals: List[Proposal] = []

    for group_fp, group in groups.items():
        table = group[0]

        for column in range(table.ncols):
            heading = normalize_label(table.column_names[column])
            if heading not in duplicated:
                continue
            # only where it is NOT duplicated here, i.e. the slot was dropped
            neighbour = normalize_label(table.column_names[column + 1]) if column + 1 < table.ncols else ""
            previous = normalize_label(table.column_names[column - 1]) if column else ""
            if heading in (neighbour, previous):
                continue

            affected, pages, examples = 0, set(), []
            for member in group:
                for row in member.rows:
                    cell = _cell(row, column)
                    if cell is None or cell.value is not None:
                        continue
                    v = (cell.raw or "").strip()
                    code = next((c for c in ordered if v.startswith(c + " ")), None)
                    if not code:
                        continue
                    affected += 1
                    pages.add(member.page)
                    if len(examples) < 6:
                        examples.append({
                            "page": str(member.page),
                            "row": str(row.index + 1),
                            "original": v,
                            "left": code,
                            "right": v[len(code):].strip(),
                        })

            if affected:
                proposals.append(Proposal(
                    fingerprint=group_fp,
                    kind=SPLIT_SINGLE_COLUMN,
                    column=column,
                    description=(
                        f'Restore the column the parser dropped: split '
                        f'"{table.column_names[column]}" into two on the narrower pages.'
                    ),
                    evidence=(
                        f'This heading appears twice on the wider pages and once here, so '
                        f'the parser collapsed two columns into one. {affected} rows on '
                        f"page(s) {', '.join(map(str, sorted(pages)))} begin with one of "
                        f'{len(ordered)} codes seen elsewhere in this document. Headings '
                        f'move with their columns, so no figure changes heading.'
                    ),
                    affected_rows=affected,
                    risk=PROPOSE,
                    params={"column": column, "vocabulary": ordered},
                    examples=examples,
                ))

    return proposals


def detect_all(tables: List[Table]) -> List[Proposal]:
    return detect_merged_columns(tables) + detect_dropped_columns(tables)


def detect_report_only(tables: List[Table]) -> List[Dict[str, Any]]:
    """
    Structural problems that must NOT be auto-corrected.

    Where the parser dropped a column entirely, repairing it would mean inserting a
    column and shifting every numeric column across - exactly the operation that
    silently misaligns figures. These are described so an analyst knows the affected
    fields are unreliable, and left untouched.
    """
    findings: List[Dict[str, Any]] = []

    # A summary table at the end of a report legitimately has its own columns, so it is
    # not evidence that anything went wrong. Only compare the detail tables.
    if len(tables) > 1:
        biggest = max(t.ncols for t in tables)
        detail = [t for t in tables if t.ncols >= biggest - 1]
    else:
        detail = list(tables)

    shapes: Dict[int, List[int]] = {}
    for table in detail:
        shapes.setdefault(table.ncols, []).append(table.page)

    if len(shapes) > 1:
        # +1 because column_names excludes the row-label column
        detail = "; ".join(
            f"{n + 1} columns on page(s) {', '.join(map(str, pages))}"
            for n, pages in sorted(shapes.items(), reverse=True)
        )
        findings.append({
            "kind": "inconsistent_column_count",
            "risk": REPORT,
            "description": f"The parser returned different column counts for this document: {detail}.",
            "impact": (
                "A column was dropped on the narrower pages. If a proposal above offers "
                "to restore it, approving that resolves this. If not, the affected text "
                "columns on those pages are unreliable - the figures themselves still sit "
                "under their own headings and reconcile normally."
            ),
        })
    return findings


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

def _split_single(table: Table, proposal: Dict[str, Any]) -> int:
    """
    Split one column into two, moving every later column one place right.

    Headings are shifted in lockstep with their data, so a figure can never end up under
    a different heading. Verified by test, not assumed.
    """
    params = proposal.get("params") or {}
    column = params.get("column")
    vocabulary = sorted(params.get("vocabulary") or [], key=len, reverse=True)
    if column is None or not vocabulary or column >= table.ncols:
        return 0

    # already split (a second run, or the layout was correct)
    if column + 1 < table.ncols and normalize_label(table.column_names[column]) == \
            normalize_label(table.column_names[column + 1]):
        return 0

    splittable = 0
    for row in table.rows:
        cell = _cell(row, column)
        if cell is None or cell.value is not None:
            continue
        v = (cell.raw or "").strip()
        if any(v.startswith(c + " ") for c in vocabulary):
            splittable += 1
    if not splittable:
        return 0

    from .models import Cell

    changed = 0
    for row in table.rows:
        # make room: everything to the right of `column` moves one place right
        for cell in row.cells:
            if cell.column > column:
                cell.column += 1

        cell = _cell(row, column)
        if cell is None or cell.value is not None:
            continue
        v = (cell.raw or "").strip()
        code = next((c for c in vocabulary if v.startswith(c + " ")), None)
        if not code:
            continue
        remainder = v[len(code):].strip()
        cell.raw = code
        cell.value = parse_number(code)
        row.cells.append(Cell(column=column + 1, raw=remainder, value=parse_number(remainder)))
        row.cells.sort(key=lambda c: c.column)
        row.repairs.append(
            f'restored dropped column: "{v}" -> "{code}" | "{remainder}"'
        )
        changed += 1

    # the heading is duplicated to match the wider pages of the same document
    table.column_names.insert(column + 1, table.column_names[column])
    return changed


def apply_date_order(table: Table, order: str) -> int:
    """
    Re-read the date cells with the order an analyst confirmed.

    Possible without re-running the parser because every cell keeps the text exactly as
    printed, so only the interpretation changes.
    """
    from .locales import DateFormat
    from .locales import parse_date as parse_with

    fmt = DateFormat(order=order, evidence="confirmed by an analyst", confident=True)
    changed = 0
    for row in table.rows:
        for cell in row.cells:
            if cell.value is not None or cell.date_value is not None:
                continue
            parsed = parse_with(cell.raw, fmt)
            if parsed is not None:
                cell.date_value = parsed
                changed += 1
    return changed


def correction(kind: str, fp: str, before: str, after: str,
               column: int = -1) -> Proposal:
    """
    Turn a hand correction into a rule that replays on the same layout.

    Matching is on the text the parser produced, not on a row position, because row
    positions move between months and the text does not. A label corrected once is
    therefore corrected on sight from then on, wherever it appears.
    """
    if kind not in CORRECTABLE:
        raise ValueError(f"{kind} cannot be learned from a correction")
    what = "row label" if kind == RELABEL_ROW else "column heading"
    return Proposal(
        kind=kind,
        column=column,
        description=f'{what}: "{before}" reads "{after}"',
        evidence="corrected by an analyst",
        affected_rows=0,
        # A person's judgement, not arithmetic. It carries the weight of the approval
        # that created it and nothing more, which is exactly what PROPOSE means.
        risk=PROPOSE,
        params={"before": before, "after": after},
        fingerprint=fp,
    )


def _relabel(table: Table, proposal: Dict[str, Any]) -> int:
    params = proposal.get("params") or {}
    before, after = params.get("before"), params.get("after")
    if not before or not after:
        return 0
    target = normalize_label(before)
    changed = 0
    for row in table.rows:
        if row.label and normalize_label(row.label) == target and row.label != after:
            row.repairs.append(f'label corrected: "{row.label}" -> "{after}"')
            row.label = after
            changed += 1
    return changed


def _rename_column(table: Table, proposal: Dict[str, Any]) -> int:
    params = proposal.get("params") or {}
    before, after = params.get("before"), params.get("after")
    if not before or not after:
        return 0
    target = normalize_label(before)
    changed = 0
    for i, name in enumerate(table.column_names):
        if normalize_label(name) == target and name != after:
            table.column_names[i] = after
            changed += 1
    return changed


def apply_proposal(table: Table, proposal: Dict[str, Any]) -> int:
    """Apply one approved fix. Returns the number of rows changed."""
    kind = proposal.get("kind")
    if kind == RELABEL_ROW:
        return _relabel(table, proposal)
    if kind == RENAME_COLUMN:
        return _rename_column(table, proposal)
    if kind == DATE_ORDER:
        order = (proposal.get("params") or {}).get("order") or proposal.get("choice")
        return apply_date_order(table, order) if order else 0
    if kind == SPLIT_SINGLE_COLUMN:
        return _split_single(table, proposal)
    if kind != SPLIT_MERGED_COLUMN:
        return 0

    params = proposal.get("params") or {}
    left, right = params.get("left"), params.get("right")
    vocabulary = sorted(params.get("vocabulary") or [], key=len, reverse=True)
    if left is None or right is None or not vocabulary:
        return 0

    changed = 0
    for row in table.rows:
        lc = next((c for c in row.cells if c.column == left), None)
        rc = next((c for c in row.cells if c.column == right), None)
        if lc is None or rc is None:
            continue
        lv, rv = (lc.raw or "").strip(), (rc.raw or "").strip()
        if not lv or lv != rv:
            continue
        if lc.value is not None or rc.value is not None:
            continue
        code = next((c for c in vocabulary if lv.startswith(c + " ")), None)
        if not code:
            continue
        lc.raw = code
        lc.value = parse_number(code)
        rc.raw = lv[len(code):].strip()
        rc.value = parse_number(rc.raw)
        row.repairs.append(f'split merged column: "{lv}" -> "{lc.raw}" | "{rc.raw}"')
        changed += 1
    return changed


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class RuleStore:
    """Approved fixes, keyed by layout fingerprint, persisted in blob storage."""

    def _blob(self, fp: str) -> str:
        return f"{RULES_PREFIX}/{fp}.json"

    def load(self, fp: str) -> Dict[str, Any]:
        if not store.configured:
            return {}
        try:
            container = store.service.get_container_client(store.container_name)
            raw = container.download_blob(self._blob(fp)).readall()
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def rules_for(self, fp: str) -> List[Dict[str, Any]]:
        return (self.load(fp) or {}).get("rules") or []

    def approve(self, fp: str, proposal: Dict[str, Any], approved_by: str,
                layout: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Record an approval so this fix applies automatically from now on."""
        if not store.configured:
            raise RuntimeError("blob storage is required to remember approved fixes")

        record = self.load(fp) or {"fingerprint": fp, "rules": []}
        if layout:
            record["layout"] = layout

        entry = dict(proposal)
        entry["approved_by"] = approved_by
        entry["approved_at"] = datetime.now(timezone.utc).isoformat()
        entry["times_applied"] = 0

        record["rules"] = [r for r in record["rules"] if r.get("id") != entry.get("id")]
        record["rules"].append(entry)

        store.put_bytes(
            self._blob(fp),
            json.dumps(record, indent=2).encode("utf-8"),
            "application/json",
        )
        logger.info(f"approved {entry.get('kind')} for layout {fp} by {approved_by}")
        return record

    def revoke(self, fp: str, rule_id: str, revoked_by: str) -> Dict[str, Any]:
        """
        Withdraw a rule so it stops applying.

        The rule is moved aside rather than deleted. A fix that has been applying
        silently for months has touched real deliverables, and erasing the record of it
        would leave those files unexplainable - the opposite of what the audit trail is
        for. It stops applying; it does not stop having happened.
        """
        if not store.configured:
            raise RuntimeError("blob storage is required to manage saved fixes")

        record = self.load(fp)
        if not record:
            raise KeyError(f"no rules stored for layout {fp}")

        rule = next((r for r in record.get("rules") or [] if r.get("id") == rule_id), None)
        if rule is None:
            raise KeyError(f"no rule {rule_id} on layout {fp}")

        record["rules"] = [r for r in record["rules"] if r.get("id") != rule_id]
        rule["revoked_by"] = revoked_by
        rule["revoked_at"] = datetime.now(timezone.utc).isoformat()
        record.setdefault("revoked", []).append(rule)

        store.put_bytes(
            self._blob(fp),
            json.dumps(record, indent=2).encode("utf-8"),
            "application/json",
        )
        logger.info(
            f"revoked {rule.get('kind')} ({rule_id}) on layout {fp} by {revoked_by}; "
            f"it had been applied {rule.get('times_applied', 0)} time(s)"
        )
        return rule

    def note_applied(self, fp: str, rule_ids: List[str]) -> None:
        """Increment usage counters, so the value of each saved fix is visible."""
        if not store.configured or not rule_ids:
            return
        try:
            record = self.load(fp)
            if not record:
                return
            for rule in record.get("rules") or []:
                if rule.get("id") in rule_ids:
                    rule["times_applied"] = int(rule.get("times_applied") or 0) + 1
                    rule["last_applied_at"] = datetime.now(timezone.utc).isoformat()
            store.put_bytes(
                self._blob(fp),
                json.dumps(record, indent=2).encode("utf-8"),
                "application/json",
            )
        except Exception as e:
            logger.warning(f"could not update usage counters for {fp}: {e}")

    def list_all(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not store.configured:
            return []
        container = store.service.get_container_client(store.container_name)
        out = []
        for blob in container.list_blobs(name_starts_with=RULES_PREFIX + "/"):
            try:
                record = json.loads(container.download_blob(blob.name).readall().decode("utf-8"))
            except Exception:
                continue
            out.append({
                "fingerprint": record.get("fingerprint"),
                "layout": record.get("layout"),
                "rules": [
                    {
                        "id": r.get("id"),
                        "kind": r.get("kind"),
                        "description": r.get("description"),
                        "approved_by": r.get("approved_by"),
                        "approved_at": r.get("approved_at"),
                        "times_applied": r.get("times_applied", 0),
                    }
                    for r in record.get("rules") or []
                ],
                "revoked": [
                    {
                        "id": r.get("id"),
                        "kind": r.get("kind"),
                        "description": r.get("description"),
                        "revoked_by": r.get("revoked_by"),
                        "revoked_at": r.get("revoked_at"),
                        "times_applied": r.get("times_applied", 0),
                    }
                    for r in record.get("revoked") or []
                ],
            })
            if len(out) >= limit:
                break
        return out


rule_store = RuleStore()
