# What this system can and cannot return

You asked for 8-10% a month. I have built the system anyway, and built it
properly. But you would be trading it on a false premise if I did not first
show you the arithmetic, because that number is not a stretch goal you reach by
tuning parameters — it is outside the achievable set for long-only Nifty 100
cash equity, and no amount of optimisation moves it inside.

This document is the argument. Disagree with it if you can find the flaw; the
system does not depend on you agreeing.

---

## 1. What 8-10% a month compounds into

A monthly target is an annual target wearing a disguise. 8% a month is **152% a
year**. 10% a month is **214% a year**.

Starting from ₹1,00,000:

| Monthly | Annual | After 1 yr | After 3 yr | After 5 yr | After 10 yr |
|--------:|-------:|-----------:|-----------:|-----------:|------------:|
| 2%  |  27% | ₹1,26,824 |  ₹2,03,989 |    ₹3,28,103 |      ₹10,76,516 |
| 3%  |  43% | ₹1,42,576 |  ₹2,89,828 |    ₹5,89,160 |      ₹34,71,099 |
| 5%  |  80% | ₹1,79,586 |  ₹5,79,182 |   ₹18,67,919 |    ₹3,48,91,199 |
| **8%**  | **152%** | ₹2,51,817 | ₹15,96,817 | ₹1,01,25,706 | **₹102.5 crore** |
| **10%** | **214%** | ₹3,13,843 | ₹30,91,268 | ₹3,04,48,164 | **₹927 crore** |

Sustaining 8% a month for ten years turns ₹1 lakh into ₹102 crore. Ten years is
not a long time to run a trading system; people run them for careers.

If that were reachable by a rules-based momentum strategy on the hundred most
heavily analysed stocks in India, it would already have been arbitraged away by
people with better data, lower costs and more capital than either of us.

## 2. Against the record

| | Annualised |
|---|---:|
| Bank fixed deposit | 7% |
| Nifty 50, total return, long run | ~13% |
| Top-decile Indian equity mutual fund | ~18% |
| Warren Buffett, Berkshire 1965-2023 | 19.8% |
| **A very good systematic swing system, well executed** | **~22%** |
| Medallion fund, net of fees — the best documented record in history | ~39% |
| Your 8%/month target | **152%** |
| Your 10%/month target | **214%** |

The target is roughly **four times** the best sustained track record ever
documented, achieved by a fund of PhDs with proprietary data, co-located
execution and near-zero transaction costs, which closed to outside money
because the strategy could not absorb more capital.

## 3. Why costs alone make it impossible at ₹1 lakh

This is the part that is specific to your account size, and it is the one most
people never run the numbers on.

At ₹1,00,000 split into 6 positions, each position is about ₹17,000. A round
trip on that position costs about **0.84%** — brokerage both ways, STT both
ways, exchange and SEBI charges, GST, stamp duty, the flat ₹20+GST DP charge on
the sell, and realistic slippage. Run `swing costs` to see it broken down.

The DP charge is the cruel one: it is **flat**. It costs the same ₹23.60 whether
the position is ₹17,000 or ₹17,00,000. On your account it is 0.14% by itself.

What that does to you annually:

| Turnover | Typical hold | Annual cost | As % of capital |
|---|---|---:|---:|
| 0.5x/month | ~2 months | ₹5,040 | **5.0%** |
| 1x/month | ~1 month | ₹10,080 | **10.1%** |
| 2x/month | ~2 weeks | ₹20,160 | **20.2%** |
| 4x/month | ~1 week | ₹40,320 | **40.3%** |

To net 152% a year while turning the book over twice a month, you must gross
**172%**. The frictions do not care how good your signals are.

This is measurable, not theoretical. On this system's own test runs, the gross
edge before costs was **+0.19R per trade** and the net edge after costs was
**−0.07R**. Costs consumed the entire edge and more. That single measurement is
why the shipped configuration holds positions for weeks rather than days — see
[SYSTEM.md](SYSTEM.md).

## 4. Individual +8% months will happen. Consecutive ones will not.

This matters, because it is how the target survives contact with reality for a
few months and then destroys an account.

A genuinely good system — say 22% a year with 19% annualised volatility — has a
monthly distribution averaging about +1.7% with a standard deviation of about
5.5%. In that distribution:

- **P(a month returns ≥ +8%) ≈ 13%** — roughly one month in eight.
- **P(twelve consecutive months ≥ +8%) ≈ 1 in 62 billion.**

So you *will* see +8% months. You will probably see one in your first year. The
danger is concluding from it that the target is achievable and sizing up — the
month after a +8% month is drawn from the same distribution, and that
distribution has a −8% month in it too.

This system reports **"Months ≥ +8%"** in every backtest and report,
specifically so you can watch how rare they are in your own results rather than
taking my word for it.

## 5. The levers that actually exist, and what each costs

I am not telling you to accept 22% and be quiet. Here is the honest menu.

**Leverage (Groww MTF).** The only lever that scales returns directly. It scales
drawdown exactly as hard, and you pay ~14% a year on the borrowed portion:

| Leverage | Net return on a 22% system | Max drawdown |
|---|---:|---:|
| 1.0x | ~22% | ~25% |
| 2.0x | ~30% | ~50% |
| 3.0x | ~38% | ~75% |
| 4.0x | ~46% | **~100% — the account is gone** |

Even at 4x, badly over-leveraged and near-certain to be wiped out by one bad
quarter, you reach 46% a year. Not 152%. **Leverage cannot bridge this gap** —
it runs out of account before it runs out of distance.

**A different instrument.** Index and stock options give you convexity that cash
equity does not. Some options strategies genuinely can return 8% in a month.
They can also return −100% in a month, they require a completely different risk
framework, and you specified cash equity. If you want to explore this later it
is a different system, not a parameter change to this one.

**More capital.** Percentage returns are what they are, but the fixed-cost drag
falls sharply with size. At ₹10 lakh the same round trip costs 0.47% instead of
0.84%. Your edge nearly doubles in net terms with no change in skill. **Adding
capital is by far the highest-return action available to you**, which is worth
knowing before you spend six months tuning parameters.

**Smaller caps.** More inefficiency, more momentum, worse liquidity, wider
spreads, and much worse behaviour in a crash. You specified Nifty 100. The
universe file is one CSV — you can widen it later and let the walk-forward tell
you whether it helped.

## 6. So what should you actually expect?

For this system, on Nifty 100 cash, long only, no leverage, at ₹1 lakh, well
executed and left alone:

| | Realistic range |
|---|---|
| CAGR | **12-25%** in good conditions |
| Max drawdown | **20-30%**, and you should plan for it |
| Positive months | **50-60%** |
| Average month | **+1% to +2%** |
| Best month you'll see | **+10-15%** |
| Worst month you'll see | **−8% to −12%** |
| Flat or losing stretches | **6-18 months** — this is normal, not a malfunction |
| Trades | **15-40 a year** |

And a warning the backtest cannot give you: a long-only system with a regime
filter spends substantial time in cash. In the test runs there were **calendar
years with almost no trades at all**. The system sitting on its hands through a
choppy year is it working correctly. The temptation to override it during those
months is the single most likely cause of this system failing for you.

## 7. What I have done about the target

I did not silently retune the system toward a number I do not believe in. Instead:

1. Every backtest and report prints **"Months ≥ +8%"**, so the gap between
   target and reality is in front of you continuously, measured on your data.
2. `swing costs` shows exactly what turnover costs you, so the constraint is
   quantified rather than argued about.
3. The walk-forward optimiser maximises **Calmar net of a turnover penalty**,
   not raw return. Optimising for raw return on a small account produces a
   configuration that trades constantly and donates the edge to the broker.
4. The promotion gate will not install a configuration whose out-of-sample edge
   is statistically indistinguishable from luck, no matter how good its
   backtest looks.

If after all this you want to keep 8-10% a month as the stated goal, that is
your call and the system will keep reporting honestly against it. What it will
not do is quietly adopt settings that chase the target and hand you a 60%
drawdown in exchange.

---

*Every number in this document is reproducible: `swing costs` for the cost
tables, `swing backtest --report` for the monthly distribution, and the
compounding table is just `(1+r)^n`.*
