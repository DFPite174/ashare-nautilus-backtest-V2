"""
A 股交易制度与市场约束规则引擎 (Market Rules Engine)
支持主板/创业板/科创板/北交所交易手数约束、动态涨跌停板、停牌判断与挂单校验。
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass(frozen=True)
class DailyMarketRule:
    """单个证券单日的交易规则与价格边界"""
    trade_status: str       # 'OPEN', 'SUSPENDED', 'DAY_BLOCKED'
    limit_state: str        # 'BOUNDED', 'UNBOUNDED', 'UNAVAILABLE'
    up_limit: Optional[float] = None
    down_limit: Optional[float] = None
    buy_min: int = 100      # 最小买入股数 (主板100, 科创板200)
    buy_step: int = 100     # 买入递增单位 (主板100, 科创板1)
    sell_min: int = 100     # 最小卖出单位
    sell_step: int = 100    # 卖出递增单位
    max_order: int = 1000000 # 单笔最大申报股数 (主板100万, 科创板10万)


def validate_rules(config: Dict[str, Any]) -> None:
    """严格校验输入的市场规则完整性与有效性"""
    rules = config.get("market_rules", {})
    timeline = config.get("timeline", [])
    bars_dict = config.get("bars", {})

    dates = {t[:10] for t in timeline}
    for date_str in dates:
        for symbol in bars_dict:
            r = rules.get(date_str, {}).get(symbol)
            if not r:
                raise ValueError(f"Missing daily market rule for {symbol} on {date_str}")
            status = r.get("trade_status")
            if status not in ("OPEN", "SUSPENDED", "DAY_BLOCKED"):
                raise ValueError(f"Unknown trading status: {symbol} {date_str} ({status})")
            limit_state = r.get("limit_state")
            if limit_state not in ("BOUNDED", "UNBOUNDED", "UNAVAILABLE"):
                raise ValueError(f"Unknown price limit state: {symbol} {date_str} ({limit_state})")
            if limit_state == "UNAVAILABLE" and status == "OPEN":
                raise ValueError(f"Unavailable price band on tradable day: {symbol} {date_str}")
            if limit_state == "BOUNDED":
                lo, hi = r.get("down_limit"), r.get("up_limit")
                if lo is None or hi is None or not (0 < lo < hi):
                    raise ValueError(f"Invalid price band bounds: {symbol} {date_str} ({lo}, {hi})")

    # 校验价格与上下限的因果一致性
    for symbol, bars in bars_dict.items():
        for bar in bars:
            dt = bar["timestamp"][:10]
            r = rules.get(dt, {}).get(symbol)
            if not r:
                continue
            if r.get("trade_status") == "OPEN" and r.get("limit_state") == "BOUNDED":
                lo, hi = r["down_limit"], r["up_limit"]
                if bar["low"] < lo - 1e-8 or bar["high"] > hi + 1e-8:
                    raise ValueError(f"Bar OHLC violates price limits: {symbol} {bar['timestamp']}")


def block_reason(
    rule: Dict[str, Any],
    bar: Dict[str, Any],
    side: str,
    execution_price: Optional[float] = None
) -> Optional[str]:
    """
    判断指定挂单是否受 A 股规则限制而被拦截阻断
    :param rule: 当日市场规则字典
    :param bar: 当日报价 Bar
    :param side: 'buy' 或 'sell'
    :param execution_price: 预期执行价格
    :return: 阻断原因代码 (如 'LIMIT_UP_CLOSE', 'SUSPENDED')，若允许交易则返回 None
    """
    status = rule.get("trade_status")
    if status != "OPEN":
        return status
    if bar.get("volume", 0) <= 0:
        return "ZERO_VOLUME"

    if rule.get("limit_state") == "BOUNDED":
        lo, hi = rule["down_limit"], rule["up_limit"]
        close_p = bar.get("close", 0.0)
        # 日线收盘执行：保守策略在涨停日拒绝买单，跌停日拒绝卖单
        if side.lower() == "buy" and close_p >= hi - 1e-8:
            return "LIMIT_UP_CLOSE"
        if side.lower() == "sell" and close_p <= lo + 1e-8:
            return "LIMIT_DOWN_CLOSE"
        if execution_price is not None:
            if not (lo - 1e-8 <= float(execution_price) <= hi + 1e-8):
                return "PRICE_OUTSIDE_BAND"

    return None


def round_order(delta: float, held: float, rule: Dict[str, Any]) -> int:
    """
    A 股报单股数整手与零股规则规整计算
    :param delta: 期望调仓股数变化量 (正为买，负为卖)
    :param held: 当前实际持仓总股数
    :param rule: 标的对应规则
    :return: 规整后的报单股数 (正买负卖，0为不成交)
    """
    max_order = rule.get("max_order", 1000000)
    buy_step = rule.get("buy_step", 100)
    buy_min = rule.get("buy_min", 100)
    sell_step = rule.get("sell_step", 100)
    sell_min = rule.get("sell_min", 100)

    if delta > 0:
        # 买入必须为整手递增单位
        qty = min(int(delta // buy_step) * buy_step, max_order)
        return qty if qty >= buy_min else 0

    # 卖出逻辑
    wanted = min(int(abs(delta)), int(held), max_order)
    if not wanted:
        return 0

    # 若为全部清仓：允许将持有的零股一次性全数卖出 (A 股法定规则)
    if wanted == int(held):
        return -wanted

    # 非全量清仓：必须按卖出最小递增单位规整，不可主动拆分产生新的零股
    qty = int(wanted // sell_step) * sell_step
    return -qty if qty >= sell_min else 0
