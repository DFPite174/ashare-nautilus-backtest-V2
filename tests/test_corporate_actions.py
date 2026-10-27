"""
公司行为 (Corporate Actions) 全链路集成与会计核验测试
全面覆盖审计项 N06, N07, N08, N09：
1. [N06] 新增股份上市日 (share_listing_date)：除权日扩大持仓，但送转股份严格在上市日才解冻为可卖。
2. [N07] 除息至派息期间应收分红 (Receivable Dividends) 机制：确保除权除息价格下跌时总 NAV 经济价值严格守恒不虚降。
3. [N08] 严格登记日权益快照防护：声明登记日的公司行为绝不静默按派息日现存仓位派发。
4. [N09] 拆股整数股规整 (ROUND_FLOOR) 与历史批次 lots 精确守恒；Cython 原生 split_native 结合 NautilusTrader Position 状态联动。
"""

import pytest
from datetime import date, datetime
from decimal import Decimal

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs

from engine.split_guard import load_bridge
from engine.golden_ledger import GoldenLedger
from contracts import CorporateAction, Fill, OrderSide as ContractOrderSide


def test_split_native_loading_and_execution():
    """
    【R09 & N09】验证 Cython 原生拆股扩展加载及对原生 Position 的安全调整
    """
    bridge = load_bridge()
    assert hasattr(bridge, "apply_split"), "split_native 应提供 apply_split 接口"

    inst = TestInstrumentProvider.equity()
    order = TestExecStubs.market_order(instrument=inst)
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=inst,
        last_qty=Quantity.from_int(100),
        last_px=Price.from_str("100.00"),
        position_id=PositionId("P-001"),
    )
    pos = Position(inst, fill)
    assert float(pos.quantity) == 100.0
    assert pos.avg_px_open == 100.0

    # 执行 10 送 10 (拆股比例 2.0)
    split_event_time = pos.ts_last + 1000
    res = bridge.apply_split(pos, 2.0, split_event_time, inst)

    assert res["ratio"] == "2.0"
    # 持仓股数应由 100 变为 200
    assert float(pos.quantity) == 200.0
    assert pos.signed_qty == 200.0
    # 开仓均价应由 100.0 调整为 50.0
    assert pos.avg_px_open == 50.0
    # 经济价值守恒：200 股 * 50.0 元 = 100 股 * 100.0 元 = 10,000 元
    assert float(pos.quantity) * pos.avg_px_open == 10000.0


def test_corporate_action_share_listing_date_delays_tradable():
    """
    【N06 验证】新增送转股份上市日 (share_listing_date) 控制：
    除权日增加总持仓，但送转新增股份必须等到 share_listing_date 当天开盘才解冻为可卖持仓！
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-ca-list-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册 10 送 10，除权日 2025-05-08，但新增股份上市日为 2025-05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 12),
        bonus_ratio=Decimal("1.0"),
        share_listing_date=date(2025, 5, 12),
    )
    ledger.register_corporate_action(ca)

    # 1. 2025-05-07 登记日：原 1000 股转为可卖
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")
    assert ledger.positions["600519"] == Decimal("1000.0")
    assert ledger.tradable_positions["600519"] == Decimal("1000.0")

    # 2. 2025-05-08 除权日：总持仓变为 2000 股，但可卖持仓必须依然是 1000 股 (新增 1000 股未上市)
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")
    assert ledger.positions["600519"] == Decimal("2000.0"), "总持仓应在除权日转增至 2000 股"
    assert ledger.tradable_positions["600519"] == Decimal("1000.0"), "新增股份尚未上市，可卖持仓严禁提前释放"

    # 3. 2025-05-12 上市日：新增 1000 股正式解冻，可卖持仓转为 2000 股
    ledger.on_trade_day_start("2025-05-12")
    ledger.process_corporate_actions_for_date("2025-05-12")
    assert ledger.tradable_positions["600519"] == Decimal("2000.0"), "新增股份上市日应当正式解冻为可卖"


def test_corporate_action_receivable_dividend_nav_conservation():
    """
    【N07 验证】应收分红会计机制与总 NAV 平滑守恒：
    除息日股价从 100 元下跌至 98 元 (每股派 2 元)，在派现日现金到账前，
    应收分红计入 NAV，杜绝除息日净值虚假塌陷。
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-ca-div-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册派现 2 元，登记日 05-07，除息日 05-08，派息日 05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 12),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # 1. 2025-05-07 登记日
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")
    nav_d2 = ledger.mark_to_market("2025-05-07", {"600519": Decimal("100.0")})
    # NAV = 现金 399970 + 股票 100000 = 499970
    assert nav_d2.total_equity == Decimal("499970.00")

    # 2. 2025-05-08 除息日：收盘价除息至 98.0 元
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")
    nav_d3 = ledger.mark_to_market("2025-05-08", {"600519": Decimal("98.0")})

    # 关键断言 (N07)：市值 98000 + 现金 399970 + 应收分红 2000 = 499970.00 (NAV 严格守恒，绝不虚降 2000 元！)
    assert nav_d3.receivable_dividends == Decimal("2000.00")
    assert nav_d3.total_equity == Decimal("499970.00"), "除息日至派息日期间 NAV 必须保持权益守恒"

    # 3. 2025-05-12 派息日：现金 2000 元到账，应收分红结清
    ledger.on_trade_day_start("2025-05-12")
    ledger.process_corporate_actions_for_date("2025-05-12")
    nav_d4 = ledger.mark_to_market("2025-05-12", {"600519": Decimal("98.0")})
    assert nav_d4.receivable_dividends == Decimal("0.00")
    assert nav_d4.cash == Decimal("399970.00") + Decimal("2000.00")
    assert nav_d4.total_equity == Decimal("499970.00")


def test_corporate_action_missing_record_date_entitlement_fails():
    """
    【N08 验证】声明了登记日的公司行为，若快照丢失或漏调度，派息日严禁静默回退
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 9),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # 故意跳过 05-07 登记日调度，直接在 05-08 除息日触发
    ledger.on_trade_day_start("2025-05-08")
    with pytest.raises(RuntimeError, match="Missing entitlement snapshot"):
        ledger.process_corporate_actions_for_date("2025-05-08")


def test_corporate_action_integer_quantization_and_lot_conservation():
    """
    【N09 验证】拆股必须按整数股向下取整 (ROUND_FLOOR)，杜绝产生小数股并保持批次守恒
    """
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    ledger.on_trade_day_start("2025-05-06")

    # 买入两笔，各 1 股
    f1 = Fill(fill_id="f1", symbol="600519", side=ContractOrderSide.BUY, quantity=Decimal("1.0"), price=Decimal("10.0"), fee=Decimal("5.0"), timestamp=datetime(2025, 5, 6, 9, 30))
    f2 = Fill(fill_id="f2", symbol="600519", side=ContractOrderSide.BUY, quantity=Decimal("1.0"), price=Decimal("10.0"), fee=Decimal("5.0"), timestamp=datetime(2025, 5, 6, 9, 31))
    ledger.apply_fill(f1, "2025-05-06")
    ledger.apply_fill(f2, "2025-05-06")

    assert ledger.positions["600519"] == Decimal("2.0")

    # 执行 1.25 拆细比例：2 股 * 1.25 = 2.5 股 -> A 股法定向下取整为 2 股整股，杜绝 2.5 股小数！
    ledger.apply_stock_split("600519", Decimal("1.25"), "2025-05-07")
    assert ledger.positions["600519"] == Decimal("2.0")
    assert isinstance(ledger.positions["600519"], Decimal)
    # 批次总和与总持仓严格一致
    sum_lots = sum(q for _, q in ledger.lots["600519"])
    assert sum_lots == ledger.positions["600519"]
