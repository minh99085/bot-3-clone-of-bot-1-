"""Autonomy orchestrator — wires MCHB/CBPF/EHO/RASP/RGMC/lifecycle into Hermes.

Hooks:
  * on_settlement(stl) — called from process_settlement
  * autonomy_tick()    — called end of each hermes turn + continuous loop

Never mutates STRICT_REAL_FREEZE keys.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from autonomy.alerts import alert
from autonomy.cbpf import get_cbpf
from autonomy.data_ingest import ingest_active_markets_15m, nightly_bulk_download
from autonomy.eho import run_eho, should_run_eho
from autonomy.freeze import TARGET_RESOLVED_FOR_REPORT, TARGET_WR, assert_mutable_only
from autonomy.mchb import build_context_from_meta, get_mchb
from autonomy.rasp import get_rasp
from autonomy.registry import ModelRegistry
from autonomy.rgmc import RiskGuardian
from autonomy.schemas import AutonomyState, SettlementReward
from hermes.state_io import ensure_dirs, paper_dir, update_state_field

logger = logging.getLogger(__name__)


def _state_path() -> Path:
    return paper_dir() / "autonomy_state.json"


def load_autonomy_state() -> AutonomyState:
    p = _state_path()
    if not p.is_file():
        return AutonomyState()
    try:
        return AutonomyState.model_validate(json.loads(p.read_text()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("autonomy state load failed: %s", exc)
        return AutonomyState()


def save_autonomy_state(st: AutonomyState) -> None:
    ensure_dirs()
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    st.updated_at = datetime.now(timezone.utc)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(st.model_dump_json(indent=2))
    tmp.replace(p)


def on_settlement(stl: Any) -> dict[str, Any]:
    """Primary learning hook after each resolved paper trade."""
    out: dict[str, Any] = {"ok": False}
    try:
        st = load_autonomy_state()
        rg = RiskGuardian(st)

        won = bool(getattr(stl, "won", False))
        pnl = float(getattr(stl, "pnl_usd", 0.0) or 0.0)
        size = float(getattr(stl, "size_usd", 0.0) or 0.0)
        notes = (getattr(stl, "notes", None) or "") + " "
        # Parse optional model_q / components from notes if present
        model_q = None
        components: dict[str, float] = {}
        meta = {}
        # Settlement may carry meta via notes key=value
        for part in notes.replace(";", " ").split():
            if "=" in part:
                k, _, v = part.partition("=")
                meta[k.strip()] = v.strip()
        try:
            if "model_q" in meta:
                model_q = float(meta["model_q"])
        except ValueError:
            pass

        # Map trade outcome → UP resolution for Brier on model_q = P(UP)
        direction = str(getattr(stl, "direction", "UP")).upper()
        if "DOWN" in direction or "NO" in direction:
            resolved_yes = not won  # won on DOWN ⇒ UP lost
        else:
            resolved_yes = won
        brier = None
        if model_q is not None:
            y = 1.0 if resolved_yes else 0.0
            brier = (float(model_q) - y) ** 2

        reward = SettlementReward(
            pnl_usd=pnl,
            size_usd=size,
            won=won,
            brier=brier,
            model_q=model_q,
            resolved_yes=resolved_yes,
        ).as_unit_reward()

        # --- MCHB update ---
        tf = getattr(stl, "timeframe", None) or "5m"
        ctx = build_context_from_meta(
            timeframe=str(tf),
            family="mispricing",
            dislocation=float(meta.get("dislocation", 0) or 0),
            hour=int(getattr(stl, "hourly_bucket", 12) or 12),
        )
        arm = meta.get("bandit_arm") or meta.get("mchb_arm") or "exploit"
        get_mchb().update(ctx, arm, reward)

        # --- CBPF update ---
        if model_q is not None:
            components = {"ensemble": float(model_q), "momentum": float(model_q)}
        if components:
            get_cbpf().update(
                components,
                resolved_yes=bool(resolved_yes),
                p_market=float(meta.get("pm_implied_up", 0.5) or 0.5),
            )
            # Export fusion weights into mutable params
            st.mutable_params.update(
                assert_mutable_only(get_cbpf().mutable_export())
            )

        # Also keep legacy signal_calibration in sync
        try:
            from strategy.signal_calibration import record_resolved_trade

            if model_q is not None:
                record_resolved_trade(
                    p_up=float(model_q),
                    resolved_yes=bool(resolved_yes),
                    components=components or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("legacy calibration: %s", exc)

        # --- RGMC ---
        equity = st.equity + pnl
        actions = rg.observe_settlement(won=won, pnl_usd=pnl, equity=equity)
        st = rg.state
        if any("DD" in a for a in actions):
            alert("dd", "; ".join(actions), meta=rg.self_report())

        # --- Registry shadow tracking ---
        reg = ModelRegistry()
        shadow_stat = reg.record_shadow_trade(won=won)
        if shadow_stat.get("ready"):
            promoted = reg.promote()
            if promoted:
                st.prod_model_version = promoted.version
                st.last_promote_at = datetime.now(timezone.utc).isoformat()
                # Apply promoted mutable params
                try:
                    st.mutable_params.update(assert_mutable_only(promoted.params))
                except Exception:  # noqa: BLE001
                    pass
                alert(
                    "promote",
                    f"{promoted.name}@{promoted.version}",
                    meta={"wr": shadow_stat.get("wr"), "n": shadow_stat.get("n")},
                )
                _append_skill_note(
                    "self_improve",
                    f"Promoted {promoted.name}@{promoted.version} wr={shadow_stat.get('wr'):.1%}",
                )

        if rg.needs_rollback():
            restored = reg.rollback()
            st.last_rollback_at = datetime.now(timezone.utc).isoformat()
            if restored:
                st.prod_model_version = restored.version
                try:
                    st.mutable_params.update(assert_mutable_only(restored.params))
                except Exception:  # noqa: BLE001
                    pass
            alert(
                "rollback",
                f"live WR={rg.rolling_wr():.1%}",
                meta=rg.self_report(),
            )
            _append_skill_note(
                "risk_guardian",
                f"Rollback after WR={rg.rolling_wr():.1%} n={st.n_resolved}",
            )

        # Apply soft knobs into mutable
        st.mutable_params["soft_kappa_scale"] = st.soft_kappa_scale
        st.mutable_params["size_multiplier"] = st.size_multiplier

        save_autonomy_state(st)
        out = {
            "ok": True,
            "reward": reward,
            "actions": actions,
            "report": rg.self_report(),
            "shadow": shadow_stat,
        }
        if st.n_resolved >= TARGET_RESOLVED_FOR_REPORT:
            update_state_field(
                "Autonomy Target",
                (
                    f"MET ≥{TARGET_WR:.0%} WR"
                    if rg.rolling_wr() >= TARGET_WR
                    else f"building WR={rg.rolling_wr():.1%}"
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_settlement autonomy failed: %s", exc)
        out = {"ok": False, "error": str(exc)}
    return out


def autonomy_tick(*, force_nightly: bool = False, force_eho: bool = False) -> dict[str, Any]:
    """Periodic autonomy maintenance — safe to call every turn."""
    summary: dict[str, Any] = {}
    st = load_autonomy_state()

    # 15m ingest (throttled)
    try:
        last = st.last_ingest_at
        run_ingest = True
        if last:
            try:
                ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                run_ingest = (datetime.now(timezone.utc) - ts).total_seconds() >= 15 * 60
            except Exception:  # noqa: BLE001
                run_ingest = True
        if run_ingest:
            batch = ingest_active_markets_15m()
            st.last_ingest_at = datetime.now(timezone.utc).isoformat()
            summary["ingest_15m"] = {"ok": batch.ok, "n": batch.n_rows}
    except Exception as exc:  # noqa: BLE001
        summary["ingest_15m"] = {"ok": False, "error": str(exc)}

    # RASP fit from CEX history if available
    try:
        from connectors.cex_realtime import get_asset_price_history

        _t, prices = get_asset_price_history("BTC", max_points=240)
        if len(prices) >= 20:
            summary["rasp"] = get_rasp().fit_from_prices(prices)
            st.mutable_params["regime_weights"] = get_rasp().active_weights()
    except Exception as exc:  # noqa: BLE001
        summary["rasp"] = {"error": str(exc)}

    # EHO
    try:
        if force_eho or should_run_eho(
            n_resolved=st.n_resolved,
            last_eho_n=st.last_eho_n,
            last_eho_at=st.last_eho_at,
        ):
            # Fast when forced in tests; lighter n_markets overnight
            fast = os.environ.get("HERMES_EHO_FAST", "0") == "1" or force_eho
            result = run_eho(
                current_params={
                    k: float(v)
                    for k, v in st.mutable_params.items()
                    if isinstance(v, (int, float))
                },
                population=4 if fast else 8,
                generations=2 if fast else 4,
                n_markets=400 if fast else 600,
            )
            st.last_eho_at = datetime.now(timezone.utc).isoformat()
            st.last_eho_n = st.n_resolved
            summary["eho"] = {
                "promoted": result.promoted,
                "wr": result.wr,
                "dd": result.max_dd,
                "reason": result.reason,
                "n_evals": result.n_evals,
            }
            # Always register as shadow candidate; promote path is registry+shadow trades
            reg = ModelRegistry()
            card = reg.register(
                "fusion",
                result.params,
                metrics={"wr": result.wr, "dd": result.max_dd},
                notes=result.reason,
            )
            st.shadow_model_version = card.version
            if result.promoted:
                # Seed mutable params immediately as soft shadow (prod gates untouched)
                st.mutable_params.update(assert_mutable_only(result.params))
                alert("eho", result.reason, meta={"wr": result.wr, "dd": result.max_dd})
                _append_skill_note("self_improve", f"EHO: {result.reason}")
    except Exception as exc:  # noqa: BLE001
        summary["eho"] = {"error": str(exc)}
        logger.warning("eho tick failed: %s", exc)

    # Nightly bulk
    try:
        run_night = force_nightly
        if not run_night and st.last_nightly_at:
            try:
                ts = datetime.fromisoformat(st.last_nightly_at.replace("Z", "+00:00"))
                run_night = (datetime.now(timezone.utc) - ts).total_seconds() >= 20 * 3600
            except Exception:  # noqa: BLE001
                run_night = False
        elif not st.last_nightly_at and st.n_resolved >= 0 and force_nightly:
            run_night = True
        if run_night:
            nb = nightly_bulk_download(force=force_nightly)
            st.last_nightly_at = datetime.now(timezone.utc).isoformat()
            summary["nightly"] = {"ok": nb.ok, "n": nb.n_rows, "error": nb.error}
    except Exception as exc:  # noqa: BLE001
        summary["nightly"] = {"error": str(exc)}

    # CBPF refit hint
    try:
        cbpf = get_cbpf()
        if st.n_resolved - st.last_cbpf_n >= 25 and cbpf.n_updates >= 15:
            metrics = cbpf.refit()
            st.last_cbpf_n = st.n_resolved
            st.mutable_params.update(assert_mutable_only(cbpf.mutable_export()))
            summary["cbpf"] = metrics
    except Exception as exc:  # noqa: BLE001
        summary["cbpf"] = {"error": str(exc)}

    save_autonomy_state(st)
    summary["state"] = {
        "n_resolved": st.n_resolved,
        "mutable": {k: st.mutable_params.get(k) for k in ("swarm_weight", "soft_kappa_scale", "size_multiplier")},
    }
    return summary


def apply_soft_sizing(size_usd: float, kappa: float) -> tuple[float, float]:
    """Apply RGMC soft multipliers — never increases past inputs' intent beyond 1.0× κ base.

    Returns (size_usd', kappa').
    """
    st = load_autonomy_state()
    sz = float(size_usd) * float(st.size_multiplier)
    k = float(kappa) * float(min(1.0, st.soft_kappa_scale))
    return max(0.0, sz), max(0.05, k)


def mchb_gate(meta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Decide exploit/explore/skip for a candidate; returns (arm, decision_meta)."""
    ctx = build_context_from_meta(
        timeframe=str(meta.get("timeframe") or "5m"),
        seconds_to_resolution=float(meta.get("seconds_to_resolution") or 300),
        liquidity_usd=float(meta.get("liquidity_usd") or 5000),
        momentum=float(meta.get("momentum") or 0),
        dislocation=float(meta.get("dislocation") or 0),
        hurst=meta.get("hurst"),
        garch_vol=meta.get("garch_vol"),
        family="mispricing",
    )
    dec = get_mchb().decide(ctx)
    return dec.arm, {
        "mchb_arm": dec.arm,
        "mchb_family": dec.family,
        "mchb_uncertainty": dec.uncertainty,
        "mchb_forced": dec.forced_exploit,
        "mchb_context": dec.context_key,
        "mchb_scores": dec.scores,
    }


def _append_skill_note(skill: str, note: str) -> None:
    from hermes.state_io import KNOWLEDGE, write_text

    path = KNOWLEDGE / "skills" / f"{skill}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        prev = path.read_text() if path.is_file() else f"# {skill}\n"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        block = f"\n- **{stamp}**: {note}\n"
        if "## Auto Log" not in prev:
            prev = prev.rstrip() + "\n\n## Auto Log\n"
        write_text(path, prev.rstrip() + block)
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill note failed: %s", exc)
