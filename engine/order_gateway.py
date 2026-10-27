"""
A 股前置订单网关与生命周期管理器 (AShare Order Gateway & Lifecycle Manager)

核心职责与风控闭环：
1. 前置不可绕过风控校验 (Pre-Trade Risk Checks)：
   - 单笔申报最大股数上限 (max_order 校验，超额直接拦截)
   - 涨跌停与停牌阻断 (Limit-up buy / Limit-down sell / Suspension fail-safe)
   - 2% 申报价格笼子校验 (强制要求有效基准价格，基准缺失/NaN 时严禁放行)
   - 报单整手规整与最小单位校验 (买入整手递增，零股仅允许一次性卖出清仓)
   - 有效期门控 (TimeInForce)：强制当日有效 (TimeInForce.DAY)，拒绝跨日 GTC 挂单
   - 幂等申报保护：拒绝重复提交相同 ClientOrderId 的活动订单
2. 批次化 T+1 可用持仓与挂单预留管理 (Batch-based T+1 Inventory & Reservation)：
   - 按交易发生日独立记录买入批次 (lot_date, qty)
   - 仅严格早于当前交易日 (lot_date < trade_date) 的批次才可释放为已结算持仓
   - 同日多次日切调用严格幂等，时序回退严格报错阻断
   - 可用卖出量 = 历史已结算持仓 - 未成交卖单活动预留量
3. 成交与终态事件全周期闭环：
   - 成交事件以 trade_id 幂等去重，按成交纳秒时戳 (Asia/Shanghai) 归属交易日
   - 真实对接原生 CANCELED / REJECTED / EXPIRED / DENIED 终态回调，安全释放卖单预留，杜绝库存泄漏
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Optional, Set, Tuple, Any
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
    工业级 A 股订单网关与挂单库存状态机
    """

    def __init__(self):
        # 结算持仓与卖单活动预留：symbol -> Decimal
        self._settled_shares: Dict[str, Decimal] = {}
        self._reserved_sell_shares: Dict[str, Decimal] = {}

        # 批次化买入批次 (N01): symbol -> List[(buy_date_str, qty)]
        self._bought_lots: Dict[str, List[Tuple[str, Decimal]]] = {}

        # 订单跟踪：client_order_id -> Order
        self._active_orders: Dict[str, Order] = {}
        self._order_reserved_qty: Dict[str, Decimal] = {}
        self._order_symbol: Dict[str, str] = {}
        self._order_side: Dict[str, OrderSide] = {}
        self._order_declared_qty: Dict[str, Decimal] = {}
        self._order_cum_filled_qty: Dict[str, Decimal] = {}

        # 成交去重 (N03): trade_id 集合
        self._processed_trade_ids: Set[str] = set()

        # 当前会话日期
        self._current_session_date: Optional[str] = None

    def on_trade_day_start(self, trade_date: str) -> None:
        """
        交易日开始：推进 T+1 状态流转。
        具备严格时序单调性与幂等性保护：
        1. 严禁时序回退 (trade_date 必须单调递增)。
        2. 同一交易日重复调用具有完全幂等性，绝不错误释放当日新买股份。
        3. 仅严格早于 trade_date (lot_date < trade_date) 的买入股份转为可卖持仓。
        """
        if self._current_session_date is not None:
            if trade_date < self._current_session_date:
                raise ValueError(
                    f"Trade date regression detected: received {trade_date} which precedes current {self._current_session_date}"
                )
            if trade_date == self._current_session_date:
                # 幂等返回，避免重复释放 (N01)
                return

        self._current_session_date = trade_date

        # 扫描历史买入批次，将已跨日 (lot_date < trade_date) 的股份结算
        for sym, lot_list in list(self._bought_lots.items()):
            remaining_lots: List[Tuple[str, Decimal]] = []
            for lot_date, qty in lot_list:
                if lot_date < trade_date:
                    self._settled_shares[sym] = self._settled_shares.get(sym, Decimal("0")) + qty
                else:
                    remaining_lots.append((lot_date, qty))
            if remaining_lots:
                self._bought_lots[sym] = remaining_lots
            else:
                self._bought_lots.pop(sym, None)

    def apply_stock_split(self, symbol: str, split_ratio: Decimal | float) -> None:
        """
        【Q08, V03】公司行为拆股 / 送转股结算持仓与历史买入批次联动同步
        采用与 GoldenLedger 严格一致的账户级总持仓取整与批次舍入补偿算法，保证总权益严格守恒。
        """
        ratio = Decimal(str(split_ratio))
        if ratio <= Decimal("0.0"):
            raise ValueError(f"Split ratio must be positive, got {ratio}")

        total_held = self.get_total_position(symbol)
        if total_held <= Decimal("0.0"):
            return

        # 账户级总持仓目标整数值
        target_total = (total_held * ratio).to_integral_value(rounding=ROUND_FLOOR)

        # 逐批次向下取整
        if symbol in self._settled_shares:
            self._settled_shares[symbol] = (
                self._settled_shares[symbol] * ratio
            ).to_integral_value(rounding=ROUND_FLOOR)

        if symbol in self._bought_lots:
            updated_lots = []
            for d, q in self._bought_lots[symbol]:
                updated_lots.append(
                    (d, (q * ratio).to_integral_value(rounding=ROUND_FLOOR))
                )
            self._bought_lots[symbol] = updated_lots

        # 计算舍入差额并补偿至最早批次 (发生时间优先原则，与 GoldenLedger 严格对齐)
        current_total = self.get_total_position(symbol)
        diff = target_total - current_total
        if diff != Decimal("0.0"):
            if symbol in self._settled_shares and self._settled_shares[symbol] > Decimal("0.0"):
                self._settled_shares[symbol] += diff
            elif symbol in self._bought_lots and len(self._bought_lots[symbol]) > 0:
                d, q = self._bought_lots[symbol][0]
                self._bought_lots[symbol][0] = (d, q + diff)
            else:
                self._settled_shares[symbol] = self._settled_shares.get(symbol, Decimal("0.0")) + diff

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
        获取当前标的总持仓股数 = 历史已结算 + 今日/尚未跨日结算的所有买入批次
        """
        settled = self._settled_shares.get(symbol, Decimal("0"))
        unsettled = sum((q for _, q in self._bought_lots.get(symbol, [])), Decimal("0"))
        return settled + unsettled

    def validate_and_reserve(
        self,
        order: Order,
        rule: Dict[str, Any],
        bar: Dict[str, Any],
        benchmark_price: Optional[float] = None,
    ) -> OrderValidationResult:
        """
        在提交至撮合引擎前执行严格风控拦截并原子预留库存 (N03 & N04)
        """
        order_id_str = str(order.client_order_id)
        symbol = str(order.instrument_id.symbol)
        side_str = "buy" if order.side == OrderSide.BUY else "sell"
        qty = Decimal(str(order.quantity.as_double()))

        # 1. 幂等订单身份检查 (N03)
        if order_id_str in self._active_orders:
            return OrderValidationResult(
                is_valid=False,
                rejection_reason=f"DUPLICATE_ORDER_ID: Order '{order_id_str}' is already active and reserved",
            )

        # 2. 有效期校验：A 股普通股票必须当日有效 (DAY)，拒绝跨日 GTC 挂单
        if order.time_in_force not in (TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK):
            return OrderValidationResult(
                is_valid=False,
                rejection_reason=f"INVALID_TIME_IN_FORCE: A-share equity rejects {order.time_in_force}, only DAY/IOC/FOK allowed",
            )

        # 3. 数量合法性与整手校验
        if qty <= Decimal("0"):
            return OrderValidationResult(is_valid=False, rejection_reason="INVALID_QUANTITY_NON_POSITIVE")

        # 3.1 单笔最大申报上限校验 (N04)
        max_order = rule.get("max_order", 1000000)
        if qty > Decimal(str(max_order)):
            return OrderValidationResult(
                is_valid=False,
                rejection_reason=f"MAX_ORDER_EXCEEDED: Requested quantity {qty} exceeds maximum allowed order size {max_order}",
            )

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

            # 零股规则：如果卖出不是全部清仓，则必须为整手递增 (100股)
            total_held = self.get_total_position(symbol)
            sell_step = rule.get("sell_step", 100)
            sell_min = rule.get("sell_min", 100)
            if qty != total_held and (qty < sell_min or (qty % sell_step != 0)):
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason=f"INVALID_LOT_SIZE: Non-liquidation sell {qty} must be multiple of {sell_step}",
                )

        # 4. 行情数据完备性与有限性校验 (N04)
        if not isinstance(bar, dict):
            return OrderValidationResult(is_valid=False, rejection_reason="INVALID_BAR_DATA: Bar must be a dictionary")
        close_p = bar.get("close")
        if close_p is None or not isinstance(close_p, (int, float)) or not math.isfinite(close_p) or close_p <= 0:
            return OrderValidationResult(is_valid=False, rejection_reason="INVALID_BAR_CLOSE: Bar close price must be positive and finite")
        vol = bar.get("volume")
        if vol is None or not isinstance(vol, (int, float)) or not math.isfinite(vol) or vol < 0:
            return OrderValidationResult(is_valid=False, rejection_reason="INVALID_BAR_VOLUME: Bar volume must be non-negative and finite")

        # 5. 市场规则与停牌/涨跌停阻断校验 (Fail-safe, Q04, V01)
        trade_status = str(rule.get("trade_status") or "").upper()
        if trade_status in ("CLOSED", "SESSION_CLOSED"):
            return OrderValidationResult(
                is_valid=False,
                rejection_reason="MARKET_SESSION_CLOSED: Market is currently closed",
            )

        bar_ts = bar.get("ts_event") or bar.get("ts_init")
        if bar_ts is not None and order.time_in_force == TimeInForce.DAY:
            dt_bar = datetime.fromtimestamp(bar_ts / 1e9, tz=SHANGHAI_TZ)
            if dt_bar.hour >= 15:
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason="SESSION_CLOSED: Market session closed at 15:00:00, cannot accept DAY orders after close",
                )

        session_phase = str(
            rule.get("session_phase") or bar.get("session_phase") or "CONTINUOUS"
        ).upper()

        limit_state = rule.get("limit_state")
        if limit_state == "BOUNDED":
            lo, hi = rule.get("down_limit"), rule.get("up_limit")
            if (
                lo is None
                or hi is None
                or not isinstance(lo, (int, float))
                or not isinstance(hi, (int, float))
                or not math.isfinite(lo)
                or not math.isfinite(hi)
                or lo <= 0
                or hi <= lo
            ):
                return OrderValidationResult(
                    is_valid=False,
                    rejection_reason="PRICE_BAND_UNAVAILABLE: Bounded limit rules must specify finite positive down_limit and up_limit",
                )

        order_price = order.price.as_double() if hasattr(order, "price") and order.price else None
        reason = block_reason(rule, bar, side_str, execution_price=order_price)
        if reason is not None:
            return OrderValidationResult(is_valid=False, rejection_reason=reason)

        # 6. 2% 价格笼子校验 (N04, Q03, V05)
        # 官方规则：2% 价格笼子仅在连续竞价阶段 (CONTINUOUS) 生效；
        # 集合竞价 (OPENING_AUCTION, CLOSING_AUCTION, CALL_AUCTION) 仅受当日涨跌幅价格带限制，不适用 2% 价格笼子。
        if order_price is not None:
            if not math.isfinite(order_price) or order_price <= 0:
                return OrderValidationResult(is_valid=False, rejection_reason="INVALID_ORDER_PRICE: Order price must be positive and finite")

            if session_phase in ("CONTINUOUS", "CONTINUOUS_AUCTION"):
                if limit_state in ("BOUNDED", "UNBOUNDED"):
                    if benchmark_price is None or not isinstance(benchmark_price, (int, float)) or not math.isfinite(benchmark_price) or benchmark_price <= 0:
                        return OrderValidationResult(
                            is_valid=False,
                            rejection_reason="BENCHMARK_PRICE_REQUIRED: Valid positive finite benchmark_price is required for price collar validation in continuous auction",
                        )
                    if not validate_price_collar(order_price, benchmark_price, side_str):
                        return OrderValidationResult(
                            is_valid=False,
                            rejection_reason=f"PRICE_COLLAR_VIOLATION: Order price {order_price} violates 2% collar with benchmark {benchmark_price} in continuous auction",
                        )
            elif session_phase in ("OPENING_AUCTION", "CLOSING_AUCTION", "CALL_AUCTION"):
                # 集合竞价：严格由涨跌停价格带约束，若超限在第 5 步 block_reason 已拦截
                pass

        # 7. 校验通过，原子预留卖出数量并记录活动订单及申报总量 (Q06)
        if order.side == OrderSide.SELL:
            self._reserved_sell_shares[symbol] = self._reserved_sell_shares.get(symbol, Decimal("0")) + qty
            self._order_reserved_qty[order_id_str] = qty

        self._active_orders[order_id_str] = order
        self._order_symbol[order_id_str] = symbol
        self._order_side[order_id_str] = order.side
        self._order_declared_qty[order_id_str] = qty
        self._order_cum_filled_qty[order_id_str] = Decimal("0.0")
        return OrderValidationResult(is_valid=True)

    def on_order_filled(self, event) -> str:
        """
        处理真实成交事件：
        - 以 event.trade_id 严格幂等去重 (N03)
        - 严格由成交事件的纳秒时戳计算交易发生日 (Asia/Shanghai)
        - 释放卖出预留量并扣减结算持仓
        - 买入股份按 (fill_date, qty) 存入独立买入批次 (严格保证 T+1 隔离，N01)
        :return: trade_date (YYYY-MM-DD)
        """
        trade_id = str(event.trade_id)
        dt = datetime.fromtimestamp(event.ts_event / 1e9, tz=SHANGHAI_TZ)
        fill_date = dt.strftime("%Y-%m-%d")

        # 1. 幂等去重检查 (N03): 若已完整成功处理，直接返回
        if trade_id in self._processed_trade_ids:
            return fill_date

        order_id_str = str(event.client_order_id)
        fill_qty = Decimal(str(event.last_qty.as_double()))
        symbol = str(event.instrument_id.symbol)

        # 2. 前置完整性与身份契约校验 (Q06, V06)
        # 2.1 校验成交订单是否经由网关风控登记
        if order_id_str not in self._order_declared_qty:
            raise RuntimeError(
                f"Untracked order fill received: client_order_id '{order_id_str}' was not validated/reserved by gateway"
            )

        # 2.2 校验标的证券代码是否与报单申报严格一致 (V06)
        expected_sym = self._order_symbol.get(order_id_str)
        if symbol != expected_sym:
            raise RuntimeError(
                f"Instrument symbol mismatch for order '{order_id_str}': expected '{expected_sym}', got '{symbol}'"
            )

        # 2.3 校验订单买卖方向是否与报单申报严格一致 (V06)
        expected_side = self._order_side.get(order_id_str)
        if expected_side is not None and event.order_side != expected_side:
            raise RuntimeError(
                f"Order side mismatch for order '{order_id_str}': expected {expected_side}, got {event.order_side}"
            )

        # 2.4 校验累计成交量是否超单 (Q06)
        declared_qty = self._order_declared_qty[order_id_str]
        cum_qty = self._order_cum_filled_qty.get(order_id_str, Decimal("0.0")) + fill_qty
        if cum_qty > declared_qty:
            raise RuntimeError(
                f"Cumulative fill quantity {cum_qty} exceeds declared order quantity {declared_qty} for order {order_id_str}"
            )

        # 3. 校验全部通过后，原子提交去重记录与状态流转 (V06)
        self._processed_trade_ids.add(trade_id)
        self._order_cum_filled_qty[order_id_str] = cum_qty

        if event.order_side == OrderSide.BUY:
            if symbol not in self._bought_lots:
                self._bought_lots[symbol] = []
            self._bought_lots[symbol].append((fill_date, fill_qty))
        elif event.order_side == OrderSide.SELL:
            # 扣除已预留量
            current_reserved = self._reserved_sell_shares.get(symbol, Decimal("0"))
            self._reserved_sell_shares[symbol] = max(Decimal("0"), current_reserved - fill_qty)

            # 扣除结算持仓
            current_settled = self._settled_shares.get(symbol, Decimal("0"))
            self._settled_shares[symbol] = max(Decimal("0"), current_settled - fill_qty)

            # 调整该订单剩余预留额度
            if order_id_str in self._order_reserved_qty:
                self._order_reserved_qty[order_id_str] = max(
                    Decimal("0"), self._order_reserved_qty[order_id_str] - fill_qty
                )

        # 若订单已终态关闭，清理映射
        order = self._active_orders.get(order_id_str)
        if order and hasattr(order, "is_closed") and order.is_closed:
            self._active_orders.pop(order_id_str, None)
            self._order_reserved_qty.pop(order_id_str, None)
            self._order_symbol.pop(order_id_str, None)
            self._order_side.pop(order_id_str, None)

        return fill_date

    def _release_order_reservation(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """内部通用释放订单预留逻辑 (N02)"""
        reserved = self._order_reserved_qty.pop(client_order_id, Decimal("0"))
        sym = symbol or self._order_symbol.pop(client_order_id, None)
        if reserved > Decimal("0") and sym:
            current_reserved = self._reserved_sell_shares.get(sym, Decimal("0"))
            self._reserved_sell_shares[sym] = max(Decimal("0"), current_reserved - reserved)
        self._active_orders.pop(client_order_id, None)
        self._order_symbol.pop(client_order_id, None)
        self._order_side.pop(client_order_id, None)

    def on_order_canceled(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """原生 CANCELED 回调：释放卖单未成交预留 (N02)"""
        self._release_order_reservation(client_order_id, symbol)

    def on_order_rejected(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """原生 REJECTED 回调：释放卖单预留 (N02)"""
        self._release_order_reservation(client_order_id, symbol)

    def on_order_expired(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """原生 EXPIRED 回调：释放卖单预留 (N02)"""
        self._release_order_reservation(client_order_id, symbol)

    def on_order_denied(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """原生 DENIED 回调：释放卖单预留 (Q05)"""
        self._release_order_reservation(client_order_id, symbol)

    def on_order_canceled_or_rejected(self, client_order_id: str, symbol: Optional[str] = None) -> None:
        """兼容别名接口 (N02)"""
        self._release_order_reservation(client_order_id, symbol)

    def on_session_close(self) -> Set[str]:
        """
        日终收市处理：清理所有活动挂单，释放预留，并返回到期的订单 ID 集合以供向原生引擎撤单
        """
        expired_order_ids = set(self._active_orders.keys())
        for order_id in list(self._active_orders.keys()):
            self._release_order_reservation(order_id)
        return expired_order_ids
