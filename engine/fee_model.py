"""
A 股交易费税模型 (A-Share Fee & Tax Model)
实现真实且符合监管标准的法定费税：
1. 券商佣金：支持订单级累计计算，单笔订单最低 5 元保底（分批撮合不重复扣收保底）。
2. 证券过户费：双向收取十万分之一 (0.001%)。
3. 证券交易印花税：仅卖出单边收取万分之五 (0.05%) (2023-08-28 调整前为 0.1%)。
4. 作用域隔离与独立核验 Oracle (N05 & N10):
   - 基于 (scope_id, trader_id, strategy_id, instrument_id, client_order_id) 形成强隔离缓存
   - 原生收费路径自动按订单时戳/交易日匹配历史法定税率 (N05)
   - 独立费用 Oracle 准确支持部分成交累计成交额差额计算 (N10)
"""

from __future__ import annotations
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity, Currency
from nautilus_trader.model.orders import Order


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
    cum_trade_val_before: Decimal = Decimal("0.0"),
) -> Decimal:
    """
    【独立费用 Oracle】供测试与双轨账本独立核算每笔成交的法定应纳税费。
    支持分批成交增量计算 (N10)。
    """
    r = rates or get_ashare_rates_for_date(trade_date)
    trade_val = qty * price
    comm_rate = r["commission_rate"]
    min_comm = r["min_commission"]

    if is_new_order and cum_trade_val_before == Decimal("0.0"):
        incremental_comm = max(trade_val * comm_rate, min_comm)
    else:
        cum_val_after = cum_trade_val_before + trade_val
        comm_due_before = max(cum_trade_val_before * comm_rate, min_comm) if cum_trade_val_before > 0 else Decimal("0.0")
        comm_due_after = max(cum_val_after * comm_rate, min_comm)
        incremental_comm = comm_due_after - comm_due_before

    trans = trade_val * r["transfer_fee_rate"]
    stamp = Decimal("0.0")
    if str(side).upper() in ("SELL", "ORDERSIDE.SELL"):
        stamp = trade_val * r["stamp_duty_rate"]

    return (incremental_comm + trans + stamp).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class AShareFeeModel(FeeModel):
    """
    符合中国 A 股法定标准的订单级费税计算模型
    """

    def __init__(
        self,
        commission_rate: Optional[float] = None,
        min_commission: Optional[float] = None,
        stamp_duty_rate: Optional[float] = None,
        transfer_fee_rate: Optional[float] = None,
        currency: Currency = CNY,
        scope_id: str = "default_run",
        current_trade_date: Optional[str] = None,
    ):
        super().__init__()
        self._custom_comm_rate = Decimal(str(commission_rate)) if commission_rate is not None else None
        self._custom_min_comm = Decimal(str(min_commission)) if min_commission is not None else None
        self._custom_stamp_rate = Decimal(str(stamp_duty_rate)) if stamp_duty_rate is not None else None
        self._custom_transfer_rate = Decimal(str(transfer_fee_rate)) if transfer_fee_rate is not None else None
        self.currency = currency
        self.scope_id = scope_id
        self.current_trade_date = current_trade_date

        # 订单级多维命名空间费用追踪 (杜绝跨策略或跨运行缓存串扰)
        self._order_raw_commission: Dict[str, Decimal] = {}
        self._order_commission_charged: Dict[str, Decimal] = {}

    def set_scope(self, scope_id: str) -> None:
        """设置运行命名空间并重置缓存"""
        self.scope_id = scope_id
        self.reset()

    def set_trade_date(self, trade_date: str) -> None:
        """设置当前交易日用于费率动态匹配"""
        self.current_trade_date = trade_date

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

    def _resolve_rates(self, order: Order) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
        """按订单发生日动态解析法定费税率 (N05)"""
        order_date: Optional[str] = self.current_trade_date
        if order_date is None and hasattr(order, "ts_init") and order.ts_init > 0:
            dt = datetime.fromtimestamp(order.ts_init / 1e9, tz=SHANGHAI_TZ)
            order_date = dt.strftime("%Y-%m-%d")

        matched = get_ashare_rates_for_date(order_date)
        comm_rate = self._custom_comm_rate if self._custom_comm_rate is not None else matched["commission_rate"]
        min_comm = self._custom_min_comm if self._custom_min_comm is not None else matched["min_commission"]
        # 如果未显式覆盖印花税，或显式传入了基准 0.0005，则根据历史分水岭自动匹配
        if self._custom_stamp_rate is not None and self._custom_stamp_rate != Decimal("0.0005"):
            stamp_rate = self._custom_stamp_rate
        else:
            stamp_rate = matched["stamp_duty_rate"]

        if self._custom_transfer_rate is not None and self._custom_transfer_rate != Decimal("0.00001"):
            transfer_rate = self._custom_transfer_rate
        else:
            transfer_rate = matched["transfer_fee_rate"]

        return comm_rate, min_comm, stamp_rate, transfer_rate

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
        comm_rate, min_comm, stamp_rate, transfer_rate = self._resolve_rates(order)
        trade_value = Decimal(str(fill_px.as_double())) * Decimal(str(fill_qty.as_double()))

        # 1. 券商佣金（按多维复合键 order_key 跟踪母单累计佣金，分批成交不重复收 5 元）
        order_key = self._make_order_key(order)
        current_raw_comm = trade_value * comm_rate

        prev_raw = self._order_raw_commission.get(order_key, Decimal("0.0"))
        prev_charged = self._order_commission_charged.get(order_key, Decimal("0.0"))

        new_raw = prev_raw + current_raw_comm
        self._order_raw_commission[order_key] = new_raw

        # 截至本次 fill，该订单理论应缴纳的累计佣金
        total_comm_due = max(new_raw, min_comm)
        incremental_comm = total_comm_due - prev_charged
        self._order_commission_charged[order_key] = prev_charged + incremental_comm

        # 2. 证券过户费 (双边征收)
        transfer_fee = trade_value * transfer_rate

        # 3. 证券交易印花税 (仅卖方单边征收)
        stamp_duty = Decimal("0.0")
        if order.side == OrderSide.SELL:
            stamp_duty = trade_value * stamp_rate

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
