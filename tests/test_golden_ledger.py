"""
独立黄金账本单元测试 - 验证现金流、T+1 违规拦截、分红计算与盯市估值
"""

import pytest
from datetime import date, datetime
from decimal import Decimal
from contracts import Fill, OrderSide
from engine.golden_ledger import GoldenLedger


def test_golden_ledger_basic_buy_and_t1():
    ledger = GoldenLedger(initial_cash=Decimal("1500000.0"))
    assert ledger.cash == Decimal("1500000.0")

    # Day 1: 买入 1000 股 600519.SH @ 1000.0, 手续费 300.0
    ledger.on_trade_day_start("2025-05-06")
    fill_buy = Fill(
        fill_id="f1",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("1000.0"),
        fee=Decimal("300.0"),
        timestamp=datetime(2025, 5, 6, 15, 0, 0),
    )
    ledger.apply_fill(fill_buy, "2025-05-06")
    assert ledger.cash == Decimal("499700.00")
    assert ledger.positions["600519.SH"] == Decimal("1000.0")
    # T+1 当日可卖应为 0
    assert ledger.tradable_positions.get("600519.SH", Decimal("0.0")) == Decimal("0.0")

    # 现金分红每股 10 元
    div_received = ledger.apply_dividend("600519.SH", Decimal("10.0"))
    assert div_received == Decimal("10000.00")
    assert ledger.cash == Decimal("509700.00")


def test_golden_ledger_insufficient_cash():
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")
    fill = Fill(
        fill_id="f1",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("1000.0"),
        fee=Decimal("300.0"),
        timestamp=datetime(2025, 5, 6, 15, 0, 0),
    )
    with pytest.raises(ValueError, match="Ledger cash deficit"):
        ledger.apply_fill(fill, "2025-05-06")


def test_golden_ledger_t1_violation_on_same_day():
    ledger = GoldenLedger(initial_cash=Decimal("2000000.0"))
    ledger.on_trade_day_start("2025-05-06")
    fill_buy = Fill(
        fill_id="f1",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("1000.0"),
        fee=Decimal("300.0"),
        timestamp=datetime(2025, 5, 6, 10, 0, 0),
    )
    ledger.apply_fill(fill_buy, "2025-05-06")

    # 同日卖出 -> T+1 违规
    fill_sell = Fill(
        fill_id="f2",
        symbol="600519.SH",
        side=OrderSide.SELL,
        quantity=Decimal("500.0"),
        price=Decimal("1050.0"),
        fee=Decimal("262.5"),
        timestamp=datetime(2025, 5, 6, 14, 0, 0),
    )
    with pytest.raises(ValueError, match="T\\+1 violation"):
        ledger.apply_fill(fill_sell, "2025-05-06")

    # Day 2: 推进到下一日，允许卖出
    ledger.on_trade_day_start("2025-05-07")
    ledger.apply_fill(fill_sell, "2025-05-07")
    assert ledger.positions["600519.SH"] == Decimal("500.0")
    assert ledger.tradable_positions["600519.SH"] == Decimal("500.0")
