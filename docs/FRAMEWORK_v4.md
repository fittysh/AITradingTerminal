# Trading Framework — v4, sweep-reversal · 2026-09-06 (superseded by v5 · 2026-11-09, see ../FRAMEWORK.md)

Owner: Fittysh (Malaysia, UTC+8 / MYT). Market: Bitget USDT-M Futures — crypto only (TradFi/RWA
excluded). 24/7. Account: $600. This document is authoritative — the code implements exactly this.

> Signal-edge note (2026-09-06): v4 replaces the v3 trend+FVG continuation engine with an ICT
> liquidity-sweep reversal engine (operator-directed). The PDH/PDL daily-failure screen was
> backtested with NO standalone edge (BACKTEST_v4.md), so it runs as research/watchlist only and
> does not gate entries. The sweep-reversal engine itself is not yet validated on deep data —
> everything is DEMO and nothing is enabled for live orders until a signal proves positive.

## The idea in one line
Find the draw on liquidity, wait for a stop raid of the opposite pool, confirm a structure shift,
then enter in premium/discount at an FVG or Order Block — stop beyond the raid, target the draw on
liquidity at 1:2+. Protect the account first.

## Core Responsibilities (run_bot.py)
Runs 24/7, market day from 07:00 MYT.
1. Scan + both research routines every 4 hours → Telegram + Gmail:
   - Routine 1 — top 20 movers + 20 losers, 24h volume ≥ $1M, TradFi excluded (Last / 24h /
     Volume / Funding / PDH / PDL respected-or-broken).
   - Module A liquidity-sweep screen (sweep_screen.py) over the whole universe → sweeps.json +
     a promoted-candidate table. Degraded: no historical OI/CVD on Bitget public data, so those
     acceptance clauses are unavailable and the mandatory OI gate is off (volume+displacement+time_decay).
   - Routine 2 — PDH/PDL daily-failure screen (daily_failure) over the whole universe: coins whose
     previous day FAILED to close beyond the day-before's high/low. Research watchlist only — does not
     gate entries (no backtested edge).
2. Framework-signal scan — ALL coins, every 4 hours (run_bot.py signals): runs the v4 engine over
   the whole universe → coins meeting the framework + in-zone watch → Telegram + Gmail, and writes the
   signal coins to signals_live.json.
3. Trade every 30 minutes — enters ONLY coins in signals_live.json, re-validated live. Entries
   therefore always match the framework-signal scanner (fixed 2026-09-12 journal-vs-scanner mismatch).
4. Manage ~every 5 minutes — R-ladder + 48h time-stop on open positions.
5. Daily report 07:00 & 19:00 MYT — trade journal / net-of-fees breakdown + PDF → Telegram + Gmail.
6. Journal every day, even with zero trades.

## Rules (paper_bot.py) — the protection shell
- Risk $2.5 per position (= 1R), market orders allowed.
- Max 5 open positions ($12.50 max exposure). Correlation cap: 3 per side.
- Daily LOSS stop — ARMED: halt NEW entries after −$15 realized OR 6 losing trades on the day.
- PROTECT mode: once +$15 realized (daily target), halt NEW entries for the rest of the day;
  open positions keep managing. Daily gates reset at MYT midnight.
- Exits (R-ladder): hard stop −1R; at +1R take 50% off + stop→breakeven + arm trailing; the
  runner trails 1% behind the peak and locks +1R once +2R prints, targeting the DOL (≥1:2).
  48h time-stop. All on the exchange (executes with the machine off).

## Timeframe tiers (focus 1H/4H · pairings 4H→15M, 1H→5M)
Each tier reads structure + stop-raid on its TF and executes on the entry TF. evaluate() tries
T1 → T2; first signal wins. Trigger = stop raid OR CISD (Change in State of Delivery).

| Tier | Structure + Stop Raid (+ Daily bias) | Entry |
|------|--------------------------------------|-------|
| T1   | 4H                                   | 15m   |
| T2   | 1H                                   | 5m    |

> S15 unavailable: Bitget public candles serve down to 1m only (15s/1s → HTTP 400), so the
> table's S15 entry is floored at M1.

## The entry sequence (strategy.py::evaluate → _reversal)

## 1. Market Structure + Daily Bias (the draw on liquidity)
market_structure() on the tier's structure TF reads Intermediate-Term swings (ITH/ITL): higher ITH+ITL
→ up, lower → down, else range. Combined with daily bias = PDH/PDL (daily_bias). A bullish
context (daily not-bearish and structure not-down) looks for longs; a bearish context for
shorts. The opposing liquidity pool is the Draw on Liquidity (DOL) = the take-profit target.

## 2. Stop Raid (liquidity sweep) OR CISD
On the stop-raid TF, the most recent swing low (long) / swing high (short) that price pierced (took
resting liquidity) then reclaimed (closed back through) — within ~30 bars. Raid extreme = stop ref.

OR — Change in State of Delivery (CISD, _cisd): if no stop raid, accept a CISD instead — the last
delivery leg's origin (top of the down-close bodies for a long / bottom of up-close bodies for a short)
gets closed through in the opposite direction. Swing low/high = stop ref. Either trigger then requires
the structure shift + premium/discount + POI + entry-candle steps below.

## 3. Structure Shift (MSS / CHoCH)
After the raid, price must break the last opposing pivot that formed before the raid — a bullish
shift (close above the prior lower-high) for longs, bearish (close below the prior higher-low) for
shorts. This is the "Failure to Manipulate": the raid was the attempted manipulation; the reclaim +
structure shift are its failure → trade the reversal.

## 4. Premium / Discount
The dealing range runs from the raid extreme to the post-shift extreme; equilibrium is its midpoint.
Enter only in the correct half — discount (below equilibrium) for longs, premium (above) for shorts.

## 5. Point of Interest — FVG / Order Block / Unicorn
Within the correct half, the entry POI is a fresh Fair Value Gap (preferred; a Unicorn if a
breaker block overlaps it, breaker_overlap) or, failing that, an Order Block (order_block — the
last opposite-close candle before the displacement leg). Setups are tagged sweep-* / OB-* /
unicorn-* so their win rates can be compared.

## 6. Entry trigger + risk
Price must be inside the POI with a solid-body confirmation candle on the entry TF (body ≥ 50%
of range — a wick/shadow is a likely fake and is rejected). Then:
- Stop beyond the raid extreme (floored ≥0.8%, and the whole setup is rejected if the stop is >12%
  away — spike/poor-RR guard).
- Target = the Draw on Liquidity (nearest opposing pool), ≥ 1:2 floor.

## Code map
strategy.py::evaluate() — the v4 sweep-reversal engine; _reversal() the per-tier sequence;
order_block, _swing_seq, market_structure, find_fvgs, breaker_overlap, daily_bias,
daily_failure, liquidity_sweep the primitives. TIERS holds the 3 tiers.
paper_bot.py — protection shell: RISK_PER_TRADE=2.5, MAX_OPEN_POSITIONS=5, CORR_MAX_PER_SIDE=3,
DAILY_MAX_LOSS=15 + MAX_LOSSES_PER_DAY=6 (ARMED), DAILY_TARGET=15 (PROTECT),
MAX_TRADE_AGE_HOURS=48; R-ladder in mark().
sweep_screen.py — Module A liquidity-sweep screener (see MODULE_A_liquidity_sweep.md).
run_bot.py — scan (routine 1 + Module A + routine 2) / trade / manage / report / signals;
TradFi exclusion, market entry + exchange SL/TP + R-ladder + 48h time-stop + PROTECT + correlation cap.
bitget_report.py — authoritative net-of-fees daily report.

## Output
Journal journal/YYYY-MM-DD.md, dashboard dashboard/index.html, Telegram + Gmail (scan + research
scan + signals every 4h; daily report + PDF at 07:00 & 19:00 MYT).
