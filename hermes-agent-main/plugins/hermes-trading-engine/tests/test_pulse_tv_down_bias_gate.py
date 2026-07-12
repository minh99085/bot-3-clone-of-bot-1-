"""TradingView DOWN-bias gate (Townhall P3)."""

from __future__ import annotations

from engine.pulse.tv_down_bias_gate import TradingViewDownBiasGate

_STRONG_UP_EDGE = {"edge_score_bucket": "high", "cex_agreement_bucket": "strong"}


def test_blocks_bullish_aligned_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="bullish_aligned", tv_direction="UP")
    assert r["decision"] == "block"
    assert "tv_down_bias_bullish_aligned_up" in r["reasons"]


def test_blocks_up_without_bearish():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="mixed", tv_direction="UP")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_without_bearish" in r["reasons"]


def test_blocks_up_on_bearish_down_stack():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="DOWN")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_on_bearish_down_stack" in r["reasons"]


def test_blocks_up_tv_down_non_bearish():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False)
    r = g.evaluate(side="up", mtf_alignment="mixed", tv_direction="DOWN")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_tv_down_non_bearish" in r["reasons"]


def test_allows_up_tv_down_bearish_when_stack_rule_off():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False,
                                block_up_without_bearish=False)
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned",
                      tv_direction="DOWN", **_STRONG_UP_EDGE)["decision"] == "pass"


def test_allows_down_and_bearish_up_when_stack_rule_off():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_on_bearish_down_stack=False)
    assert g.evaluate(side="down", mtf_alignment="bearish_aligned")["decision"] == "pass"
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="DOWN",
                      **_STRONG_UP_EDGE)["decision"] == "pass"


def test_blocks_up_against_confirmed_down():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0)
    r = g.evaluate(side="up", tf_confirm="confirmed_down", tv_direction="DOWN",
                   mtf_alignment="bearish_aligned")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_against_confirmed_down" in r["reasons"]


def test_allows_up_when_confirmed_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False)
    assert g.evaluate(side="up", tf_confirm="confirmed_up",
                      **_STRONG_UP_EDGE)["decision"] == "pass"


def test_disabled_passes():
    g = TradingViewDownBiasGate(enabled=False)
    assert g.evaluate(side="up", mtf_alignment="bullish_aligned")["decision"] == "pass"


def test_blocks_mixed_mtf_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_up_tv_down_non_bearish=False)
    r = g.evaluate(side="up", mtf_alignment="mixed", tv_direction="DOWN")
    assert r["decision"] == "block"
    assert "tv_down_bias_mixed_mtf_up" in r["reasons"]


def test_blocks_bullish_supertrend_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", supertrend_direction="bullish")
    assert r["decision"] == "block"
    assert "tv_down_bias_bullish_supertrend_up" in r["reasons"]


def test_blocks_up_vwap_above():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", vwap_state="above")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_vwap_above" in r["reasons"]


def test_blocks_up_bb_expansion_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", bb_state="expansion_up")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_bb_expansion_up" in r["reasons"]


def test_blocks_up_range_breakout_down():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", range_state="breakout_down")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_range_breakout_down" in r["reasons"]


def test_blocks_up_bb_squeeze():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", bb_state="squeeze")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_bb_squeeze" in r["reasons"]


def test_blocks_up_range_top():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", range_state="range_top")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_range_top" in r["reasons"]


def test_blocks_up_markov_chop_noise():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", markov_state="chop_noise")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_markov_chop_noise" in r["reasons"]


def test_blocks_up_late_ttc():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", ttc_s=260.0)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_late_ttc" in r["reasons"]


def test_blocks_up_early_ttc():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", ttc_s=90.0)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_early_ttc" in r["reasons"]


def test_blocks_up_htf_bullish():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", htf_bias="bullish")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_htf_bullish" in r["reasons"]


def test_blocks_up_bear_close_near_low():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", candle_pressure="bear_close_near_low")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_bear_close_near_low" in r["reasons"]


def test_blocks_up_medium_edge():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", edge_score_bucket="medium")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_medium_edge" in r["reasons"]


def test_blocks_up_weak_cex():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", cex_agreement_bucket="na")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_weak_cex" in r["reasons"]


def test_allows_up_high_edge_strong_cex():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_up_on_bearish_down_stack=False,
                                block_up_tv_down_non_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False)
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned",
                      tv_direction="DOWN", ttc_s=180.0,
                      edge_score_bucket="high",
                      cex_agreement_bucket="strong")["decision"] == "pass"


def test_blocks_up_ask_heavy_ob():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", ob_pressure_bucket="ask_heavy")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_ask_heavy_ob" in r["reasons"]


def test_blocks_up_tf_confirm_conflict():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", tf_confirm="conflict")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_tf_confirm_conflict" in r["reasons"]


def test_blocks_up_cvd_neutral():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", cvd_state="neutral")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_cvd_neutral" in r["reasons"]


def test_blocks_up_cvd_buy_pressure():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", cvd_state="buy_pressure")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_cvd_buy_pressure" in r["reasons"]


def test_blocks_up_low_conviction():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False,
                                up_min_conviction=0.40)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", conviction=0.15)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_low_conviction" in r["reasons"]


def test_allows_up_mid_ttc_window():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_up_on_bearish_down_stack=False,
                                block_up_tv_down_non_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False,
                                block_up_mid_ttc=False)
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned",
                      tv_direction="DOWN", ttc_s=180.0)["decision"] == "pass"


def test_blocks_up_bearish_mtf_tv_up():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_on_bearish_down_stack=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned", tv_direction="UP",
                   edge_score_bucket="high", cex_agreement_bucket="strong", ttc_s=180.0)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_bearish_mtf_tv_up" in r["reasons"]


def test_blocks_up_mid_ttc():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_early_ttc=False,
                                block_up_late_ttc=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", ttc_s=150.0)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_mid_ttc" in r["reasons"]


def test_blocks_up_neutral_zscore():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", zscore_bucket="-1..1")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_neutral_zscore" in r["reasons"]


def test_blocks_up_volume_active():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", volume_state="active")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_volume_active" in r["reasons"]


def test_blocks_up_not_stale():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_on_bearish_down_stack=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", stale_divergence="not_stale")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_not_stale" in r["reasons"]


def test_blocks_up_medium_confidence():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", confidence_tier="medium")
    assert r["decision"] == "block"
    assert "tv_down_bias_up_medium_confidence" in r["reasons"]


def test_blocks_up_underdog_entry():
    g = TradingViewDownBiasGate(enabled=True, exploration_rate=0.0,
                                block_up_without_bearish=False,
                                block_mixed_mtf_up=False,
                                block_up_on_bearish_down_stack=False,
                                block_up_markov_chop_noise=False,
                                block_up_medium_edge=False,
                                block_up_weak_cex=False)
    r = g.evaluate(side="up", mtf_alignment="bearish_aligned",
                   tv_direction="DOWN", ask_price=0.52)
    assert r["decision"] == "block"
    assert "tv_down_bias_up_underdog_entry" in r["reasons"]
    assert g.evaluate(side="up", mtf_alignment="bearish_aligned",
                      tv_direction="DOWN", ask_price=0.56,
                      **_STRONG_UP_EDGE)["decision"] == "pass"