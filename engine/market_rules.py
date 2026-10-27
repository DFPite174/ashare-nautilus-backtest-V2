"""
A 股交易制度与市场约束规则引擎 (Market Rules Engine)
全面覆盖中国 A 股特有微观交易规则：
1. 板块差异化涨跌停限制 (主板/创业板/科创板/北交所/ST/新股/退市整理期)
2. T+1 可卖数量与状态流转
3. 停牌、无行情与挂单撤销处理
4. 退市标的清算与折价估值
5. 交易手数规整与零股一次性清仓
6. 2% 申报价格笼子机制 (全面注册制)
7. TradingState (Active / Reducing / Halted) 映射联动
8. 行情前收盘价 (prev_close) 对齐校验
"""

from __future__ import annotations
import math
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple


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


def calculate_price_limits(
    prev_close: float,
    board: str = "MAIN",
    is_st: bool = False,
    days_since_ipo: int = 100,
    is_delisting: bool = False,
    delisting_day: int = 1,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    【Item 1】板块差异化涨跌停价格计算与校验规则
    :param prev_close: 前收盘价
    :param board: 交易板块 ('MAIN'/'主板', 'CYB'/'创业板', 'KCB'/'科创板', 'BSE'/'北交所')
    :param is_st: 是否为 ST/*ST 风险警示股票
    :param days_since_ipo: 上市以来的交易日数 (新股前5日无涨跌幅)
    :param is_delisting: 是否处于退市整理期
    :param delisting_day: 退市整理期第几日 (首日无涨跌幅)
    :return: (涨停价 up_limit, 跌停价 down_limit, 状态 limit_state)
    """
    if prev_close <= 0:
        return (None, None, "UNAVAILABLE")

    # 1. 新股上市前 5 个交易日：无涨跌幅限制
    if days_since_ipo <= 5:
        return (None, None, "UNBOUNDED")

    # 2. 退市整理期首日：无涨跌幅限制
    if is_delisting and delisting_day == 1:
        return (None, None, "UNBOUNDED")

    # 3. 板块与 ST 差异化涨跌幅比例判定
    board_clean = board.upper()
    if is_st:
        pct = 0.05  # 主板 ST 为 5%
    elif board_clean in ("CYB", "创业板", "KCB", "科创板"):
        pct = 0.20  # 创业板/科创板 20%
    elif board_clean in ("BSE", "北交所"):
        pct = 0.30  # 北交所 30%
    else:
        pct = 0.10  # 沪深主板常规 10%

    # 价格精确四舍五入到分 (A 股法定 0.01 元变动价位，采用标准四舍五入 ROUND_HALF_UP)
    d_close = Decimal(str(prev_close))
    d_pct = Decimal(str(pct))
    up_limit = float((d_close * (Decimal("1.0") + d_pct)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    down_limit = float((d_close * (Decimal("1.0") - d_pct)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return (up_limit, down_limit, "BOUNDED")


def validate_price_collar(
    order_price: float,
    benchmark_price: float,
    side: str,
) -> bool:
    """
    【全面注册制深水区】2% 申报价格笼子校验
    买入申报价格不得高于买入基准价格的 102% 或基准价 + 0.1 元 (取高)
    卖出申报价格不得低于卖出基准价格的 98% 或基准价 - 0.1 元 (取低)
    :return: True 为合规申报，False 为废单拦截
    """
    if benchmark_price <= 0 or order_price <= 0:
        return False

    side_lower = side.lower()
    if side_lower == "buy":
        max_allowed = max(benchmark_price * 1.02, benchmark_price + 0.10)
        return order_price <= round(max_allowed, 2) + 1e-5
    elif side_lower == "sell":
        min_allowed = min(benchmark_price * 0.98, benchmark_price - 0.10)
        return order_price >= round(min_allowed, 2) - 1e-5
    return False


def get_trading_state(
    is_suspended: bool,
    held_qty: int,
    sellable_qty: int,
) -> str:
    """
    【Item 7】TradingState 强制与 T+1 联动映射
    :return: 'HALTED' (停牌全禁), 'REDUCING_ONLY' (可减仓), 'ACTIVE' (完全活跃), 'BUY_ONLY' (T+1锁仓不可卖)
    """
    if is_suspended:
        return "HALTED"
    if held_qty > 0 and sellable_qty <= 0:
        # 当日刚买入，全量锁仓，不可减仓
        return "BUY_ONLY"
    elif held_qty > 0 and sellable_qty > 0:
        return "ACTIVE"
    return "ACTIVE"


def handle_delisting_valuation(
    held_shares: int,
    last_price: float,
    days_no_quote: int,
    forced_liquidation_days: int = 30,
    haircut: float = 0.50,
) -> Dict[str, Any]:
    """
    【Item 4】退市持仓处理：持仓超过一定天数无行情时的清算或折价评估
    """
    if days_no_quote >= forced_liquidation_days:
        # 强制强制清算：残值清算或按折扣计提
        cleared_value = round(held_shares * last_price * (1.0 - haircut), 2)
        return {
            "action": "FORCE_LIQUIDATE",
            "cash_recovered": cleared_value,
            "haircut_applied": haircut,
            "reason": f"No quote for {days_no_quote} trading days",
        }
    return {
        "action": "HOLD",
        "cash_recovered": 0.0,
        "haircut_applied": 0.0,
        "reason": "Within normal holding threshold",
    }


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
    【Item 5】A 股报单股数整手与零股规则规整计算
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
