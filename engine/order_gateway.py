"""
A 股前置订单网关与生命周期管理器 (AShare Order Gateway & Lifecycle Manager)

核心职责：
1. 前置风控校验 (Pre-Trade Risk Checks)：
   - 涨跌停阻断 (Limit-up buy / Limit-down sell block)
   - 2% 申报价格笼子校验 (2% Price Collar)
   - 报单手数规整与最小单位校验 (Lot sizing: 100 股整手，零股仅允许一次性卖出)
   - 有效期门控 (TimeInForce)：强制当日有效 (TimeInForce.DAY)，严禁跨日 GTC 挂单
2. T+1 可用持仓与挂单预留管理 (Inventory Reservation)：
   - 可用卖出量 = 历史已结算持仓 (settled_shares) - 未成交卖单预留量 (reserved_sell_shares)
   - 申报卖单时即时预留份额，杜绝同日超额重复申报
   - 撤单、拒单、到期、成交时精准释放或扣减预留
3. 成交事件日期对齐：
   - 严格按成交事件自身纳秒时戳 (Asia/Shanghai) 确定交易归属日，消除跨日未决订单的时序错配
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional, Set, Any
from zoneinfo import ZoneInfo

from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.orders import Order
from engine.market_rules import validate_price_collar, block_reason


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class OrderValidationResult:
    is_valid: bool
    rejection_reason: Optional[str] = None


class AShareOrderGateway:
    """
    不可绕过的 A 股订单网关与挂单库存状态机
    """

    def __init__(self):
        # 标的持仓状态：symbol -> {settled_shares, reserved_sell_shares, today_bought_shares}
        self._settled_shares: Dict[str, Decimal] = {}
        self._reserved_sell_shares: Dict[str, Decimal] = {}
        self._today_bought_shares: Dict[str, Decimal] = {}

        # 订单跟踪：client_order_id -> Order
        self._active_orders: Dict[str, Order] = {}
        self._order_reserved_qty: Dict[str, Decimal] = {}
        self._current_session_date: Optional[str] = None

    def on_trade_day_start(self, trade_date: str) -> None:
        """
        交易日开始：将前一交易日买入的股份自动释放为已结算可卖持仓 (T+1 状态流转)
        """
        self._current_session_date = trade_date
        for sym, bought_qty in self._today_bought_shares.items():
            if bought_qty > Decimal("0"):
                self._settled_shares[sym] = self._settled_shares.get(sym, Decimal("0")) + bought_qty
        self._today_bought_shares.clear()

    def get_sellable_qty(self, symbol: str) -> Decimal:
        """
        获取当前标的合规可卖出股数 = 历史已结算股份 - 当前处于活动状态的卖单预留
        """
        settled = self._settled_shares.get(symbol, Decimal("0"))
        reserved = self._reserved_sell_shares.get(symbol, Decimal("0"))
        available = settled - reserved
        return max(Decimal("0"), available)

    def get_total_position(self, symbol: str) -> Decimal:
        """
        获取当前标的总持仓股数 (已结算 + 今日买入)
        """
        settled = self._settled_shares.get(symbol, Decimal("0"))
        today = self._today_bought_shares.get(symbol, Decimal("0"))
        return settled + today

    def validate_and_reserve(
        self,
        order: Order,
        rule: Dict[str, Any],
        bar: Dict[str, Any],
        benchmark_price: Optional[float] = None,
    ) -> OrderValidationResult:
        """
        在提交至撮合引擎前执行前置风控拦截并原子预留库存
        """
        symbol = str(order.instrument_id.symbol)
        side_str = "buy" if order.side == OrderSide.BUY else "sell"
        qty = Decimal(str(order.quantity.as_double()))

        # 1. 有效期校验：A 股普通股票必须当日有效 (DAY)，拒绝跨日 GTC 挂单
        if order.time_in_force not in (TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK):
            return OrderValidationResult(
                is_valid=False,
                rejection_reason=f"INVALID_TIME_IN_FORCE: A-share equity rejects {order.time_in_force}, only DAY/IOC/FOK allowed",
            )

        # 2. 数量合法性与整手校验
        if qty <= Decimal("0"):
            return OrderValidationResult(is_valid=False, rejection_reason="INVALID_QUANTITY_NON_POSITIVE")

        if order.side == OrderSide.BUY:
            buy_min = rule.get("buy_min", 100)
            buy_step = rule.get("buy_step", 100)
            if qty < buy_min or (qty % buy_step != 0):
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason=f"INVALID_LOT_SIZE: Buy quantity {qty} must be at least {buy_min} and multiple of {buy_step}",
                )
        elif order.side == OrderSide.SELL:
            # 卖出必须有充足的 T+1 已结算且未被预留的股份
            sellable = self.get_sellable_qty(symbol)
            if qty > sellable:
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason=f"T1_SETTLEMENT_VIOLATION: Requested sell {qty} exceeds sellable {sellable}",
                )

            # 零股规则：如果卖出不是全部清仓，则必须为 100 股整手递增
            total_held = self.get_total_position(symbol)
            sell_step = rule.get("sell_step", 100)
            sell_min = rule.get("sell_min", 100)
            if qty != total_held and (qty < sell_min or (qty % sell_step != 0)):
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason=f"INVALID_LOT_SIZE: Non-liquidation sell {qty} must be multiple of {sell_step}",
                )

        # 3. 市场规则与停牌/涨跌停阻断校验 (Fail-safe)
        order_price = order.price.as_double() if hasattr(order, "price") and order.price else None
        reason = block_reason(rule, bar, side_str, execution_price=order_price)
        if reason is not None:
            return OrderValidationResult(is_valid=False, rejection_reason=reason)

        # 4. 2% 价格笼子校验 (若提供了基准价格与限价单价格)
        if order_price is not None and benchmark_price is not None and benchmark_price > 0:
            if not validate_price_collar(order_price, benchmark_price, side_str):
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason=f"PRICE_COLLAR_VIOLATION: Order price {order_price} violates 2% collar with benchmark {benchmark_price}",
                )

        # 5. 校验通过，原子预留卖出数量并记录活动订单
        order_id_str = str(order.client_order_id)
        if order.side == OrderSide.SELL:
            self._reserved_sell_shares[symbol] = self._reserved_sell_shares.get(symbol, Decimal("0")) + qty
            self._order_reserved_qty[order_id_str] = qty

        self._active_orders[order_id_str] = order
        return OrderValidationResult(is_valid=True)

    def on_order_filled(self, event) -> str:
        """
        处理真实成交事件：
        - 严格由成交事件的纳秒时戳计算交易发生日 (Asia/Shanghai)
        - 释放卖出预留量并扣减结算持仓
        - 买入股份记入当日买入仓位 (T+1 锁定)
        :return: trade_date (YYYY-MM-DD)
        """
        order_id_str = str(event.client_order_id)
        fill_qty = Decimal(str(event.last_qty.as_double()))
        symbol = str(event.instrument_id.symbol)

        # 计算真实成交日
        dt = datetime.fromtimestamp(event.ts_event / 1e9, tz=SHANGHAI_TZ)
        fill_date = dt.strftime("%Y-%m-%d")

        if event.order_side == OrderSide.BUY:
            self._today_bought_shares[symbol] = self._today_bought_shares.get(symbol, Decimal("0")) + fill_qty
        elif event.order_side == OrderSide.SELL:
            # 扣除已预留量
            current_reserved = self._reserved_sell_shares.get(symbol, Decimal("0"))
            self._reserved_sell_shares[symbol] = max(Decimal("0"), current_reserved - fill_qty)

            # 扣除持仓
            current_settled = self._settled_shares.get(symbol, Decimal("0"))
            self._settled_shares[symbol] = max(Decimal("0"), current_settled - fill_qty)

            # 调整订单剩余预留
            if order_id_str in self._order_reserved_qty:
                self._order_reserved_qty[order_id_str] = max(
                    Decimal("0"), self._order_reserved_qty[order_id_str] - fill_qty
                )

        # 若订单全部成交，清理活动状态
        order = self._active_orders.get(order_id_str)
        if order and hasattr(order, "is_closed") and order.is_closed:
            self._active_orders.pop(order_id_str, None)
            self._order_reserved_qty.pop(order_id_str, None)

        return fill_date

    def on_order_canceled_or_rejected(self, client_order_id: str, symbol: str) -> None:
        """
        订单被撤销、拒绝或日终到期：安全释放卖单预留量
        """
        reserved = self._order_reserved_qty.pop(client_order_id, Decimal("0"))
        if reserved > Decimal("0"):
            current_reserved = self._reserved_sell_shares.get(symbol, Decimal("0"))
            self._reserved_sell_shares[symbol] = max(Decimal("0"), current_reserved - reserved)
        self._active_orders.pop(client_order_id, None)

    def on_session_close(self) -> Set[str]:
        """
        日终收市处理：清理所有活动挂单 (A 股当日有效规则：收市未成交订单全部到期释放)
        """
        expired_order_ids = set(self._active_orders.keys())
        for order_id in list(self._active_orders.keys()):
            order = self._active_orders.pop(order_id)
            symbol = str(order.instrument_id.symbol)
            reserved = self._order_reserved_qty.pop(order_id, Decimal("0"))
            if reserved > Decimal("0"):
                current_reserved = self._reserved_sell_shares.get(symbol, Decimal("0"))
                self._reserved_sell_shares[symbol] = max(Decimal("0"), current_reserved - reserved)
        return expired_order_ids
