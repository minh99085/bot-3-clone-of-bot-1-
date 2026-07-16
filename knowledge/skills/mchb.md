# Meta-Contextual Hierarchical Bandit (MCHB)

## Pseudocode
```
family ~ Thompson(Beta_family)
if family disabled or E[family] < 0.35: return SKIP
x = features(vol, ttr, liq, sentiment, hour, |disloc|, hurst)
for arm in {exploit, explore, skip}:
    UCB[arm] = θ̂·x + α √(xᵀ A⁻¹ x)
if max_uncertainty < τ:  # auto-explore only when uncertain
    return EXPLOIT if family_sample ≥ 0.45 else SKIP
else:
    return argmax UCB
on settlement: update Beta_family + LinUCB(arm) with risk-adj reward
```

## Reward
`0.7 * sigmoid(pnl/size) + 0.3 * (1 - brier/0.25)`

## State
`data/paper/<instance>/mchb_state.json`

## Auto Log
