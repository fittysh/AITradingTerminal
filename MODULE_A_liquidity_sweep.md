# Module A — liquidity-sweep screen (as implemented in `sweep_screen.py`)

> The framework references this document but its original text was not part of the hand-over, so this
> file specifies exactly what the code does, reconstructed from the framework's summary: *"Module A
> liquidity-sweep screen over the whole universe → sweeps.json + a promoted-candidate table. Degraded:
> no historical OI/CVD on Bitget public data, so those acceptance clauses are unavailable and the
> mandatory OI gate is off (volume + displacement + time_decay)."* Owners with the original spec should
> reconcile the thresholds below with it.

## Purpose
A research screen that runs with every 4-hour scan and lists the freshest stop raids across the whole
crypto universe, scored for how convincingly the resting liquidity was taken. It **does not gate
entries**: the trade loop only ever enters coins listed by the framework-signal scan
(`signals_live.json`).

## Detection
For every coin (TradFi/RWA excluded) and both structure timeframes (**4H** and **1H**), for each
direction:

- `strategy.liquidity_sweep(candles, direction, lookback=24)`: the most recent **pool**, meaning a
  5-bar fractal swing low (sell-side, bullish reversal) or high (buy-side, bearish reversal), that was
  first **pierced** within the last 24 closed bars and **reclaimed** by a close back through the level
  within 3 bars, with the raid extreme not taken out since.

## Acceptance clauses

| Clause | Rule | Status |
|---|---|---|
| `oi` | open interest flushed through the sweep | **unavailable**: Bitget public data has no OI history |
| `cvd` | cumulative volume delta diverged at the extreme | **unavailable**: no historical trade tape |
| `volume` | max volume of the pierce → reclaim bars ≥ **1.5×** the 20 bars before the pierce | active |
| `displacement` | largest reversal-direction body from the pierce to 3 bars after the reclaim ≥ **1.5×** the average body of the 20 bars before | active |
| `time_decay` | freshness weight `0.5 ^ (age / 6 bars)` ≥ **0.25**, i.e. at most 12 bars old | active |

**Degraded mode (current):** the mandatory OI gate is off. A sweep is **promoted** when
`volume ∧ displacement ∧ time_decay` all pass.

**Score** (0–100, for ranking only): `100 · decay · (½·min(vol×, 3)/3 + ½·min(disp×, 3)/3)`.

## Outputs
- `state/sweeps.json`: `{generated_at, mode: "degraded", clauses, unavailable: ["oi","cvd"], gate,
  sweeps: [...], promoted: [...], errors: [...]}`. Each sweep carries symbol, tf, direction, pool
  (sell-side / buy-side), level, extreme, pierce/reclaim timestamps, age in bars, vol×, disp×, decay,
  the clause results, score and promoted flag.
- The promoted-candidate table goes into the 4-hour **scan** message (Telegram + Gmail) and the
  dashboard's **Market** tab.

## Upgrading out of degraded mode
When an OI/CVD history source is available, fill `clauses["oi"]` / `clauses["cvd"]` in
`sweep_screen.score_sweep()`, add `oi` to the promotion rule (it is the mandatory gate), and set
`MODE = "full"`.
