# Real-time execution

`swing live` runs the strategy against the market as it happens: it places the
orders, keeps a protective stop at the broker for every position, watches for
those stops failing, and writes tomorrow's plan after the close. You stop
working an order sheet by hand.

**What it does not change is the strategy.** Every decision is still made on a
daily close and executed at the next open, exactly as backtested. "Real time"
here means the validated fills happen reliably and on schedule — not that the
system trades more often. Trading more often is precisely what the cost table
in [SYSTEM.md](SYSTEM.md) forbids at ₹1 lakh, and an intraday version of this
strategy would hand most of its edge to the broker.

---

## The trading day

| Time (IST) | Phase | What happens |
|---|---|---|
| 08:50 | prep | Re-login (daily token), book any stops that fired, reconcile the book against broker holdings, make sure every position has exactly one protective stop |
| 09:15:05 | open | Exits at market first (frees cash), then entries: gap rule against the actual open, re-sized at the real price, stop re-anchored to the fill, marketable limit order |
| every 30 s | monitor | Fills, partial fills, stops that fired, and the watchdog |
| 15:31 | close | Cancel any unfilled entry, final sync |
| 16:15 | eod | Refresh and validate data, update the circuit breaker, write tomorrow's plan, ratchet trailing stops up at the broker |

Each phase runs once per session and its completion is saved, so the process
can be killed and restarted at any point. It will not repeat the open and will
not duplicate an order. If it comes up after 09:45 it executes **exits only** —
an entry at 11:00 is not the trade the backtest measured.

## How positions stay protected

Three layers, so no single failure leaves a position naked:

1. **A GTT stop at the broker.** It lives at Groww, not in this process, so it
   protects the position even if the machine running this is off. Every
   evening it is ratcheted up to the new trailing level — never down.
2. **The watchdog.** If price trades clearly through a stop and stays there for
   90 seconds while the broker's stop is still showing ACTIVE, the system
   cancels it and sells at market. A position with no broker stop at all gets
   no grace period beyond one confirming poll.
3. **End-of-day replay.** The planner replays every position's stop against
   the day's bars. If a bar traded through a stop and no exit was recorded, you
   get a critical alert and an exit is scheduled for the open.

A sell for shares under an active stop is never sent until that stop has been
cancelled. If the cancel fails because the stop already fired, the second sell
is not sent. That ordering is what prevents a double-sell.

## Safety rails

| Rail | How |
|---|---|
| **Two switches for real money** | `execution.mode = "live"` in the config **and** `SWING_LIVE_CONFIRM=yes` in the environment. No single typo goes live. |
| **Kill switch** | `swing live kill --mode live` (or `touch state/live/KILL`). No new orders within one poll. Broker stops stay in place — freezing must never mean unprotecting. |
| **Canary size** | `execution.canary_qty: 1` caps every order at one share: real orders, real fills, trivial money. |
| **Hard caps** | Per-order value (30% of capital), orders per day (20), buy value per day (80%), limit price within 5% of LTP, quote no older than 3 minutes. Independent of the strategy's own sizing. |
| **Your own holdings** | Shares already in your demat when it starts, or bought by hand later, are recorded as yours and never sold, stopped or added to. |
| **Reconciliation** | A position you sold by hand is closed in the book. A position that is merely *smaller* than expected is not guessed at — it blocks new entries until you look. |
| **Idempotent orders** | Every order has a reference derived from (session, symbol, purpose) and is written to disk before it is sent. After a crash or a timeout the order is looked up by reference, never re-sent blind. |
| **Holiday calendar** | Live and shadow mode refuse to start without `config/nse_holidays.csv` listing holidays for the current year. On a holiday a broker can queue an order and fill it at the next open, at a price the plan never considered. Add next year's list each December. |
| **Flatten** | `swing live flatten --mode live --yes-sell-everything` cancels every stop and sells every position at market. |
| **Alerts** | `state/<mode>/alerts.log`, and Telegram if configured. |

## Where it has to run

**Not in this cloud workspace, and not on a laptop that sleeps.** Since
1 April 2026, orders placed through a broker API must come from a static IP
address registered with the broker; orders from anywhere else are rejected,
and access tokens need a fresh login every day ([Groww's guidance on static IPs
for API trading](https://groww.in/blog/static-ip-api-trading-setup)). Groww
also offers a hosted option that avoids running your own static IP; the
practical alternative is a small VPS (any provider with a static IPv4) that is
on during market hours. At a few orders a day this system is far below the
10-orders-per-second level above which exchange registration of the strategy
is required.

## Rollout — do these in order

Each step exists because the previous one cannot find what it finds.

**1. Replay and parity on your own data** (no broker needed)

```bash
swing fetch && swing validate-data
swing live replay --days 60          # the live executor over the last 60 sessions
swing live parity --sessions 120     # live executor vs backtest, trade by trade
```

Parity must say `OK`. On the synthetic test data it matches 14 of 14 entries
and 9 of 9 exit dates, with ending equity within 0.03%. If it diverges on your
data, stop and find out why: a divergence means the live system is not trading
the strategy the backtest measured. (This check is how a real bug was found:
initial stops were not being re-anchored to the actual fill.)

**2. Probe the API** (read-only, no orders)

```bash
pip install growwapi
export GROWW_API_KEY=... GROWW_TOTP_SECRET=...
swing live probe
```

Every line must say `ok`. The adapter was written against the `growwapi` 1.5.0
source, but **the Groww API was unreachable from where it was written**, so the
exact response field names could not be checked. The probe calls every
read-only endpoint, runs each parser on the real response, and saves the raw
JSON. A `FAIL` means a field name differs — fix `broker/groww.py` before going
further. Things it specifically settles:

- holdings, margin, LTP/OHLC, order-list and GTT-list response shapes
- whether LTP keys are `NSE_RELIANCE` or `NSE:RELIANCE` (the SDK's own
  docstrings contradict each other; set `execution.ltp_symbol_separator`)

**3. Shadow mode — two to four weeks**

```bash
swing live run --mode shadow --once eod     # writes the first plan
swing live run --mode shadow                 # then leave it running
```

Real Groww prices, real timing, simulated fills, **no orders sent**. Check
`swing live status --mode shadow` and the alerts each day. Compare the shadow
journal (`state/shadow/journal_paper.csv`) with what the backtest says over the
same weeks.

**4. Live canary — one week**

Set `execution.mode = "live"` and `execution.canary_qty: 1`, add
`SWING_LIVE_CONFIRM=yes` to the environment, and run `--mode live`. Every order
is one share. Watch the first open in the Groww app as it happens. This is
where the things nothing else can test show up, for example:

- whether a GTT is accepted against shares bought the same day (if not, the
  system falls back to a same-day SL-M stop and retries the GTT the next
  morning — check the alerts)
- tick-size rounding, order-reference format, partial fills

**5. Full size.** Set `canary_qty: 0`. Keep the kill switch command in your
phone's notes.

## Everyday operation

```bash
swing live status --mode live     # phases, positions, stops, recent alerts
tail -f state/live/alerts.log
ls orders/live/                   # each evening's plan: .md to read, .json it executes
```

The plan written at 16:15 is the one executed at 09:15, unchanged. Read the
`.md` the evening before if you want to know what tomorrow will do.

## When something goes wrong

| Situation | What it does | What you do |
|---|---|---|
| Process crashes or the VPS reboots | systemd restarts it; it resumes the phase it was in | Nothing, unless the healthcheck alert keeps firing |
| Down through the whole open | Broker stops protect positions; no entries that day | Find out why |
| Broker login fails | Alerts, retries every minute within the phase window | Check the API key / TOTP secret |
| You sold a position by hand | Book closes it at the sell fill | Nothing |
| Position smaller than expected | Entries blocked, critical alert | Reconcile by hand, then restart |
| Data vendor fails at 16:15 | No new plan; stops stay in place; tomorrow's open is skipped as stale | Run `swing live run --once eod` once data is back |
| A stop did not fire | Watchdog exits after 90 s; alert | Report it to Groww |
| Anything you do not understand | — | `swing live kill`, then read `state/<mode>/audit/<date>.jsonl` |

The audit log records every decision and every declined decision, one JSON line
each. When something odd happens at 11:42, it is how you find out what the
system believed at 11:41.

## What was and was not verified

Verified here, with tests that fail when the protection is removed:

- the executor reproduces the backtest's trades on replayed data (parity test)
- crash between sending and recording an order → adopted, not re-sent
- ambiguous timeout on placement → resolved by reference
- stop fires during an exit → no double sell
- broker stop never fires → watchdog exits
- no broker stop at all → exit on the first confirmed breach
- manual sale, partial mismatch, your own holdings, kill switch, stale plan,
  missed opening window, restart mid-session
- the Groww adapter's calls against a stand-in with `growwapi` 1.5.0's exact
  signatures: order parameters, tick rounding direction, timeouts always set,
  write timeouts treated as ambiguous and never retried, reads retried, one
  re-login on an expired token, unreadable responses raising instead of
  returning "empty"
- TOTP against the RFC 6238 test vectors

**Not verified, because no market or broker endpoint was reachable:** anything
that depends on Groww's live behaviour — real response field names, GTT
acceptance on unsettled shares, real fills. Steps 2-4 of the rollout exist to
settle those before full size, and none of them can be skipped.
