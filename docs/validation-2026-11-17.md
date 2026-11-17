# Framework v5 validation on real Bitget history — 2026-11-17

Command: `python run_bot.py validate --source bitget --days 30 --top 30 --quiet`
Window: **2026-10-18 → 2026-11-17** (30 days), 30 coins (top by volume, crypto only), Bitget USDT-M public
history, replayed with no lookahead. Raw reports: `validate/reports/validate_20261018_20261117.md` / `.json`
(not committed).

## Verdict in plain words

1. **Does the bot follow the framework? Yes.** All 197 entries were re-checked from the raw candles against
   every step and the risk rules. None failed.
2. **Does the framework pass the proof rule on real data? No.** It made enough trades (195), but it lost
   money: net −$49.78, profit factor 0.81. The rule needs a net result above zero and a profit factor of
   at least 1.2. It was losing before fees too (gross −$20.47).

## 1 · Rule audit (197 entries)

| Rule | Passed | Failed |
|---|---|---|
| listed by the signal scan | 197 | 0 |
| 1 context | 197 | 0 |
| 2 trigger | 197 | 0 |
| 3 structure shift | 197 | 0 |
| 4 discount / premium | 197 | 0 |
| 5 C2 / C3 closure | 197 | 0 |
| 6 zone | 197 | 0 |
| 7 entry candle | 197 | 0 |
| stop + target | 197 | 0 |
| risk per trade | 197 | 0 |
| protection shell | 197 | 0 |

There are 197 entries but 195 closed trades because 2 positions were still open when the window ended.

## 2 · Proof rule

| Check | Result | Value |
|---|---|---|
| at least 30 closed trades | pass | 195 |
| net result above zero (after fees and funding) | **fail** | −$49.78 |
| profit factor at least 1.2 | **fail** | 0.81 |

Totals:

| Measure | Value |
|---|---|
| Win rate | 48.2% (94 won, 101 lost) |
| Expectancy | −0.10R per trade |
| Average win | +0.91R |
| Average loss | −1.04R |
| Best trade | +1.68R |
| Worst trade | −1.17R |
| Gross P&L | −$20.47 |
| Fees | $29.31 |
| Funding | $0 (not simulated) |
| Net P&L | −$49.78 |
| Max drawdown | −$84.45 |
| Average hold | 10.2 h |

The average win is under 1R. Half the position comes off at +1R and the trail then stops out most
runners early, so the 2R target is rarely reached. At a 48% win rate, that payoff cannot cover the
losses.

## 3 · Results by group

| Group | Value | Trades | Win rate | Expectancy | Net |
|---|---|---|---|---|---|
| closure | C2 | 141 | 48.9% | −0.07R | −$26.08 |
| closure | C3 | 54 | 46.3% | −0.18R | −$23.70 |
| entry type | series close | 165 | 46.1% | −0.14R | −$58.76 |
| entry type | FVG retest | 30 | 60.0% | +0.12R | +$8.98 |
| tier | T1 (4H/15m) | 57 | 45.6% | −0.17R | −$24.26 |
| tier | T2 (1H/5m) | 138 | 49.3% | −0.07R | −$25.52 |
| family | OB | 164 | 48.8% | −0.09R | −$37.88 |
| family | sweep (FVG) | 20 | 45.0% | −0.14R | −$7.23 |
| family | unicorn | 11 | 45.5% | −0.17R | −$4.67 |
| trigger | CISD | 108 | 49.1% | −0.11R | −$30.61 |
| trigger | raid | 87 | 47.1% | −0.09R | −$19.17 |
| side | long | 94 | 47.9% | −0.11R | −$26.49 |
| side | short | 101 | 48.5% | −0.09R | −$23.29 |

Only one group made money: the FVG retest entries (30 trades, +0.12R, +$8.98). That is too few trades to
show an edge on their own. Every other group lost. C3 closures did worse than C2 closures, and T1 did
worse than T2.

## 4 · Data coverage

- **5m, 15m and 1H are complete** for all 30 coins over the full window plus warm-up.
- **1D shows 41 of 42 candles for every coin.** The missing candle is today's daily bar, which has not
  closed yet. This is not missing data.
- **AKEUSDT 4H has 455 of 492 candles.** Its history starts 2026-09-02 20:00, which is short of the 52-day
  4H warm-up, but it does cover the replay window itself.

**History-fetch fix (committed with this report).** The first run's cache showed one missing 5m bar
every 201 bars, about 0.5% of every history. Bitget's `history-candles` endpoint only returns bars that
open before the bar containing `endTime`. `BitgetPublic.history` paged backwards with
`endTime = first − 1`, which skipped the bar just before each page. The fix pages with
`endTime = first`. A test with a fake Bitget that follows these rules now fails on the old code and passes
on the fix (`tests/test_infra.py::test_history_pages_without_gaps`). The full suite passes (91 tests). All
results above come from a clean re-run with the fix.

The replay's coverage check only flags a history that is missing more than 2% of its bars, so it never
reported this 0.5% loss. It is worth tightening, but it was not changed here.

## 5 · Caveats

- The Bitget replay simulates no funding payments (funding = $0).
- Costs are 0.06% taker fee plus 0.02% slippage per side.
- One 30-day window on 30 coins is a small sample, taken in one market regime: most coins fell in this
  window (BTC went from about 81.7k to 64.2k). A different window could give a different result. A
  failing profit factor of 0.81 over 195 trades is still a clear "not proven".
- The replay checks entries every 30 minutes and manages trades every 5 minutes on 5m bars, as the
  schedule does. Fills inside a 5m bar are approximated.

## 6 · Comparison with the TTrades articles

Sources:
- [Understanding Candle 2 Closures Within the Fractal Model](https://ttrades.com/understanding-candle-2-closures-within-the-fractal-model/) (2025-11-15)
- [Candle 3 Closure: a complete guide](https://ttrades.com/candle-3-closure-a-complete-guide-to-identifying-continuations-and-reversals/)

**Where they agree**

- **C2 closure (step 5).** Article: price sweeps the previous candle's low and closes back inside the
  previous candle's range, which for a bullish setup means back above that low. This matches the
  framework, and the framework's note that "back inside" means "a close beyond C1's low" is correct.
- **Timeframe pairs.** The articles give daily → 1H, 1H → 5m and 4H → 15m. The framework uses
  4H/15m (T1) and 1H/5m (T2). It does not use daily → 1H.

**Where they differ** (no rules were changed; the owner decides)

1. **The C3 closure uses a different reference level.**
   - Article: C3 closes over the body of C2 and engulfs it, which for a long means a close above C2's
     body top. The article also says C3 must not sweep C2's high or low.
   - Framework: C3 closes back above C1's low.
   - C2's body top is usually above C1's low, so the framework accepts C3 closures the article would
     reject. (C3 not sweeping C2's low holds automatically in the framework, because C2 is defined as
     the candle that printed the extreme.)
2. **The C3 closure precondition is broader in the article.** Article: C3 applies whenever C2 "fails to
   give the required closure", including a C2 that never swept C1 at all. The framework only knows the
   case where C2 swept C1's low and closed below it.
3. **The articles require a point of interest.** A C2 or C3 closure only counts at a higher-timeframe point
   of interest: a swing high or low, a fair-value gap, or failing those an opposing candle. "If either is
   missing, the swing point is ignored."
   - The framework's raid trigger roughly covers this, since it sweeps a swing.
   - The CISD trigger (used when there is no raid, 108 of the 195 trades) does not require a point of
     interest where C2 forms.
   - The step 6 zone is the entry zone, not the point of interest where C2 forms.
4. **The articles use equilibrium filters that the framework does not have.**
   - C2: if C2 has a large wick, C3 should respect the wick's midpoint, or the swing is less likely to hold.
   - C3: mark C3's full range and look for C4 to wick into the upper half (lower half for a short). Entries
     are refined within that half.
   - Instead, the framework uses the premium/discount of the dealing range (step 4) and the FVG or
     order-block zone (step 6).
5. **The articles tie the trade to a specific next candle.** A C2 closure anticipates C3 as the
   continuation, and a C3 closure anticipates C4 as the expansion. A C3 closing beyond C2's high "usually
   leads to a Candle 4 expansion". The framework does not tie the entry to that candle. It only requires
   the series to end within the last 20 entry-timeframe bars, plus the 30-minute freshness window.
6. **The series and series close (step 7) are not defined in either article.** The articles only say to
   drop to the aligned lower timeframe, "confirm the change in the state of delivery" and wait for
   displacement.
   - The framework's series level (the highest open of the run of down-close candles, entry on a ≥50%
     body closing above it) works like a change in state of delivery, but it comes from the annotated
     chart, not from these articles.
   - The ≥50% body rule, the FVG retest as a second entry, and the 20-bar limit are not in the articles.
7. **The articles' validity checklist has four parts:** a sweep, a close back inside, a higher-timeframe
   point of interest, and structure supporting continuation into C3. The framework covers the first two
   in step 5, and a structure shift in step 3. As in point 3, it has no explicit point-of-interest check
   for C2.

The framework's interpretation note that says the two articles could not be opened is now out of date. It
was left unchanged because FRAMEWORK.md is the owner's document.
