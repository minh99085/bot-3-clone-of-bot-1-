"""Loop-engine synthesis (WS5) — read live performance, emit the next minimal experiment.

This is the deterministic core of the "self-improving quant loop" the project is built around
(AGENTS.md quant-team mandate: read live performance -> hypothesize -> propose a minimal gate/strategy
change -> measure on a soak). It ingests the published light report (which now carries the signal-edge
verdicts, dep-arb outcome calibration, risk-free arb capture stats and the verifier veto-quality from
this session's work) and emits a PRIORITIZED, structured list of advisory proposals.

ADVISORY ONLY: every proposal is paper-only, auto_apply=False, and names the evidence gate that would
justify acting. It NEVER changes config or places a trade. Deterministic + testable on purpose — the
loop's hypotheses should be reproducible, not an opaque LLM guess (an LLM can narrate them, but the
triggers live here).
"""

from __future__ import annotations

from typing import Any, Optional

P_HIGH, P_MED, P_LOW = "high", "medium", "low"


def _deep_get(obj: Any, key: str) -> Optional[Any]:
    """First occurrence of ``key`` anywhere in a nested dict/list (report sections move around)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_get(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_get(v, key)
            if found is not None:
                return found
    return None


def _proposal(priority, area, observation, hypothesis, proposed_change, evidence_gate) -> dict:
    return {"priority": priority, "area": area, "observation": observation,
            "hypothesis": hypothesis, "proposed_change": proposed_change,
            "evidence_gate": evidence_gate, "paper_only": True, "auto_apply": False}


def _rank(proposals: list) -> list:
    order = {P_HIGH: 0, P_MED: 1, P_LOW: 2}
    return sorted(proposals, key=lambda p: order.get(p["priority"], 3))


def synthesize(report: dict, *, min_samples: int = 50) -> dict:
    """Inspect the report and return ranked next-experiment proposals + a plain-English summary."""
    report = report or {}
    proposals: list = []

    # --- 1) Signal-edge: a confidently anti-predictive signal is a FADE opportunity ------------- #
    se = _deep_get(report, "signal_edge") or {}
    for fc in (se.get("fade_candidates") or []):
        if int(fc.get("n", 0) or 0) >= min_samples:
            proposals.append(_proposal(
                P_HIGH, "signals",
                "Signal '%s' (%s) is anti-predictive: acc %.3f over n=%d, Wilson upper %.3f < 0.5."
                % (fc.get("source"), fc.get("context"), float(fc.get("accuracy") or 0),
                   int(fc.get("n") or 0), float(fc.get("wilson_hi") or 0)),
                "Trading the INVERSE of this signal has positive expected accuracy.",
                "Promote source=%s to FADE in the signal-edge layer (trade inverse), gated + capped."
                % fc.get("source"),
                "FADE only while Wilson upper stays < 0.5 on a rolling window; demote if it crosses."))
    for fl in (se.get("follow_candidates") or []):
        if int(fl.get("n", 0) or 0) >= min_samples:
            proposals.append(_proposal(
                P_MED, "signals",
                "Signal '%s' (%s) is reliably right: Wilson lower %.3f > 0.5 over n=%d."
                % (fl.get("source"), fl.get("context"), float(fl.get("wilson_lo") or 0),
                   int(fl.get("n") or 0)),
                "Following this signal in-context adds directional edge.",
                "Promote source=%s/%s to FOLLOW, small size, gated." % (fl.get("source"), fl.get("context")),
                "Keep FOLLOW only while Wilson lower > breakeven; walk-forward must hold."))

    # --- 2) Verifier: is the maker-checker's veto earning its keep? ----------------------------- #
    vq = _deep_get(report, "veto_quality") or {}
    if vq.get("verdict") == "vetoes_costing_edge":
        proposals.append(_proposal(
            P_HIGH, "verifier",
            "Verifier veto_quality=vetoes_costing_edge: %d vetoed setups would have won at "
            "win-rate %.3f (pnl %.2f)." % (int(vq.get("n") or 0),
                                           float(vq.get("vetoed_would_have_win_rate") or 0),
                                           float(vq.get("vetoed_would_have_pnl_usd") or 0)),
            "The 'when unsure, veto' verifier is destroying real edge, not protecting capital.",
            "Wire explore_approve into the follow path (shrink instead of hard-veto) and/or soften "
            "the verifier prompt; re-grade.",
            "Only while veto_quality stays 'vetoes_costing_edge' at n>=min; revert if it flips."))
    elif vq.get("verdict") == "good_vetoes":
        proposals.append(_proposal(
            P_LOW, "verifier",
            "Verifier veto_quality=good_vetoes: the vetoed setups would have lost — the veto helps.",
            "Keep the verifier strict; do NOT wire explore_approve yet.",
            "No change.", "Re-check each soak; act only if it flips to vetoes_costing_edge."))

    # --- 3) Directional funnel: is the WS2 un-pause actually collecting data? ------------------- #
    lifecycle = _deep_get(report, "candidate_lifecycle") or {}
    terminals = (lifecycle.get("terminals") or {}) if isinstance(lifecycle, dict) else {}
    accepted = int(terminals.get("accepted", 0) or 0)
    created = int(lifecycle.get("created", 0) or 0) if isinstance(lifecycle, dict) else 0
    if created > 100 and accepted == 0:
        proposals.append(_proposal(
            P_HIGH, "directional",
            "Lifecycle: %d candidates created, 0 accepted — directional is still collecting no data."
            % created,
            "A gate downstream of the WS2 exploration (verifier veto / underdog floor / down-bias) is "
            "still blocking every candidate.",
            "Check rejected_by_stage; if execution-gate/verifier dominate, wire explore_approve or "
            "relax the underdog floor quota for exploration.",
            "Need accepted>0 and settled>0 before any bucket can be judged."))

    # --- 6) 5x headline ------------------------------------------------------------------------- #
    fx = _deep_get(report, "five_x_improvement_status")
    ratio = _deep_get(report, "improvement_ratio")
    if fx and fx != "proven" and ratio is not None:
        primary = _deep_get(report, "primary_edge_source")
        proposals.append(_proposal(
            P_MED, "headline",
            "5x not proven (improvement_ratio %.3f); primary edge source: %s." % (float(ratio), primary),
            "Total P&L is below the 5x baseline; the proven lane should get the capital.",
            "Concentrate sizing/iteration on the primary edge source (%s) and park unproven lanes."
            % primary,
            "improvement_ratio must trend up across soaks with the proven lane leading."))

    ranked = _rank(proposals)
    return {
        "schema": "loop_synthesis/1.0", "observe_only": True, "auto_apply": False,
        "proposal_count": len(ranked),
        "top_priority": (ranked[0]["area"] if ranked else None),
        "proposals": ranked,
        "summary": _summary(ranked),
        "note": ("Advisory next-experiment proposals from live performance. Paper-only; never edits "
                 "config or trades. Each names the evidence gate that would justify acting."),
    }


def _summary(ranked: list) -> str:
    if not ranked:
        return "No actionable experiment surfaced from the current report — keep soaking for samples."
    highs = [p for p in ranked if p["priority"] == P_HIGH]
    lead = ranked[0]
    head = "%d proposal(s); %d high-priority." % (len(ranked), len(highs))
    return "%s Top: [%s] %s" % (head, lead["area"], lead["proposed_change"])
