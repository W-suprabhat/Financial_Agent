"""
Score Azure deployments against the fixture PDFs on known-correct values.

    python -m scripts.make_fixtures            # generate fixtures first
    python -m scripts.benchmark_models DIR_GPT4O gpt-4o_latest DIR_ChatBot

Deployment names are Azure deployment names, NOT model names - the two often differ,
and a model asked to identify itself will frequently answer wrongly. Read the real
mapping from Azure AI Foundry -> Deployments.

This makes real API calls and costs tokens.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.extractor import FinancialExtractor  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Ground truth for the fixtures. Compared on magnitude, since sign normalization
# is applied by the model layer.
CASES = {
    "hard_statement.pdf": {
        "revenue": 4812.6, "cost_of_goods_sold": 2918.4, "gross_profit": 1894.2,
        "operating_expenses": 742.9, "operating_income": 1151.3, "interest_expense": 88.7,
        "tax_expense": 265.4, "net_income": 797.2,
        "total_assets": 8549.6, "current_assets": 2345.5, "total_liabilities": 3832.4,
        "current_liabilities": 1422.4, "total_equity": 4717.2, "cash": 489.5,
        "accounts_receivable": 1043.7, "inventory": 812.3, "accounts_payable": 1102.4,
        "short_term_debt": 320.0, "long_term_debt": 2410.0,
        "operating_cash_flow": 1288.9, "investing_cash_flow": 690.3,
        "financing_cash_flow": 512.9, "net_change_cash": 85.7,
    },
    "scanned_statement.pdf": {
        "revenue": 2455, "cost_of_goods_sold": 1180, "gross_profit": 1275,
        "operating_expenses": 610, "operating_income": 665, "interest_expense": 45,
        "tax_expense": 155, "net_income": 465,
    },
}


def score(data, truth):
    ok, bad = 0, []
    for field, want in truth.items():
        got = getattr(data, field, None)
        if got is None:
            bad.append(f"{field}=MISSING")
        elif abs(abs(float(got)) - abs(want)) < 0.051:
            ok += 1
        else:
            bad.append(f"{field}={got}!={want}")
    return ok, bad


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Azure deployments")
    parser.add_argument("deployments", nargs="+", help="Azure deployment names")
    args = parser.parse_args()

    available = [name for name in CASES if (FIXTURES / name).exists()]
    if not available:
        print("No fixtures found. Run: python -m scripts.make_fixtures", file=sys.stderr)
        return 1

    header = f"{'deployment':24s}" + "".join(f"{n.replace('_statement.pdf',''):>18s}" for n in available)
    print(header)
    print("-" * len(header))

    details = []
    for deployment in args.deployments:
        cells = []
        for name in available:
            truth = CASES[name]
            try:
                extractor = FinancialExtractor(deployment=deployment)
                start = time.time()
                data = extractor.extract_from_pdf(str(FIXTURES / name))
                elapsed = time.time() - start
                ok, bad = score(data, truth)
                cells.append(f"{ok}/{len(truth)} {elapsed:6.1f}s")
                if bad:
                    details.append((deployment, name, bad))
            except Exception as e:
                msg = str(e)
                tag = "404" if "DeploymentNotFound" in msg else ("429" if "429" in msg else "ERR")
                cells.append(f"{tag:>12s}  ")
                details.append((deployment, name, [msg[:200]]))
        print(f"{deployment:24s}" + "".join(f"{c:>18s}" for c in cells))

    if details:
        print("\nIssues")
        for deployment, name, bad in details:
            print(f"  {deployment} [{name}]: {'; '.join(bad[:6])}")
    else:
        print("\nAll deployments scored 100% on all fixtures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
