"""Regression tests for the sixth audit, with valid execution timestamps."""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import importlib.util
from pathlib import Path

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, StrategyId, TraderId, Venue
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import LimitOrder
from contracts import Fill, OrderSide as LedgerSide
from engine.order_gateway import AShareOrderGateway
from engine.golden_ledger import GoldenLedger

D = Decimal

def test_failed_sell_lot_invariant_is_atomic():
    ledger=GoldenLedger(D(1000));ledger.positions['X']=D(100);ledger.tradable_positions['X']=D(100)
    ledger.lots['X']=[('2025-05-05',D(99))]
    from copy import deepcopy
    before=deepcopy(ledger.__dict__)
    fill=Fill(fill_id='bad-lot',symbol='X',side=LedgerSide.SELL,quantity=D(100),price=D(10),fee=D(5),timestamp=datetime(2025,5,6,tzinfo=ZoneInfo('Asia/Shanghai')))
    with pytest.raises(RuntimeError):ledger.apply_fill(fill,'2025-05-06')
    assert ledger.__dict__==before

def test_sell_preserves_locked_lot_when_listing_lot_appended_later():
    ledger=GoldenLedger(D(1000));ledger.positions['X']=D(200);ledger.tradable_positions['X']=D(100)
    ledger.lots['X']=[('2025-05-06',D(100)),('2025-05-05',D(100))]
    fill=Fill(fill_id='settled-only',symbol='X',side=LedgerSide.SELL,quantity=D(100),price=D(10),fee=D(5),timestamp=datetime(2025,5,6,tzinfo=ZoneInfo('Asia/Shanghai')))
    ledger.apply_fill(fill,'2025-05-06')
    assert ledger.lots['X']==[('2025-05-06',D(100))]
RULE = dict(trade_status="OPEN", limit_state="BOUNDED", up_limit=110., down_limit=90.,
            buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000)
BAR = dict(close=100., volume=10000.)

def stamp(s="2025-05-06 10:00:00"):
    return int(datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()*1e9)

def order(t=None, tif=TimeInForce.DAY, price="100.00"):
    return LimitOrder(trader_id=TraderId("TRADER-001"), strategy_id=StrategyId("S-001"),
                     instrument_id=InstrumentId.from_str("600519.SSE"), client_order_id=ClientOrderId("O"),
                     order_side=OrderSide.BUY, quantity=Quantity.from_int(100), price=Price.from_str(price),
                     time_in_force=tif, init_id=UUID4(), ts_init=stamp() if t is None else t)

@pytest.mark.parametrize("tif", [TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK])
def test_all_order_types_obey_close_without_optional_bar_timestamp(tif):
    result = AShareOrderGateway().validate_and_reserve(order(stamp("2025-05-06 15:00:00"), tif), RULE, BAR, 100)
    assert not result.is_valid

def test_missing_time_and_unknown_phase_fail_closed():
    g = AShareOrderGateway()
    assert not g.validate_and_reserve(order(0), RULE, BAR, 100).is_valid
    assert not g.validate_and_reserve(order(price="109.00"), dict(RULE, session_phase="CONTINUOS"), BAR, 100).is_valid
    assert g._active_orders == {}

def test_unbounded_closing_auction_range_and_bounded_auction():
    t = stamp("2025-05-06 14:58:00")
    g = AShareOrderGateway()
    rule = dict(RULE, limit_state="UNBOUNDED", session_phase="CLOSING_AUCTION")
    assert not g.validate_and_reserve(order(t, price="150.00"), rule, BAR, 100).is_valid
    assert g.validate_and_reserve(order(t, price="109.00"), dict(RULE, session_phase="CLOSING_AUCTION"), BAR, 100).is_valid

def test_complete_instrument_identity_checked_before_state_change():
    g = AShareOrderGateway()
    g.validate_and_reserve(order(), RULE, BAR, 100)
    event = SimpleNamespace(trade_id="T", client_order_id=ClientOrderId("O"),
                            instrument_id=InstrumentId.from_str("600519.SZSE"), order_side=OrderSide.BUY,
                            ts_event=stamp(), last_qty=Quantity.from_int(100))
    with pytest.raises(RuntimeError, match="[Ii]nstrument"):
        g.on_order_filled(event)
    assert not g._processed_trade_ids and not g._bought_lots

def small_ledger():
    l = GoldenLedger(D(10000))
    l.apply_fill(Fill(fill_id="F", symbol="600519", side=LedgerSide.BUY, quantity=D(105),
                      price=D(10), fee=D(0), timestamp=datetime.fromtimestamp(stamp()/1e9, ZoneInfo("Asia/Shanghai"))), "2025-05-06")
    return l

@pytest.mark.parametrize("px", [D(-10), D(0), D("Infinity"), D("NaN")])
def test_invalid_fractional_price_rejected_without_mutation(px):
    l = small_ledger()
    before = (l.cash, dict(l.positions), dict(l.lots))
    with pytest.raises(ValueError):
        l.apply_stock_split("600519", D("1.5"), "2025-05-07", fractional_policy="cash_in_lieu", fractional_cash_price=px)
    assert before == (l.cash, l.positions, l.lots)

def test_split_cannot_use_future_valuation():
    l = small_ledger()
    l.mark_to_market("2025-05-08", {"600519": D(30)})
    before = (l.cash, dict(l.positions))
    with pytest.raises(ValueError):
        l.apply_stock_split("600519", D("1.5"), "2025-05-07", fractional_policy="cash_in_lieu")
    assert before == (l.cash, l.positions)

def test_existing_corporate_e2e_compares_actual_native_cash(monkeypatch):
    path = Path(__file__).with_name("test_corporate_actions.py")
    spec = importlib.util.spec_from_file_location("observed_corporate_e2e", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = module.BacktestEngine
    engines = []
    def observe(*a, **kw):
        e = factory(*a, **kw); engines.append(e); return e
    monkeypatch.setattr(module, "BacktestEngine", observe)
    try:
        module.test_corporate_action_e2e_engine_split_and_reconciliation()
        actual = float(engines[0].portfolio.account(Venue("SSE")).balances_total()[CNY])
        assert actual == 100200., "cash dividend must reach the actual native account"
    finally:
        for e in engines: e.dispose()
