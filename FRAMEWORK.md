# Trading Framework — source of truth (v5, sweep-reversal + candle 2/3 closure · 2026-11-09)

Owner: Fittysh (Malaysia, UTC+8 / MYT). Market: Bitget USDT-M Futures — crypto only (TradFi/RWA
excluded). 24/7. Account: $600. This document is authoritative — the code implements exactly this.
The previous version is kept in [docs/FRAMEWORK_v4.md](docs/FRAMEWORK_v4.md).

> What changed from v4 (2026-11-09, operator-directed):
> - New step 5, **Confirmation**: the reversal must show a candle-2 or candle-3 closure (TTrades
>   fractal model) on the setup timeframe.
> - Step 7, **Entry candle**: besides the ≥50% body, the entry candle must close its body beyond the
>   "series" (the run of candles that delivered price into the zone). An FVG left by that move ranks
>   the signal first (+FVG) and gives a second entry on its retest.
> - New **daily journal** layout (title, P&L note, currency, At a glance, trade log).
> - Everything else (context, trigger, shift, premium/discount, zones, stop, target, risk, exits,
>   schedule, live lock) is unchanged from v4.
>
> Nothing here has been proven on real Bitget history yet. Everything stays DEMO/paper, and live
> orders stay locked until the edge gate below passes.

## The idea in one line
Find where liquidity sits, wait for a stop raid on the opposite side, confirm a structure shift and a
candle 2 or candle 3 closure, enter at a fair-value gap or order block when a candle closes its body
through the series, and target the liquidity at 1:2 or better.

## Timeframes
T1 reads structure on **4H** and enters on **15m**; T2 reads structure on **1H** and enters on **5m**
(the fractal model's pairs). T1 is tried first, and the first signal wins.

| Tier | Setup TF (structure, raid, C2/C3 closure, zone, daily bias) | Entry TF (series + entry candle) |
|------|------|------|
| T1   | 4H   | 15m  |
| T2   | 1H   | 5m   |

## Entry: all seven steps must pass (strategy.py::evaluate → _reversal)

1. **Context** — 4H/1H market structure (ITH/ITL) and the daily bias (previous day's close vs the day
   before's range) allow the direction. The opposing liquidity pool is the draw on liquidity (DOL).
2. **Trigger** — a swing is swept and reclaimed within 30 bars (stop raid), or, when there is no raid,
   a shift in delivery (CISD: the origin of the last delivery leg is closed through the other way).
   The raid / swing extreme is the stop reference.
3. **Structure shift** — a candle closes through the last opposing pivot formed before the raid.
4. **Discount or premium** — longs only below the middle of the dealing range (raid extreme →
   post-shift extreme), shorts only above.
5. **Confirmation: candle 2 or candle 3 closure** (setup TF) — see the definitions below.
6. **Zone** — a fresh fair-value gap (a "unicorn" if a breaker block overlaps it), otherwise the order
   block, inside the correct half. Tags: `sweep-*` (FVG), `unicorn-*`, `OB-*`.
7. **Entry candle** (entry TF):
   - price is in the zone and the entry candle has a body of at least 50% of its range;
   - for the candle 2 / candle 3 closure, the candle closes its body **above the series** for a long
     and **below the series** for a short;
   - combined with a fair-value gap for a better signal and opportunity (see below).

Stop: beyond the raid extreme (0.1% buffer), at least 0.8% away; setups needing more than 12% are
skipped. Target: the opposing liquidity, never closer than 2R.

Setup tag = `family-tier-trigger-closure`, e.g. `sweep-T1-raid-C2`, `unicorn-T2-cisd-C3`.

### Step 5 — candle 2 and candle 3 closure (setup TF)

C2 = the candle that printed the reversal extreme (the raid low for a long), C1 = the candle before it.
For a long (a short mirrors it with highs):

- **Candle 2 closure** — C2 trades below C1's low and closes back above it (back inside C1's range).
- **Candle 3 closure** — C2 closed below C1's low (no C2 closure); the next candle, C3, closes back
  above C1's low.
- **Neither** — C2 and C3 both closed below C1's low: a continuation, not a reversal → no trade.
  If C2 closed below and C3 has not closed yet, the setup waits for C3.

### Step 7 — the series and the entry candle (entry TF)

From the annotated chart: the **series** is the last run of down-close candles (up-close for a short)
that delivered price into the pullback low inside the zone. Its level is the top of its bodies — the
highest open in the run. The entry candle is the first candle after the series that closes its body
above that level, with a body of at least 50% of its range.

Reading the annotated chart left to right: two down-close candles form the series (the horizontal line
is the top of their bodies); the next candle sweeps below the previous candle's low and closes back
inside it (the candle 2 closure); four small candles stay under the line; then a candle closes its body
above the line — that candle is the long entry, and the move leaves a fair-value gap behind it.

- Candles between the series and the entry candle may be anything (a C2 candle, small inside
  candles) as long as none of them has closed above the level yet.
- The pullback must have traded into the zone without any entry-TF close through the zone's far edge.
- No close may fall back below the level after the entry candle; if it does, the close has failed and
  the bot waits for a new series.
- The series must end within the last 20 entry-TF bars.

**Combined with an FVG.** When the move off the pullback low leaves a fair-value gap on the entry TF:
- the signal is marked **+FVG** and ranked ahead of signals without one;
- a **retest** of that FVG is a second entry: after the series close, a candle trades into the FVG and
  closes with a ≥50% body in the trade direction, while every close since the series close stayed
  above the series level.

**Timing.** Entry checks run every 30 minutes while entry candles close every 15m / 5m, so an entry
candle (or an FVG retest) counts while it closed within the last 30 minutes. Older ones are spent.
A newer shallow pullback candle does not cancel a fresh entry candle unless it closes back through
the series level.

## Ranking
More signals than free slots: signals before in-zone, then +FVG, then tier (T1 first), then
reward:risk.

## Risk (paper_bot.py — the protection shell, unchanged)
- $2.50 risk per trade (1R).
- At most 5 positions open, and at most 3 longs and 3 shorts.
- New entries stop after −$15 or 6 losing trades in a day, or once +$15 is banked (PROTECT).
- These daily limits reset at 00:00 MYT.

## Exits (unchanged)
- Stop at −1R.
- At +1R, take half off and move the stop to breakeven.
- The rest trails 1% behind the peak, with +1R locked once +2R prints.
- Any trade still open after 48 hours is closed.

## Schedule (MYT, unchanged)
- Scan and signals every 4 hours (2 minutes after each 4H close).
- Check for entries every 30 minutes (only coins listed by the last signal scan, re-validated live).
- Manage open trades every 5 minutes.
- Reports at 07:00 and 19:00.
- Daily journal for every market day (07:00 → 07:00), even with zero trades — new layout below.

## Daily journal (journal.py)
`journal/YYYY-MM-DD.html` (the designed page, light and dark) and `.md` (terminal UI); the dashboard's
Journal tab shows the same layout.
- Title **Crypto Trading Journal**, a one-line note on how P&L is worked out, the market day and the
  currency.
- **At a glance**: Total P&L ($) and Entries (rows filled in).
- **Trade log**: Date · Ticker · Side · Size · Entry ($) · Exit ($) · P&L ($), newest first. Rows are the
  trades closed in the market day. P&L is net (fees and funding included); Exit is the size-weighted
  average when a trade closed in parts. Dates use the design's month/day/year format.
- Below it: trade notes (setup tag, C2/C3, entry type, +FVG, exit reason, R) and day notes (open
  positions, risk gates, errors).

## Live trading lock (unchanged)
Live trading is locked until at least 30 closed trades show a positive net result with a profit
factor of at least 1.2, and the confirmation phrase is set.

## Open items (from the v4 review — not decided yet, so the bot keeps v4's behaviour)
- No cap on position size: one trade can reach $312.50, and five can total $1,562.50.
- The 10× leverage is set only in the code's config, not in the framework.
- Free margin isn't checked before an entry.
- No limit on new entries per day.
- No rule for whether you approve each order.

`FRAMEWORK_v5_TEMPLATE.md` still has the blanks for these and for the volume / open interest / funding
requirements.

## Interpretation notes
- The two TTrades articles (candle 2 closures, candle 3 closure) could not be opened from the build
  environment (ttrades.com is not in its network allowlist). Steps 5 and 7 follow the fractal model as
  commonly taught plus the annotated chart. Where a choice was needed:
  - "back inside C1's range" = a close beyond C1's low (high for shorts); closing beyond C1's other
    extreme counts too;
  - a series can be a single candle;
  - the FVG retest as a second entry is the reading of "better signal *and opportunity*".
- Everything else follows the v4 notes in `strategy.py`'s module docstring.

## Code map
strategy.py — `candle_closure` (step 5), `entry_trigger` / `_series_state` (step 7), `evaluate` /
`_reversal` (the sequence), plus the v4 primitives. paper_bot.py — protection shell and R-ladder.
run_bot.py — scan / signals / trade / manage / report; `_rank` orders the signals. journal.py — the
daily journal. dashboard.py + dashboard/template.html, tui.py — the interfaces.
bitget_report.py — the net-of-fees report.
