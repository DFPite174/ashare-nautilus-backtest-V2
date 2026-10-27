"""
公司行为 (Corporate Actions) 全链路集成与会计核验测试
验证：
1. [R09] 通过 split_guard 安全加载 Cython 原生扩展 split_native，调整 NautilusTrader 原生持仓与开仓均价。
2. 原生持仓送转股前后经济价值守恒 (总价值与权益一致)。
3. GoldenLedger 独立账本送转股与现金红利全流程会计核算 (登记日锁仓、除权除息日拆股、派现日现金到账)。
"""

import pytest
from datetime import date
from decimal import Decimal

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position

from engine.split_guard import load_bridge
from engine.golden_ledger import GoldenLedger
from contracts import CorporateAction, Fill, OrderSide as ContractOrderSide


def test_split_native_loading_and_execution():
    """
    【R09】验证 Cython 原生拆股扩展加载及对原生 Position 的安全调整
    """
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    from nautilus_trader.test_kit.stubs.execution import TestExecStubs
    from nautilus_trader.test_kit.stubs.events import TestEventStubs
    from nautilus_trader.model.identifiers import PositionId

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


def test_corporate_actions_full_accounting_in_golden_ledger():
    """
    【R09】验证独立黄金账本中公司行为的严格会计周期：
    Day 1 (2025-05-06): 买入 1000 股 @ 100.0
    Day 2 (2025-05-07): 登记日 (Record Date)，锁定权益 1000 股
    Day 3 (2025-05-08): 除权除息日 (Ex Date)，10送10送转股生效 -> 变为 2000 股
    Day 4 (2025-05-09): 派息日 (Pay Date)，现金分红 1000股 * 2元 = 2000元到账
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-ca-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.20"),
        timestamp=pytest.importorskip("datetime").datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册 10 送 10、派 2 元红利
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 9),
        bonus_ratio=Decimal("1.0"),
        reserve_ratio=Decimal("0.0"),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # Day 1 收盘估值 (100.0)
    nav_d1 = ledger.mark_to_market("2025-05-06", {"600519": Decimal("100.0")})
    assert nav_d1.total_equity == Decimal("500000.0") - Decimal("30.20")

    # Day 2: 登记日
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")
    assert ledger.positions["600519"] == Decimal("1000.0")
    nav_d2 = ledger.mark_to_market("2025-05-07", {"600519": Decimal("102.0")})

    # Day 3: 除权除息日 (股价除权由 102 折半调整为 51)
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")
    # 持仓翻倍至 2000 股
    assert ledger.positions["600519"] == Decimal("2000.0")
    assert ledger.tradable_positions["600519"] == Decimal("2000.0")
    # 按除权价 51.0 估值，总持仓市值守恒：2000 * 51.0 = 102,000 元
    nav_d3 = ledger.mark_to_market("2025-05-08", {"600519": Decimal("51.0")})
    assert nav_d3.positions_value == Decimal("102000.0")

    # Day 4: 派息日
    ledger.on_trade_day_start("2025-05-09")
    cash_prior = ledger.cash
    ledger.process_corporate_actions_for_date("2025-05-09")
    # 现金增加 2000 元 (1000股登记持仓 * 2元)
    assert ledger.cash == cash_prior + Decimal("2000.00")
    nav_d4 = ledger.mark_to_market("2025-05-09", {"600519": Decimal("52.0")})
    assert nav_d4.total_equity == nav_d3.total_equity + Decimal("2000.00") + Decimal("2000.0") * Decimal("1.0")
