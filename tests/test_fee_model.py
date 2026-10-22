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
