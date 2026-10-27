"""
A 股交易费税模型 (A-Share Fee & Tax Model)
实现真实且符合监管标准的法定费税：
1. 券商佣金：支持订单级累计计算，单笔订单最低 5 元保底（分批撮合不重复扣收保底）。
2. 证券过户费：双向收取十万分之一 (0.001%)。
3. 证券交易印花税：仅卖出单边收取万分之五 (0.05%)。
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity, Currency
from nautilus_trader.model.orders import Order


class AShareFeeModel(FeeModel):
    """
    符合中国 A 股法定标准的订单级费税计算模型
    """

    def __init__(
        self,
        commission_rate: float = 0.0003,    # 佣金率 万三
        min_commission: float = 5.0,        # 单笔订单最低佣金 5 元
        stamp_duty_rate: float = 0.0005,    # 卖出单边印花税 万五
        transfer_fee_rate: float = 0.00001, # 双边过户费 十万分之一
        currency: Currency = CNY,
    ):
        super().__init__()
        self.commission_rate = Decimal(str(commission_rate))
        self.min_commission = Decimal(str(min_commission))
        self.stamp_duty_rate = Decimal(str(stamp_duty_rate))
        self.transfer_fee_rate = Decimal(str(transfer_fee_rate))
        self.currency = currency

        # 订单级费用追踪：避免一笔订单在部分成交（Partial Fills）时重复扣取最低 5 元保底
        self._order_raw_commission: Dict[str, Decimal] = {}
        self._order_commission_charged: Dict[str, Decimal] = {}

    def reset(self) -> None:
        """重置订单级费用缓存"""
        self._order_raw_commission.clear()
        self._order_commission_charged.clear()

    def get_commission(
        self,
        order: Order,
        fill_qty: Quantity,
        fill_px: Price,
        instrument: Optional[Instrument] = None,
    ) -> Money:
        """
        NautilusTrader FeeModel 原生虚函数：计算单笔成交应扣税费
        """
        trade_value = Decimal(str(fill_px.as_double())) * Decimal(str(fill_qty.as_double()))

        # 1. 券商佣金（按母订单 Order 跟踪累计佣金，分批成交不重复收 5 元）
        order_key = str(order.client_order_id)
        current_raw_comm = trade_value * self.commission_rate

        prev_raw = self._order_raw_commission.get(order_key, Decimal("0.0"))
        prev_charged = self._order_commission_charged.get(order_key, Decimal("0.0"))

        new_raw = prev_raw + current_raw_comm
        self._order_raw_commission[order_key] = new_raw

        # 截至本次 fill，该订单理论应缴纳的累计佣金
        total_comm_due = max(new_raw, self.min_commission)
        incremental_comm = total_comm_due - prev_charged
        self._order_commission_charged[order_key] = prev_charged + incremental_comm

        # 2. 证券过户费 (双边征收，十万分之一)
        transfer_fee = trade_value * self.transfer_fee_rate

        # 3. 证券交易印花税 (仅卖方单边征收，千分之0.5)
        stamp_duty = Decimal("0.0")
        if order.side == OrderSide.SELL:
            stamp_duty = trade_value * self.stamp_duty_rate

        total_fee = (incremental_comm + transfer_fee + stamp_duty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        curr = instrument.quote_currency if instrument and hasattr(instrument, "quote_currency") else self.currency
        return Money(float(total_fee), curr)

    def get_fee(
        self,
        order: Order,
        fill_price: Price,
        fill_qty: Quantity,
        instrument: Optional[Instrument] = None,
    ) -> Money:
        """兼容别名调用"""
        return self.get_commission(order, fill_qty, fill_price, instrument)
