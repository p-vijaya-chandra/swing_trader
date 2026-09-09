"""Command line interface.

    swing fetch       download/refresh price history into the local cache
    swing universe    show or refresh the Nifty 100 constituent list
    swing backtest    run one backtest and write a report
    swing walkforward evaluate the CURRENT config out of sample, fold by fold
    swing learn       search parameters per fold, gate, and maybe promote a champion
    swing scan        rank the universe as of the latest session
    swing plan        write tomorrow's order sheet for Groww
    swing position    record fills and closes
    swing journal     attribution and edge-decay report over your real trades
    swing costs       what frictions actually cost at your position size
    swing doctor      check the setup and the data for problems
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from .backtest import compute_metrics
from .backtest.metrics import summary_lines
from .config import Config
from .costs import CostModel
from .runtime import Dataset, load_universe, run_backtest
from .util import NA, fmt_inr, is_na, mean, median


# --------------------------------------------------------------------- helpers
def _cfg(args) -> Config:
    cfg = Config.load(args.config) if args.config else Config()
    if getattr(args, "capital", None):
        cfg.set("capital", float(args.capital))
    if getattr(args, "set", None):
        for kv in args.set:
            if "=" not in kv:
                raise SystemExit(f"--set expects key=value, got '{kv}'")
            k, v = kv.split("=", 1)
            try:
                val: Any = json.loads(v)
            except json.JSONDecodeError:
                val = v
            cfg.set(k.strip(), val)
    if getattr(args, "champion", False):
        from .learn import ChampionStore
        store = ChampionStore(cfg.get("paths.state_dir", "state"))
        champ = store.load()
        if champ is None:
            print("  ! --champion given but no champion has been promoted yet; "
                  "using the base config")
        else:
            print(f"  using champion v{champ.version} "
                  f"(promoted {champ.created_utc}, score {champ.score:.3f})")
            cfg = cfg.with_overrides(champ.params)
    return cfg


def _dataset(cfg: Config, args) -> Dataset:
    ds = Dataset.load(cfg, synthetic=getattr(args, "synthetic", False),
                      seed=getattr(args, "seed", 14))
    print(f"  data: {ds.coverage()}")
    return ds


SYNTHETIC_WARNING = (
    "SYNTHETIC DATA - these numbers demonstrate that the pipeline runs. They are "
    "not evidence about profitability. Fetch real history before believing any of it.")


def _data_note(ds: Dataset) -> str:
    return SYNTHETIC_WARNING if ds.synthetic else "Real cached history."


# ----------------------------------------------------------------------- fetch
def cmd_fetch(args) -> int:
    cfg = _cfg(args)
    from .data.providers import get_provider, ProviderError
    from .data.store import DataStore

    symbols, sectors, _ = load_universe(cfg.get("universe.file"))
    index_sym = cfg.get("regime.index_symbol", "NIFTY100")
    want = symbols + [index_sym]
    if args.symbols:
        want = args.symbols

    store = DataStore(cfg.get("data.cache_dir", "data_cache"))
    try:
        prov = get_provider(args.provider or cfg.get("data.provider", "yfinance"), cfg)
    except ProviderError as exc:
        print(f"error: {exc}")
        return 2

    # Incremental by default: for anything already cached, ask only for the bars
    # since its last one. A daily refresh then costs a few hundred rows instead
    # of ten years, which is the difference between a 10-second cron job and one
    # that gets rate-limited every morning.
    plan: Dict[str, str] = {}
    for sym in want:
        if args.full:
            plan[sym] = args.start
            continue
        cached = store.load(sym)
        if cached is None or not len(cached):
            plan[sym] = args.start
        else:
            plan[sym] = _shift_iso(cached.dates[-1], -5)   # small overlap for restatements

    fresh = [s for s in want if plan[s] == args.start]
    incr = [s for s in want if s not in fresh]
    print(f"Fetching {len(want)} symbols "
          f"({len(fresh)} full history, {len(incr)} incremental)")
    if incr and not args.full:
        print("  (pass --full to re-download everything from scratch)")

    got: Dict[str, object] = {}
    for group_start in sorted(set(plan.values())):
        batch = [s for s in want if plan[s] == group_start]
        got.update(prov.fetch(batch, group_start, args.end or ""))

    for sym, ser in got.items():
        store.upsert(ser)

    print(f"\nCached {len(got)}/{len(want)} symbols into {store.cache_dir}")
    missing = [s for s in want if s not in got]
    if missing:
        print(f"Missing ({len(missing)}): {', '.join(missing[:20])}"
              + (" ..." if len(missing) > 20 else ""))
        print("Common causes: the symbol was renamed on NSE (ZOMATO -> ETERNAL), it is "
              "newly listed, or the vendor is rate-limiting. Re-run to retry.")

    print("\nNow run `swing validate-data`. Free EOD data is good enough to trade on")
    print("and bad enough to ruin a backtest, and it never announces the difference.")
    return 0


def _shift_iso(date: str, days: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(date[:10]) + dt.timedelta(days=days)).isoformat()


# --------------------------------------------------------------- validate-data
def cmd_validate(args) -> int:
    cfg = _cfg(args)
    from .data.quality import check_dataset
    from .data.store import DataStore

    symbols, _, _ = load_universe(cfg.get("universe.file"))
    store = DataStore(cfg.get("data.cache_dir", "data_cache"))
    series = store.load_many(symbols)
    index = store.load(cfg.get("regime.index_symbol", "NIFTY100"))
    if not series:
        print(f"Nothing cached in {store.cache_dir}. Run `swing fetch` first.")
        return 2

    rep = check_dataset(series, index,
                        min_bars=int(cfg.get("universe.min_history_bars", 260)),
                        universe=symbols)

    print(f"\n{rep.n_symbols} symbols, {rep.n_bars:,} bars, "
          f"{rep.first_date} .. {rep.last_date}")
    print(f"index series: {'present' if rep.index_present else 'MISSING'}\n")

    if not rep.issues:
        print("No problems found. The data is fit to backtest on.")
        return 0

    for issue in rep.errors:
        print(issue)
    if rep.errors and rep.warnings:
        print()
    for issue in rep.warnings[: args.max_warnings]:
        print(issue)
    if len(rep.warnings) > args.max_warnings:
        print(f"... and {len(rep.warnings) - args.max_warnings} more warnings "
              f"(--max-warnings to show more)")

    print(f"\n{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
    counts = rep.by_kind()
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:22s} {v}")

    bad = rep.bad_symbols()
    if bad:
        print(f"\n{len(bad)} symbol(s) with errors: {', '.join(bad[:20])}"
              + (" ..." if len(bad) > 20 else ""))
        if args.write_exclusions:
            with open(args.write_exclusions, "w", encoding="utf-8") as fh:
                fh.write("# symbols failing swing validate-data\n")
                for b in bad:
                    if not b.startswith("("):
                        fh.write(b + "\n")
            print(f"Written to {args.write_exclusions}")
        else:
            print("Fix them at the source, or drop them: "
                  "`swing validate-data --write-exclusions state/excluded.txt`")

    print("\nWhy this matters: every defect above produces a plausible equity curve")
    print("rather than an error. An unadjusted bonus reads as a -50% day, stops out")
    print("every holder, and leaves ATR inflated for weeks afterwards.")
    return 1 if rep.errors else 0


# --------------------------------------------------------------------- bundle
def cmd_bundle(args) -> int:
    """Export/import the price cache as one file.

    The point is portability: fetch on a machine with market-data access, then
    move the exact bytes somewhere else and get identical backtests. A bundle is
    a plain tar.gz of CSVs - inspectable, diffable, and not a pickle.
    """
    cfg = _cfg(args)
    import tarfile
    from .data.store import DataStore

    store = DataStore(cfg.get("data.cache_dir", "data_cache"))

    if args.bundle_cmd == "export":
        syms = store.symbols()
        if not syms:
            print(f"Nothing cached in {store.cache_dir}.")
            return 2
        os.makedirs(os.path.dirname(args.path) or ".", exist_ok=True)
        with tarfile.open(args.path, "w:gz") as tf:
            tf.add(store.cache_dir, arcname="data_cache")
        size = os.path.getsize(args.path)
        cov = store.coverage()
        ends = [v[1] for v in cov.values()]
        print(f"Exported {len(syms)} symbols ({size/1e6:.1f} MB) to {args.path}")
        if ends:
            print(f"Latest session in the bundle: {max(ends)}")
        return 0

    if args.bundle_cmd == "import":
        if not os.path.exists(args.path):
            print(f"No such file: {args.path}")
            return 2
        with tarfile.open(args.path, "r:gz") as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.endswith(".csv")
                       and ".." not in m.name and not m.name.startswith("/")]
            if not members:
                print("Bundle contains no CSVs.")
                return 2
            dest = os.path.dirname(store.cache_dir) or "."
            tf.extractall(dest, members=members)
        print(f"Imported {len(members)} symbol files into {store.cache_dir}")
        print("Run `swing validate-data` before trusting it.")
        return 0
    return 1


# -------------------------------------------------------------------- universe
def cmd_universe(args) -> int:
    cfg = _cfg(args)
    symbols, sectors, names = load_universe(cfg.get("universe.file"))
    if args.check:
        from .data.store import DataStore
        store = DataStore(cfg.get("data.cache_dir", "data_cache"))
        cov = store.coverage()
        missing = [s for s in symbols if s not in cov]
        print(f"{len(symbols)} symbols in the universe file, {len(cov)} cached, "
              f"{len(missing)} missing")
        if missing:
            print("missing:", ", ".join(missing))
        short = [(s, v) for s, v in cov.items() if v[2] < int(cfg.get("universe.min_history_bars", 260))]
        if short:
            print(f"\n{len(short)} with too little history to trade:")
            for s, v in sorted(short, key=lambda x: x[1][2])[:20]:
                print(f"  {s:14s} {v[2]:5d} bars  {v[0]}..{v[1]}")
        return 0
    counts: Dict[str, int] = {}
    for s in symbols:
        counts[sectors[s]] = counts.get(sectors[s], 0) + 1
    print(f"{len(symbols)} symbols in {cfg.get('universe.file')}\n")
    for sec, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {sec:16s} {n:3d}")
    print("\nThis file is a SNAPSHOT. NSE reshuffles the Nifty 100 twice a year, and a")
    print("backtest run on today's constituents over ten years of history has survivorship")
    print("bias baked in: it only knows about the companies that made it. Treat backtest")
    print("returns as optimistic by roughly 1-3% a year for that reason alone.")
    return 0


# -------------------------------------------------------------------- backtest
def cmd_backtest(args) -> int:
    cfg = _cfg(args)
    ds = _dataset(cfg, args)
    t0 = time.time()
    res = run_backtest(cfg, ds, start=args.start or "", end=args.end or "",
                       verbose=args.verbose)
    if not res.daily:
        print("No sessions in range - check --start/--end against the cached data.")
        return 1
    m = compute_metrics(res.dates, res.equity_curve, res.trades, res.costs_total)
    print(f"\nBacktest finished in {time.time()-t0:.1f}s")
    if ds.synthetic:
        print(f"\n*** {SYNTHETIC_WARNING} ***")
    print()
    print("\n".join(summary_lines(m)))

    from .reporting import monthly_table_text, equity_sparkline
    print("\nMonthly returns")
    print("\n".join(monthly_table_text(m.get("monthly_returns") or {})))
    print("\nEquity  " + equity_sparkline(res.equity_curve))

    if res.open_positions:
        print(f"\nStill open at the end ({len(res.open_positions)}):")
        for p in res.open_positions:
            print(f"  {p['symbol']:14s} {p['qty']:5d} @ {p['entry_price']:9.2f} "
                  f"-> {p['last_price']:9.2f}  {p['r_multiple']:+.2f}R  {p['bars_held']}d")

    if args.report:
        from .reporting import write_report
        extra = []
        if res.rejections:
            extra.append(("Signals refused (and why)", "\n".join(
                f"{k:24s} {v}" for k, v in sorted(res.rejections.items(), key=lambda x: -x[1]))))
        path = write_report(args.report, args.title or "Swing backtest", m, res, cfg,
                            extra, data_note=_data_note(ds))
        print(f"\nReport written to {path}")

    if args.journal:
        from .learn import Journal
        j = Journal(args.journal)
        j.clear()
        n = j.append(res.trades)
        print(f"Wrote {n} trades to {args.journal}")
    return 0


# ----------------------------------------------------------------- walkforward
def cmd_walkforward(args) -> int:
    cfg = _cfg(args)
    ds = _dataset(cfg, args)
    from .learn import WalkForward

    wf = WalkForward(cfg, ds, run_backtest, verbose=True)
    folds = wf.folds()
    if not folds:
        print("Not enough history for even one fold. Fetch more data, or lower "
              "learn.train_years.")
        return 1
    print(f"\nEvaluating the current config over {len(folds)} out-of-sample windows")
    print("(no parameter fitting here - this is the config as it stands)\n")
    out = wf.run(select=None)
    print()
    print("\n".join(out.summary()))
    if out.stitched_metrics:
        print("\nStitched out-of-sample record")
        print("\n".join(summary_lines(out.stitched_metrics)))
    if ds.synthetic:
        print(f"\n*** {SYNTHETIC_WARNING} ***")
    return 0


# ---------------------------------------------------------------------- learn
def cmd_learn(args) -> int:
    cfg = _cfg(args)
    ds = _dataset(cfg, args)
    from .learn import (ChampionStore, WalkForward, evaluate_promotion,
                        objective_score, robust_pick)
    from .learn.search import default_space, random_search, ParamSpace
    from .learn.walkforward import stability_report

    space = ParamSpace.from_json(args.space) if args.space else default_space()
    n_samples = args.samples or int(cfg.get("learn.n_random_samples", 120))
    objective = cfg.get("learn.objective", "calmar_turnover")
    k = int(cfg.get("learn.robust_neighbourhood", 5))

    wf = WalkForward(cfg, ds, run_backtest, verbose=False)
    folds = wf.folds()
    if not folds:
        print("Not enough history for a walk-forward. Fetch more data.")
        return 1

    print(f"\nWalk-forward search: {len(folds)} folds x {n_samples} samples "
          f"over {len(space.specs)} parameters")
    print(f"objective = {objective}, robust neighbourhood k = {k}")
    print("Parameters for each test window are chosen using ONLY that fold's "
          "training window.\n")

    chosen: List[Dict[str, Any]] = []
    t0 = time.time()

    def select(train_start: str, train_end: str) -> Config:
        def ev(c: Config) -> float:
            m = wf.evaluate(c, train_start, train_end)
            m.pop("_result", None)
            return objective_score(m, objective, min_trades=max(8, int(0.4 * int(
                cfg.get("learn.promotion_min_trades", 40)))))
        res = random_search(cfg, space, ev, n_samples=n_samples,
                            seed=args.seed_search, progress=args.verbose)
        best = robust_pick(res, space, k=k)
        if best is None:
            chosen.append({})
            return cfg
        chosen.append(dict(best.params))
        return cfg.with_overrides(best.params)

    out = wf.run(select=select)
    print(f"\nSearch finished in {time.time()-t0:.0f}s")
    for f, m, p in zip(out.folds, out.fold_metrics, chosen):
        r = m.get("period_return")
        print(f"  {f.describe()}  OOS return "
              f"{'n/a' if is_na(r) else format(100*r,'+6.1f')+'%'}  trades {m.get('n_trades')}")

    print()
    print("\n".join(out.summary()))

    if not out.stitched_metrics:
        print("\nNo out-of-sample trades were produced - nothing to evaluate.")
        return 1

    print("\nStitched out-of-sample record (this is the honest number)")
    print("\n".join(summary_lines(out.stitched_metrics)))

    stab = stability_report(chosen, space.paths())
    print("\nParameter stability across folds")
    print("A parameter the search re-picks wildly every window is noise being fitted,")
    print("not a setting. Freeze those at a sensible prior instead.\n")
    for kk, v in sorted(stab.items(), key=lambda x: -(x[1]["cv"] if not is_na(x[1]["cv"]) else 0)):
        flag = "  <-- unstable" if (not is_na(v["cv"]) and v["cv"] > 0.35) else ""
        print(f"  {kk:34s} mean {v['mean']:8.3f}  cv {v['cv']:5.2f}  "
              f"range [{v['min']:g}, {v['max']:g}]{flag}")

    # The config to promote: the median of what the folds chose. Averaging over
    # folds is itself a regulariser - it refuses to bet on any single window.
    consensus: Dict[str, Any] = {}
    for p in space.paths():
        vals = [c[p] for c in chosen if isinstance(c.get(p), (int, float))]
        if not vals:
            continue
        med = median(vals)
        spec = next(s for s in space.specs if s.path == p)
        consensus[p] = int(round(med)) if spec.kind == "int" else round(med, 6)

    score = objective_score(out.stitched_metrics, objective,
                            min_trades=int(cfg.get("learn.promotion_min_trades", 40)))
    print(f"\nConsensus parameters (per-fold median), OOS objective = {score:.3f}")
    for kk, v in sorted(consensus.items()):
        print(f"  {kk:34s} {v}")

    store = ChampionStore(cfg.get("paths.state_dir", "state"))
    champ = store.load()
    decision = evaluate_promotion(cfg, champ, score, out.stitched_metrics, out.oos_trades)
    print("\nPromotion gate")
    for r in decision.reasons:
        print(f"  - {r}")
    lo, hi = decision.r_ci
    if not is_na(lo):
        print(f"  - 90% CI on out-of-sample per-trade R: [{lo:+.3f}, {hi:+.3f}]")

    audit = {"objective": objective, "score": score, "params": consensus,
             "promoted": decision.promoted, "reasons": decision.reasons,
             "n_oos_trades": len(out.oos_trades), "synthetic": ds.synthetic,
             "folds": len(out.folds),
             "oos_cagr": out.stitched_metrics.get("cagr"),
             "oos_max_dd": out.stitched_metrics.get("max_drawdown")}

    if decision.promoted and not args.dry_run:
        if ds.synthetic and not args.allow_synthetic_promotion:
            print("\nREFUSING to promote a champion fitted on synthetic data. "
                  "Pass --allow-synthetic-promotion only if you are testing the "
                  "machinery itself.")
            audit["promoted"] = False
            audit["reasons"].append("blocked: synthetic dataset")
        else:
            c = store.promote(consensus, objective, score, out.stitched_metrics,
                              note=f"{len(out.folds)} folds, {len(out.oos_trades)} OOS trades")
            print(f"\nPROMOTED champion v{c.version} -> "
                  f"{os.path.join(store.state_dir,'champion.json')}")
            print("Use it with:  swing plan --champion")
    elif args.dry_run:
        print("\n(dry run - nothing written)")
    store.audit(audit)
    print(f"Audit appended to {store.audit_path}")

    if args.journal:
        from .learn import Journal
        j = Journal(args.journal)
        j.clear()
        j.append(out.oos_trades)
        print(f"Out-of-sample trades written to {args.journal}")
    return 0


# ----------------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    cfg = _cfg(args)
    ds = _dataset(cfg, args)
    from .strategy import SwingStrategy

    feats = ds.features(cfg)
    rm = ds.regime(cfg)
    dates = ds.all_dates()
    as_of = args.date or dates[-1]
    st = SwingStrategy(cfg)
    reg = rm.at(as_of)
    equity = float(cfg.get("capital", 100000.0))

    print(f"\nScan as of {as_of}")
    print(f"Regime: {reg.label}  (exposure target {100*reg.exposure:.0f}%)  - {reg.reason}")
    if not is_na(reg.breadth):
        print(f"Breadth: {100*reg.breadth:.0f}% of the universe above its "
              f"{cfg.get('regime.breadth_ma')} DMA")
    print()

    ranked = st.rank_universe(feats, as_of, equity)
    if not ranked:
        print("Nothing passes the screen today.")
        return 0
    n = args.top or 20
    print(f"{'#':>3} {'SYMBOL':<13} {'SCORE':>6} {'CLOSE':>10} {'STOP':>10} "
          f"{'STOP%':>6} {'ATR%':>6} {'ADX':>5} {'RS':>7} {'SECTOR':<14} TRIGGER")
    for c in ranked[:n]:
        f = feats[c.symbol]
        i = f.series.pos(as_of)
        trig = st.trigger(f, i) or ""
        snap = c.snapshot
        print(f"{c.rank:>3} {c.symbol:<13} {c.score:>+6.2f} {c.close:>10.2f} "
              f"{c.stop_ref:>10.2f} {100*(c.close-c.stop_ref)/c.close:>5.1f}% "
              f"{100*(snap.get('atr_pct') or 0):>5.1f}% {(snap.get('adx') or 0):>5.1f} "
              f"{100*(snap.get('rs_index') or 0):>6.1f}% {c.sector:<14} {trig}")

    trig_n = sum(1 for c in ranked[:n]
                 if st.trigger(feats[c.symbol], feats[c.symbol].series.pos(as_of)))
    print(f"\n{len(ranked)} names pass the screen; {trig_n} of the top {n} are "
          f"triggering an entry setup today.")
    if ds.synthetic:
        print(f"\n*** {SYNTHETIC_WARNING} ***")
    return 0


# ----------------------------------------------------------------------- plan
def cmd_plan(args) -> int:
    cfg = _cfg(args)
    ds = _dataset(cfg, args)
    from .live import LivePlanner, PositionBook, render_plan_markdown, write_plan_csv

    state_dir = cfg.get("paths.state_dir", "state")
    book = PositionBook.load(os.path.join(state_dir, "positions.json"),
                             float(cfg.get("capital", 100000.0)))
    feats = ds.features(cfg)
    rm = ds.regime(cfg)
    as_of = args.date or ds.all_dates()[-1]

    entry_filter = None
    if bool(cfg.get("learn.meta_label.enabled", False)):
        from .learn.metalabel import MetaLabeler
        ml = MetaLabeler.load(os.path.join(state_dir, "metalabel.json"))
        if ml and ml.weights:
            entry_filter = ml.as_entry_filter(float(cfg.get("learn.meta_label.threshold", 0.5)))
            print(f"  meta-label filter active (trained on {ml.n_train} trades)")

    planner = LivePlanner(cfg, feats, rm, entry_filter=entry_filter)
    plan = planner.plan(book, as_of)

    _, _, names = load_universe(cfg.get("universe.file"))
    md = render_plan_markdown(plan, cfg, names, _data_note(ds))
    orders_dir = cfg.get("paths.orders_dir", "orders")
    os.makedirs(orders_dir, exist_ok=True)
    md_path = os.path.join(orders_dir, f"{as_of}.md")
    csv_path = os.path.join(orders_dir, f"{as_of}.csv")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    write_plan_csv(plan, csv_path)

    print()
    print(md)
    print(f"\nWritten to {md_path} and {csv_path}")
    if ds.synthetic:
        print(f"\n*** {SYNTHETIC_WARNING} ***")
    return 0


# ------------------------------------------------------------------- position
def cmd_position(args) -> int:
    cfg = _cfg(args)
    from .live import LivePosition, PositionBook

    state_dir = cfg.get("paths.state_dir", "state")
    path = os.path.join(state_dir, "positions.json")
    book = PositionBook.load(path, float(cfg.get("capital", 100000.0)))
    _, sectors, _ = load_universe(cfg.get("universe.file"))

    if args.pos_cmd == "list":
        print(f"Cash: Rs {fmt_inr(book.cash)}   Positions: {len(book.positions)}"
              f"   Halted: {book.halted}")
        if not book.positions:
            print("(flat)")
            return 0
        print(f"\n{'SYMBOL':<13} {'QTY':>6} {'ENTRY':>10} {'STOP':>10} {'RISK':>9} "
              f"{'DATE':<12} SETUP")
        for p in book.positions:
            risk = (p.entry_price - p.stop) * p.qty
            print(f"{p.symbol:<13} {p.qty:>6} {p.entry_price:>10.2f} {p.stop:>10.2f} "
                  f"{risk:>9,.0f} {p.entry_date:<12} {p.setup}")
        total_risk = sum(max(0.0, (p.entry_price - p.stop)) * p.qty for p in book.positions)
        print(f"\nOpen risk if every stop fills: Rs {fmt_inr(total_risk)} "
              f"({100*total_risk/max(book.capital_base,1):.1f}% of base capital)")
        return 0

    if args.pos_cmd == "add":
        if book.get(args.symbol):
            print(f"{args.symbol} is already open. Close it first or edit {path}.")
            return 1
        stop = args.stop
        if stop is None:
            ds = _dataset(cfg, args)
            feats = ds.features(cfg)
            f = feats.get(args.symbol)
            if f is None:
                print(f"No price data for {args.symbol}; pass --stop explicitly.")
                return 1
            i = f.series.pos(args.date) if args.date else len(f.series) - 1
            if i is None:
                i = len(f.series) - 1
            from .rules import initial_stop
            stop = initial_stop(args.price, f.get("atr", i), f.get("structure_low", i), cfg)
            print(f"  derived stop {stop:.2f} "
                  f"({100*(args.price-stop)/args.price:.1f}% below entry)")
        cost = args.qty * args.price
        book.positions.append(LivePosition(
            symbol=args.symbol, sector=sectors.get(args.symbol, "Other"),
            qty=args.qty, entry_price=args.price, entry_date=args.date,
            stop=stop, initial_stop=stop, setup=args.setup or "",
            risk_per_share=args.price - stop, note=args.note or ""))
        book.cash -= cost
        book.save(path)
        print(f"Added {args.qty} {args.symbol} @ {args.price:.2f} "
              f"(Rs {fmt_inr(cost)}), stop {stop:.2f}, "
              f"risking Rs {fmt_inr((args.price-stop)*args.qty)}")
        print(f"Cash now Rs {fmt_inr(book.cash)}")
        return 0

    if args.pos_cmd == "close":
        p = book.get(args.symbol)
        if p is None:
            print(f"{args.symbol} is not open.")
            return 1
        cm = CostModel(cfg)
        charges = cm.charges(args.price, p.qty, "SELL").total
        proceeds = args.price * p.qty - charges
        gross = (args.price - p.entry_price) * p.qty
        rps = p.risk_per_share or max(p.entry_price - p.initial_stop, 1e-9)
        r = (args.price - p.entry_price) / rps
        book.cash += proceeds
        book.positions = [x for x in book.positions if x.symbol != args.symbol]
        book.save(path)

        from .learn import Journal
        j = Journal(os.path.join(state_dir, "journal.csv"))
        row = {"symbol": p.symbol, "sector": p.sector, "setup": p.setup,
               "entry_date": p.entry_date, "exit_date": args.date,
               "entry_price": p.entry_price, "exit_price": args.price, "qty": p.qty,
               "initial_stop": p.initial_stop, "risk_per_share": rps,
               "gross_pnl": gross, "costs": charges, "net_pnl": gross - charges,
               "r_multiple": r, "exit_reason": args.reason or "manual",
               "regime_at_entry": "", "entry_rank": 0, "entry_score": 0,
               "bars_held": 0, "mae_r": NA, "mfe_r": NA, "live": 1}
        for k, v in (p.entry_snapshot or {}).items():
            row[f"f_{k}"] = v
        j.append([row])
        print(f"Closed {p.qty} {args.symbol} @ {args.price:.2f}: "
              f"gross Rs {fmt_inr(gross)}, costs Rs {fmt_inr(charges)}, "
              f"net Rs {fmt_inr(gross-charges)} ({r:+.2f}R)")
        print(f"Cash now Rs {fmt_inr(book.cash)}; journalled to {j.path}")
        return 0

    if args.pos_cmd == "stop":
        p = book.get(args.symbol)
        if p is None:
            print(f"{args.symbol} is not open.")
            return 1
        if args.price < p.stop:
            print(f"Refusing to LOWER a stop ({p.stop:.2f} -> {args.price:.2f}). "
                  "Stops ratchet up only - that rule is the system.")
            return 1
        p.stop = args.price
        book.save(path)
        print(f"{args.symbol} stop raised to {args.price:.2f}")
        return 0

    if args.pos_cmd == "cash":
        book.cash = args.amount
        book.save(path)
        print(f"Cash set to Rs {fmt_inr(book.cash)}")
        return 0

    return 1


# -------------------------------------------------------------------- journal
def cmd_journal(args) -> int:
    cfg = _cfg(args)
    from .learn import Journal, feature_attribution, edge_decay_check
    from .learn.journal import attribution_suggestions

    state_dir = cfg.get("paths.state_dir", "state")
    j = Journal(args.file or os.path.join(state_dir, "journal.csv"))
    rows, _ = j.read()
    if not rows:
        print(f"No trades in {j.path}.\n"
              "Populate it from a backtest (swing backtest --journal state/journal.csv) "
              "or from live closes (swing position close ...).")
        return 1

    live = [r for r in rows if r.get("live")]
    print(f"{len(rows)} trades in {j.path} ({len(live)} recorded live)\n")

    rs = [r["r_multiple"] for r in rows if isinstance(r.get("r_multiple"), (int, float))]
    wins = [x for x in rs if x > 0]
    print(f"Expectancy   : {mean(rs):+.3f} R over {len(rs)} trades")
    print(f"Win rate     : {100*len(wins)/len(rs):.1f}%")
    print(f"Median R     : {median(rs):+.3f}")
    net = [r.get("net_pnl") for r in rows if isinstance(r.get("net_pnl"), (int, float))]
    if net:
        print(f"Net P&L      : Rs {fmt_inr(sum(net))}")
    costs = [r.get("costs") for r in rows if isinstance(r.get("costs"), (int, float))]
    if costs:
        print(f"Costs paid   : Rs {fmt_inr(sum(costs))} "
              f"(Rs {fmt_inr(sum(costs)/len(costs))} per trade)")

    if args.decay and live:
        base = [r for r in rows if not r.get("live")]
        rep = edge_decay_check(live, base or rows,
                               window=int(cfg.get("learn.decay_window_trades", 60)),
                               alert_pct=float(cfg.get("learn.decay_alert_pct", 5.0)))
        print(f"\nEdge decay check\n  {rep.message}")
        if rep.alert:
            print("  ACTION: halve risk.risk_per_trade and re-run `swing learn` "
                  "before restoring size.")

    attr = feature_attribution(rows)
    if attr:
        print("\nWhat the entry conditions were worth (realised R by quartile)")
        print("Treat these as hypotheses to test, never as instructions to follow.\n")
        sug = attribution_suggestions(attr)
        if sug:
            for s in sug[: args.top or 8]:
                print(f"  - {s}")
        else:
            print("  Nothing stands out beyond noise at this sample size.")
        if len(rows) < 200:
            print(f"\n  (only {len(rows)} trades - at this size most apparent patterns "
                  "are noise. Wait for 200+ before acting on any of it.)")
    return 0


# ---------------------------------------------------------------------- costs
def cmd_costs(args) -> int:
    cfg = _cfg(args)
    cm = CostModel(cfg)
    capital = float(cfg.get("capital", 100000.0))
    print("\nGroww cash-delivery frictions, as configured\n")
    print(f"{'Position':>14} {'Round trip':>11} {'Cost':>11}  {'Break-even move':>16}")
    for n in (4, 5, 6, 8, 10, 12):
        pv = capital / n
        rt = cm.round_trip_pct(pv)
        print(f"{fmt_inr(pv):>14} {100*rt:>10.2f}% {fmt_inr(rt*pv):>11}  {100*rt:>15.2f}%")
    print(f"\n(at {fmt_inr(capital)} capital, split into N positions)")

    pv = capital / int(cfg.get("risk.max_positions", 6))
    rt = cm.round_trip_pct(pv)
    risk_rs = capital * float(cfg.get("risk.risk_per_trade", 0.012))
    print(f"\nAt your configured {cfg.get('risk.max_positions')} positions:")
    print(f"  position size      Rs {fmt_inr(pv)}")
    print(f"  round trip         {100*rt:.2f}%  (Rs {fmt_inr(rt*pv)})")
    print(f"  risk per trade     Rs {fmt_inr(risk_rs)}  (1R)")
    print(f"  costs as a fraction of 1R: {rt*pv/risk_rs:.2f}R")
    print(f"\n  Every round trip starts {rt*pv/risk_rs:.2f}R in the hole. That is the")
    print( "  number that decides how often you can afford to trade - not the win rate.")
    for turns in (1, 2, 4):
        annual = turns * 12 * int(cfg.get("risk.max_positions", 6)) * rt * pv
        print(f"  turning the book over {turns}x/month costs Rs {fmt_inr(annual)}/yr "
              f"= {100*annual/capital:.1f}% of capital")
    return 0


# --------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    problems, warnings = [], []
    print("\nChecking configuration and data\n")

    uni = cfg.get("universe.file")
    if not os.path.exists(uni):
        problems.append(f"universe file missing: {uni}")
    else:
        symbols, _, _ = load_universe(uni)
        print(f"  universe file      {len(symbols)} symbols")

    from .data.store import DataStore
    store = DataStore(cfg.get("data.cache_dir", "data_cache"))
    cov = store.coverage()
    if not cov:
        warnings.append(f"no cached price data in {store.cache_dir} - run `swing fetch`")
    else:
        starts = [v[0] for v in cov.values()]
        ends = [v[1] for v in cov.values()]
        print(f"  price cache        {len(cov)} symbols, {min(starts)} .. {max(ends)}")
        stale = [s for s, v in cov.items() if v[1] < max(ends)]
        if len(stale) > len(cov) * 0.1:
            warnings.append(f"{len(stale)} symbols are not updated to {max(ends)} - "
                            "re-run `swing fetch`")
        idx = cfg.get("regime.index_symbol")
        if idx not in cov:
            problems.append(f"index series '{idx}' is not cached - the regime filter, "
                            "which is the main drawdown control, will be DISABLED")

    # risk arithmetic
    n = int(cfg.get("risk.max_positions", 6))
    rpt = float(cfg.get("risk.risk_per_trade", 0.012))
    heat = float(cfg.get("risk.max_portfolio_heat", 0.075))
    if n * rpt > heat + 1e-9:
        warnings.append(
            f"max_positions x risk_per_trade = {100*n*rpt:.1f}% exceeds "
            f"max_portfolio_heat {100*heat:.1f}% - the heat cap will silently stop you "
            f"reaching {n} positions")
    frac = float(cfg.get("risk.max_position_frac", 0.25))
    if n * frac < 1.0:
        warnings.append(
            f"max_positions x max_position_frac = {100*n*frac:.0f}% of equity, so the "
            "book can never be fully invested even in a risk-on regime")

    cm = CostModel(cfg)
    cap = float(cfg.get("capital", 100000.0))
    pv = cap / max(n, 1)
    rt_cost = cm.round_trip_pct(pv) * pv
    r_rs = cap * rpt
    if rt_cost > 0.15 * r_rs:
        warnings.append(
            f"a round trip costs Rs {fmt_inr(rt_cost)} = {rt_cost/r_rs:.2f}R. Above ~0.15R "
            "the strategy is paying the broker a large share of its edge; hold longer "
            "or use fewer, larger positions")
    minv = float(cfg.get("risk.min_position_value", 8000.0))
    if minv > pv:
        problems.append(f"min_position_value Rs {fmt_inr(minv)} exceeds the natural "
                        f"position size Rs {fmt_inr(pv)} - every entry will be refused")

    if float(cfg.get("regime.exposure", {}).get("risk_off", 0.0)) > 0.5:
        warnings.append("regime.exposure.risk_off is above 50% - you are keeping most "
                        "of the book on through bear markets, which is where long-only "
                        "cash systems do their real damage")

    state_dir = cfg.get("paths.state_dir", "state")
    champ_p = os.path.join(state_dir, "champion.json")
    if os.path.exists(champ_p):
        from .learn import ChampionStore
        c = ChampionStore(state_dir).load()
        print(f"  champion           v{c.version} promoted {c.created_utc} "
              f"(score {c.score:.3f})")
    else:
        print("  champion           none promoted yet (using base config)")

    print()
    for p in problems:
        print(f"  PROBLEM  {p}")
    for w in warnings:
        print(f"  warning  {w}")
    if not problems and not warnings:
        print("  Everything checks out.")
    print()
    return 1 if problems else 0


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swing", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to a JSONC config (default: built-in defaults)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override any config key, e.g. --set risk.risk_per_trade=0.008")
    p.add_argument("--capital", type=float, help="starting capital in rupees")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, synthetic=True):
        if synthetic:
            sp.add_argument("--synthetic", action="store_true",
                            help="use generated data (pipeline demo; NOT real results)")
            sp.add_argument("--seed", type=int, default=14, help="synthetic data seed")
        sp.add_argument("--champion", action="store_true",
                        help="overlay the promoted champion parameters")

    sp = sub.add_parser("fetch", help="download price history")
    sp.add_argument("--start", default="2014-01-01")
    sp.add_argument("--end", default="")
    sp.add_argument("--provider", default="", help="yfinance | csvdir:/path")
    sp.add_argument("--symbols", nargs="*", help="only these symbols")
    sp.add_argument("--full", action="store_true",
                    help="re-download everything instead of just the new bars")
    sp.set_defaults(func=cmd_fetch, champion=False)

    sp = sub.add_parser("validate-data",
                        help="check cached data for defects that corrupt backtests")
    sp.add_argument("--max-warnings", type=int, default=25)
    sp.add_argument("--write-exclusions", metavar="PATH",
                    help="write failing symbols to a file")
    sp.set_defaults(func=cmd_validate, champion=False)

    sp = sub.add_parser("bundle", help="export/import the price cache as one file")
    bsub = sp.add_subparsers(dest="bundle_cmd", required=True)
    be = bsub.add_parser("export", help="write the cache to a .tar.gz")
    be.add_argument("path")
    bi = bsub.add_parser("import", help="load a .tar.gz written by `bundle export`")
    bi.add_argument("path")
    sp.set_defaults(func=cmd_bundle, champion=False)

    sp = sub.add_parser("universe", help="show or check the universe")
    sp.add_argument("--check", action="store_true", help="compare against the price cache")
    sp.set_defaults(func=cmd_universe)

    sp = sub.add_parser("backtest", help="run one backtest")
    common(sp)
    sp.add_argument("--start", default="")
    sp.add_argument("--end", default="")
    sp.add_argument("--report", help="write an HTML report here")
    sp.add_argument("--title", default="")
    sp.add_argument("--journal", help="write the trade list to this CSV")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("walkforward",
                        help="evaluate the current config out of sample")
    common(sp)
    sp.set_defaults(func=cmd_walkforward)

    sp = sub.add_parser("learn", help="search parameters per fold and maybe promote")
    common(sp)
    sp.add_argument("--samples", type=int, help="parameter samples per fold")
    sp.add_argument("--space", help="JSONC search-space file")
    sp.add_argument("--seed-search", type=int, default=11)
    sp.add_argument("--dry-run", action="store_true", help="never write a champion")
    sp.add_argument("--allow-synthetic-promotion", action="store_true",
                    help=argparse.SUPPRESS)
    sp.add_argument("--journal", help="write out-of-sample trades to this CSV")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("scan", help="rank the universe")
    common(sp)
    sp.add_argument("--date", help="as-of date (default: latest cached session)")
    sp.add_argument("--top", type=int, default=20)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("plan", help="write tomorrow's order sheet")
    common(sp)
    sp.add_argument("--date", help="as-of date (default: latest cached session)")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("position", help="record fills and closes")
    psub = sp.add_subparsers(dest="pos_cmd", required=True)
    psub.add_parser("list", help="show the open book")
    a = psub.add_parser("add", help="record a buy fill")
    a.add_argument("symbol")
    a.add_argument("--qty", type=int, required=True)
    a.add_argument("--price", type=float, required=True)
    a.add_argument("--date", required=True)
    a.add_argument("--stop", type=float, help="omit to derive it from ATR/structure")
    a.add_argument("--setup", default="")
    a.add_argument("--note", default="")
    a.add_argument("--synthetic", action="store_true", help=argparse.SUPPRESS)
    a.add_argument("--seed", type=int, default=14, help=argparse.SUPPRESS)
    c = psub.add_parser("close", help="record a sell fill")
    c.add_argument("symbol")
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--date", required=True)
    c.add_argument("--reason", default="manual")
    s2 = psub.add_parser("stop", help="raise a stop")
    s2.add_argument("symbol")
    s2.add_argument("--price", type=float, required=True)
    ca = psub.add_parser("cash", help="set the cash balance")
    ca.add_argument("amount", type=float)
    sp.set_defaults(func=cmd_position, champion=False)

    sp = sub.add_parser("journal", help="attribution and edge-decay report")
    sp.add_argument("--file", help="journal CSV (default: state/journal.csv)")
    sp.add_argument("--decay", action="store_true", help="run the edge-decay check")
    sp.add_argument("--top", type=int, default=8)
    sp.set_defaults(func=cmd_journal, champion=False)

    sp = sub.add_parser("costs", help="what frictions cost at your size")
    sp.set_defaults(func=cmd_costs, champion=False)

    sp = sub.add_parser("doctor", help="check the setup for problems")
    sp.set_defaults(func=cmd_doctor, champion=False)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
