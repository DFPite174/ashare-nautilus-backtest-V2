"""
中国 A 股 8 项核心适配规则全面校验套件 (All A-Share Test Items Validation)
对应官方与机构级清单：
1. 板块差异化涨跌停限制 (主板/创业板/科创板/北交所/ST/新股/退市整理期)
2. T+1 可卖数量与状态流转 (日内做T、跨日结转、超额卖出拦截)
3. 停牌、无行情与挂单撤销处理 (SUSPENDED / ZERO_VOLUME)
4. 退市持仓处理 (30天无行情强制清算与折价计提)
5. 交易手数规整与零股一次性清仓 (主板100股/科创板200股起+1股递增/零股全量卖出)
6. 法定费税模型 (最低5元佣金、单边印花税万五、双边过户费十万分之一)
7. TradingState (Active / Reducing / Halted / Buy-Only) 联动映射
8. 行情前收盘价 (prev_close) 对齐与注册制 2% 申报价格笼子
"""

import pytest
from decimal import Decimal
from datetime import datetime

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, ClientOrderId, TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders import MarketOrder

from engine.fee_model import AShareFeeModel
from engine.golden_ledger import GoldenLedger
from engine.market_rules import (
    calculate_price_limits,
    validate_price_collar,
    get_trading_state,
    handle_delisting_valuation,
    block_reason,
    round_order,
)
from contracts import Fill, OrderSide as ContractOrderSide


# ==============================================================================
# 【Item 1】板块差异化涨跌停校验
# ==============================================================================
def test_item1_board_differentiated_price_limits():
    """验证各板块的法定涨跌幅度与四舍五入规则"""
    prev_close = 10.00

    # 1. 主板常规标的：±10% -> [9.00, 11.00]
    up, down, state = calculate_price_limits(prev_close, board="MAIN", is_st=False)
    assert up == 11.00
    assert down == 9.00
    assert state == "BOUNDED"

    # 2. 主板 ST / *ST 风险警示标的：±5% -> [9.50, 10.50]
    up_st, down_st, state_st = calculate_price_limits(prev_close, board="MAIN", is_st=True)
    assert up_st == 10.50
    assert down_st == 9.50
    assert state_st == "BOUNDED"

    # 2.1 主板 ST 在 2026-07 规则调整后：实施 ±10% 涨跌幅
    up_st_2026, down_st_2026, _ = calculate_price_limits(prev_close, board="MAIN", is_st=True, rule_date="2026-07-02")
    assert up_st_2026 == 11.00
    assert down_st_2026 == 9.00

    # 3. 创业板 (CYB)：常规与 ST 均为 ±20% -> [8.00, 12.00]
    up_cyb, down_cyb, state_cyb = calculate_price_limits(prev_close, board="CYB")
    assert up_cyb == 12.00
    assert down_cyb == 8.00
    assert state_cyb == "BOUNDED"
    up_cyb_st, down_cyb_st, _ = calculate_price_limits(prev_close, board="CYB", is_st=True)
    assert up_cyb_st == 12.00
    assert down_cyb_st == 8.00

    # 4. 科创板 (KCB)：常规与 ST 均为 ±20% -> [8.00, 12.00]
    up_kcb, down_kcb, state_kcb = calculate_price_limits(prev_close, board="科创板")
    assert up_kcb == 12.00
    assert down_kcb == 8.00
    assert state_kcb == "BOUNDED"
    up_kcb_st, down_kcb_st, _ = calculate_price_limits(prev_close, board="科创板", is_st=True)
    assert up_kcb_st == 12.00
    assert down_kcb_st == 8.00

    # 5. 北交所 (BSE)：仅上市首日无涨跌幅，次日起恢复 ±30%
    up_bse_d1, _, state_bse_d1 = calculate_price_limits(prev_close, board="BSE", days_since_ipo=1)
    assert state_bse_d1 == "UNBOUNDED"
    up_bse_d2, down_bse_d2, state_bse_d2 = calculate_price_limits(prev_close, board="BSE", days_since_ipo=2)
    assert state_bse_d2 == "BOUNDED"
    assert up_bse_d2 == 13.00
    assert down_bse_d2 == 7.00

    # 6. 主板/双创前 5 日无限制
    up_ipo, down_ipo, state_ipo = calculate_price_limits(prev_close, board="MAIN", days_since_ipo=3)
    assert up_ipo is None
    assert down_ipo is None
    assert state_ipo == "UNBOUNDED"

    # 7. 退市整理期首日：无涨跌幅限制
    up_delist, down_delist, state_delist = calculate_price_limits(
        prev_close, board="MAIN", is_delisting=True, delisting_day=1
    )
    assert up_delist is None
    assert down_delist is None
    assert state_delist == "UNBOUNDED"

    # 8. 未知板块必须显式拒绝，绝不静默回退
    with pytest.raises(ValueError, match="Unknown trading board"):
        calculate_price_limits(prev_close, board="INVALID_BOARD")

    # 9. 4舍5入到分精度检验 (如 prev_close = 10.35)
    up_frac, down_frac, _ = calculate_price_limits(10.35, board="MAIN")
    assert up_frac == 11.39
    assert down_frac in (9.31, 9.32)


# ==============================================================================
# 【Item 2】T+1 可卖数量与状态流转检查 (含日内做 T)
# ==============================================================================
def test_item2_t1_available_position_state_machine():
    """验证 T+1 可卖数量、同日锁仓拦截以及日内回转(做T)支持"""
    ledger = GoldenLedger(initial_cash=Decimal("1000000.0"))

    # --- Day 1 (2025-05-06) ---
    ledger.on_trade_day_start("2025-05-06")
    assert ledger.tradable_positions.get("600519.SH", Decimal(0)) == Decimal(0)

    # 场景 A: 买入 1000 股
    fill_buy1 = Fill(
        fill_id="f1", symbol="600519.SH", side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"), price=Decimal("100.0"), fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0)
    )
    ledger.apply_fill(fill_buy1, "2025-05-06")
    assert ledger.positions["600519.SH"] == Decimal("1000.0")
    assert ledger.tradable_positions.get("600519.SH", Decimal(0)) == Decimal(0)

    # 场景 B: 同日尝试卖出 500 股 -> 必须触发 T+1 拒绝
    fill_sell_illegal = Fill(
        fill_id="f2", symbol="600519.SH", side=ContractOrderSide.SELL,
        quantity=Decimal("500.0"), price=Decimal("102.0"), fee=Decimal("25.0"),
        timestamp=datetime(2025, 5, 6, 14, 0, 0)
    )
    with pytest.raises(ValueError, match="T\\+1 violation"):
        ledger.apply_fill(fill_sell_illegal, "2025-05-06")

    # --- Day 2 (2025-05-07) 跨日结转 ---
    ledger.on_trade_day_start("2025-05-07")
    # 之前买入的 1000 股已全部转化为可卖股数
    assert ledger.tradable_positions["600519.SH"] == Decimal("1000.0")

    # 场景 C: 日内做 T 操作
    # 1. 盘中新买入 500 股 (今日新增锁定)
    fill_buy2 = Fill(
        fill_id="f3", symbol="600519.SH", side=ContractOrderSide.BUY,
        quantity=Decimal("500.0"), price=Decimal("101.0"), fee=Decimal("15.0"),
        timestamp=datetime(2025, 5, 7, 10, 0, 0)
    )
    ledger.apply_fill(fill_buy2, "2025-05-07")
    assert ledger.positions["600519.SH"] == Decimal("1500.0")  # 总持仓 1500
    assert ledger.tradable_positions["600519.SH"] == Decimal("1000.0") # 可卖仍为 1000

    # 2. 盘中高抛卖出 800 股 (合规做 T)
    fill_sell_t = Fill(
        fill_id="f4", symbol="600519.SH", side=ContractOrderSide.SELL,
        quantity=Decimal("800.0"), price=Decimal("105.0"), fee=Decimal("60.0"),
        timestamp=datetime(2025, 5, 7, 14, 0, 0)
    )
    ledger.apply_fill(fill_sell_t, "2025-05-07")
    assert ledger.positions["600519.SH"] == Decimal("700.0")  # 总持仓 700 股
    assert ledger.tradable_positions["600519.SH"] == Decimal("200.0") # 可卖剩 200 股

    # 3. 再次尝试超额卖出 300 股 -> 可卖仅剩 200，拦截！
    fill_sell_exceed = Fill(
        fill_id="f5", symbol="600519.SH", side=ContractOrderSide.SELL,
        quantity=Decimal("300.0"), price=Decimal("105.0"), fee=Decimal("20.0"),
        timestamp=datetime(2025, 5, 7, 14, 30, 0)
    )
    with pytest.raises(ValueError, match="T\\+1 violation"):
        ledger.apply_fill(fill_sell_exceed, "2025-05-07")


# ==============================================================================
# 【Item 3】停牌与无行情处理
# ==============================================================================
def test_item3_suspension_and_no_data_handling():
    """验证停牌标的拦截与成交量为0时的订单阻断"""
    # 1. 停牌标的挂单校验
    suspended_rule = {
        "trade_status": "SUSPENDED",
        "limit_state": "UNAVAILABLE",
    }
    bar_normal = {"close": 10.0, "volume": 1000}
    assert block_reason(suspended_rule, bar_normal, "buy") == "SUSPENDED"
    assert block_reason(suspended_rule, bar_normal, "sell") == "SUSPENDED"

    # 2. 无行情 / 成交量为 0 校验
    open_rule = {
        "trade_status": "OPEN",
        "limit_state": "BOUNDED",
        "up_limit": 11.0,
        "down_limit": 9.0,
    }
    bar_zero_volume = {"close": 10.0, "volume": 0}
    assert block_reason(open_rule, bar_zero_volume, "buy") == "ZERO_VOLUME"


# ==============================================================================
# 【Item 4】退市持仓处理 (30天无行情清算/计提)
# ==============================================================================
def test_item4_delisting_liquidation_and_haircut():
    """验证持仓标的长期无行情时的折价计提与财务真实性 (不凭空变现)"""
    # 正常持仓 20 天无行情 -> 继续持有观察
    res_normal = handle_delisting_valuation(held_shares=1000, last_price=10.0, days_no_quote=20)
    assert res_normal["action"] == "HOLD"
    assert res_normal["is_cash_realized"] is False
    assert res_normal["assessed_market_value"] == 10000.00

    # 超过 30 交易日无行情 -> 触发资产折价准备 (折价 50%)
    res_haircut = handle_delisting_valuation(
        held_shares=1000, last_price=10.0, days_no_quote=35, forced_liquidation_days=30, haircut=0.50
    )
    assert res_haircut["action"] == "HAIRCUT_VALUATION"
    assert res_haircut["assessed_market_value"] == 5000.00
    assert res_haircut["valuation_loss"] == 5000.00
    assert res_haircut["haircut_applied"] == 0.50
    assert res_haircut["is_cash_realized"] is False


# ==============================================================================
# 【Item 5】申报数量单位与取整 (整手与零股清仓)
# ==============================================================================
def test_item5_lot_size_and_odd_lot_rules():
    """验证沪深主板整手(100股)、科创板(200起+1股)以及零股一次性卖出规则"""
    main_rule = {
        "buy_step": 100, "buy_min": 100,
        "sell_step": 100, "sell_min": 100,
        "max_order": 1000000,
    }
    # 1. 主板买入：低于 100 股拒单
    assert round_order(50, 0, main_rule) == 0
    # 2. 主板买入：250 股规整为整手 200 股
    assert round_order(250, 0, main_rule) == 200

    # 3. 零股全量清仓：持有 183 股 (分红配股残留)，全额卖出 183 股 -> 法定允许！
    assert round_order(-183, 183, main_rule) == -183

    # 4. 零股部分卖出：持有 183 股，欲卖 150 股 -> 必须规整为 100 股，禁止拆零
    assert round_order(-150, 183, main_rule) == -100

    # 5. 科创板特殊规则：200 股起购，之后可按 1 股递增
    star_rule = {
        "buy_step": 1, "buy_min": 200,
        "sell_step": 1, "sell_min": 1,
        "max_order": 100000,
    }
    assert round_order(190, 0, star_rule) == 0      # 低于 200 股起购门槛
    assert round_order(201, 0, star_rule) == 201    # 允许 201 股申报


# ==============================================================================
# 【Item 6】A 股法定费用模型 (最低5元佣金、印花税、过户费)
# ==============================================================================
def test_item6_statutory_fee_model():
    """验证 NautilusTrader 继承的 AShareFeeModel 扣费真实性"""
    model = AShareFeeModel(
        commission_rate=0.0003,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        currency=CNY,
    )
    inst_id = InstrumentId(Symbol("600519"), Venue("SSE"))

    def make_order(side: OrderSide, cid: str = "O-1") -> MarketOrder:
        return MarketOrder(
            trader_id=TraderId("TRADER-001"),
            strategy_id=StrategyId("STRATEGY-001"),
            instrument_id=inst_id,
            client_order_id=ClientOrderId(cid),
            order_side=side,
            quantity=Quantity.from_int(100),
            time_in_force=TimeInForce.GTC,
            init_id=UUID4(),
            ts_init=0,
        )

    # 1. 小额买入 (100 股 @ 10 元 = 1000 元)
    # 佣金 0.3 元 < 5 元，触发 5 元保底；过户费 0.01 元；印花税 0 元 -> 总计 5.01 元
    fee_buy_small = model.get_commission(
        make_order(OrderSide.BUY, cid="O-BUY"), Quantity.from_int(100), Price.from_str("10.00")
    )
    assert fee_buy_small.as_double() == 5.01

    # 2. 大额卖出 (1000 股 @ 100 元 = 100,000 元)
    # 佣金 30 元 (> 5 元)；过户费 1 元；卖方印花税 50 元 -> 总计 81.00 元
    fee_sell_large = model.get_commission(
        make_order(OrderSide.SELL, cid="O-SELL"), Quantity.from_int(1000), Price.from_str("100.00")
    )
    assert fee_sell_large.as_double() == 81.00


# ==============================================================================
# 【Item 7】TradingState 联动状态判定
# ==============================================================================
def test_item7_trading_state_t1_coupling():
    """验证交易标的状态根据 T+1 锁仓与停牌的动态联动"""
    # 1. 标的停牌 -> 强制 HALTED
    assert get_trading_state(is_suspended=True, held_qty=1000, sellable_qty=1000) == "HALTED"

    # 2. 当日刚买入，已买入但可卖为 0 -> 状态为 BUY_ONLY (禁止发任何减仓卖单)
    assert get_trading_state(is_suspended=False, held_qty=1000, sellable_qty=0) == "BUY_ONLY"

    # 3. 拥有正常可用持仓 -> 完全 ACTIVE
    assert get_trading_state(is_suspended=False, held_qty=1000, sellable_qty=1000) == "ACTIVE"


# ==============================================================================
# 【Item 8】行情前收盘价 (prev_close) 对齐与注册制 2% 价格笼子
# ==============================================================================
def test_item8_prev_close_and_price_collar():
    """验证涨跌停基准前收盘价依赖与 2% 申报价格笼子合规校验"""
    # 1. 涨跌停依赖准确的 prev_close
    prev_close = 20.00
    up, down, _ = calculate_price_limits(prev_close, board="MAIN")
    assert up == 22.00
    assert down == 18.00

    # 2. 全面注册制 2% 申报价格笼子校验 (基准价格 20.00 元)
    # 买入限制：不超过 20.00 * 1.02 = 20.40 元
    assert validate_price_collar(order_price=20.30, benchmark_price=20.00, side="buy") is True
    assert validate_price_collar(order_price=20.40, benchmark_price=20.00, side="buy") is True
    assert validate_price_collar(order_price=20.45, benchmark_price=20.00, side="buy") is False  # 超出 2% 废单！

    # 卖出限制：不低于 20.00 * 0.98 = 19.60 元
    assert validate_price_collar(order_price=19.70, benchmark_price=20.00, side="sell") is True
    assert validate_price_collar(order_price=19.60, benchmark_price=20.00, side="sell") is True
    assert validate_price_collar(order_price=19.55, benchmark_price=20.00, side="sell") is False # 低于 98% 废单！
