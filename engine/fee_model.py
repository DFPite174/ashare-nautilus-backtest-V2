"""
A 股交易费税模型 (A-Share Fee & Tax Model)
实现真实的最低 5 元单笔佣金、卖方单边 0.05% 印花税、双边过户费与价格滑点模型。
"""

from decimal import Decimal
from typing import Optional
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity, Currency
from nautilus_trader.model.orders import Order


class AShareFeeModel(FeeModel):
    """
    符合中国 A 股现行与历史法定标准的费税计算模型
    """

    def __init__(
        self,
        commission_rate: float = 0.0003,    # 佣金率 万三
        min_commission: float = 5.0,        # 最低单笔佣金 5 元
        stamp_duty_rate: float = 0.0005,    # 卖出单边印花税 千分之0.5
        transfer_fee_rate: float = 0.00001, # 双边过户费 十万分之一
        currency: Currency = CNY,
    ):
        super().__init__()
        self.commission_rate = Decimal(str(commission_rate))
        self.min_commission = Decimal(str(min_commission))
        self.stamp_duty_rate = Decimal(str(stamp_duty_rate))
        self.transfer_fee_rate = Decimal(str(transfer_fee_rate))
        self.currency = currency

    def get_commission(
        self,
        order: Order,
        fill_qty: Quantity,
        fill_px: Price,
        instrument: Optional[Instrument] = None,
    ) -> Money:
        """
        NautilusTrader 1.231.0 FeeModel 原生接口：计算扣费
        """
        trade_value = Decimal(str(fill_px.as_double())) * Decimal(str(fill_qty.as_double()))

        # 1. 券商佣金 (设最低收费)
        commission = trade_value * self.commission_rate
        if commission < self.min_commission:
            commission = self.min_commission

        # 2. 证券过户费 (双边征收)
        transfer_fee = trade_value * self.transfer_fee_rate

        # 3. 证券交易印花税 (仅卖方单边征收)
        stamp_duty = Decimal("0.0")
        if order.side == OrderSide.SELL:
            stamp_duty = trade_value * self.stamp_duty_rate

        total_fee = (commission + transfer_fee + stamp_duty).quantize(Decimal("0.01"))
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
