"""One-command bootstrap: download history + pre-train autonomy models.

Usage:
  export PYTHONPATH=. HERMES_PAPER_ONLY=1 HERMES_EHO_FAST=1
  python -m autonomy.bootstrap
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from autonomy.cbpf import get_cbpf
from autonomy.data_ingest import nightly_bulk_download, pull_gamma_markets
from autonomy.eho import run_eho
from autonomy.freeze import assert_mutable_only
from autonomy.orchestrator import load_autonomy_state, save_autonomy_state
from autonomy.rasp import get_rasp, synthetic_hard_examples
from autonomy.registry import ModelRegistry
from hermes.logging_config import enforce_paper_only, setup_logging
from hermes.state_io import ensure_dirs

logger = logging.getLogger("autonomy.bootstrap")


def run_bootstrap(*, fast: bool = True) -> dict:
    enforce_paper_only()
    setup_logging()
    ensure_dirs()

    report: dict = {}
    logger.info("=== Autonomy bootstrap start (fast=%s) ===", fast)

    # 1. Data
    g = pull_gamma_markets(limit=50)
    report["gamma"] = {"ok": g.ok, "n": g.n_rows, "error": g.error}
    bulk = nightly_bulk_download(force=True)
    report["bulk"] = {"ok": bulk.ok, "n": bulk.n_rows, "path": bulk.path, "error": bulk.error}

    # 2. RASP pretrain on synthetic + any bulk prices
    import numpy as np

    rng = np.random.default_rng(7)
    prices = list(50_000 * np.cumprod(1 + rng.normal(0, 0.0015, size=300)))
    hard = synthetic_hard_examples(prices, n=20, seed=1)
    for h in hard:
        prices.extend(h["prices"][-40:])
    report["rasp"] = get_rasp().fit_from_prices(prices)

    # 3. CBPF warm-up with synthetic component outcomes
    cbpf = get_cbpf()
    for i in range(40):
        y = bool(rng.random() > 0.35)
        comps = {
            "momentum": float(rng.uniform(0.55, 0.85) if y else rng.uniform(0.15, 0.45)),
            "obi": float(rng.uniform(0.5, 0.8) if y else rng.uniform(0.2, 0.5)),
            "lognormal": float(rng.uniform(0.5, 0.75) if y else rng.uniform(0.25, 0.5)),
        }
        cbpf.update(comps, resolved_yes=y, p_market=0.5)
    report["cbpf"] = cbpf.refit() or cbpf.last_metrics

    # 4. EHO shadow search (fast)
    eho = run_eho(
        population=4 if fast else 8,
        generations=2 if fast else 4,
        n_markets=400 if fast else 800,
        seed=42,
    )
    report["eho"] = {
        "promoted": eho.promoted,
        "wr": eho.wr,
        "dd": eho.max_dd,
        "reason": eho.reason,
        "n_evals": eho.n_evals,
    }
    reg = ModelRegistry()
    card = reg.register(
        "fusion",
        eho.params,
        metrics={"wr": eho.wr, "dd": eho.max_dd, "shadow_n": 0, "shadow_wins": 0},
        notes=f"bootstrap: {eho.reason}",
    )
    report["registry"] = {"version": card.version, "status": card.status.value}

    st = load_autonomy_state()
    st.mutable_params.update(assert_mutable_only(eho.params))
    st.mutable_params.update(assert_mutable_only(cbpf.mutable_export()))
    st.mutable_params["regime_weights"] = get_rasp().active_weights()
    st.shadow_model_version = card.version
    st.last_eho_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    save_autonomy_state(st)

    logger.info("=== Autonomy bootstrap done ===")
    logger.info("%s", report)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Bootstrap Hermes autonomy stack")
    p.add_argument("--full", action="store_true", help="Heavier EHO (slower)")
    args = p.parse_args(argv)
    report = run_bootstrap(fast=not args.full)
    ok = bool(report.get("eho", {}).get("wr", 0) >= 0.80 or report.get("gamma", {}).get("ok"))
    print("BOOTSTRAP_OK" if ok else "BOOTSTRAP_PARTIAL", report)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
