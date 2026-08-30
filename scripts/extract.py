"""
CLI: run the agent against a PDF on disk.

    python -m scripts.extract path/to/statement.pdf
    python -m scripts.extract path/to/statement.pdf --format csv
"""

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import run_extraction          # noqa: E402
from app.formatters import FinancialDataFormatter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract financial data from a PDF")
    parser.add_argument("pdf", help="Path to the PDF")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--out", help="Write to this file instead of stdout")
    args = parser.parse_args()

    path = Path(args.pdf)
    if not path.exists():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1

    pdf_base64 = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    result = run_extraction(pdf_base64, path.name)

    if result.get("error") or result.get("extracted_data") is None:
        print(f"FAILED after {result.get('attempt', 0)} attempt(s): {result.get('error')}",
              file=sys.stderr)
        return 1

    data = result["extracted_data"]
    formatter = FinancialDataFormatter()

    print(
        f"# {path.name}: {result['attempt']} attempt(s), "
        f"{result['extraction_confidence']:.0%} of expected fields populated",
        file=sys.stderr,
    )

    text = formatter.to_json(data) if args.format == "json" else formatter.to_csv([data])
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
