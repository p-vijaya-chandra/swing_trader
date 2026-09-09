# The strategy, and why each piece is there

Every component below earns its place or it would not be here. Where a choice
was forced by the ₹1 lakh account size rather than by the market, I say so.

---

## The shape of the thing

Long-only. Nifty 100 cash delivery. Daily bars, decisions at the close, orders
filled at the next open. Six positions maximum, held for weeks, exited by a
trailing stop. No shorting, no leverage, no intraday.

That is a **trend-following system**, and it has the payoff profile of one:
roughly 30-40% of trades win, the winners are 3-5x the size of the losers, and
most of the annual return arrives in a handful of months. If a 27% win rate
would make you abandon it, do not trade it — that number is the design, not a
malfunction.

---

## 1. Regime filter — the most important component

`src/swingtrader/regime.py`

On a long-only cash book you cannot short and cannot hedge cheaply. Essentially
all of your catastrophic risk is one scenario: *holding a basket of high-beta
large caps into a market that falls 25%*. Stock selection cannot save you there
— in a real crash the correlation of everything goes to one. Only **refusing to
hold** can.

So every session is classified using three inputs:

| Input | What it catches |
|---|---|
| Index vs its 200 DMA | The primary bull/bear switch |
| Index vs its 50 DMA | Intermediate deterioration |
| **Breadth** — % of the universe above its own 50 DMA | An index being carried by five mega-caps over a rotting market. This is a recurring Nifty failure mode and the index MA alone will not see it. |

The classification sets **target gross exposure**: 100% risk-on, 55% neutral,
**0% risk-off**. Risk-off means no new positions, and existing ones trail on a
tightened 1.5 ATR stop instead of 3.0 — so the book winds itself down over days
rather than being dumped at the exact low.

The consequence you must accept: **this system spends real time in cash.** In
testing there were calendar years with almost no trades. That is it working.

## 2. Screen — a veto, not a preference

`SwingStrategy.passes_screen`

Binary. It answers only "is this name tradeable today?" and never expresses a
ranking. A name must be:

- **Liquid** — 20-day median traded value ≥ ₹25 crore
- **Buyable** — one share must not already breach the position cap. At ₹1 lakh
  this quietly removes the highest-priced constituents, and pretending otherwise
  would make the backtest unimplementable.
- **In an uptrend** — close > EMA50 > EMA200, with EMA200 sloping up
- **Volatile enough to move, not so volatile the stop is meaningless** — ATR%
  between 1.2% and 7.0%
- **Near its highs** — no more than 25% below the 52-week high
- **Actually trending** — ADX ≥ 18
- **Not lagging the index** — 63-day relative strength ≥ 0

## 3. Rank — a preference among survivors

The composite is dominated (55% weight) by **annualised regression slope ×
R²**, fitted to log price over 90 days.

This is worth explaining because it is the heart of the selection. Fitting a
line to *log* price gives a compounding rate rather than a rupee slope, so a
₹200 stock and a ₹2,000 stock are directly comparable. Multiplying by R²
discounts a name that got its move in one gap and then went sideways — the fit
is poor, so the score is cut. What survives is a **steady climb**, which is what
continues; a single violent gap is what mean-reverts.

The rest: 20% 126-day rate of change, 15% relative strength vs the index, and a
−10% **penalty** on ATR% so that among equally-trending names the calmer one
wins.

## 4. Trigger — timing

A high-ranked name is not a buy until price does something specific *today*.
Without a trigger you buy extended names at arbitrary points and your stop
distance — and therefore your position size — is arbitrary too. Buying rank
without a trigger is the most common way retail momentum systems end up with a
45% drawdown.

Two setups:

- **Breakout** — close takes out the highest high of the prior 20 sessions, on
  ≥1.2× average volume.
- **Pullback continuation** — in an established uptrend, price dips to within
  1 ATR of the 20 EMA with a 3-period RSI under 35, *and then closes back above
  the previous day's high*. The reclaim is essential. Buying the dip without it
  is how you catch the one name that keeps falling.

## 5. Position sizing — risk first

`RiskManager.size`

You decide what being wrong costs, and the stop distance determines the
quantity:

```
qty = floor( (equity × 1.2%) / (entry − stop) )
```

A ₹1,200 loss whether the name is a quiet FMCG stock or a volatile metal name.
That is what makes R-multiples comparable across names and across time, which is
what makes the whole learning loop possible.

Then a stack of caps, any of which can refuse the trade: max 25% of equity per
position, max 6 positions, max 7.5% total portfolio heat, **max 3 positions per
sector** (the universe is ~23% financials — without this cap a "diversified"
book is one bet on rate policy), and a minimum position value of ₹8,000 below
which fixed costs eat the edge.

## 6. Stops and exits — slow on purpose

- **Initial stop**: below the recent swing low where the thesis is actually
  wrong, clamped into a 1.5–3.5 ATR band. Too tight and ordinary noise closes a
  trade that was right; too wide and the position size stops meaning anything.
  *(This clamp was added after an early version allowed a 0.8 ATR stop and
  produced a 10% win rate.)*
- **Chandelier trail**: highest high since entry − 3.0 ATR. It **only ratchets
  up**. Widening a stop to "give it room" is how a 1R loss becomes a 4R loss.
- **Tightened to 1.5 ATR in a risk-off regime.**
- **Time stop**: 25 sessions to prove itself, and only cut if still underwater.
  Dead money costs you the trade you cannot take, because the slot and the heat
  budget are already spent.
- **No profit target.** This is deliberate and it is what you asked for: a
  position is held as long as momentum persists. Nothing but the trail takes a
  winner off. Momentum-based and rank-decay exits exist in the code but ship
  **disabled** — see below.

## 7. Why the defaults are slow: the ₹1 lakh cost problem

This is the single most important design constraint, and it is not a matter of
opinion — it was measured.

At ₹1 lakh with 6 positions, each position is ~₹17,000, and a round trip costs
**0.84%**. The DP charge alone is a *flat* ₹23.60 per sell, which is 0.14% of
that position by itself.

Running the same strategy with and without costs:

| | Expectancy per trade |
|---|---:|
| Gross, before costs | **+0.19R** |
| Net, after real costs | **−0.07R** |
| **Cost drag** | **0.25R per trade** |

Costs consumed the entire edge. And annually:

| Turnover | Annual cost | % of capital |
|---|---:|---:|
| 1x/month | ₹10,080 | 10.1% |
| 2x/month | ₹20,160 | **20.2%** |
| 4x/month | ₹40,320 | **40.3%** |

**Turnover, not stock selection, is the binding constraint on this account.**
That is why the shipped exits are slow, why the momentum and rank-decay exits
default to off, and why it is 6 larger positions rather than 10 smaller ones.
Run `swing costs` to see it for your own settings.

## 8. Drawdown control

Three layers, each with a defined way out:

1. **De-risk**: past −10%, risk per trade halves.
2. **Circuit breaker**: past −20%, no new positions.
3. **Probation**: after resuming, half size for 60 sessions.

The breaker needs care. An early version halted at −20% and resumed only when
the drawdown recovered past −10% — which is **unreachable**, because with no
trading the equity curve flatlines, the all-time peak never updates, and the
drawdown never moves. The system switched itself off permanently in 2020 and the
backtest reported it as "low volatility". Now there are two ways back in: the
drawdown recovers, *or* a 40-session cooldown elapses and the regime is risk-on
again. On resume the risk reference resets to current equity, so the breaker
judges the new attempt rather than one already paid for. The reported drawdown
still uses the true all-time peak — hiding a real drawdown from yourself is
useless.

---

## The self-improving loop

"Self-improving" here means four specific mechanisms, none of which is a
black box that silently rewrites your strategy.

### Walk-forward, purged
`swing learn` — Train on 4 years, leave a 10-day embargo, test on the next 6
months, roll forward. The embargo matters: a trade open across the boundary
appears in both windows and leaks training information into the test. Each test
window starts from fresh capital, so one lucky early fold cannot compound into
every later fold's position sizing.

### Never take the peak
The single best parameter set in a search is almost always the *luckiest*. Each
candidate is re-scored by the **median of its k nearest neighbours** in
normalised parameter space, and the winner is the centre of the best-performing
*region*. A configuration whose neighbours are all mediocre is a fluke; one
surrounded by good neighbours has tolerance around it. In testing, a search
containing a deliberately planted lucky spike (score 6.75 vs a broad hill at
1.98) selected the hill.

The objective is **Calmar penalised for turnover**, not raw return — optimising
raw return on a small account produces a configuration that trades constantly
and donates the edge to the broker.

### A promotion gate that says no
A challenger replaces the champion only if it beats it by a real margin, is not
worse on drawdown *or* expectancy, has ≥40 out-of-sample trades, and its
bootstrap CI on per-trade R excludes zero. Tested against 20 pure-noise
candidates, at most 4 pass — consistent with the CI's own false-positive rate.
Every decision, promoted or rejected, is appended to `state/promotions.jsonl`,
so months later "why is it trading like this?" has an answer.

**It refuses outright to promote a champion fitted on synthetic data.**

### Learning from your actual trades
`swing journal` — Every trade stores the feature snapshot from its entry.

- **Attribution** buckets realised R by feature quartile and flags monotonic
  relationships. Absolute price levels are excluded: "trades above ₹2,700 did
  better" is a statement about which stocks were expensive, not a condition you
  can screen on.
- **Edge decay** compares recent live expectancy against the *bootstrap
  sampling distribution* of the backtest, not its point estimate. A 60-trade run
  below the backtest mean is normal; one below the 5th percentile of 60-trade
  samples drawn from it is not, and triggers an automatic instruction to halve
  risk.
- **Meta-labelling** (off by default) is a regularised logistic regression that
  can veto entries the rules already like. It never picks a stock, sizes a
  position or overrides an exit, and it abstains when a feature is missing
  rather than guessing. Linear and L2-regularised because on 300 trades a
  gradient-boosted forest fits the noise perfectly and reports a beautiful
  in-sample AUC while doing so.

---

## What is deliberately not here

- **No intraday data.** Different game, different costs, far more operational risk.
- **No fundamentals.** They do not add much over weeks, and clean point-in-time
  Indian fundamental data is expensive.
- **No neural networks.** With ~40 trades a year, the sample cannot support them.
  The bottleneck is transaction costs and sample size, not model capacity.
- **No auto-execution.** Groww retail API access is not something to assume, and
  at ₹1 lakh the value of automation is small next to the cost of an unattended
  bug. You get an order sheet; you place the orders.

## Known limitations, stated plainly

1. **Survivorship bias.** The universe file is today's Nifty 100 tested against
   ten years of history — it only knows about companies that made it. Assume
   backtest returns are optimistic by roughly 1-3% a year for this alone.
2. **No corporate-action handling beyond the vendor's.** Yahoo adjusts splits
   and bonuses; verify anything that looks anomalous.
3. **Intraday stops are assumed to fill at the stop price.** Real gaps are
   modelled (a gap through the stop fills at the open), but a stop is still
   assumed to trigger cleanly. In a circuit-breaker move it will not.
4. **The cost model is a snapshot.** Verify against your own contract note.
5. **The regime filter will whipsaw.** It will take you out near lows and back
   in above them. That is the premium you pay for not being in the 2008-shaped
   scenario, and it is a real cost, not a bug to be tuned away.
