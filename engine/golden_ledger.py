"""
A 股独立参考黄金账本 (Golden Ledger)
独立于回测引擎之外运行的确定性会计账本，用于验证 NautilusTrader 或其他回测引擎的现金流、
持仓演进、除权除息到账及 NAV 曲线无任何偏差。
具备强幂等性、原子性事务控制、单调时序估值与公司行为会计保护。
"""

from __future__ import annotations
from decimal import Decimal
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

        # 公司行为追踪 (R09: 登记日权益快照与支付日兑付分离)
        self.pending_corporate_actions: List[CorporateAction] = []
        self.dividend_entitlements: Dict[str, Decimal] = {} # action_key -> entitled_shares
        self.processed_action_ids: Set[str] = set()

    def on_trade_day_start(self, trade_date: str) -> None:
        """
        交易日开始：推进 T+1 时钟，将之前买入的股份转为可卖状态
        """
        for sym, lot_list in self.lots.items():
            tradable_qty = Decimal("0.0")
            for lot_date, qty in lot_list:
                if lot_date < trade_date:
                    tradable_qty += qty
            self.tradable_positions[sym] = tradable_qty

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

            # 扣减 lots (FIFO 优先扣除最早股份)
            rem = fill.quantity
            new_lots = []
            for lot_date, q in self.lots.get(fill.symbol, []):
                if lot_date < trade_date and rem > 0:
                    if q <= rem:
                        rem -= q
                    else:
                        new_lots.append((lot_date, q - rem))
                        rem = Decimal("0.0")
                else:
                    new_lots.append((lot_date, q))
            self.lots[fill.symbol] = new_lots

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

    def apply_stock_split(self, symbol: str, split_ratio: Decimal, trade_date: str) -> None:
        """
        【R09】处理股票拆细 / 送转股：
        保持持仓总市值不变，按 split_ratio 等比调整持仓数量与各批次历史 shares
        """
        if split_ratio <= Decimal("0.0"):
            raise ValueError(f"Split ratio must be positive, got {split_ratio}")

        if symbol in self.positions:
            self.positions[symbol] = (self.positions[symbol] * split_ratio).quantize(Decimal("1.0"))

        if symbol in self.tradable_positions:
            self.tradable_positions[symbol] = (self.tradable_positions[symbol] * split_ratio).quantize(Decimal("1.0"))

        if symbol in self.lots:
            self.lots[symbol] = [
                (d, (q * split_ratio).quantize(Decimal("1.0")))
                for d, q in self.lots[symbol]
            ]

    def register_corporate_action(self, action: CorporateAction) -> None:
        """
        【R09】注册公司行为事件
        """
        self.pending_corporate_actions.append(action)

    def process_corporate_actions_for_date(self, current_date_str: str) -> None:
        """
        【R09】按交易日推进并原子执行公司行为会计链：
        1. 股权登记日 (record_date): 锁定登记日收盘持仓权益 (entitlement snapshot)
        2. 除权除息日 (ex_date): 执行送转股拆细 (apply_stock_split)
        3. 派息到账日 (pay_date): 根据登记日锁定的权益派发税前现金红利到账
        """
        from datetime import date
        cur_d = date.fromisoformat(current_date_str) if isinstance(current_date_str, str) else current_date_str

        for action in self.pending_corporate_actions:
            act_id = f"{action.symbol}_{action.ex_date}"

            # 1. 登记日快照
            if action.record_date and cur_d == action.record_date:
                held = self.positions.get(action.symbol, Decimal("0.0"))
                self.dividend_entitlements[act_id] = held

            # 2. 除权除息日送转股处理
            if cur_d == action.ex_date:
                total_ratio = Decimal("1.0") + action.bonus_ratio + action.reserve_ratio
                if total_ratio > Decimal("1.0") and f"{act_id}_split" not in self.processed_action_ids:
                    self.apply_stock_split(action.symbol, total_ratio, current_date_str)
                    self.processed_action_ids.add(f"{act_id}_split")

            # 3. 派息日现金到账
            pay_d = action.pay_date or action.ex_date
            if cur_d == pay_d and action.cash_div > Decimal("0.0"):
                if f"{act_id}_cash" not in self.processed_action_ids:
                    # 优先取登记日锁定的持仓，若无登记日则取除息日持仓
                    entitled_qty = self.dividend_entitlements.get(
                        act_id, self.positions.get(action.symbol, Decimal("0.0"))
                    )
                    div_cash = (entitled_qty * action.cash_div).quantize(Decimal("0.01"))
                    self.cash += div_cash
                    self.processed_action_ids.add(f"{act_id}_cash")

    def mark_to_market(self, trade_date: Any, close_prices: Dict[str, Decimal]) -> DailyNAV:
        """
        【R05 & R08】按每日收盘价盯市估值并生成每日资产净值 (NAV) 记录。
        具备单调时间序列保护：
        1. 严禁时序回退 (trade_date 必须单调递增或等同)。
        2. 报价缓存记录归属时间 (asof_date)，严禁将未来日期的价格用于历史估值。
        3. 若当前持仓完全缺失历史报价，显式抛出 KeyError，严禁静默假定 0 价值。
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

        total_equity = self.cash + pos_val
        nav = DailyNAV(
            date=trade_date_str,
            cash=self.cash,
            positions_value=pos_val,
            total_equity=total_equity,
            holdings=dict(self.positions),
            tradable_holdings=dict(self.tradable_positions),
        )
        self.history.append(nav)
        self.last_valuation_date = trade_date_str
        return nav
