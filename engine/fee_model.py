"""
A 股交易费税模型 (A-Share Fee & Tax Model)
实现真实且符合监管标准的法定费税：
1. 券商佣金：支持订单级累计计算，单笔订单最低 5 元保底（分批撮合不重复扣收保底）。
2. 证券过户费：双向收取十万分之一 (0.001%)。
3. 证券交易印花税：仅卖出单边收取万分之五 (0.05%) (2023-08-28 调整前为 0.1%)。
4. 作用域隔离与独立核验 Oracle (R07):
   - 基于 (scope_id, trader_id, strategy_id, instrument_id, client_order_id) 形成强隔离缓存
   - 提供按历史生效日期自动匹配税率功能与独立费用 Oracle 函数
"""

from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity, Currency
from nautilus_trader.model.orders import Order


def get_ashare_rates_for_date(trade_date: Optional[str] = None) -> Dict[str, Decimal]:
    """
    根据历史交易日自动匹配法定费税率：
    - 印花税 (仅卖出)：2023-08-28 起减半征收为万分之五 (0.0005)；此前为千分之一 (0.001)
    - 过户费 (双边)：2022-04-29 起下调为十万分之一 (0.00001)；此前为十万分之二 (0.00002)
    - 佣金率：市场标准万三 (0.0003)，单笔最低 5 元
    """
    if trade_date and trade_date < "2023-08-28":
        stamp_duty = Decimal("0.0010")
    else:
        stamp_duty = Decimal("0.0005")

    if trade_date and trade_date < "2022-04-29":
        transfer_fee = Decimal("0.00002")
    else:
        transfer_fee = Decimal("0.00001")

    return {
        "commission_rate": Decimal("0.0003"),
        "min_commission": Decimal("5.0"),
        "stamp_duty_rate": stamp_duty,
        "transfer_fee_rate": transfer_fee,
    }


def calculate_expected_fee(
    side: str,
    qty: Decimal,
    price: Decimal,
    is_new_order: bool = True,
    trade_date: Optional[str] = None,
    rates: Optional[Dict[str, Decimal]] = None,
) -> Decimal:
    """
    【独立费用 Oracle】供测试与双轨账本独立核算每笔成交的法定应纳税费
    """
    r = rates or get_ashare_rates_for_date(trade_date)
    trade_val = qty * price

    # 1. 佣金
    raw_comm = trade_val * r["commission_rate"]
    if is_new_order:
        comm = max(raw_comm, r["min_commission"])
    else:
        comm = raw_comm

    # 2. 过户费 (双向)
    trans = trade_val * r["transfer_fee_rate"]

    # 3. 印花税 (仅卖方)
    stamp = Decimal("0.0")
    if side.upper() in ("SELL", "OrderSide.SELL"):
        stamp = trade_val * r["stamp_duty_rate"]

    return (comm + trans + stamp).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
        scope_id: str = "default_run",
    ):
        super().__init__()
        self.commission_rate = Decimal(str(commission_rate))
        self.min_commission = Decimal(str(min_commission))
        self.stamp_duty_rate = Decimal(str(stamp_duty_rate))
        self.transfer_fee_rate = Decimal(str(transfer_fee_rate))
        self.currency = currency
        self.scope_id = scope_id

        # 订单级多维命名空间费用追踪 (R07: 杜绝跨策略或跨运行缓存串扰)
        self._order_raw_commission: Dict[str, Decimal] = {}
        self._order_commission_charged: Dict[str, Decimal] = {}

    def set_scope(self, scope_id: str) -> None:
        """设置运行命名空间并重置缓存"""
        self.scope_id = scope_id
        self.reset()

    def reset(self) -> None:
        """重置订单级费用缓存"""
        self._order_raw_commission.clear()
        self._order_commission_charged.clear()

    def _make_order_key(self, order: Order) -> str:
        tid = getattr(order, "trader_id", "")
        sid = getattr(order, "strategy_id", "")
        iid = getattr(order, "instrument_id", "")
        cid = getattr(order, "client_order_id", "")
        return f"{self.scope_id}:{tid}:{sid}:{iid}:{cid}"

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

        # 1. 券商佣金（按多维复合键 order_key 跟踪母单累计佣金，分批成交不重复收 5 元）
        order_key = self._make_order_key(order)
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
