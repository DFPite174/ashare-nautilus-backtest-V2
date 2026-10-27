"""
A 股费税模型单元测试 - 验证最低 5 元佣金、0.05% 印花税与过户费
"""

import pytest
from decimal import Decimal
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, ClientOrderId, TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import MarketOrder

from engine.fee_model import AShareFeeModel


def create_dummy_order(side: OrderSide) -> MarketOrder:
    inst_id = InstrumentId(Symbol("600519_SH"), Venue("SSE"))
    return MarketOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("O-1"),
        order_side=side,
        quantity=Quantity.from_int(100),
        time_in_force=TimeInForce.GTC,
        init_id=UUID4(),
        ts_init=0,
    )


def test_min_commission_applied():
    model = AShareFeeModel(commission_rate=0.0003, min_commission=5.0)
    order = create_dummy_order(OrderSide.BUY)
    # 小额买入: 100 股 @ 10.0 元 = 1000 元成交额
    # 理论佣金 = 1000 * 0.0003 = 0.3 元 < 5 元，触发最低 5 元佣金
    # 过户费 = 1000 * 0.00001 = 0.01 元
    # 总费用 = 5.01 元
    fee = model.get_fee(order, Price.from_str("10.0"), Quantity.from_int(100))
    assert fee.as_double() == 5.01


def test_sell_stamp_duty_applied():
    model = AShareFeeModel(commission_rate=0.0003, min_commission=5.0, stamp_duty_rate=0.0005)
    order = create_dummy_order(OrderSide.SELL)
    # 大额卖出: 1000 股 @ 100.0 元 = 100,000 元成交额
    # 佣金 = 100,000 * 0.0003 = 30.0 元 (> 5 元)
    # 印花税 = 100,000 * 0.0005 = 50.0 元
    # 过户费 = 100,000 * 0.00001 = 1.0 元
    # 总费用 = 81.0 元
    fee = model.get_fee(order, Price.from_str("100.0"), Quantity.from_int(1000))
    assert fee.as_double() == 81.00


def test_partial_fills_no_duplicate_min_commission():
    """验证母订单分两笔成交时，不重复收取最低 5 元佣金"""
    model = AShareFeeModel(commission_rate=0.0003, min_commission=5.0)
    order = create_dummy_order(OrderSide.BUY)

    # 第一笔成交：50 股 @ 10.0 元 = 500 元
    # 佣金 = 5.0 元（保底），过户费 = 0.005 元 -> 四舍五入后 5.01 元
    fee_1 = model.get_fee(order, Price.from_str("10.0"), Quantity.from_int(50))
    assert fee_1.as_double() == 5.01

    # 第二笔成交：50 股 @ 10.0 元 = 500 元
    # 母订单累计成交额 1000 元，理论累计佣金 0.3 元 <= 5.0 元，因此第二笔增量佣金为 0.0 元！
    # 仅收取第二笔过户费 0.005 -> 0.01 元
    fee_2 = model.get_fee(order, Price.from_str("10.0"), Quantity.from_int(50))
    assert fee_2.as_double() == 0.01

    # 两笔合计收费 5.02 元 (5元佣金 + 0.01元双边过户费舍入)，杜绝重复扣除 5 元保底
    assert round(fee_1.as_double() + fee_2.as_double(), 2) == 5.02


def test_fee_model_scope_and_strategy_isolation():
    """
    【R07 费用累计缓存隔离与多维命名空间】
    验证即使两个独立的测试运行或策略产生完全相同的 ClientOrderId ('O-1')，
    在不同作用域/策略/交易员命名空间下，各自均正确独立计收最低 5 元保底佣金。
    """
    from engine.fee_model import calculate_expected_fee, get_ashare_rates_for_date

    model = AShareFeeModel(commission_rate=0.0003, min_commission=5.0, scope_id="run_1")
    order_run1 = create_dummy_order(OrderSide.BUY)  # ClientOrderId("O-1")

    # Run 1 首笔买入: 100 股 @ 10.0 -> 5.01 元
    fee_1 = model.get_fee(order_run1, Price.from_str("10.0"), Quantity.from_int(100))
    assert fee_1.as_double() == 5.01

    # 切换到 Run 2 作用域，使用同样 ID 的订单对象
    model.set_scope("run_2")
    order_run2 = create_dummy_order(OrderSide.BUY)  # ClientOrderId("O-1")

    # Run 2 首笔买入: 必须重新计收最低 5 元保底佣金 (5.01 元)，绝不能被 Run 1 缓存污染成 0.01 元！
    fee_2 = model.get_fee(order_run2, Price.from_str("10.0"), Quantity.from_int(100))
    assert fee_2.as_double() == 5.01


def test_historical_fee_rates_and_oracle():
    """
    【R07 历史分水岭费率与独立 Oracle 验证】
    """
    from engine.fee_model import calculate_expected_fee, get_ashare_rates_for_date

    # 2023-08-28 之前：印花税千分之一 (0.001)
    rates_old = get_ashare_rates_for_date("2023-05-01")
    assert rates_old["stamp_duty_rate"] == Decimal("0.0010")

    # 2023-08-28 之后：印花税万分之五 (0.0005)
    rates_new = get_ashare_rates_for_date("2024-01-01")
    assert rates_new["stamp_duty_rate"] == Decimal("0.0005")

    # Oracle 独立算例：1000 股 @ 100 元 = 100,000 元卖出
    # 旧印花税: 佣金 30 + 过户费 1 + 印花税 100 = 131.00
    fee_old = calculate_expected_fee("SELL", Decimal("1000"), Decimal("100"), trade_date="2023-05-01")
    assert fee_old == Decimal("131.00")

    # 新印花税: 佣金 30 + 过户费 1 + 印花税 50 = 81.00
    fee_new = calculate_expected_fee("SELL", Decimal("1000"), Decimal("100"), trade_date="2024-01-01")
    assert fee_new == Decimal("81.00")
