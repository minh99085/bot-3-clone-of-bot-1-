"""Beginner-friendly CLI for validating 80%+ win rate.

Recommended first command::

    python -m backtest --fast

Also works as::

    python backtest/run.py --fast
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from backtest.artifacts import (
    apply_best_params_to_config,
    save_best_params,
    save_run_bundle,
)
from backtest.compare import compare_naive_vs_enhanced
from backtest.engine import BacktestEngine
from backtest.metrics import (
    calibration_points,
    compute_metrics,
    threshold_sweep,
)
from backtest.monte_carlo import run_monte_carlo
from backtest.plots import (
    save_calibration,
    save_equity_and_drawdown,
    save_monte_carlo_hist,
    save_threshold_sweep,
)
from backtest.tuning import run_tuning
from models.config import EnhancedMispriceConfig, load_enhanced_config

logger = logging.getLogger(__name__)


def _console(no_rich: bool = False) -> Console:
    return Console(force_terminal=not no_rich, no_color=no_rich, highlight=not no_rich)


def _cmd_string(argv: list[str]) -> str:
    return "python -m backtest " + " ".join(shlex.quote(a) for a in argv)


def _params_snapshot(cfg: EnhancedMispriceConfig, n_markets: int, seed: int) -> dict[str, Any]:
    return {
        "n_markets": n_markets,
        "seed": seed,
        "mode": cfg.mode,
        "min_edge": cfg.min_edge,
        "min_conviction": cfg.min_conviction,
        "kappa_base": cfg.kappa_base,
        "risk_budget": cfg.risk_budget,
        "extreme_q_high": cfg.extreme_q_high,
        "extreme_q_low": cfg.extreme_q_low,
        "n_eff_crypto": cfg.n_eff.crypto,
        "max_single_market_pct": cfg.max_single_market_pct,
        "bankroll": cfg.bankroll,
        "brier_noise_calibrated": cfg.brier_noise_calibrated,
        "market_noise": cfg.market_noise,
    }


def format_verdict(
    *,
    win_rate: float,
    n_trades: int,
    max_dd: float,
    target_met: bool,
    mc_p5: Optional[float] = None,
) -> str:
    """One-line clear verdict for beginners."""
    status = "✅ Target met" if target_met else "❌ Target missed"
    parts = [
        f"{status}: {win_rate:.1%} win rate on {n_trades:,} trades",
    ]
    if mc_p5 is not None:
        parts.append(f"Monte Carlo 5th percentile: {mc_p5:.1%}")
    parts.append(f"Max DD: {max_dd:.1%}")
    return " | ".join(parts)


def print_verdict_banner(console: Console, verdict: str, *, ok: bool) -> None:
    style = "bold green" if ok else "bold red"
    console.print(Panel(verdict, title="Verdict", border_style=style, style=style))


def print_metrics_table(console: Console, m) -> None:
    table = Table(title="Backtest metrics (plain English)", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Win rate", f"{m.win_rate:.1%}")
    table.add_row("Trades taken", f"{m.n_trades:,}")
    table.add_row("Decisions seen", f"{m.n_decisions:,}")
    table.add_row("Selectivity", f"{m.selectivity:.1%}")
    table.add_row("Profit factor", f"{m.profit_factor:.2f}")
    table.add_row("Max drawdown", f"{m.max_drawdown_pct:.1%}")
    table.add_row("Expectancy / trade", f"${m.expectancy_usd:.2f}")
    table.add_row("Model Brier", f"{m.brier:.3f}")
    table.add_row("Target (≥80% WR, DD≤15%)", "YES ✅" if m.target_met else "NO ❌")
    console.print(table)
    console.print(Panel(m.plain_english, title="What this means", border_style="blue"))


def run_pipeline(args: argparse.Namespace, argv: list[str]) -> int:
    console = _console(getattr(args, "no_rich", False))
    command = _cmd_string(argv)
    filter_mode = getattr(args, "filter_mode", None)
    cfg = load_enhanced_config(args.config, mode=filter_mode)

    # --- resolve n_markets / plots / mc from --fast ---
    seed = int(args.seed if args.seed is not None else cfg.synthetic_seed)
    cfg.synthetic_seed = seed

    n_markets = args.n_markets
    mc_runs = 0
    if args.fast:
        n_markets = n_markets or 1500
        # Fast mode: skip heavy plots unless user explicitly passed --plots
        do_plots = bool(args.plots_explicit) and not bool(args.no_plots)
        mc_runs = 8
        console.print(
            Panel(
                "[bold]⚡ Fast demo mode[/]\n"
                f"filter_mode={cfg.mode} · n_markets={n_markets} · seed={seed} · "
                f"light Monte Carlo ({mc_runs} runs)\n"
                "Target: finish quickly and show whether ≥80% WR holds.",
                border_style="yellow",
            )
        )
    else:
        n_markets = n_markets or 5000
        do_plots = (not bool(args.no_plots)) and bool(args.plots)
        mc_runs = 20 if args.mode == "synthetic" and not args.optimize else 0

    if args.optimize:
        return _run_optimize(console, cfg, args, command, n_markets, seed)

    # --- main backtest ---
    console.print(
        f"[bold]Running {args.mode} backtest[/] · n_markets={n_markets} · seed={seed}"
    )

    engine = BacktestEngine(cfg, mode="enhanced", seed=seed)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]Backtest[/]"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        disable=args.no_rich,
        transient=True,
    ) as progress:
        task = progress.add_task("simulating", total=3)
        if args.mode == "historical":
            from backtest.historical import (
                load_historical_decisions,
                write_example_historical_csv,
            )

            progress.advance(task)
            csv_path = args.csv
            if not csv_path:
                write_example_historical_csv()
            decisions = load_historical_decisions(csv_path)
            progress.advance(task)
            if len(decisions) < 20:
                console.print("[yellow]Few historical rows — falling back to synthetic.[/]")
                er = engine.run_synthetic(n_markets=n_markets, seed=seed)
            else:
                er = engine.run_on_decisions(decisions)
            progress.advance(task)
        else:
            progress.advance(task)
            er = engine.run_synthetic(n_markets=n_markets, seed=seed)
            progress.advance(task)
            progress.advance(task)

    m = compute_metrics(er)
    print_metrics_table(console, m)

    mc_p5: Optional[float] = None
    mc_summary = None
    if mc_runs > 0 and args.mode == "synthetic":
        console.print(f"[dim]Quick Monte Carlo ({mc_runs} seeds) for consistency…[/]")
        mc_summary, _ = run_monte_carlo(
            config=cfg,
            n_runs=mc_runs,
            n_markets=min(n_markets, 3000),
            base_seed=seed,
            show_progress=not args.no_rich,
        )
        mc_p5 = mc_summary.p5_wr
        console.print(
            f"Monte Carlo: mean WR={mc_summary.mean_wr:.1%} · "
            f"p5={mc_summary.p5_wr:.1%} · consistent={mc_summary.consistent}"
        )

    # Optional baseline compare
    compare_block = ""
    compare_payload = None
    if args.compare_baseline:
        console.print("[bold]Comparing naive misprice vs full enhanced stack…[/]")
        cmp = compare_naive_vs_enhanced(
            config=cfg, n_markets=min(n_markets, 5000), seed=seed
        )
        console.print(cmp.summary_text())
        compare_block = "\n\n" + cmp.summary_text()
        compare_payload = cmp.to_dict()

    verdict = format_verdict(
        win_rate=m.win_rate,
        n_trades=m.n_trades,
        max_dd=m.max_drawdown_pct,
        target_met=m.target_met,
        mc_p5=mc_p5,
    )
    print_verdict_banner(console, verdict, ok=m.target_met)
    if m.target_met and args.fast:
        console.print(
            Panel(
                "[bold green]🎉 Success![/]\n"
                "The Kelly + Beta conviction filters cleared the 80% win-rate target "
                "in fast mode. Next: run a full validation:\n"
                "  [cyan]python -m backtest --n-markets 5000 --seed 42[/]",
                border_style="green",
            )
        )

    # --- artifacts ---
    plot_paths: list[Path] = []

    def _save_plots(run_dir: Path) -> list[Path]:
        paths = []
        p1 = save_equity_and_drawdown(er.equity_curve, run_dir / "equity_drawdown.png")
        p2 = save_calibration(calibration_points(er.decisions), run_dir / "calibration.png")
        p3 = save_threshold_sweep(threshold_sweep(er.decisions), run_dir / "threshold_sweep.png")
        for p in (p1, p2, p3):
            if p:
                paths.append(p)
        if mc_summary is not None:
            p4 = save_monte_carlo_hist(mc_summary.win_rates, run_dir / "wr_hist.png")
            if p4:
                paths.append(p4)
        return paths

    params = _params_snapshot(cfg, n_markets, seed)
    extra = {
        "mode": args.mode,
        "n_markets": er.n_markets,
        "n_decision_points": er.n_decision_points,
        "seed": er.seed,
        "fast": bool(args.fast),
        "threshold_sweep": threshold_sweep(er.decisions),
        "calibration": calibration_points(er.decisions),
        "monte_carlo": mc_summary.to_dict() if mc_summary else None,
        "compare": compare_payload,
    }

    from backtest.artifacts import new_run_dir
    import json
    import yaml

    run_dir = new_run_dir()
    if do_plots:
        plot_paths = _save_plots(run_dir)

    report_body = (
        f"Reproduce with:\n  {command}\n\n"
        f"{'=' * 60}\n\n"
        f"{verdict}\n\n"
        f"{m.summary_text()}"
        f"{compare_block}"
    )
    (run_dir / "report.txt").write_text(report_body)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "tag": "fast" if args.fast else args.mode,
                "command": command,
                "verdict": verdict,
                "metrics": m.to_dict(),
                "extra": extra,
            },
            indent=2,
            default=str,
        )
    )
    (run_dir / "metrics.json").write_text(json.dumps(m.to_dict(), indent=2, default=str))
    (run_dir / "parameters_used.yaml").write_text(
        yaml.safe_dump(params, sort_keys=False, default_flow_style=False)
    )
    if plot_paths:
        (run_dir / "plots_index.json").write_text(
            json.dumps([str(p) for p in plot_paths], indent=2)
        )
    (run_dir / "extra.json").write_text(json.dumps(extra, indent=2, default=str))

    console.print(f"[green]Artifacts → {run_dir}[/]")
    if not m.target_met:
        console.print(
            Panel(
                "Win rate below 80%. See [bold]BACKTEST_GUIDE.md[/] → Troubleshooting.\n"
                "Quick try: [cyan]python -m backtest --optimize --fast[/]",
                border_style="red",
                title="Next step",
            )
        )
    return 0 if m.target_met else 1


def _run_optimize(
    console: Console,
    cfg: EnhancedMispriceConfig,
    args: argparse.Namespace,
    command: str,
    n_markets: int,
    seed: int,
) -> int:
    trials = 12 if args.fast else (args.trials or cfg.tune_trials)
    n_m = min(n_markets, 2500) if args.fast else n_markets
    console.print(
        Panel(
            f"[bold]Parameter optimization[/]\n"
            f"trials={trials} · n_markets={n_m} · seed={seed}\n"
            "Objective: maximize WR × (1−DD) × log(1+return) with WR≥80% and DD≤15%.",
            border_style="magenta",
        )
    )
    result = run_tuning(
        config=cfg,
        n_trials=trials,
        n_markets=n_m,
        seed=seed,
        show_progress=not args.no_rich,
    )
    console.print(Panel(result.plain_english, title="Tuning result"))
    table = Table(title="Best parameters")
    table.add_column("Parameter")
    table.add_column("Value", justify="right")
    for k, v in result.best_params.items():
        table.add_row(k, str(v))
    console.print(table)

    ok = bool(result.best_metrics.get("feasible"))
    verdict = format_verdict(
        win_rate=float(result.best_metrics.get("win_rate") or 0),
        n_trades=int(result.best_metrics.get("n_trades") or 0),
        max_dd=float(result.best_metrics.get("max_drawdown_pct") or 0),
        target_met=ok,
    )
    print_verdict_banner(console, verdict, ok=ok)

    save_best_params(result.best_params, metrics=result.best_metrics)
    if ok:
        apply_best_params_to_config(args.config, result.best_params)
        console.print(
            "[green]Active config updated with best params "
            f"({args.config}) and config/best_params.json[/]"
        )

    path = save_run_bundle(
        tag="optimize",
        metrics=result.best_metrics,
        summary_text=result.plain_english + "\n" + str(result.best_params),
        command=command,
        parameters=result.best_params,
        extra=result.to_dict(),
        verdict=verdict,
    )
    console.print(f"[green]Artifacts → {path}[/]")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m backtest",
        description=(
            "Hermes backtest — validate that Kelly + Beta conviction deliver ≥80% win rate. "
            "Beginners: start with  python -m backtest --fast"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=["synthetic", "historical"],
        default="synthetic",
        help="synthetic = fake realistic markets (recommended); historical = CSV/Gamma",
    )
    p.add_argument(
        "--filter-mode",
        choices=["strict", "strict_real", "moderate", "aggressive"],
        default=None,
        help=(
            "Entry-filter profile from config MODE_PRESETS "
            "(default: mode: in enhanced_misprice.yaml). "
            "strict_real = high WR with real cex_implied_up as q; "
            "moderate = more trades with looser real-q gates."
        ),
    )
    p.add_argument(
        "--n-markets",
        "--n_markets",
        dest="n_markets",
        type=int,
        default=None,
        help="Number of synthetic markets (default 5000 full / 1500 with --fast)",
    )
    p.add_argument(
        "--fast",
        action="store_true",
        help="Quick validation: ~1500 markets, light Monte Carlo, skip heavy plots (<2 min)",
    )
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    p.add_argument(
        "--optimize",
        action="store_true",
        help="Search thresholds for ≥80%% WR and save best params to config",
    )
    p.add_argument(
        "--plots",
        dest="plots",
        action="store_true",
        default=True,
        help="Save equity/drawdown/calibration/threshold plots (default on for full runs)",
    )
    p.add_argument(
        "--no-plots",
        dest="no_plots",
        action="store_true",
        help="Skip all plot generation",
    )
    p.add_argument(
        "--compare-baseline",
        action="store_true",
        help="Also run naive misprice-only and show win-rate lift vs enhanced",
    )
    p.add_argument(
        "--no-rich",
        action="store_true",
        help="Plain terminal output (no colors / progress bars)",
    )
    p.add_argument("--csv", type=str, default=None, help="Historical CSV path (with --mode historical)")
    p.add_argument("--config", default="config/enhanced_misprice.yaml")
    p.add_argument("--trials", type=int, default=None, help="Optimization trials (with --optimize)")
    p.add_argument("-v", "--verbose", action="store_true")

    # Legacy subcommands still work
    sub = p.add_subparsers(dest="legacy_cmd")
    for name, help_txt in (
        ("run", "Alias for default pipeline"),
        ("monte-carlo", "Full Monte Carlo distribution"),
        ("tune", "Alias for --optimize"),
        ("compare", "Alias for --compare-baseline"),
    ):
        sp = sub.add_parser(name, help=help_txt)
        sp.add_argument("--n-markets", "--n_markets", dest="n_markets", type=int, default=None)
        sp.add_argument("--seed", type=int, default=42)
        sp.add_argument("--plots", action="store_true", default=True)
        sp.add_argument("--n_runs", type=int, default=None)
        sp.add_argument("--trials", type=int, default=None)
        sp.add_argument("--config", default="config/enhanced_misprice.yaml")
        sp.add_argument(
            "--filter-mode",
            choices=["strict", "strict_real", "moderate", "aggressive"],
            default=None,
        )
        sp.add_argument("--fast", action="store_true")
        sp.add_argument("--no-rich", action="store_true")
        sp.add_argument("--csv", type=str, default=None)
        sp.add_argument("--historical", action="store_true")
        sp.add_argument("-v", "--verbose", action="store_true")

    return p


def _legacy_dispatch(args: argparse.Namespace, argv: list[str]) -> int:
    """Keep old subcommands working."""
    console = _console(getattr(args, "no_rich", False))
    cfg = load_enhanced_config(args.config, mode=getattr(args, "filter_mode", None))
    if args.legacy_cmd == "monte-carlo":
        n_runs = args.n_runs or (10 if args.fast else cfg.monte_carlo_runs)
        n_m = args.n_markets or (2000 if args.fast else 4000)
        summary, _ = run_monte_carlo(
            config=cfg,
            n_runs=n_runs,
            n_markets=n_m,
            base_seed=args.seed,
            show_progress=not args.no_rich,
        )
        console.print(Panel(summary.plain_english, title="Monte Carlo"))
        verdict = format_verdict(
            win_rate=summary.mean_wr,
            n_trades=int(sum(summary.n_trades) / max(1, len(summary.n_trades))),
            max_dd=float(sum(summary.max_dds) / max(1, len(summary.max_dds))),
            target_met=summary.consistent and summary.mean_wr >= 0.80,
            mc_p5=summary.p5_wr,
        )
        print_verdict_banner(console, verdict, ok=summary.consistent)
        path = save_run_bundle(
            tag="monte_carlo",
            metrics=summary.to_dict(),
            summary_text=summary.plain_english,
            command=_cmd_string(argv),
            parameters={"n_runs": n_runs, "n_markets": n_m, "seed": args.seed},
            verdict=verdict,
        )
        if args.plots:
            save_monte_carlo_hist(summary.win_rates, path / "wr_hist.png")
        console.print(f"[green]Artifacts → {path}[/]")
        return 0 if summary.consistent else 1

    if args.legacy_cmd == "tune":
        args.optimize = True
        args.fast = bool(args.fast)
        args.mode = "synthetic"
        args.plots = True
        args.plots_explicit = False
        args.no_plots = False
        args.compare_baseline = False
        args.csv = None
        return run_pipeline(args, argv)

    if args.legacy_cmd == "compare":
        args.compare_baseline = True
        args.mode = "historical" if getattr(args, "historical", False) else "synthetic"
        args.optimize = False
        args.plots = True
        args.plots_explicit = False
        args.no_plots = False
        return run_pipeline(args, argv)

    # run
    args.mode = "historical" if getattr(args, "historical", False) else "synthetic"
    args.optimize = False
    args.compare_baseline = False
    args.plots_explicit = True
    args.no_plots = not args.plots
    return run_pipeline(args, argv)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quieter libraries
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Track whether user explicitly passed --plots
    args.plots_explicit = "--plots" in argv
    if args.no_plots:
        args.plots = False

    if args.legacy_cmd:
        return _legacy_dispatch(args, argv)

    # Default one-command path
    return run_pipeline(args, argv)


if __name__ == "__main__":
    raise SystemExit(main())
