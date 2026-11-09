# Trading Framework v5: template to fill in

Fill every `___` and hand it back; I'll rebuild the bot to match it and test it against v4 before it
trades. The **v4** column shows what the bot does today, so you can keep a value by copying it.
Rows marked **NEW** are gaps the review below found: v4 has no rule for them. Examples are in
*italics*; replace them with your own. Anything left blank, I'll ask about rather than guess.

## Review of v4 (trader-skill pack checks)

The pack is built for US stocks, so its checklists were applied to v4's rules by reading the code;
its tools weren't run (its risk tool rejects every crypto ticker by design).

| Check | Skill | v4 today | Verdict |
|---|---|---|---|
| Per-trade risk | budget | Fixed $2.50 (1R), sized from the stop distance | OK, but it doesn't grow or shrink with the account |
| Max size of one position | risk, budget | No cap. A 0.8% stop gives $312.50 notional, 52% of the $600 account | **Gap** |
| Total exposure | risk | No cap. 5 positions can reach $1,562.50 notional, 2.6× the account | **Gap** |
| Leverage and margin | risk | 10× isolated, set only in `config.py`; not in the framework document. Paper mode doesn't track margin | **Gap** |
| Cash / margin buffer | risk, budget | Free margin isn't checked before an entry | **Gap** |
| Min / max order size | risk | Exchange minimum enforced; no maximum | **Gap** (max) |
| Blocked coins | risk, budget | TradFi/RWA excluded; no personal block list | Minor gap |
| Shorts | risk | Allowed by design (futures) | OK, but not written in the framework |
| Duplicates | risk | One position per coin; a setup is never traded twice | OK |
| Daily order limit | risk | None; only the loss stop (−$15 or 6 losers) and PROTECT (+$15) stop entries | **Gap** |
| Rule format | screener | Thresholds are constants in code. Each step shows pass/fail with text, but not a separate value and threshold | Improve: one rule per requirement |
| Missing data | screener | Engine: missing data means no signal. Sweep screen: OI/CVD skipped ("degraded") | Needs a stated rule for OI and funding |
| Ranking | screener | Signals before in-zone, then tier, then reward:risk | OK, but not written in the framework |
| Order approval | broker | Fully automatic every 30 min; no per-order yes. Live needs a confirmation phrase plus a proven edge | **Your decision**: the pack never trades without a yes, while v4 is built to trade unattended |
| Data age | analyst, news | Signal list ignored after 8h; a stale entry candle never confirms | OK |
| Proof before live | — | Never replayed on real Bitget history (network blocked here); synthetic results say nothing about edge | **Open** |

---

## 0. Basics

| Field | v4 | v5 |
|---|---|---|
| Name, version, date | v4 sweep-reversal · 2026-09-06 | ___ |
| Owner, time zone | Fittysh · MYT (UTC+8) | ___ |
| Exchange and product | Bitget USDT-M perpetual futures | ___ |
| Account size | $600 | ___ |
| Start in | paper | ___ *(paper / Bitget demo)* |
| The idea in one line | Find the draw on liquidity, wait for a stop raid of the opposite pool, confirm a structure shift, enter at an FVG or order block | ___ |

## 1. Universe: which coins

| Field | v4 | v5 |
|---|---|---|
| Coins scanned | All crypto perpetuals, TradFi/RWA excluded | ___ |
| Minimum 24h volume to trade | None | ___ |
| Blocked coins **NEW** | None beyond TradFi | ___ |
| Directions | Long and short | ___ |

## 2. Timeframes

| Role | v4 | v5 |
|---|---|---|
| Trend / bias | Daily, plus 4H (T1) or 1H (T2) | ___ |
| Setup | 4H (T1) / 1H (T2) | ___ |
| Entry trigger | 15m (T1) / 5m (T2) | ___ |
| Several tiers? Which first? | T1 then T2, first signal wins | ___ |

## 3. Signal requirements

One block per requirement, in the screener's rule format: what is measured, the operator and value,
what happens when the data is missing, and whether it is a gate or adds to a score.

- **Operators:** `>` `>=` `<` `<=` `between [low, high]` `crosses above` `crosses below`
- **Missing data:** **fail** (no signal) · **skip** (ignore this requirement and say so) · **wait** (check again next run)

### R1 · Candle close

| Field | Your answer |
|---|---|
| Rule in your words | ___ |
| Timeframe | ___ |
| What is measured | ___ *e.g. close vs a level (swing high/low, previous day high/low, EMA), body % of range, where the close sits in the range* |
| Operator and value | ___ *e.g. close > the swing high; body ≥ 60% of the range* |
| Which candle | ___ *e.g. last closed; 2 closes in a row* |
| Long | ___ |
| Short | ___ *(or "mirror")* |
| Gate, or score weight | ___ |
| If data missing | fail / skip / wait |

### R2 · Volume

| Field | Your answer |
|---|---|
| Rule in your words | ___ |
| Timeframe | ___ |
| What is measured | ___ *e.g. signal-candle volume vs the 20-bar average; 24h volume in USDT* |
| Operator and value | ___ *e.g. ≥ 1.5× the 20-bar average* |
| Long | ___ |
| Short | ___ *(or "mirror")* |
| Gate, or score weight | ___ |
| If data missing | fail / skip / wait |

### R3 · Open interest

Bitget publishes only the current OI. Any rule that compares now with earlier needs history the bot
records itself (a snapshot every few minutes), so the clock starts when recording starts.

| Field | Your answer |
|---|---|
| Rule in your words | ___ |
| Timeframe / look-back | ___ |
| What is measured | ___ *e.g. % change in OI over the last N bars; OI change vs price change* |
| Operator and value | ___ *e.g. OI up ≥ 3% over 4 bars while price rises (new longs); OI down ≥ 2% at a sweep (stops flushed)* |
| Long | ___ |
| Short | ___ *(or "mirror")* |
| Gate, or score weight | ___ |
| If data missing | fail / skip / wait |
| History to record before trading on it | ___ days |

### R4 · Funding rate

| Field | Your answer |
|---|---|
| Rule in your words | ___ |
| What is measured | ___ *e.g. current rate per 8h; average of the last 3 settlements* |
| Operator and value | ___ *e.g. no longs when funding ≥ +0.05% / 8h; no shorts when ≤ −0.05%* |
| Long | ___ |
| Short | ___ *(or "mirror")* |
| Gate, or score weight | ___ |
| If data missing | fail / skip / wait |
| Avoid entries close to a settlement (00:00 / 08:00 / 16:00 UTC)? | ___ *minutes before / after, or no* |

### R5 · (copy a block for any other requirement)

### How the requirements combine

| Field | v4 | v5 |
|---|---|---|
| Decision | All 6 steps must pass | ___ *e.g. every gate passes AND score ≥ 3 of 5* |
| More signals than free slots: rank by | Signal before in-zone, then tier, then reward:risk | ___ |
| A signal stays valid for | Until the next scan (8h at most) | ___ |
| Re-check on fresh candles right before entry? | Yes | ___ |

## 4. Entry, stop and target

| Field | v4 | v5 |
|---|---|---|
| Order type | Market | ___ *(market / limit at ___)* |
| Stop placement | 0.1% beyond the raid extreme | ___ |
| Minimum stop distance | 0.8% | ___ |
| Maximum stop distance (skip the trade) | 12% | ___ |
| Target | Nearest opposing liquidity, never closer than 2R | ___ |
| Minimum reward:risk | 2.0 | ___ |

## 5. Exits and trade management

| Field | v4 | v5 |
|---|---|---|
| Hard stop | −1R | ___ |
| Partial take-profit | 50% at +1R | ___ |
| Stop to breakeven | At +1R | ___ |
| Trailing stop | 1% behind the peak, from +1R | ___ |
| Profit lock | +1R once +2R prints | ___ |
| Final target | The liquidity target | ___ |
| Time stop | 48h | ___ |
| Exit early when a requirement turns against the trade? **NEW** | No | ___ *e.g. close if funding passes ___ or OI drops ___%* |

## 6. Risk limits

| Limit | v4 | v5 |
|---|---|---|
| Risk per trade | $2.50 fixed (1R) | ___ *($ fixed, or % of equity)* |
| Max open positions | 5 | ___ |
| Max per side | 3 longs / 3 shorts | ___ |
| Max notional per trade **NEW** | None ($312.50 possible) | ___ |
| Max total notional **NEW** | None ($1,562.50 possible) | ___ |
| Leverage and margin mode **NEW** | 10× isolated, in code only | ___ |
| Free margin to keep **NEW** | Not checked | ___ |
| Daily loss stop | −$15 or 6 losing trades | ___ |
| Daily profit lock (PROTECT) | +$15 | ___ |
| Max new entries per day **NEW** | No limit | ___ |
| Cooldown after a loss on the same coin **NEW** | None (a setup is never traded twice) | ___ |
| Daily reset | 00:00 MYT | ___ |
| Costs to assume | 0.06% taker fee, 0.02% slippage | ___ |

## 7. Execution and approval

| Field | v4 | v5 |
|---|---|---|
| Who places orders | The bot, automatically | ___ |
| Your approval per order **NEW** | Not asked | ___ *e.g. none · Telegram reply within ___ min · only above $___ notional* |
| Orders held on the exchange (demo/live) | Stop, target, 50% at +1R, trailing stop | ___ |
| Before live trading | Confirmation phrase plus ≥30 trades, net > 0, profit factor ≥ 1.2 | ___ |

## 8. Market analyst: the scans

| Field | v4 | v5 |
|---|---|---|
| Movers / losers | Top 20 / 20, 24h volume ≥ $1M, held or broke previous day high/low | ___ *(keep / change / drop)* |
| Sweep screen | Volume + displacement + freshness; OI off | ___ |
| Daily-failure screen | Research only | ___ |
| New screens **NEW** | — | ___ *e.g. biggest OI changes, funding extremes, volume spikes, OI rising against price* |
| Columns each report shows | Last, 24h, volume, funding, previous day high/low | ___ |

## 9. Schedule (MYT)

| Job | v4 | v5 |
|---|---|---|
| Scan + signals | Every 4h, 2 min after each 4H close | ___ |
| Check for entries | Every 30 min | ___ |
| Manage open trades | Every 5 min | ___ |
| Reports | 07:00 and 19:00 | ___ |
| Journal / market day starts | 07:00 | ___ |
| OI and funding snapshots **NEW** | Not recorded | ___ |

## 10. Data rules

| Field | v4 | v5 |
|---|---|---|
| Candles used | Closed only | ___ |
| Data too old | Signal list ignored after 8h; stale entry candle never confirms | ___ |
| A data source fails | That run fails, an error alert goes out, the next run retries | ___ |

## 11. Reports and journal

| Field | v4 | v5 |
|---|---|---|
| Telegram / Gmail | Scans and signals (both); entries and exits (Telegram); reports + PDF (both) | ___ |
| Win-rate breakdowns | By setup family, tier, trigger, side | ___ *e.g. add one per requirement (R1–R5), by funding and OI bucket* |

## 12. Proof before trusting it

| Field | v4 | v5 |
|---|---|---|
| Replay on real Bitget history | Not done yet | ___ days |
| Minimum closed trades | 30 | ___ |
| Profit factor / expectancy | ≥ 1.2 / above zero | ___ |
| Maximum drawdown allowed | Not set | ___ |
| Compare against v4 in the same replay? | — | ___ *(yes / no)* |
