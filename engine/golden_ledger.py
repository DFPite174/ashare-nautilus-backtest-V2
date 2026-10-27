"""
A 股独立参考黄金账本 (Golden Ledger)
独立于回测引擎之外运行的确定性会计账本，用于验证 NautilusTrader 或其他回测引擎的现金流、
持仓演进、除权除息到账及 NAV 曲线无任何偏差。
具备强幂等性、原子性事务控制与严密异常保护。
"""

from __future__ import annotations
from decimal import Decimal
from typing import Dict, List, Any, Set
from contracts import DailyNAV, Fill, OrderSide


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
        self.last_close_prices: Dict[str, Decimal] = {} # 最近可用收盘价缓存

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
        处理除权除息日现金分红到账
        """
        held = self.positions.get(symbol, Decimal("0.0"))
        if held > 0 and cash_per_share > 0:
            total_div = (held * cash_per_share).quantize(Decimal("0.01"))
            self.cash += total_div
            return total_div
        return Decimal("0.0")

    def mark_to_market(self, trade_date: Any, close_prices: Dict[str, Decimal]) -> DailyNAV:
        """
        按每日收盘价盯市估值并生成每日资产负债记录。
        如果当前持仓缺少报价，优先采用最新已知价格；若完全无历史报价则显式报错，绝不静默按 0 估值。
        """
        # 更新收盘价缓存
        for sym, px in close_prices.items():
            if px > Decimal("0.0"):
                self.last_close_prices[sym] = px

        pos_val = Decimal("0.0")
        for sym, qty in self.positions.items():
            if qty > Decimal("0.0"):
                if sym in close_prices and close_prices[sym] > Decimal("0.0"):
                    px = close_prices[sym]
                elif sym in self.last_close_prices:
                    px = self.last_close_prices[sym]
                else:
                    raise KeyError(f"Missing valuation price for position {sym} on date {trade_date}")
                pos_val += qty * px

        total_equity = self.cash + pos_val
        nav = DailyNAV(
            date=trade_date if not isinstance(trade_date, str) else trade_date,
            cash=self.cash,
            positions_value=pos_val,
            total_equity=total_equity,
            holdings=dict(self.positions),
            tradable_holdings=dict(self.tradable_positions),
        )
        self.history.append(nav)
        return nav
