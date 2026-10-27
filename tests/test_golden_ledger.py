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


def test_golden_ledger_idempotency_and_atomicity():
    """验证成交幂等去重与校验失败时的原子性事务控制"""
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f100",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("5.0"),
        timestamp=datetime(2025, 5, 6, 10, 0, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 1. 幂等去重：重复应用同一 fill_id 必须抛出异常
    with pytest.raises(ValueError, match="Duplicate fill detected"):
        ledger.apply_fill(fill, "2025-05-06")

    # 2. 原子性：资金不足失败的 fill 绝不能污染 ledger.fills 列表
    initial_fills_len = len(ledger.fills)
    huge_fill = Fill(
        fill_id="f101",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("1000000.0"),
        price=Decimal("100.0"),
        fee=Decimal("50.0"),
        timestamp=datetime(2025, 5, 6, 11, 0, 0),
    )
    with pytest.raises(ValueError, match="Ledger cash deficit"):
        ledger.apply_fill(huge_fill, "2025-05-06")

    # 失败后，fills 记录数保持不变
    assert len(ledger.fills) == initial_fills_len
    assert "f101" not in ledger.processed_fill_ids


def test_golden_ledger_monotonic_valuation_and_future_price_protection():
    """
    【R08 估值单调性与未来价格隔离】
    1. 严禁时序回退估值
    2. 严禁使用未来日期缓存的价格估值历史日期
    3. 无任何历史报价时抛出 KeyError
    """
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f201",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("5.0"),
        timestamp=datetime(2025, 5, 6, 10, 0, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 1. 2025-05-06 正常估值
    nav_1 = ledger.mark_to_market("2025-05-06", {"600519.SH": Decimal("102.0")})
    assert nav_1.total_equity == Decimal("89995.0") + Decimal("10200.0")

    # 2. 尝试向过去日期倒序估值 -> 必须抛错阻止
    with pytest.raises(ValueError, match="precedes last valuation date"):
        ledger.mark_to_market("2025-05-05", {"600519.SH": Decimal("100.0")})

    # 3. 2025-05-07 停牌无报价：自动沿用 05-06 价格，不抛错
    nav_2 = ledger.mark_to_market("2025-05-07", {})
    assert nav_2.positions_value == Decimal("10200.0")

    # 4. 未持仓标的或无历史价格的持仓标的抛出 KeyError
    ledger.positions["000001.SZ"] = Decimal("50.0")
    with pytest.raises(KeyError, match="Missing valuation price"):
        ledger.mark_to_market("2025-05-08", {})


def test_golden_ledger_corporate_action_split_and_dividend():
    """
    【R09 公司行为会计链】
    验证送转股拆细 (10送10) 与现金分红登记日快照与到账
    """
    from datetime import date
    from contracts import CorporateAction

    ledger = GoldenLedger(initial_cash=Decimal("200000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f301",
        symbol="600519.SH",
        side=OrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 10, 0, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")
    ledger.on_trade_day_start("2025-05-07")  # T+1 可卖转为 1000 股

    # 注册公司行为：10 送 10 (bonus=1.0), 每股派息 2 元 (cash_div=2.0)
    # 登记日 2025-05-07, 除权除息日 2025-05-08, 派现日 2025-05-09
    ca = CorporateAction(
        symbol="600519.SH",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 9),
        bonus_ratio=Decimal("1.0"),
        reserve_ratio=Decimal("0.0"),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # 1. 2025-05-07 登记日：锁定权益 (1000 股)
    ledger.process_corporate_actions_for_date("2025-05-07")
    assert ledger.positions["600519.SH"] == Decimal("1000.0")

    # 2. 2025-05-08 除权除息日：执行 10 送 10，持仓翻倍为 2000 股
    ledger.process_corporate_actions_for_date("2025-05-08")
    assert ledger.positions["600519.SH"] == Decimal("2000.0")
    assert ledger.tradable_positions["600519.SH"] == Decimal("2000.0")

    # 3. 2025-05-09 派息日：现金分红到账 1000股 * 2元 = 2000 元
    cash_before = ledger.cash
    ledger.process_corporate_actions_for_date("2025-05-09")
    assert ledger.cash == cash_before + Decimal("2000.00")
