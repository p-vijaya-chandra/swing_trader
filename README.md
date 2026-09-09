# swingtrader

A systematic swing-trading system for Indian equities — Nifty 100 cash delivery,
Groww, ₹1,00,000 starting capital, with a walk-forward learning loop that
improves it on evidence rather than on hope.

**Read [docs/EXPECTATIONS.md](docs/EXPECTATIONS.md) first.** You asked for 8-10%
a month. That is 152-214% a year, it would turn ₹1 lakh into ₹102 crore in a
decade, and it is roughly four times the best sustained track record in
financial history. The system is built properly and it is yours to run — but you
should know what it can actually return before you trade it. That document is
the arithmetic, not an opinion.

---

## What it does

Every evening after the close it produces an order sheet: which positions to
exit, which trailing stops to raise, and which new positions to open with exact
quantities, limit prices and stop levels. You place those orders in Groww. It
records what filled, and it learns from what happened.

```
swing plan
```

```markdown
## 1. Exits - place at market on open
| Symbol | Qty | Approx value | Why |
| LTIM   |   9 | Rs 15,924.15 | time_stop (held 48 sessions, -0.08R) |

## 2. Stop updates - modify the existing GTT
| Symbol     | Qty | New trigger  | Change |
| ICICIPRULI |  12 | Rs 1,419.80  | raise stop 1,274.42 -> 1,419.80 (locks +0.31R) |

## 3. New entries
| Symbol  | Qty | Buy limit   | Stop (GTT)  | Value       | Risk        | Why |
| SIEMENS |  12 | Rs 1,649.84 | Rs 1,506.56 | Rs 19,221   | Rs 1,142    | pullback (rank 7) |
```

## The strategy in one paragraph

A market-regime filter decides how much to be invested at all — index versus its
200 and 50 day averages, plus breadth, because an index carried by five mega-caps
over a rotting market is a trap the index average alone will not see. When it
says risk-off, exposure goes to zero. Within a risk-on market, a screen vetoes
anything illiquid, not-trending, too volatile or far from its highs; survivors
are ranked by annualised regression slope × R² (a steady climb scores well, a
single violent gap does not); and a name is only bought when it actually
triggers today — a 20-day breakout on volume, or a pullback to the 20 EMA that
reclaims the prior day's high. Size is set by risk, not by capital: 1.2% of
equity divided by the distance to the stop. Positions are held for weeks and
exited by a chandelier trailing stop that only ever ratchets up, so a winner
runs as long as momentum lasts.

Full reasoning, including what is deliberately absent: **[docs/SYSTEM.md](docs/SYSTEM.md)**

## Quick start

```bash
pip install -r requirements.txt      # only needed to DOWNLOAD data
export PYTHONPATH=src

# see it run end to end on generated data, no download required
swing backtest --synthetic --report runs/demo.html

# then the real thing
swing doctor
swing fetch --start 2014-01-01
swing backtest --report runs/baseline.html
swing walkforward
```

## Commands

| | |
|---|---|
| `swing fetch` | Download/refresh price history into the local cache |
| `swing doctor` | Check config and data for problems before they cost money |
| `swing costs` | What frictions actually cost at your position size |
| `swing backtest` | One backtest plus a self-contained HTML report |
| `swing walkforward` | Evaluate the current config out of sample, fold by fold |
| `swing learn` | Search parameters per fold, gate the result, maybe promote |
| `swing scan` | Rank the universe as of the latest session |
| `swing plan` | Write tomorrow's order sheet |
| `swing position` | Record fills, closes and stop changes |
| `swing journal` | Attribution and edge-decay report over your real trades |

Day-to-day routine: **[docs/OPERATIONS.md](docs/OPERATIONS.md)**

## What "self-improving" means here

Four mechanisms, none of them a black box that rewrites your strategy behind
your back:

- **Purged walk-forward.** Train 4 years, embargo 10 days, test 6 months, roll.
  Fresh capital each fold, so one lucky window cannot compound into the rest.
- **Never take the peak.** The best-scoring parameter set in a search is usually
  the luckiest. Candidates are re-scored by the median of their nearest
  neighbours, so the winner is the centre of a good *region*. Given a search
  containing a planted lucky spike scoring 6.75 against a broad hill at 1.98, it
  picks the hill.
- **A promotion gate that says no.** A challenger must beat the champion by a
  real margin, be no worse on drawdown or expectancy, have ≥40 out-of-sample
  trades, and clear a bootstrap CI that excludes zero. Against 20 pure-noise
  challengers, at most 4 get through. Every decision is logged to
  `state/promotions.jsonl`.
- **Learning from your real trades.** Every trade stores its entry conditions.
  Attribution buckets outcomes by feature quartile; an edge-decay monitor
  compares live expectancy against the backtest's bootstrap distribution and
  tells you to halve risk when live results fall outside it.

## Design notes worth knowing

**No dependencies.** The engine — indicators, backtest, walk-forward, learning
— uses nothing outside the Python 3.9+ standard library. `yfinance` is needed
only to *download* data. A broken pip or an offline VPS cannot stop the daily
routine.

**The backtest cannot see the future, and this is tested.** `tests/test_no_lookahead.py`
truncates the entire dataset at a cut date and re-runs; the equity curve and
trade list before the cut must come back bit-identical. Lookahead bias is silent
and it makes results look *better*, so it gets a test rather than a code review.

**Live and backtest share one implementation.** Both import `swingtrader/rules.py`.
The classic failure of a systematic setup is the live script drifting from the
backtest until the thing being traded is not the thing that was validated —
`tests/test_parity.py` pins them to the same exit date, bar for bar.

**Costs are modelled properly, because at ₹1 lakh they decide everything.** A
round trip on a ₹17,000 position costs 0.84%, of which a flat ₹23.60 DP charge
is 0.14% on its own. Measured on this system: **+0.19R per trade gross, −0.07R
net.** Costs consumed the entire edge. That single measurement is why the
defaults hold positions for weeks rather than days. Run `swing costs`.

**Synthetic data is for testing the pipeline, never for evidence.** `--synthetic`
exercises every code path without a vendor, and every command that uses it says
so loudly. `swing learn` refuses outright to promote a champion fitted on it.

## Tests

```bash
python -m pytest tests/ -q      # 93 tests
```

Covering indicator correctness against naive implementations, the no-lookahead
truncation test, cash/exposure/sector/heat invariants, the circuit-breaker
hysteresis, cost monotonicity, backtest-live parity, and the learning
machinery's refusal to promote noise.

## Status and honest caveats

This is a complete, tested implementation. It has **not** been validated on real
market data — the environment it was built in has no access to market data
providers, so every number quoted above comes from synthetic data or from
closed-form cost arithmetic. Validating it on real Nifty 100 history is the
first thing you should do, and `swing fetch` followed by `swing walkforward` is
how.

Other things you should know before risking money:

- The universe file is **today's** Nifty 100 tested against ten years of
  history, so it carries survivorship bias — assume backtest returns are
  optimistic by roughly 1-3% a year for that alone.
- The Groww cost figures are 2025 published rates and must be verified against
  your own contract note.
- The regime filter *will* whipsaw you out near lows and back in above them.
  That is the premium for avoiding the drawdown that does not recover.
- A trend system wins on ~30% of trades and makes its year in a handful of
  months. If that would make you abandon it, this is not the right system for
  you.

Nothing here is investment advice, and past performance — real or simulated —
does not predict future results.
