"""
Table-first data model.

The document is represented as it is printed - every row, every column, exact
values - rather than mapped into a fixed set of fields. An earlier version of this
project collapsed each statement into 24 named fields, which discarded roughly 96%
of a real report and destroyed the segment breakdown that was the whole point of it.

Nothing here infers anything. Structure is derived in structure.py and verified in
reconcile.py.
"""

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional
import re

# Cell roles assigned by structure inference
ROLE_SECTION = "section"            # heading with no figures of its own
ROLE_BLOCK_HEADER = "block_header"  # opens a block closed by a "Total for X" row
ROLE_SUBTOTAL = "subtotal"          # closes a block
ROLE_LINE_ITEM = "line_item"        # ordinary detail row

from .locales import DATE_LIKE as _DATE_SHAPE
from .locales import DateFormat, NumberFormat
from .locales import parse_date as _parse_date_localised
from .locales import parse_number as _parse_number_localised

# A figure: an optional currency symbol, digits grouped by any convention, an optional
# CR/DR marker, and nothing alphabetic beyond that. A slash is excluded outright so a
# date is never mistaken for a number - without that, 11/15/2017 parsed as 11152017 and
# 825 dates in one rent roll silently became figures.
LOOKS_NUMERIC = re.compile(
    r"^[^A-Za-z0-9/]*"          # leading currency symbol, bracket or sign
    r"\(?-?\s*"
    r"\d[\d.,'   ]*"            # digits with any grouping or decimal convention
    r"\s*-?\)?"                 # trailing minus or closing bracket
    r"\s*(?:CR|DR|C|D)?"        # credit / debit marker
    r"[^A-Za-z0-9/]*$",
    re.I,
)

# A column heading that declares itself the row total: "Total", "Totals", "Year Total",
# "Total (USD)". Not "Total Charges", which names a measure rather than a sum of the
# other columns.
TOTAL_HEADING = re.compile(r"^(?:.*\s)?totals?\s*(?:\([^)]*\))?$", re.I)


def parse_number(text: Optional[str], fmt: Optional["NumberFormat"] = None) -> Optional[float]:
    """
    Parse a printed figure.

    Handles the conventions financial exports actually use: parentheses, leading and
    trailing minus, CR/DR markers, any currency symbol, and grouping by comma, point,
    apostrophe or space. Pass the document's NumberFormat so "1.234,56" is read the way
    the document meant it.
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t or not LOOKS_NUMERIC.match(t):
        return None
    # A dot-separated date such as 31.12.2023 satisfies the numeric shape, and reading it
    # as a figure yields 31122023. Dates are claimed by the date parser first.
    if _DATE_SHAPE.match(t):
        return None
    return _parse_number_localised(t, fmt)


def parse_date(text: Optional[str], fmt: Optional["DateFormat"] = None) -> Optional[date]:
    """
    Parse a printed date using the order established for the document.

    Without a DateFormat an ambiguous value such as "03/04/2023" returns None rather
    than a guess. Guessing was the previous behaviour and it was silently inconsistent:
    trying month-first then falling back read 03/04 as 4 March and 13/04 as 13 April,
    producing two conventions inside one column with nothing to indicate it.
    """
    return _parse_date_localised(text, fmt)


@dataclass
class Cell:
    column: int          # index into Table.column_names
    raw: str             # exactly as printed
    value: Optional[float]
    # Set once the document's date convention is known. Kept on the cell so the workbook
    # and the JSON agree on what a value means.
    date_value: Optional[date] = None

    @property
    def typed(self) -> Any:
        """Number, date, or text - whichever the cell actually holds."""
        if self.value is not None:
            return self.value
        if self.date_value is not None:
            return self.date_value
        return (self.raw or "").strip() or None


@dataclass
class Row:
    index: int                              # position within the table, 0-based
    page: int
    label: str                              # row label exactly as printed
    cells: List[Cell] = field(default_factory=list)
    depth: int = 0                          # nesting level from structure inference
    role: str = ROLE_LINE_ITEM
    block_start: Optional[int] = None        # subtotals: index of the row they total
    children: List[int] = field(default_factory=list)
    repairs: List[str] = field(default_factory=list)  # audit trail of any changes

    def value_at(self, column: int) -> Optional[float]:
        for c in self.cells:
            if c.column == column:
                return c.value
        return None

    def values(self, ncols: int) -> List[Optional[float]]:
        """Numeric values only. Used by reconciliation, which must never see text."""
        out: List[Optional[float]] = [None] * ncols
        for c in self.cells:
            if 0 <= c.column < ncols:
                out[c.column] = c.value
        return out

    def display(self, ncols: int) -> List[Any]:
        """
        Everything the row contains, correctly typed: numbers, dates, then text.

        Output must use this rather than values(). A rent roll has dates and names in
        its columns, and values() drops them - which blanked out Unit Type, Resident
        Name, Move In, Lease From and Lease To in the workbook.
        """
        out: List[Any] = [None] * ncols
        for c in self.cells:
            if 0 <= c.column < ncols:
                out[c.column] = c.typed
        return out

    @property
    def has_figures(self) -> bool:
        return any(c.value is not None for c in self.cells)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Table:
    page: int
    label_header: str = ""                              # heading over the row-label column
    column_names: List[str] = field(default_factory=list)  # data columns only
    rows: List[Row] = field(default_factory=list)

    @property
    def ncols(self) -> int:
        return len(self.column_names)

    @property
    def total_column(self) -> int:
        """
        Index of the column holding the row total.

        A column the document itself heads "Total" wins, because some reports lead with
        it rather than closing on it. Otherwise the last column, which is where
        segmented reports put it. Returns -1 when there are no columns.
        """
        for i, name in enumerate(self.column_names):
            if TOTAL_HEADING.match((name or "").strip()):
                return i
        return self.ncols - 1 if self.ncols else -1

    @property
    def data_rows(self) -> List[Row]:
        return [r for r in self.rows if r.has_figures]

    def row_by_index(self, index: int) -> Optional[Row]:
        for r in self.rows:
            if r.index == index:
                return r
        return None

    def figure_count(self) -> int:
        return sum(1 for r in self.rows for c in r.cells if c.value is not None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "label_header": self.label_header,
            "column_names": self.column_names,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class Document:
    file_name: str
    tables: List[Table] = field(default_factory=list)
    parsing_engine: Optional[str] = None
    source_blob: Optional[str] = None
    parser_output_blob: Optional[str] = None
    headings: List[str] = field(default_factory=list)
    # Everything on this dict is model-inferred, never a figure.
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def primary_table(self) -> Optional[Table]:
        """The largest table, which for a financial statement is the statement itself."""
        return max(self.tables, key=lambda t: len(t.rows), default=None)

    def figure_count(self) -> int:
        return sum(t.figure_count() for t in self.tables)

    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_name": self.file_name,
            "parsing_engine": self.parsing_engine,
            "source_blob": self.source_blob,
            "parser_output_blob": self.parser_output_blob,
            "headings": self.headings,
            "meta": self.meta,
            "tables": [t.to_dict() for t in self.tables],
        }


def normalize_label(label: Optional[str]) -> str:
    """Comparison form for row labels: lowercase alphanumerics and single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()


SUBTOTAL_RE = re.compile(r"^\s*total\s+(?:for\s+)?(.+?)\s*$", re.I)
IDENTITY_RE = re.compile(
    r"^\s*(gross\s+profit|net\s+(operating\s+)?income|net\s+profit|"
    r"operating\s+(income|profit)|profit\s+for\s+the\s+(year|period)|"
    r"net\s+assets|total\s+equity)\s*$",
    re.I,
)


def subtotal_subject(label: Optional[str]) -> Optional[str]:
    """"Total for Insurance Expense" -> "Insurance Expense"."""
    m = SUBTOTAL_RE.match(label or "")
    return m.group(1).strip() if m else None
