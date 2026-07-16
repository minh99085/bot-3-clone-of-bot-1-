"""Hermes Autonomy Stack — self-adjusting, self-improving paper alpha.

Modules
-------
* mchb     — Meta-Contextual Hierarchical Bandit (Thompson + LinUCB)
* eho      — Nightly Evolutionary Hyperparameter Optimizer (CMA-ES lite)
* cbpf     — Continual Bayesian Probability Fusion (Dirichlet)
* rasp     — Regime-Aware Self-Supervised Pretrain + Fine-Tune
* rgmc     — Risk-Guardian Meta-Controller (tighten-only)
* lifecycle — Data ingest + model registry + shadow promote / rollback

Hard constraint: NEVER mutate STRICT_REAL_FREEZE (min_edge, min_conviction, …).
Paper-only. See knowledge/skills/self_improve.md.
"""

from __future__ import annotations

__all__ = [
    "on_settlement",
    "autonomy_tick",
    "bootstrap",
]

from autonomy.orchestrator import autonomy_tick, on_settlement


def bootstrap(*args, **kwargs):  # noqa: ANN001
    from autonomy.bootstrap import run_bootstrap

    return run_bootstrap(*args, **kwargs)
