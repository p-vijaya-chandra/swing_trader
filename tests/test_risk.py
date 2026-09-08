"""Position sizing, risk caps, and the drawdown circuit breaker."""
import pytest

from swingtrader.portfolio import Position, RiskManager


def mkpos(sym, sector="IT", entry=100.0, stop=95.0, qty=10):
    return Position(symbol=sym, sector=sector, qty=qty, entry_price=entry,
                    entry_date="2020-01-01", setup="breakout", initial_stop=stop,
                    stop=stop, risk_per_share=entry - stop,
                    initial_risk_value=(entry - stop) * qty,
                    highest_high=entry, lowest_low=entry, original_qty=qty)


def test_size_risks_the_configured_fraction(cfg):
    rm = RiskManager(cfg)
    qty, why = rm.size(100000, 100000, 500.0, 470.0, {}, "IT", 0.0, 100000)
    assert why == "ok"
    # 1.2% of 100000 = 1200 risk budget / 30 per share = 40 shares
    assert qty == 40
    assert (500.0 - 470.0) * qty == pytest.approx(1200, abs=30)


def test_position_value_cap_binds(cfg):
    rm = RiskManager(cfg)
    # a very tight stop would otherwise buy a huge position
    qty, _ = rm.size(100000, 100000, 100.0, 99.9, {}, "IT", 0.0, 100000)
    assert qty * 100.0 <= 100000 * cfg.get("risk.max_position_frac") + 1e-6


def test_sector_cap_blocks_a_fourth_name(cfg):
    rm = RiskManager(cfg)
    held = {f"S{i}": mkpos(f"S{i}", "Financials") for i in range(3)}
    qty, why = rm.size(100000, 100000, 500.0, 470.0, held, "Financials", 0.0, 100000)
    assert qty == 0 and why == "sector_cap"


def test_max_positions_blocks(cfg):
    rm = RiskManager(cfg)
    held = {f"S{i}": mkpos(f"S{i}", f"Sec{i}") for i in range(rm.max_positions)}
    qty, why = rm.size(100000, 100000, 500.0, 470.0, held, "New", 0.0, 100000)
    assert qty == 0 and why == "max_positions"


def test_portfolio_heat_cap_binds(cfg):
    rm = RiskManager(cfg)
    # three positions each risking 3% of equity = 9% heat, over the 7.5% cap
    held = {f"S{i}": mkpos(f"S{i}", f"Sec{i}", entry=100.0, stop=70.0, qty=100)
            for i in range(3)}
    qty, why = rm.size(100000, 100000, 500.0, 470.0, held, "New", 0.0, 100000)
    assert qty == 0 and why == "heat_cap"


def test_exposure_cap_from_regime_binds(cfg):
    rm = RiskManager(cfg)
    qty, why = rm.size(100000, 100000, 500.0, 470.0, {}, "IT", 0.0, exposure_cap=0.0)
    assert qty == 0 and why == "exposure_cap"


def test_min_position_value_refuses_dust(cfg):
    """A wide stop gives a small quantity; if the resulting position is worth
    less than min_position_value the flat DP fee and Rs 20 brokerage eat the
    whole expected edge, so the trade is refused rather than sized down."""
    rm = RiskManager(cfg)
    qty, why = rm.size(100000, 100000, 500.0, 200.0, {}, "IT", 0.0, 100000)
    assert why == "below_min_value", f"got qty={qty} why={why}"
    assert qty == 0


def test_risk_too_small_is_reported_separately(cfg):
    """A stop so wide that even one share exceeds the risk budget."""
    rm = RiskManager(cfg)
    qty, why = rm.size(100000, 100000, 5000.0, 2000.0, {}, "IT", 0.0, 100000)
    assert qty == 0 and why == "risk_too_small"


def test_no_cash_refuses(cfg):
    rm = RiskManager(cfg)
    qty, why = rm.size(100000, 100.0, 500.0, 470.0, {}, "IT", 0.0, 100000)
    assert qty == 0 and why == "no_cash"


def test_risk_is_halved_inside_a_drawdown(cfg):
    rm = RiskManager(cfg)
    assert rm.risk_fraction(0.0) == pytest.approx(cfg.get("risk.risk_per_trade"))
    assert rm.risk_fraction(-0.12) == pytest.approx(
        cfg.get("risk.risk_per_trade") * cfg.get("risk.derisk_factor"))


class TestCircuitBreaker:
    """The halt used to be a one-way trapdoor: stop trading at -20%, equity
    flatlines, the all-time peak never updates, so the drawdown never recovers
    and the system is switched off forever. These tests pin the fix."""

    def test_halts_past_the_threshold(self, cfg):
        rm = RiskManager(cfg)
        assert rm.halt_state(-0.21, currently_halted=False) is True
        assert rm.halt_state(-0.19, currently_halted=False) is False

    def test_stays_halted_while_still_deep(self, cfg):
        rm = RiskManager(cfg)
        assert rm.halt_state(-0.18, currently_halted=True, days_halted=5,
                             regime_risk_on=True) is True

    def test_resumes_when_the_drawdown_recovers(self, cfg):
        rm = RiskManager(cfg)
        assert rm.halt_state(-0.05, currently_halted=True, days_halted=1,
                             regime_risk_on=False) is False

    def test_resumes_after_cooldown_once_regime_is_risk_on(self, cfg):
        """The escape hatch that makes the resume condition reachable at all."""
        rm = RiskManager(cfg)
        deep = -0.30
        assert rm.halt_state(deep, True, days_halted=5, regime_risk_on=True) is True
        assert rm.halt_state(deep, True, days_halted=100, regime_risk_on=False) is True
        assert rm.halt_state(deep, True, days_halted=100, regime_risk_on=True) is False

    def test_probation_halves_size_after_a_restart(self, cfg):
        rm = RiskManager(cfg)
        assert rm.probation_scale(0) == pytest.approx(cfg.get("risk.post_halt_risk_factor"))
        assert rm.probation_scale(10) == pytest.approx(cfg.get("risk.post_halt_risk_factor"))
        assert rm.probation_scale(999) == pytest.approx(1.0)


def test_engine_keeps_trading_after_a_deep_drawdown(cfg, dataset):
    """Regression test for the trapdoor: the system must still be taking trades
    in the final third of the backtest, not silently dead since 2018."""
    from swingtrader.runtime import run_backtest
    res = run_backtest(cfg, dataset, start="2016-01-01")
    assert res.trades, "no trades at all"
    dates = [d.date for d in res.daily]
    cutoff = dates[int(len(dates) * 0.66)]
    late = [t for t in res.trades if t.exit_date > cutoff]
    assert late, (f"no trades after {cutoff} - the drawdown halt has trapped the "
                  f"system again (halt_days={res.halt_days}/{len(res.daily)})")
    assert res.halt_days < len(res.daily) * 0.5
