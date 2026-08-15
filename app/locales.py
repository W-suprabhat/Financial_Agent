"""
Locale detection for figures and dates.

Financial documents do not say which convention they use, and guessing wrong is silent:
"1.234,56" read as US drops to 1.234, and "03/04/2023" read as US becomes 4 March when
the document meant 3 April. Neither raises an error, so the figures simply come out
wrong.

Worse, a fixed guess is inconsistent *within* one column. Trying month-first and then
falling back to day-first parses 03/04 as 4 March and 13/04 as 13 April - the same
column, two conventions, no warning.

So the convention is inferred from the document's own values before anything is parsed,
and when the evidence is genuinely absent that fact is reported rather than papered over.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Sign conventions seen in exports from SAP, Tally, Xero, Sage and bank statements.
TRAILING_MINUS = re.compile(r"^(?P<body>.+?)\s*-\s*$")
CREDIT_SUFFIX = re.compile(r"^(?P<body>.+?)\s*(?P<marker>CR|DR|C|D)\s*$", re.I)

# Everything that is not a digit, a separator or a sign. Stripping by exclusion avoids
# enumerating every currency symbol in the world.
NOISE = re.compile(r"[^\d.,'   \-]")

DIGITS_ONLY = re.compile(r"^\d+$")
DATE_LIKE = re.compile(
    r"^\s*(\d{1,4})\s*[/\-.]\s*([A-Za-z]{3,9}|\d{1,2})\s*[/\-.]\s*(\d{2,4})\s*$"
)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
MONTHS.update({m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)})

DMY, MDY, YMD, UNKNOWN = "DMY", "MDY", "YMD", "unknown"

# What the analyst declares. They know the source; this is the source of truth, and
# detection below exists to disagree with it rather than to replace it.
PRESETS: Dict[str, Dict[str, str]] = {
    "US": {"decimal": ".", "group": ",", "order": MDY,
           "label": "United States - 1,234.56 and MM/DD/YYYY"},
    "UK": {"decimal": ".", "group": ",", "order": DMY,
           "label": "United Kingdom - 1,234.56 and DD/MM/YYYY"},
    "IN": {"decimal": ".", "group": ",", "order": DMY,
           "label": "India - 1,23,456.78 and DD/MM/YYYY"},
    "EU": {"decimal": ",", "group": ".", "order": DMY,
           "label": "Continental Europe - 1.234,56 and DD/MM/YYYY"},
    "CH": {"decimal": ".", "group": "'", "order": DMY,
           "label": "Switzerland - 1'234.56 and DD/MM/YYYY"},
    "ISO": {"decimal": ".", "group": ",", "order": YMD,
            "label": "ISO - 1,234.56 and YYYY-MM-DD"},
    "AUTO": {"label": "Detect from the document"},
}


@dataclass
class NumberFormat:
    decimal: str = "."
    group: str = ","
    evidence: str = "default (no counter-evidence in the document)"
    confident: bool = False

    def describe(self) -> str:
        return f"decimal '{self.decimal}', thousands '{self.group}' - {self.evidence}"


@dataclass
class DateFormat:
    order: str = UNKNOWN
    evidence: str = ""
    confident: bool = False

    def describe(self) -> str:
        return f"{self.order} - {self.evidence}"


def preset(name: Optional[str]) -> Tuple[Optional[NumberFormat], Optional[DateFormat]]:
    """Turn a declared locale into the two formats, or (None, None) for AUTO."""
    entry = PRESETS.get((name or "AUTO").upper())
    if not entry or "decimal" not in entry:
        return None, None
    label = entry["label"]
    return (
        NumberFormat(decimal=entry["decimal"], group=entry["group"],
                     evidence=f"declared: {label}", confident=True),
        DateFormat(order=entry["order"], evidence=f"declared: {label}", confident=True),
    )


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------

def _split_sign(text: str) -> Tuple[str, int]:
    """Pull the sign out, whichever convention the document uses."""
    t = text.strip()
    sign = 1

    if t.startswith("(") and t.endswith(")"):
        return t[1:-1].strip(), -1

    m = CREDIT_SUFFIX.match(t)
    if m and not DIGITS_ONLY.match(m.group("marker")):
        marker = m.group("marker").upper()
        # A credit balance is negative in a value column; a debit is positive.
        sign = -1 if marker in ("CR", "C") else 1
        t = m.group("body").strip()

    if t.startswith("-"):
        return t[1:].strip(), -sign
    m = TRAILING_MINUS.match(t)
    if m:
        return m.group("body").strip(), -sign
    return t, sign


def _separators(body: str) -> List[Tuple[str, int]]:
    """Positions of every separator, in order."""
    return [(ch, i) for i, ch in enumerate(body) if ch in ".,'   "]


def parse_number(text: Optional[str], fmt: Optional[NumberFormat] = None) -> Optional[float]:
    """
    Parse a printed figure.

    With a NumberFormat the document's own convention is applied. Without one, a value
    carrying both separators still resolves on its own - the LAST separator is the
    decimal point in every convention in use - and anything genuinely ambiguous falls
    back to the more common thousands reading.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    body, sign = _split_sign(raw)
    body = NOISE.sub("", body).strip()
    if not body or not any(ch.isdigit() for ch in body):
        return None

    seps = _separators(body)
    if not seps:
        try:
            return sign * float(body)
        except ValueError:
            return None

    kinds = {ch for ch, _ in seps}

    if fmt is not None:
        cleaned = body
        for ch in (".", ",", "'", " ", " ", " "):
            if ch == fmt.decimal:
                continue
            cleaned = cleaned.replace(ch, "")
        cleaned = cleaned.replace(fmt.decimal, ".")
    elif len(kinds) > 1:
        # both present: the rightmost is the decimal, in every convention
        decimal = body[max(i for _, i in seps)]
        cleaned = body
        for ch in (".", ",", "'", " ", " ", " "):
            if ch == decimal:
                continue
            cleaned = cleaned.replace(ch, "")
        cleaned = cleaned.replace(decimal, ".")
    else:
        only = next(iter(kinds))
        tail = body.rsplit(only, 1)[-1]
        if only in ("'", " ", " ", " ") or len(seps) > 1 or len(tail) == 3:
            cleaned = body.replace(only, "")          # grouping
        else:
            cleaned = body.replace(only, ".")         # decimal

    cleaned = cleaned.replace(" ", "")
    try:
        return sign * float(cleaned)
    except ValueError:
        return None


def detect_number_format(samples: Iterable[str]) -> NumberFormat:
    """
    Work out the document's convention from values that can only be read one way.

    A value carrying both separators settles it outright. Otherwise a separator repeated
    within one value, or followed by exactly three digits, is grouping; one followed by
    one, two or four-plus digits is a decimal point.
    """
    votes: Dict[Tuple[str, str], int] = {}
    example: Dict[Tuple[str, str], str] = {}

    def vote(decimal: str, group: str, sample: str) -> None:
        key = (decimal, group)
        votes[key] = votes.get(key, 0) + 1
        example.setdefault(key, sample)

    for sample in samples:
        if sample is None:
            continue
        body, _ = _split_sign(str(sample))
        body = NOISE.sub("", body).strip()
        seps = _separators(body)
        if not seps:
            continue
        kinds = {ch for ch, _ in seps}

        if len(kinds) > 1:
            decimal = body[max(i for _, i in seps)]
            group = next(ch for ch in kinds if ch != decimal)
            vote(decimal, group, sample)
            continue

        only = next(iter(kinds))
        if only in ("'", " ", " ", " "):
            continue  # unambiguous grouping, says nothing about the decimal mark
        tail = body.rsplit(only, 1)[-1]
        if len(seps) > 1:
            vote("." if only == "," else ",", only, sample)
        elif len(tail) in (1, 2) or len(tail) >= 4:
            vote(only, "," if only == "." else ".", sample)

    if not votes:
        return NumberFormat()

    (decimal, group), count = max(votes.items(), key=lambda kv: kv[1])
    total = sum(votes.values())
    return NumberFormat(
        decimal=decimal,
        group=group,
        evidence=(f"{count} of {total} decisive value(s), e.g. "
                  f"{example[(decimal, group)]!r}"),
        confident=count >= max(2, total * 0.6),
    )


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def _parts(text: str) -> Optional[Tuple[str, str, str]]:
    m = DATE_LIKE.match(str(text))
    return (m.group(1), m.group(2), m.group(3)) if m else None


def detect_date_order(samples: Iterable[str]) -> DateFormat:
    """
    Decide day-first or month-first from values that can only mean one thing.

    A first component above 12 can only be a day; a second component above 12 can only
    be a day in second position. Where a document offers neither, that is reported as
    unknown rather than guessed - the previous behaviour silently produced two different
    conventions within a single column.
    """
    dmy = mdy = ymd = 0
    dmy_example = mdy_example = None
    seen = 0

    for sample in samples:
        parts = _parts(sample or "")
        if not parts:
            continue
        seen += 1
        a, b, c = parts

        if len(a) == 4 and a.isdigit():
            ymd += 1
            continue
        if not b.isdigit():
            dmy += 1  # "03-Apr-2023": the month is named, so the first part is a day
            dmy_example = dmy_example or sample
            continue
        first, second = int(a), int(b)
        if first > 12 and second <= 12:
            dmy += 1
            dmy_example = dmy_example or sample
        elif second > 12 and first <= 12:
            mdy += 1
            mdy_example = mdy_example or sample

    if not seen:
        return DateFormat(order=UNKNOWN, evidence="no dates found")

    if ymd and not dmy and not mdy:
        return DateFormat(YMD, f"{ymd} ISO-style value(s)", confident=True)

    if dmy and not mdy:
        return DateFormat(DMY, f"day above 12 in {dmy} value(s), e.g. {dmy_example!r}",
                          confident=True)
    if mdy and not dmy:
        return DateFormat(MDY, f"day above 12 in second position in {mdy} value(s), "
                               f"e.g. {mdy_example!r}", confident=True)
    if dmy and mdy:
        winner = DMY if dmy >= mdy else MDY
        return DateFormat(winner,
                          f"conflicting evidence ({dmy} day-first, {mdy} month-first); "
                          f"using {winner}", confident=False)

    return DateFormat(
        order=UNKNOWN,
        evidence=(f"{seen} date(s) found but every component is 12 or below, so the "
                  "document does not reveal whether it is day-first or month-first"),
    )


def parse_date(text: Optional[str], fmt: Optional[DateFormat] = None) -> Optional[date]:
    """Parse a printed date using the document's established order."""
    if text is None:
        return None
    parts = _parts(str(text))
    if not parts:
        return None
    a, b, c = parts

    if len(a) == 4 and a.isdigit():
        year, second, third = int(a), b, c
        month = MONTHS.get(str(second).lower()) if not str(second).isdigit() else int(second)
        day = int(third)
    else:
        year = int(c)
        if year < 100:
            year += 2000 if year < 70 else 1900
        if not b.isdigit():
            month = MONTHS.get(b.lower())
            day = int(a)
        else:
            first, second = int(a), int(b)
            order = (fmt.order if fmt else UNKNOWN)
            if first > 12:
                day, month = first, second
            elif second > 12:
                month, day = first, second
            elif order == DMY:
                day, month = first, second
            elif order == MDY:
                month, day = first, second
            else:
                return None  # ambiguous and undeclared: refuse rather than invent one

    if not month or not (1 <= month <= 12):
        return None
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None
