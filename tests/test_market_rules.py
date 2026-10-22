"""
A 股交易制度规则引擎单测 - 验证手数规整、零股清仓、涨跌停与停牌阻断
"""

import pytest
from engine.market_rules import round_order, block_reason, validate_rules


def test_main_board_lot_rounding():
    rule = {
        "buy_step": 100,
        "buy_min": 100,
        "sell_step": 100,
        "sell_min": 100,
        "max_order": 1000000,
    }
    # 买入 250 股 -> 规整为 200 股
    assert round_order(250, 0, rule) == 200
    # 买入 80 股 -> 低于最小买入单位 100，返回 0
    assert round_order(80, 0, rule) == 0
    # 卖出 250 股，持有 500 股 -> 规整为卖出 200 股
    assert round_order(-250, 500, rule) == -200


def test_odd_lot_full_liquidation():
    rule = {
        "buy_step": 100,
        "buy_min": 100,
        "sell_step": 100,
        "sell_min": 100,
        "max_order": 1000000,
    }
    # 持有 158 股 (因送转产生的零股)，全量清仓卖出 158 股 -> 允许卖出 -158 股
    assert round_order(-158, 158, rule) == -158
    # 卖出 120 股，持有 158 股 -> 非全量清仓，规整为卖出整手 100 股
    assert round_order(-120, 158, rule) == -100


def test_star_market_rules():
    # 科创板：200 股起购，之后可按 1 股递增
    star_rule = {
        "buy_step": 1,
        "buy_min": 200,
        "sell_step": 1,
        "sell_min": 1,
        "max_order": 100000,
    }
    # 买入 205 股 -> 允许 205 股
    assert round_order(205, 0, star_rule) == 205
    # 买入 180 股 -> 低于起购门槛 200，返回 0
    assert round_order(180, 0, star_rule) == 0


def test_block_reason_limit_up_down_and_suspension():
    rule = {
        "trade_status": "OPEN",
        "limit_state": "BOUNDED",
        "up_limit": 11.00,
        "down_limit": 9.00,
    }
    # 涨停收盘价买入 -> 阻断
    bar_limit_up = {"close": 11.00, "volume": 10000}
    assert block_reason(rule, bar_limit_up, "buy") == "LIMIT_UP_CLOSE"
    assert block_reason(rule, bar_limit_up, "sell") is None

    # 跌停收盘价卖出 -> 阻断
    bar_limit_down = {"close": 9.00, "volume": 10000}
    assert block_reason(rule, bar_limit_down, "sell") == "LIMIT_DOWN_CLOSE"
    assert block_reason(rule, bar_limit_down, "buy") is None

    # 停牌标的 -> 阻断
    suspended_rule = {
        "trade_status": "SUSPENDED",
        "limit_state": "UNAVAILABLE",
    }
    assert block_reason(suspended_rule, bar_limit_up, "buy") == "SUSPENDED"
