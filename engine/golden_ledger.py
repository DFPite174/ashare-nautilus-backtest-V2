"""
A 股独立参考黄金账本 (Golden Ledger)
独立于回测引擎之外运行的确定性会计账本，用于验证 NautilusTrader 或其他回测引擎的现金流、
持仓演进、除权除息到账及 NAV 曲线无任何偏差。
具备强幂等性、原子性事务控制、单调时序估值与公司行为会计保护。
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Any, Set, Optional, Tuple
from contracts import DailyNAV, Fill, OrderSide, CorporateAction


class GoldenLedger:
    """
    独立参考会计账本：以高精度 Decimal 进行现金与持仓复核
    """

    def __init__(self, initial_cash: Decimal):
        if initial_cash <= Decimal("0.0"):
            raise ValueError("Initial cash must be positive")
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, Decimal] = {}          # 总持仓 (股)
        self.tradable_positions: Dict[str, Decimal] = {} # T+1 可卖持仓 (股)
        self.lots: Dict[str, List[tuple[str, Decimal]]] = {} # [(date_str, qty)]
        self.history: List[DailyNAV] = []
        self.fills: List[Fill] = []
        self.processed_fill_ids: Set[str] = set()       # 幂等去重集合

        # 时序与估值缓存 (R08: 包含价格与归属日期，严禁使用未来价格)
        self.latest_prices: Dict[str, Tuple[Decimal, str]] = {} # sym -> (price, asof_date)
        self.last_valuation_date: Optional[str] = None

        # 公司行为会计链 (N06, N07, N08, N09)
        self.pending_corporate_actions: List[CorporateAction] = []
        self.dividend_entitlements: Dict[str, Decimal] = {}      # action_key -> entitled_shares
        self.receivable_dividends: Dict[str, Decimal] = {}       # action_key -> receivable_cash (应收分红)
        self.pending_listing_shares: List[Tuple[date, str, Decimal]] = [] # [(listing_date, symbol, qty)]
        self.processed_action_ids: Set[str] = set()

        # 当前推进交易日 (V07)
        self._current_trade_date: Optional[str] = None

    def on_trade_day_start(self, trade_date: str) -> None:
        """
        交易日开始：推进 T+1 时钟，将之前买入的股份转为可卖状态，并解冻满足上市日的新增送转股份 (N06)
        具备严格的单调时序与幂等性保护 (V07)：同一交易日重复调用绝不重新冻结已上市股份。
        """
        cur_d = date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date

        # 单调时序与重复调用幂等性校验 (V07)
        if self._current_trade_date is not None:
            if str(trade_date) < self._current_trade_date:
                raise ValueError(
                    f"Trade date regression detected: received {trade_date} which precedes current {self._current_trade_date}"
                )
            if str(trade_date) == self._current_trade_date:
                return

        self._current_trade_date = str(trade_date)

        # 1. 推进常规 T+1 买入股份结转
        for sym, lot_list in self.lots.items():
            tradable_qty = Decimal("0.0")
            for lot_date, qty in lot_list:
                if lot_date < trade_date:
                    tradable_qty += qty
            self.tradable_positions[sym] = tradable_qty

        # 2. 检查新增股份上市日 (share_listing_date)，解冻送转股份 (N06)
        remaining_listings: List[Tuple[date, str, Decimal]] = []
        for list_d, sym, qty in self.pending_listing_shares:
            if cur_d >= list_d:
                self.tradable_positions[sym] = self.tradable_positions.get(sym, Decimal("0")) + qty
                if sym not in self.lots:
                    self.lots[sym] = []
                self.lots[sym].append((str(list_d), qty))
            else:
                remaining_listings.append((list_d, sym, qty))
        self.pending_listing_shares = remaining_listings

    def apply_fill(self, fill: Fill, trade_date: str) -> None:
        """
        原子计入成交：
        先执行全部业务约束校验，校验失败时保证账本状态绝不受污染；
        具备 fill_id 幂等去重防护。
        """
        # 1. 幂等性校验
        if fill.fill_id in self.processed_fill_ids:
            raise ValueError(f"Duplicate fill detected: {fill.fill_id}")

        # 2. 字段合法性校验
        if fill.quantity <= Decimal("0.0"):
            raise ValueError(f"Fill quantity must be positive, got {fill.quantity}")
        if fill.price <= Decimal("0.0"):
            raise ValueError(f"Fill price must be positive, got {fill.price}")
        if fill.fee < Decimal("0.0"):
            raise ValueError(f"Fill fee cannot be negative, got {fill.fee}")

        trade_val = fill.price * fill.quantity

        # 3. 业务状态原子校验
        if fill.side == OrderSide.BUY:
            total_outflow = trade_val + fill.fee
            if self.cash < total_outflow:
                raise ValueError(f"Ledger cash deficit: available {self.cash}, needed {total_outflow}")

            # 校验通过，原子更新
            self.cash -= total_outflow
            self.positions[fill.symbol] = self.positions.get(fill.symbol, Decimal("0.0")) + fill.quantity
            if fill.symbol not in self.lots:
                self.lots[fill.symbol] = []
            self.lots[fill.symbol].append((trade_date, fill.quantity))

        elif fill.side == OrderSide.SELL:
            current_tradable = self.tradable_positions.get(fill.symbol, Decimal("0.0"))
            if current_tradable < fill.quantity:
                raise ValueError(f"Ledger T+1 violation on {fill.symbol}: tradable {current_tradable}, selling {fill.quantity}")

            # 校验通过，原子更新
            total_inflow = trade_val - fill.fee
            self.cash += total_inflow
            self.positions[fill.symbol] -= fill.quantity
            self.tradable_positions[fill.symbol] -= fill.quantity

            # 扣减 lots (FIFO 优先扣除最早股份，Q02 消除幽灵批次与负持仓)
            rem = fill.quantity
            new_lots = []
            for lot_date, q in self.lots.get(fill.symbol, []):
                if rem > 0:
                    if q <= rem:
                        rem -= q
                    else:
                        new_lots.append((lot_date, q - rem))
                        rem = Decimal("0.0")
                else:
                    new_lots.append((lot_date, q))

            if rem > Decimal("0.0"):
                raise RuntimeError(
                    f"Fatal lot inconsistency: failed to deduct {rem} shares from lots for {fill.symbol}"
                )

            # 持仓归零清理与批次强守恒校验 (Q02)
            if self.positions[fill.symbol] == Decimal("0.0"):
                self.lots[fill.symbol] = []
                self.tradable_positions[fill.symbol] = Decimal("0.0")
            else:
                self.lots[fill.symbol] = new_lots

            # 强守恒校验：已上市批次 + 待上市批次 严格恒等于 总持仓
            pending_bonus = sum(
                (q for ld, s, q in self.pending_listing_shares if s == fill.symbol),
                Decimal("0.0"),
            )
            lot_sum = sum((q for _, q in self.lots.get(fill.symbol, [])), Decimal("0.0"))
            if lot_sum + pending_bonus != self.positions[fill.symbol]:
                raise RuntimeError(
                    f"Lot conservation violated for {fill.symbol}: lots sum {lot_sum} + pending {pending_bonus} != position {self.positions[fill.symbol]}"
                )

        # 4. 全部修改成功后，记录成交历史与去重标记
        self.fills.append(fill)
        self.processed_fill_ids.add(fill.fill_id)

    def apply_dividend(self, symbol: str, cash_per_share: Decimal) -> Decimal:
        """
        处理除权除息日现金分红到账 (简易即时接口)
        """
        held = self.positions.get(symbol, Decimal("0.0"))
        if held > 0 and cash_per_share > 0:
            total_div = (held * cash_per_share).quantize(Decimal("0.01"))
            self.cash += total_div
            return total_div
        return Decimal("0.0")

    def apply_stock_split(
        self,
        symbol: str,
        split_ratio: Decimal,
        trade_date: str,
        share_listing_date: Optional[date] = None,
        fractional_policy: str = "floor",
        fractional_cash_price: Optional[Decimal] = None,
    ) -> None:
        """
        【N06, N09, Q07, V02】处理股票拆细 / 送转股：
        保持持仓总市值不变，支持严格可配置的零碎股处理策略 (fractional_policy):
        - 'floor': 向下取整保留整数股 (ROUND_FLOOR，默认)，保证批次与总持仓严格守恒。
        - 'reject': 若存在零碎股则严格拒绝并抛出异常。
        - 'cash_in_lieu': 零碎部分按除权基准价（或指定折算价）折算为现金补足入账，严禁使用虚构价格或旧单位价格。
        """
        split_ratio = Decimal(str(split_ratio))
        if split_ratio <= Decimal("0.0"):
            raise ValueError(f"Split ratio must be positive, got {split_ratio}")

        if fractional_policy not in ("floor", "reject", "cash_in_lieu"):
            raise ValueError(
                f"Invalid fractional_policy '{fractional_policy}', must be one of 'floor', 'reject', 'cash_in_lieu'"
            )

        cur_d = date.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date
        orig_held = self.positions.get(symbol, Decimal("0.0"))
        if orig_held <= Decimal("0.0"):
            return

        raw_new_held = orig_held * split_ratio
        floored_held = raw_new_held.to_integral_value(rounding=ROUND_FLOOR)
        frac = raw_new_held - floored_held

        if fractional_policy == "reject" and frac > Decimal("0.0"):
            raise ValueError(
                f"Fractional share detected under reject policy for {symbol}: held {orig_held} * ratio {split_ratio} = {raw_new_held}"
            )
        elif fractional_policy == "cash_in_lieu" and frac > Decimal("0.0"):
            # 零碎部分折现 (V02)：严禁虚构价格，严禁直接使用未经除权调整的旧单位价格
            if fractional_cash_price is not None:
                px = Decimal(str(fractional_cash_price))
            elif symbol in self.latest_prices:
                pre_split_px, _ = self.latest_prices[symbol]
                # 除权基准参考价 = 拆前价格 / 拆分比例 (新单位公允价格)
                px = (pre_split_px / split_ratio).quantize(Decimal("0.0001"))
            else:
                raise ValueError(
                    f"Missing valid valuation price for cash_in_lieu calculation on {symbol}"
                )
            cash_comp = (frac * px).quantize(Decimal("0.01"))
            self.cash += cash_comp
            new_total_held = floored_held
        else:
            new_total_held = floored_held

        bonus_qty = new_total_held - orig_held
        self.positions[symbol] = new_total_held

        # 送转新增股份是否可立即交易 (N06)
        if share_listing_date is None or cur_d >= share_listing_date:
            self.tradable_positions[symbol] = (
                self.tradable_positions.get(symbol, Decimal("0.0")) * split_ratio
            ).to_integral_value(rounding=ROUND_FLOOR)
            # 立即上市：调整现有全部 lots
            if symbol in self.lots and self.lots[symbol]:
                updated_lots: List[Tuple[str, Decimal]] = []
                allocated = Decimal("0.0")
                for i, (d, q) in enumerate(self.lots[symbol]):
                    adjusted_q = (q * split_ratio).to_integral_value(rounding=ROUND_FLOOR)
                    updated_lots.append((d, adjusted_q))
                    allocated += adjusted_q

                diff = new_total_held - allocated
                if diff != Decimal("0.0") and updated_lots:
                    d0, q0 = updated_lots[0]
                    updated_lots[0] = (d0, q0 + diff)

                self.lots[symbol] = updated_lots
        else:
            # 原持仓保持可卖状态，新产生的股份挂入 pending_listing_shares，待 share_listing_date 上市日才解冻到账 (N06)
            self.pending_listing_shares.append((share_listing_date, symbol, bonus_qty))

    def register_corporate_action(self, action: CorporateAction) -> None:
        """
        【R09】注册公司行为事件
        """
        self.pending_corporate_actions.append(action)

    def process_corporate_actions_for_date(self, current_date_str: str) -> None:
        """
        【N06, N07, N08, N09】按交易日推进并原子执行公司行为会计链：
        1. 股权登记日 (record_date): 锁定登记日收盘持仓权益快照 (entitlement snapshot)
        2. 除权除息日 (ex_date):
           - 送转股按整数股执行拆细 (apply_stock_split)，新增股份若有 share_listing_date 则延迟可卖
           - 现金分红确认应收分红 (receivable_dividends)，确保除息到派现期间净值平滑不虚降 (N07)
        3. 派息到账日 (pay_date):
           - 严格从登记日快照计算现金红利，应收分红转为实际现金流入 (N08)
        """
        cur_d = date.fromisoformat(current_date_str) if isinstance(current_date_str, str) else current_date_str

        for action in self.pending_corporate_actions:
            act_id = f"{action.symbol}_{action.ex_date}"

            # 1. 登记日快照 (N08)
            if action.record_date and cur_d == action.record_date:
                held = self.positions.get(action.symbol, Decimal("0.0"))
                self.dividend_entitlements[act_id] = held

            # 2. 除权除息日送转股与应收分红处理 (N06, N07)
            if cur_d == action.ex_date:
                # 确定计算权益所依据的股数
                if action.record_date:
                    if act_id not in self.dividend_entitlements:
                        raise RuntimeError(
                            f"Missing entitlement snapshot for corporate action {act_id} on record date {action.record_date}"
                        )
                    entitled_qty = self.dividend_entitlements[act_id]
                else:
                    entitled_qty = self.positions.get(action.symbol, Decimal("0.0"))
                    self.dividend_entitlements[act_id] = entitled_qty

                # 2.1 送转股拆细
                total_ratio = Decimal("1.0") + action.bonus_ratio + action.reserve_ratio
                if total_ratio > Decimal("1.0") and f"{act_id}_split" not in self.processed_action_ids:
                    self.apply_stock_split(
                        action.symbol,
                        total_ratio,
                        current_date_str,
                        share_listing_date=action.share_listing_date,
                        fractional_policy=action.fractional_policy,
                        fractional_cash_price=action.fractional_cash_price,
                    )
                    self.processed_action_ids.add(f"{act_id}_split")

                # 2.2 确认应收分红 (N07: 确保除息日价格下跌时，应收分红维持净值守恒)
                if action.cash_div > Decimal("0.0") and f"{act_id}_receivable" not in self.processed_action_ids:
                    receivable_cash = (entitled_qty * action.cash_div).quantize(Decimal("0.01"))
                    self.receivable_dividends[act_id] = receivable_cash
                    self.processed_action_ids.add(f"{act_id}_receivable")

            # 3. 派息日现金红利到账兑付 (N07, N08)
            pay_d = action.pay_date or action.ex_date
            if cur_d == pay_d and action.cash_div > Decimal("0.0"):
                if f"{act_id}_paid" not in self.processed_action_ids:
                    # 优先冲减应收分红转换为实际到账现金
                    if act_id in self.receivable_dividends:
                        div_cash = self.receivable_dividends.pop(act_id)
                    else:
                        if action.record_date and act_id not in self.dividend_entitlements:
                            raise RuntimeError(
                                f"Missing entitlement snapshot for corporate action {act_id} on record date {action.record_date}"
                            )
                        entitled_qty = self.dividend_entitlements.get(
                            act_id, self.positions.get(action.symbol, Decimal("0.0"))
                        )
                        div_cash = (entitled_qty * action.cash_div).quantize(Decimal("0.01"))

                    self.cash += div_cash
                    self.processed_action_ids.add(f"{act_id}_paid")

    def mark_to_market(self, trade_date: Any, close_prices: Dict[str, Decimal]) -> DailyNAV:
        """
        【R05, R08, N07】按每日收盘价盯市估值并生成每日资产净值 (NAV) 记录。
        具备单调时间序列保护与应收分红权益守恒：
        1. 严禁时序回退 (trade_date 必须单调递增或等同)。
        2. 报价缓存记录归属时间 (asof_date)，严禁将未来日期的价格用于历史估值。
        3. 若当前持仓完全缺失历史报价，显式抛出 KeyError。
        4. NAV 包含现金 + 持仓股票公允市值 + 应收现金红利 (N07 权益守恒)。
        """
        trade_date_str = str(trade_date)[:10]

        # 1. 单调时序校验 (R08)
        if self.last_valuation_date is not None:
            if trade_date_str < self.last_valuation_date:
                raise ValueError(
                    f"Valuation date regression: received {trade_date_str} which precedes last valuation date {self.last_valuation_date}"
                )

        # 2. 录入更新当日收盘价
        for sym, px in close_prices.items():
            if px > Decimal("0.0"):
                self.latest_prices[sym] = (px, trade_date_str)

        # 3. 计算持仓总市值
        pos_val = Decimal("0.0")
        for sym, qty in self.positions.items():
            if qty > Decimal("0.0"):
                if sym in close_prices and close_prices[sym] > Decimal("0.0"):
                    px = close_prices[sym]
                elif sym in self.latest_prices:
                    cached_px, asof_date = self.latest_prices[sym]
                    if asof_date > trade_date_str:
                        raise ValueError(
                            f"Future price leak detected for {sym}: cached date {asof_date} > valuation date {trade_date_str}"
                        )
                    px = cached_px
                else:
                    raise KeyError(f"Missing valuation price as-of {trade_date_str} for position {sym}")
                pos_val += qty * px

        # 4. 计算当前应收分红 (N07)
        total_receivable = sum(self.receivable_dividends.values(), Decimal("0.0"))
        total_equity = self.cash + pos_val + total_receivable

        # 5. 自动同步当日收盘对应的股权登记日快照 (V04: 锁定登记日收盘持仓作为唯一合法权益基准)
        for action in self.pending_corporate_actions:
            if action.record_date and str(action.record_date) == trade_date_str:
                act_id = f"{action.symbol}_{action.ex_date}"
                self.dividend_entitlements[act_id] = self.positions.get(action.symbol, Decimal("0.0"))

        nav = DailyNAV(
            date=trade_date_str,
            cash=self.cash,
            positions_value=pos_val,
            total_equity=total_equity,
            receivable_dividends=total_receivable,
            holdings=dict(self.positions),
            tradable_holdings=dict(self.tradable_positions),
        )
        self.history.append(nav)
        self.last_valuation_date = trade_date_str
        return nav
