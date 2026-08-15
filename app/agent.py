"""
The extraction agent.

    START
      -> parse_document      (Evalueserve IDP: submit, poll, fetch)
      -> infer_structure     (hierarchy from "Total for X" brackets, no coordinates)
      -> reconcile           (cross-foot, subtotals, accounting identities)
      -> [exceptions?] ------> repair -> infer_structure -> reconcile   (loop)
      -> describe            (model: statement type, period, currency - never figures)
      -> finalize
    END

What makes this an agent rather than a script is the repair loop: it verifies its own
output arithmetically, and when a check fails it diagnoses the cause, applies a fix, and
re-verifies. Crucially the feedback signal is objective - the document's own arithmetic -
so a repair is kept only when the exception count actually falls, never because it seemed
plausible.

The model is deliberately confined to describe(): naming the statement type, period and
currency. It never reads, alters or produces a figure.
"""

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from . import idp, locales, provenance, reconcile, repair, rules, structure
from .config import settings
from .models import Document

logger = logging.getLogger(__name__)


def _stamp() -> str:
    """UTC timestamp for the audit trail."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Below this, a majority of the figures are unproven and the status alone misleads.
UNVERIFIED_COVERAGE_PCT = 50.0


class AgentState(TypedDict, total=False):
    pdf_bytes: bytes
    file_name: str
    job_id: str
    locale: Optional[str]

    document: Optional[Document]
    checks: List[reconcile.Check]
    summary: Dict[str, Any]

    repairs: List[Dict[str, Any]]
    structure_notes: List[str]
    rounds: int
    finished_at: str
    tried: List[str]

    # learned fixes
    fingerprints: List[str]
    applied_rules: List[Dict[str, Any]]
    proposals: List[Dict[str, Any]]
    findings: List[Dict[str, Any]]

    error: Optional[str]
    trace: List[str]


def _log(state: AgentState, message: str) -> List[str]:
    trace = list(state.get("trace") or [])
    trace.append(message)
    logger.info(message)
    return trace


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

def parse_document(state: AgentState) -> dict:
    """
    Run the document through the Evalueserve parser.

    A batch hands in an already-collected Document instead: it submits every file to the
    parser before waiting on any of them, so the whole pack takes about as long as its
    slowest document rather than the sum of all of them.
    """
    file_name = state.get("file_name") or "document.pdf"
    document = state.get("document")

    if document is None:
        if not state.get("pdf_bytes"):
            return {"error": "no PDF supplied"}
        try:
            document = idp.parse_pdf(state["pdf_bytes"], file_name, state["job_id"],
                                     locale=state.get("locale"))
        except Exception as e:
            logger.error(f"parse failed for {file_name}: {e}")
            return {"error": f"document parser failed: {e}", "document": None}

    return {
        "document": document,
        "rounds": 0,
        "repairs": [],
        "tried": [],
        "trace": _log(
            state,
            f"parsed with {document.parsing_engine}: {document.row_count()} rows, "
            f"{document.figure_count()} figures across {len(document.tables)} table(s)",
        ),
    }


def apply_learned_fixes(state: AgentState) -> dict:
    """
    Apply fixes an analyst already approved for this layout.

    This is what stops the same repair being asked for every month. A rule approved once
    against a layout fingerprint is applied on sight, with no human involved.
    """
    document = state.get("document")
    if document is None:
        return {}

    fingerprints, applied = [], []
    for table in document.tables:
        fp = rules.fingerprint(table)
        fingerprints.append(fp)
        saved = rules.rule_store.rules_for(fp)
        used = []
        for rule in saved:
            changed = rules.apply_proposal(table, rule)
            if changed:
                entry = dict(rule)
                entry["rows_changed"] = changed
                entry["page"] = table.page
                # An approved rule carries its own authority: who allowed it and when.
                entry["at"] = _stamp()
                entry.setdefault("authority", rule.get("risk") or rules.PROPOSE)
                entry.setdefault("proof", f"layout {fp[:8]} matched")
                applied.append(entry)
                used.append(rule.get("id"))
        if used:
            rules.rule_store.note_applied(fp, used)

    note = (
        f"applied {len(applied)} saved fix(es) from previous approvals"
        if applied else "no saved fixes for this layout"
    )
    return {
        "fingerprints": fingerprints,
        "applied_rules": applied,
        "trace": _log(state, note),
    }


def propose_fixes(state: AgentState) -> dict:
    """
    Compute fixes for problems arithmetic cannot prove, and describe the rest.

    Proposals are NOT applied. They need one human approval, after which they become a
    saved rule and never need approving again.
    """
    document = state.get("document")
    if document is None:
        return {}

    already = {r.get("id") for r in state.get("applied_rules") or []}
    by_fingerprint = {rules.fingerprint(t): t for t in document.tables}

    proposals: List[Dict[str, Any]] = []
    for proposal in rules.detect_all(document.tables):
        entry = proposal.to_dict()
        if entry["id"] in already:
            continue
        # the proposal records the layout it was detected on
        fp = entry.get("fingerprint")
        table = by_fingerprint.get(fp)
        entry["layout"] = {
            "columns": table.column_names if table else [],
            "label_header": table.label_header if table else "",
            "document": document.file_name,
        }
        proposals.append(entry)

    findings = rules.detect_report_only(document.tables)

    # Reconciliation is this project's quality signal, so a document it could not reach
    # has to say so. Left unsaid, "1 exception(s)" over 846 figures reads exactly like a
    # clean statement carrying one error - the pipeline's silence looked like its success.
    #
    # Both conditions are required. Low coverage alone is normal: reconcile withholds
    # checks on rent rolls on purpose, and a warning there trains an analyst to ignore
    # warnings. It is low coverage *beside the document's own printed totals* that means
    # arithmetic was available and went unused.
    cov = provenance.coverage(document, state.get("checks") or [])
    if (cov["figures"]
            and cov["coverage_pct"] < UNVERIFIED_COVERAGE_PCT
            and structure.has_unrecognised_totals(document.tables)):
        findings.append({
            "kind": "unverified_figures",
            "risk": rules.REPORT,
            "description": (
                f"{cov['unchecked']} of {cov['figures']} figures carry no arithmetic "
                f"check. This document prints totals, but no account hierarchy was "
                f"recognised in it, so almost nothing could be cross-footed."
            ),
            "impact": (
                f"Treat these figures as unchecked, not as correct - the status reflects "
                f"only the {cov['verified']} figure(s) a check covers. The transcription "
                f"is faithful to the page and every figure names its source on the "
                f"Provenance sheet, but this layout was not understood well enough to "
                f"prove the numbers against each other. Verify against the source."
            ),
        })

    # A document whose dates are all 12-or-below never reveals whether it is day-first
    # or month-first. Those dates are left unparsed and reported, because reading
    # 03/04/2023 as 4 March when the document meant 3 April is wrong without being an
    # error, and the previous behaviour produced both readings inside one column.
    locale_info = (document.meta or {}).get("locale") or {}

    # The analyst declared where the document came from. If the document itself proves
    # otherwise, say so: this is the case where a pack quietly mixes regions.
    for conflict in locale_info.get("conflicts") or []:
        findings.append({
            "kind": "locale_mismatch",
            "risk": rules.REPORT,
            "description": conflict,
            "impact": (
                "The document's own convention was used, so the figures and dates here "
                "are correct. Check whether this document belongs to a different region "
                "than the rest of the batch."
            ),
        })

    # Nothing about dates is put to the analyst. The convention is settled internally:
    # the document's own evidence decides it where any date proves day-first or
    # month-first, and DOCUMENT_LOCALE settles the rest. Which of the two was used is
    # recorded on the Audit sheet, so the decision is traceable without being a prompt.

    parts = []
    if proposals:
        parts.append(f"{len(proposals)} fix(es) proposed for approval")
    if findings:
        parts.append(f"{len(findings)} structural finding(s) reported, not changed")
    return {
        "proposals": proposals,
        "findings": findings,
        "trace": _log(state, "; ".join(parts) if parts else "no proposals"),
    }


def infer_structure(state: AgentState) -> dict:
    """Rebuild the account hierarchy from row labels."""
    document = state.get("document")
    if document is None:
        return {}

    notes: List[str] = []
    for table in document.tables:
        notes.extend(structure.infer(table))

    blocks = sum(
        1 for t in document.tables for r in t.rows if r.block_start is not None
    )
    return {
        "structure_notes": notes,
        "trace": _log(state, f"inferred hierarchy: {blocks} block(s) identified"),
    }


def verify(state: AgentState) -> dict:
    """Reconcile the figures against the document's own arithmetic."""
    document = state.get("document")
    if document is None:
        return {"checks": [], "summary": reconcile.summarize([])}

    checks = reconcile.run_document(document)
    # Status and coverage together: how much the checks reached qualifies what they found.
    summary = provenance.reconciliation_summary(document, checks)
    return {
        "checks": checks,
        "summary": summary,
        "trace": _log(
            state,
            f"reconciliation: {summary['passed']}/{summary['total']} tie -> {summary['status']}",
        ),
    }


def attempt_repair(state: AgentState) -> dict:
    """
    Try one repair strategy and keep it only if the exception count falls.

    The table is deep-copied first, so a strategy that makes things worse - or changes
    nothing - is discarded rather than left in place.
    """
    document = state.get("document")
    checks = state.get("checks") or []
    failures = [c for c in checks if not c.passed]
    tried = list(state.get("tried") or [])
    rounds = int(state.get("rounds") or 0) + 1
    applied = list(state.get("repairs") or [])

    if document is None or not failures:
        return {"rounds": rounds}

    baseline = len(failures)

    for strategy in repair.STRATEGIES:
        name = strategy.__name__
        if name in tried:
            continue

        snapshot = copy.deepcopy(document.tables)
        proposals: List[repair.Repair] = []
        for table in document.tables:
            proposals.extend(strategy(table, failures))

        if not proposals:
            tried.append(name)
            continue

        # re-infer and re-verify to judge the change
        for table in document.tables:
            structure.infer(table)
        after = [c for c in reconcile.run_document(document) if not c.passed]

        if len(after) < baseline:
            # Stamp what authorised the change and what proved it. A repair is only ever
            # kept because the exception count fell, so the drop is the proof.
            proof = f"exceptions {baseline} -> {len(after)}"
            for p in proposals:
                entry = p.to_dict()
                entry.update(at=_stamp(), authority=rules.AUTO, proof=proof)
                applied.append(entry)
            tried.append(name)
            return {
                "rounds": rounds,
                "tried": tried,
                "repairs": applied,
                "trace": _log(
                    state,
                    f"repair [{name}] accepted: exceptions {baseline} -> {len(after)}; "
                    + "; ".join(p.description for p in proposals[:3]),
                ),
            }

        document.tables = snapshot
        tried.append(name)
        logger.info(f"repair [{name}] rejected: exceptions {baseline} -> {len(after)}")

    return {
        "rounds": rounds,
        "tried": tried,
        "trace": _log(state, "no remaining repair strategy improves reconciliation"),
    }


def should_repair(state: AgentState) -> str:
    """Loop while exceptions remain, strategies are untried, and rounds are left."""
    if state.get("error"):
        return "describe"
    summary = state.get("summary") or {}
    if not summary.get("failed"):
        return "describe"
    if int(state.get("rounds") or 0) >= settings.max_repair_rounds:
        return "describe"
    if len(state.get("tried") or []) >= len(repair.STRATEGIES):
        return "describe"
    return "repair"


def describe(state: AgentState) -> dict:
    """
    Ask the model for document metadata only.

    Statement type, period, currency, units: interpretation, not data. It is given the
    headings and row labels, never asked to produce or confirm a figure, so it cannot
    affect a single number in the output.
    """
    document = state.get("document")
    if document is None:
        return {}

    try:
        from .describe import describe_document, name_merged_columns
        document.meta = describe_document(document)

        # Where the parser merged two headings into one string, split the label back out.
        # The figures are already in the correct columns by this point; this only renames.
        renames = []
        for table in document.tables:
            renames.extend(name_merged_columns(table))
        if renames:
            document.meta["column_headings_split"] = renames

        note = (
            f"described: {document.meta.get('statement_type')} / "
            f"{document.meta.get('period_label')}"
            + (f"; split {len(renames)} merged heading(s)" if renames else "")
        )
    except Exception as e:
        logger.warning(f"description step failed (non-fatal): {e}")
        document.meta = {"error": str(e)}
        note = f"description unavailable: {e}"

    return {"trace": _log(state, note)}


def finalize(state: AgentState) -> dict:
    summary = state.get("summary") or {}
    bits = [
        f"{state.get('rounds') or 0} repair round(s)",
        f"{len(state.get('repairs') or [])} arithmetic repair(s)",
        f"{len(state.get('applied_rules') or [])} saved fix(es) reused",
        f"{len(state.get('proposals') or [])} awaiting approval",
        f"status: {summary.get('status')}",
    ]
    # A status is never reported on its own: it means one thing over 20 verified figures
    # and something else entirely over 3 of 846.
    cov = summary.get("coverage") or {}
    if cov.get("figures"):
        bits.append(
            f"{cov['verified']} of {cov['figures']} figures verified "
            f"({cov['coverage_pct']}%)"
        )
    return {
        "finished_at": _stamp(),
        "trace": _log(state, "done - " + ", ".join(bits)),
    }


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def build_agent():
    workflow = StateGraph(AgentState)

    workflow.add_node("parse_document", parse_document)
    workflow.add_node("apply_learned_fixes", apply_learned_fixes)
    workflow.add_node("infer_structure", infer_structure)
    workflow.add_node("verify", verify)
    workflow.add_node("repair", attempt_repair)
    workflow.add_node("propose_fixes", propose_fixes)
    workflow.add_node("describe", describe)
    workflow.add_node("finalize", finalize)

    workflow.add_edge(START, "parse_document")
    # saved fixes are applied before anything is judged, so a known layout needs no human
    workflow.add_edge("parse_document", "apply_learned_fixes")
    workflow.add_edge("apply_learned_fixes", "infer_structure")
    workflow.add_edge("infer_structure", "verify")
    workflow.add_conditional_edges(
        "verify", should_repair, {"repair": "repair", "describe": "propose_fixes"}
    )
    # a repair re-infers structure and re-verifies, closing the loop
    workflow.add_edge("repair", "infer_structure")
    workflow.add_edge("propose_fixes", "describe")
    workflow.add_edge("describe", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


agent = build_agent()


def run(pdf_bytes: Optional[bytes], file_name: str, job_id: str,
        locale: Optional[str] = None, document: Optional[Document] = None) -> AgentState:
    """
    Run the agent end to end for one document.

    Pass `document` when the parser has already been called - a batch collects every
    document first so the parser can work on them concurrently.
    """
    return agent.invoke({
        "pdf_bytes": pdf_bytes,
        "file_name": file_name,
        "job_id": job_id,
        "locale": locale,
        "document": document,
        "trace": [],
    })
