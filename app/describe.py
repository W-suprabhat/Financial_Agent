"""
Document description: the only place a model is used.

It receives row labels and headings - never figures - and returns metadata only:
statement type, period, currency, units. That boundary is deliberate. Earlier
iterations of this project let the model read the numbers, and it produced
non-deterministic results on identical input: parenthetical negatives handled
inconsistently between documents, and four wrapped-label judgments on one run versus
one on the next for byte-identical bytes.

Everything on the critical path is now deterministic. The model's output lands on its
own worksheet so a reviewer can audit inference separately from fact.
"""

import json
import logging
from typing import Any, Dict, List

from openai import OpenAI

from .config import settings
from .models import Document

logger = logging.getLogger(__name__)

PROMPT = """
You are given the headings and row labels of a financial statement that has ALREADY
been extracted from a PDF by a deterministic parser. You are NOT given the figures and
must not invent any.

Return JSON only:
{
  "company": string|null,
  "report_title": string|null,
  "statement_type": "income_statement|balance_sheet|cash_flow|trial_balance|other",
  "period_label": string|null,
  "period_start": "YYYY-MM-DD"|null,
  "period_end": "YYYY-MM-DD"|null,
  "currency": string|null,
  "units": "units|thousands|millions|billions",
  "basis": string|null,
  "segmentation": string|null,
  "notes": string|null
}

Guidance:
- "segmentation": if the columns split the report by business line, class, department
  or geography, describe what the split represents. Otherwise null.
- "units" is the scale the figures are stated in. Use "units" when the statement shows
  actual amounts rather than thousands or millions.
- "basis" is accrual or cash, if the document says.
- Only report a period_start/period_end you can defend from the text. A single month
  such as "April 1-30, 2023" has both.

HEADINGS:
{headings}

COLUMN NAMES:
{columns}

ROW LABELS IN ORDER:
{labels}
"""


def describe_document(document: Document) -> Dict[str, Any]:
    """Infer document metadata from labels alone."""
    table = document.primary_table
    labels: List[str] = []
    columns: List[str] = []
    if table is not None:
        columns = [table.label_header] + list(table.column_names)
        labels = [r.label for r in table.rows if r.label][:120]

    prompt = (
        PROMPT
        .replace("{headings}", json.dumps(document.headings[:6], ensure_ascii=False))
        .replace("{columns}", json.dumps(columns, ensure_ascii=False))
        .replace("{labels}", json.dumps(labels, ensure_ascii=False))
    )

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    response = client.responses.create(
        model=settings.openai_deployment,
        text={"format": {"type": "json_object"}},
        max_output_tokens=1200,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
    )

    raw = response.output_text
    if not raw or not raw.strip():
        raise RuntimeError(
            f"deployment '{settings.openai_deployment}' returned an empty response; "
            "if it is a reasoning model, prefer a non-reasoning one"
        )

    meta = json.loads(raw)
    meta["_inferred_by"] = settings.openai_deployment
    meta["_scope"] = "metadata only; no figure was read or produced by the model"
    return meta


NAME_COLUMNS_PROMPT = """
A PDF parser merged two adjacent column headings into a single string and applied it to
both columns. The DATA in the two columns is already correctly separated. Your only job
is to split the heading text back into the two original headings.

Merged heading: "{heading}"

Sample values from the FIRST column:
{left}

Sample values from the SECOND column:
{right}

Return JSON only:
{"left": "<heading for the first column>", "right": "<heading for the second column>"}

Rules:
- Both headings must come from words in the merged string, in the order they appear,
  using every word exactly once between them.
- Use the sample values to decide where the split falls.
- Do not invent words that are not in the merged heading.
"""


def name_merged_columns(table) -> List[Dict[str, str]]:
    """
    Split a merged column heading back into two.

    Purely a labelling task: the figures are already in the right columns, and this only
    renames headings. It is the one thing in the pipeline a deterministic rule cannot do,
    because it needs to know that "Unit Type Resident Name" divides after "Type" rather
    than after "Unit". The result is recorded as model inference, not fact.
    """
    from .models import normalize_label

    applied: List[Dict[str, str]] = []
    client = None

    for column in range(table.ncols - 1):
        left_name = table.column_names[column]
        if normalize_label(left_name) != normalize_label(table.column_names[column + 1]):
            continue
        if len(left_name.split()) < 2:
            continue

        def samples(index: int) -> List[str]:
            out = []
            for row in table.rows:
                cell = next((c for c in row.cells if c.column == index), None)
                if cell is None or cell.value is not None:
                    continue
                v = (cell.raw or "").strip()
                if v and v not in out:
                    out.append(v)
                if len(out) >= 6:
                    break
            return out

        left_samples, right_samples = samples(column), samples(column + 1)
        if not left_samples or not right_samples:
            continue

        prompt = (
            NAME_COLUMNS_PROMPT
            .replace("{heading}", left_name)
            .replace("{left}", json.dumps(left_samples, ensure_ascii=False))
            .replace("{right}", json.dumps(right_samples, ensure_ascii=False))
        )

        if client is None:
            client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)

        try:
            response = client.responses.create(
                model=settings.openai_deployment,
                text={"format": {"type": "json_object"}},
                max_output_tokens=300,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            )
            names = json.loads(response.output_text or "{}")
        except Exception as e:
            logger.warning(f"could not split heading {left_name!r}: {e}")
            continue

        new_left = (names.get("left") or "").strip()
        new_right = (names.get("right") or "").strip()
        if not new_left or not new_right:
            continue

        # Guard: the two halves must be built from the merged heading's own words, so the
        # model cannot rename a column to something the document never said.
        original_words = left_name.split()
        if [w for w in (new_left + " " + new_right).split()] != original_words:
            logger.warning(
                f"rejected heading split {new_left!r}|{new_right!r}: does not reuse "
                f"exactly the words of {left_name!r}"
            )
            continue

        table.column_names[column] = new_left
        table.column_names[column + 1] = new_right
        applied.append({
            "merged": left_name,
            "left": new_left,
            "right": new_right,
            "basis": (
                f"first column holds values like {left_samples[0]!r}, "
                f"second like {right_samples[0]!r}"
            ),
        })

    return applied
