# Running this thing

Ten minutes a day after the close. The system's edge, if it has one, is small
and it compounds — which means the operational discipline matters as much as the
strategy. Most systematic traders do not fail because their system stopped
working. They fail because they stopped following it during the month it looked
like it had.

---

## First-time setup

```bash
git clone <your repo> && cd swing_trader
pip install -r requirements.txt          # only needed to download data
export PYTHONPATH=src                    # or: pip install -e .

swing doctor                             # checks config and data for problems
swing fetch --start 2014-01-01           # ~10 min, rate-limited by the vendor
swing universe --check                   # confirms what actually cached
```

`swing fetch` uses Yahoo Finance, which is free and adequate for daily EOD work
but not flawless. Symbols get renamed on NSE (ZOMATO became ETERNAL), newly
listed names have short histories, and the vendor rate-limits. The command
reports what it could not get; re-run it to retry.

**Before trusting a single number**, verify the cost model against a real Groww
contract note and edit `config/base.jsonc` if it differs. At this account size
the frictions decide viability — see [EXPECTATIONS.md](EXPECTATIONS.md).

Then look at the evidence for yourself:

```bash
swing costs                                        # what trading actually costs you
swing backtest --report runs/baseline.html         # the honest baseline
swing walkforward                                  # out of sample, fold by fold
```

Read `runs/baseline.html` before risking money. Look at the **monthly return
table** and the **longest underwater period**, not the CAGR. The question to
answer is not "is the CAGR good?" — it is *"would I have kept placing these
orders during that 14-month flat stretch?"* If the answer is no, trade smaller
until it is yes.

---

## The daily routine (after 15:30 IST)

```bash
swing fetch --start 2024-01-01     # refresh the last stretch
swing plan                         # writes orders/<date>.md
```

The plan is a sheet with five sections. Work them **in order** — exits first,
because they free the cash the entries need.

1. **Exits** — market orders on the open.
2. **Stop updates** — modify the existing GTT. These are the profitable part of
   the system doing its work; do not skip them because nothing feels urgent.
3. **New entries** — a **limit** order at the stated price, never market. The
   limit is what stops you paying up for a gap that has already moved past the
   setup. If it does not fill, it does not fill. That is the rule working, not a
   missed trade.
4. **Holdings** — current state and what each position is doing.
5. **Watchlist** — the ranked screen, so you can see what is coming.

**The moment a buy fills, place its GTT stop.** A position without a stop is the
only genuinely unbounded risk in this system.

Then record what actually happened:

```bash
swing position add TITAN --qty 12 --price 3410.50 --date 2026-03-04 --setup breakout
swing position close INFY --price 1584.00 --date 2026-03-04 --reason stop
swing position list
```

The position book is not bookkeeping for its own sake. The planner replays each
position's trailing stop from its recorded entry, so if the book is wrong the
sheet is wrong. If a stop fired and you have not recorded it, the next plan will
tell you the book is out of sync rather than printing an impossible stop.

### If you miss a day

Nothing breaks. Run `swing plan` when you get back. Any stop that would have
fired is reported as already breached, with the date, so you can reconcile
against your Groww statement. **Do not** quietly carry on as though the position
is still live at its old stop.

---

## Weekly (Sunday, ~15 minutes)

```bash
swing journal --decay
```

Two things to read.

**Edge decay.** Is live per-trade R still inside what the backtest predicted?
The comparison is against the *sampling distribution*, not the point estimate —
being below the backtest average over 40 trades is completely normal. If it
prints `halve_risk`, do that, and investigate before restoring size.

**Attribution.** Realised R bucketed by entry conditions. Read this as a
hypothesis generator and nothing more. With 150 trades and 20 features you
*will* find spurious patterns; the tool flags monotonic relationships as worth
testing and non-monotonic ones as probably noise. **Never act on it directly** —
if something looks real, test it properly:

```bash
swing walkforward --set screen.atr_pct_max=0.055
```

---

## Quarterly (~1 hour, mostly waiting)

```bash
swing learn --samples 150 --journal state/oos_trades.csv
```

This runs the full walk-forward search, computes consensus parameters, and puts
them through the promotion gate. Read three things:

1. **The stitched out-of-sample record.** This is the honest number. Not the
   in-sample backtest, not the best fold — this one.
2. **Parameter stability.** Any parameter with a coefficient of variation above
   0.35 is being re-picked wildly every window. That is noise being fitted, not
   a setting. Freeze it at a sensible prior and remove it from the search space.
3. **The promotion gate's reasoning.** If it refused, it will say why. `"per-trade
   edge not distinguishable from zero"` means exactly that, and the correct
   response is to keep the current champion, not to loosen the gate.

If it promotes, use the new champion with `swing plan --champion`. If it refuses
three quarters running while live results also disappoint, the honest conclusion
is that this edge is not there on your data — not that the gate needs relaxing.

---

## The rules that are not in the code

The code enforces position sizing, stops, sector caps and the circuit breaker.
It cannot enforce these:

**Do not override the regime filter.** When it says risk-off and the market is
ripping upward, sitting out will feel stupid for weeks. It is a fair price for
not being fully invested in the one that does not recover.

**Do not skip a stop update because you "know" the stock is fine.** The
profitable trades in a trend system are the ones where the trail did the
deciding.

**Do not take a trade the sheet did not list.** The one you take on a hunch
is not in the backtest, is not in the journal, and quietly makes every statistic
you have meaningless.

**Do not size up after a good month.** A +8% month is drawn from the same
distribution as the −8% month. Sizing is set by the config; change it after a
walk-forward, not after a feeling.

**Do accept dead periods.** The system will sit in cash. That is the design.

### When you should actually intervene

- The data looks wrong (a stock's price jumps 40% with no news — check for a
  corporate action the vendor mishandled).
- `swing doctor` reports a problem.
- Your real fills are consistently worse than the plan's limits — raise
  `costs.slippage_bps` to match reality and re-run the backtest.
- Something structural changes: a broker tariff, an STT change, a stock leaving
  the index.

---

## Scaling up

The fixed-cost drag falls sharply with account size, so **adding capital is the
highest-return action available to you** — your net edge nearly doubles between
₹1 lakh and ₹10 lakh with no change in skill whatsoever.

```bash
swing backtest --capital 500000 --report runs/at5L.html
```

At larger sizes, revisit `risk.max_positions` (more positions become affordable),
`universe.max_price_frac_of_equity` (previously unbuyable names open up) and
`costs.slippage_bps` (larger orders move the book more).

---

## Files

| Path | What it is |
|---|---|
| `config/base.jsonc` | Every tunable, annotated |
| `config/universe_nifty100.csv` | Constituents and sectors — **a snapshot, refresh it** |
| `data_cache/` | Cached OHLCV, one CSV per symbol |
| `state/positions.json` | Your open book |
| `state/journal.csv` | Every completed trade with its entry conditions |
| `state/champion.json` | Currently promoted parameters |
| `state/promotions.jsonl` | Audit trail of every promotion decision |
| `orders/<date>.md` | Daily order sheets |
| `runs/*.html` | Backtest reports |

Back up `state/` — the journal is the only record of what actually happened, and
it is what the learning loop runs on.
