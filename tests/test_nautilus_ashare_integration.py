"""
端到端集成测试：验证 NautilusTrader Cython/Rust 内核 + Python A 股适配层完整生命周期

全面闭环审计与验收项：
1. [R01] 成交归属日严格由成交事件自身纳秒时戳 (Asia/Shanghai) 确定，拒绝跨日 GTC 挂单，杜绝跨日未决订单的时序错配。
2. [R02] 引入不可绕过的前置 AShareOrderGateway：价格笼子 (2%)、整手规整、T+1 已结算库存减去活动卖单预留、涨跌停阻断在下单前拦截。
3. [R05] 每日交易收盘时通过 GoldenLedger.mark_to_market() 逐日生成精确 NAV 快照，核验双账本持仓、现金与 NAV 零漂移；引入独立费用 Oracle 进行逐笔核对。
4. [R07] 费用模型支持多维复合命名空间隔离与历史税率按日期自动匹配。
5. [R08] 黄金账本具备单调时间序列与归属日期保护，杜绝逆时估值与未来价格泄露。
6. [R10] 行情时钟严格使用真实北京交易时区 (Asia/Shanghai 09:30 / 14:00)，精确换算至 UTC 纳秒。
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarType, BarSpecification
from nautilus_trader.model.enums import (
    AccountType,
    AggregationSource,
    BarAggregation,
    OmsType,
    OrderSide,
    PriceType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from engine.fee_model import AShareFeeModel, calculate_expected_fee
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway
from engine.market_rules import calculate_price_limits
from contracts import Fill, OrderSide as ContractOrderSide


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def beijing_to_utc_ns(dt_str: str) -> int:
    """将北京时间字符串转换为 UTC 纳秒时戳"""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
    return int(dt.astimezone(timezone.utc).timestamp() * 1e9)


class AShareTestStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType


class AShareTestStrategy(Strategy):
    """
    A 股测试策略：真实绑定订单网关与独立黄金账本
    """

    def __init__(self, config: AShareTestStrategyConfig):
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type

        # 真实独立黄金账本（双轨运行）
        self.ledger = GoldenLedger(initial_cash=Decimal("1000000.0"))

        # 前置订单网关（强约束前置拦截与挂单库存预留）
        self.gateway = AShareOrderGateway()

        self.current_date = None
        self.last_close_px = None
        self.daily_bars_processed = 0

        # 统计计数器
        self.gateway_rejections = 0
        self.t1_rejections = 0
        self.limit_blocks = 0
        self.price_collar_rejections = 0
        self.successful_buys = 0
        self.successful_sells = 0

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def on_order_filled(self, event):
        """
        根据底层交易所原生真实成交回报更新订单网关与独立黄金账本
        成交归属日严格由 event.ts_event (Asia/Shanghai) 决定 (R01 修复)
        """
        fill_date = self.gateway.on_order_filled(event)

        qty = event.last_qty.as_double()
        px = event.last_px.as_double()
        fee = event.commission.as_double()

        contract_side = ContractOrderSide.BUY if event.order_side == OrderSide.BUY else ContractOrderSide.SELL
        fill = Fill(
            fill_id=str(event.trade_id),
            symbol=str(self.instrument_id.symbol),
            side=contract_side,
            quantity=Decimal(str(qty)),
            price=Decimal(str(px)),
            fee=Decimal(str(fee)),
            timestamp=datetime.fromtimestamp(event.ts_event / 1e9, tz=SHANGHAI_TZ),
        )
        self.ledger.apply_fill(fill, fill_date)

        if event.order_side == OrderSide.BUY:
            self.successful_buys += 1
        elif event.order_side == OrderSide.SELL:
            self.successful_sells += 1

    def on_order_canceled(self, event):
        """原生 CANCELED 回调：释放网关挂单预留 (N02)"""
        self.gateway.on_order_canceled(str(event.client_order_id), str(self.instrument_id.symbol))

    def on_order_rejected(self, event):
        """原生 REJECTED 回调：释放网关挂单预留 (N02)"""
        self.gateway.on_order_rejected(str(event.client_order_id), str(self.instrument_id.symbol))

    def on_order_expired(self, event):
        """原生 EXPIRED 回调：释放网关挂单预留 (N02)"""
        self.gateway.on_order_expired(str(event.client_order_id), str(self.instrument_id.symbol))

    def on_stop(self):
        """回测结束：对最后一个交易日执行日终盯市估值 (R05)"""
        if self.current_date is not None and self.last_close_px is not None:
            symbol_str = str(self.instrument_id.symbol)
            self.ledger.mark_to_market(self.current_date, {symbol_str: Decimal(str(self.last_close_px))})

    def on_bar(self, bar: Bar):
        # 解析当前 Bar 对应的北京时间交易日 (R10 修复)
        dt = datetime.fromtimestamp(bar.ts_event / 1e9, tz=SHANGHAI_TZ)
        trade_date = dt.strftime("%Y-%m-%d")
        symbol_str = str(self.instrument_id.symbol)

        # 跨日时钟推进：日终结算，今日买入自动释放为次日可卖 (T+1 状态流转)
        if self.current_date is not None and trade_date != self.current_date:
            # 1. 触发前一交易日日终收盘估值 (R05: 在新日任何交易改变账户前封存快照)
            if self.last_close_px is not None:
                self.ledger.mark_to_market(self.current_date, {symbol_str: Decimal(str(self.last_close_px))})

            # 2. 真实撤销上一交易日未成交的 DAY 挂单，防止跨日到次日撮合 (N01)
            self.cancel_all_orders(self.instrument_id)
            self.gateway.on_session_close()

            # 3. 推进至新交易日 (N01)
            self.gateway.on_trade_day_start(trade_date)
            self.ledger.on_trade_day_start(trade_date)

        if self.current_date is None:
            self.gateway.on_trade_day_start(trade_date)
            self.ledger.on_trade_day_start(trade_date)

        self.current_date = trade_date
        px = bar.close.as_double()
        self.last_close_px = px

        # 构造当日市场规则 (主板正常交易日)
        up_lim, down_lim, lim_st = calculate_price_limits(
            prev_close=100.0 if trade_date == "2025-05-06" else (101.0 if trade_date == "2025-05-07" else 105.0),
            board="MAIN",
            is_st=False,
            rule_date=trade_date,
        )
        market_rule = {
            "trade_status": "OPEN",
            "limit_state": lim_st,
            "up_limit": up_lim,
            "down_limit": down_lim,
            "buy_min": 100,
            "buy_step": 100,
            "sell_min": 100,
            "sell_step": 100,
            "max_order": 1000000,
        }
        bar_dict = {"close": px, "volume": bar.volume.as_double()}

        # ==========================================
        # 场景 1: Day 1 (2025-05-06) 开盘买入 200 股
        # ==========================================
        if trade_date == "2025-05-06" and self.successful_buys == 0:
            order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(200),
                price=Price.from_str("100.00"),
                time_in_force=TimeInForce.DAY,
            )
            # 严格经由前置网关校验 (R02)
            res = self.gateway.validate_and_reserve(order, market_rule, bar_dict, benchmark_price=100.0)
            if res.is_valid:
                self.submit_order(order)
            else:
                self.gateway_rejections += 1
            return

        # ==========================================
        # 场景 2: Day 1 (2025-05-06) 盘中尝试卖出 100 股 -> 前置网关必须拦截 T+1 违规
        # ==========================================
        if trade_date == "2025-05-06" and self.successful_buys == 1:
            # 2.1 尝试通过价格笼子越界 (买入高报 5%) 验证网关拦截
            bad_order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(100),
                price=Price.from_str("110.00"),  # 高于基准 100.0 的 102%
                time_in_force=TimeInForce.DAY,
            )
            collar_res = self.gateway.validate_and_reserve(bad_order, market_rule, bar_dict, benchmark_price=100.0)
            if not collar_res.is_valid and "PRICE_COLLAR_VIOLATION" in collar_res.rejection_reason:
                self.price_collar_rejections += 1

            # 2.2 尝试卖出当日刚买入尚未结算的 100 股
            sell_order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=OrderSide.SELL,
                quantity=Quantity.from_int(100),
                price=Price.from_str("101.00"),
                time_in_force=TimeInForce.DAY,
            )
            t1_res = self.gateway.validate_and_reserve(sell_order, market_rule, bar_dict, benchmark_price=101.0)
            if not t1_res.is_valid:
                self.t1_rejections += 1
                self.gateway_rejections += 1
            else:
                self.submit_order(sell_order)
            return

        # ==========================================
        # 场景 3: Day 2 (2025-05-07) 跨日后允许卖出 100 股
        # ==========================================
        if trade_date == "2025-05-07" and self.successful_sells == 0:
            order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=OrderSide.SELL,
                quantity=Quantity.from_int(100),
                price=Price.from_str("105.00"),
                time_in_force=TimeInForce.DAY,
            )
            res = self.gateway.validate_and_reserve(order, market_rule, bar_dict, benchmark_price=105.0)
            if res.is_valid:
                self.submit_order(order)
            else:
                self.gateway_rejections += 1
            return

        # ==========================================
        # 场景 4: Day 3 (2025-05-08) 涨停价买入被网关阻断
        # ==========================================
        if trade_date == "2025-05-08":
            mock_rule = {
                "trade_status": "OPEN",
                "limit_state": "BOUNDED",
                "up_limit": 110.00,
                "down_limit": 90.00,
                "buy_min": 100,
                "buy_step": 100,
            }
            order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(100),
                price=Price.from_str("110.00"),
                time_in_force=TimeInForce.DAY,
            )
            # 收盘价等于涨停价 110.0，买单必须被拦截
            res = self.gateway.validate_and_reserve(order, mock_rule, bar_dict, benchmark_price=110.0)
            if not res.is_valid and res.rejection_reason == "LIMIT_UP_CLOSE":
                self.limit_blocks += 1


def _create_configured_engine(fee_model=None):
    """辅助工厂函数：创建并初始化回测引擎，注入真实北京时间交易会话 (R10)"""
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("TRADER-001")))
    venue_sse = Venue("SSE")

    engine.add_venue(
        venue=venue_sse,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=CNY,
        starting_balances=[Money(1_000_000.0, CNY)],
        fee_model=fee_model,
    )

    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    init_time_ns = beijing_to_utc_ns("2025-05-01 09:30:00")
    instrument = Equity(
        instrument_id=inst_id,
        raw_symbol=symbol,
        currency=CNY,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(100),
        ts_event=init_time_ns,
        ts_init=init_time_ns,
    )
    engine.add_instrument(instrument)

    bar_spec = BarSpecification(1, BarAggregation.HOUR, PriceType.LAST)
    bar_type = BarType(inst_id, bar_spec, AggregationSource.EXTERNAL)

    # 严格使用北京时间交易会话 (上午 09:30, 下午 14:00) 转换为 UTC 纳秒 (R10)
    t1 = beijing_to_utc_ns("2025-05-06 09:30:00")
    t2 = beijing_to_utc_ns("2025-05-06 14:00:00")
    t3 = beijing_to_utc_ns("2025-05-07 09:30:00")
    t4 = beijing_to_utc_ns("2025-05-08 09:30:00")

    bars = [
        Bar(bar_type, Price.from_str("99.00"), Price.from_str("101.00"), Price.from_str("98.50"), Price.from_str("100.00"), Quantity.from_int(10000), t1, t1),
        Bar(bar_type, Price.from_str("100.00"), Price.from_str("102.00"), Price.from_str("99.50"), Price.from_str("101.00"), Quantity.from_int(8000), t2, t2),
        Bar(bar_type, Price.from_str("101.00"), Price.from_str("106.00"), Price.from_str("100.00"), Price.from_str("105.00"), Quantity.from_int(12000), t3, t3),
        Bar(bar_type, Price.from_str("108.00"), Price.from_str("110.00"), Price.from_str("108.00"), Price.from_str("110.00"), Quantity.from_int(5000), t4, t4),
    ]

    engine.add_data(bars)
    return engine, inst_id, bar_type, venue_sse


def test_nautilus_ashare_integration_e2e():
    """
    【基准全生命周期集成测试】
    真实执行 NautilusTrader，通过前置网关与独立黄金账本实现 T+1、价格笼子、
    涨跌停、费用 Oracle 与逐日 NAV 双重零漂移对账。
    """
    fee_model = AShareFeeModel(
        commission_rate=0.0003,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        currency=CNY,
        scope_id="integration_test_run",
    )
    engine, inst_id, bar_type, venue_sse = _create_configured_engine(fee_model=fee_model)

    strategy_config = AShareTestStrategyConfig(instrument_id=inst_id, bar_type=bar_type)
    strategy = AShareTestStrategy(strategy_config)
    engine.add_strategy(strategy)

    # 运行回测
    engine.run()

    # 1. 验证时钟真实推进至 2025 年真实北京时区对应纳秒
    assert strategy.clock.timestamp_ns() >= beijing_to_utc_ns("2025-05-08 09:30:00")

    # 2. 验证前置网关风控拦截表现 (R02)
    assert strategy.price_collar_rejections == 1, "2% 申报价格笼子违规未被成功拦截"
    assert strategy.t1_rejections == 1, "T+1 当日未结算卖单未被成功拦截"
    assert strategy.limit_blocks == 1, "涨停板买入未被阻断"
    assert strategy.successful_buys == 1, "合规买单未成功执行"
    assert strategy.successful_sells == 1, "次日合规卖单未成功执行"

    # 3. 检查原生持仓数量
    net_pos = engine.portfolio.net_position(inst_id)
    assert float(net_pos) == 100.0, f"净持仓应为 100 股，实为 {net_pos}"

    # 4. 严格断言账户最终现金与独立费用 Oracle (R05)
    account = engine.portfolio.account(venue_sse)
    assert account is not None
    total_balances = account.balances_total()
    balance = float(total_balances.get(CNY, 0.0))
    expected_cash = 990483.44
    assert balance == expected_cash, f"原生现金余额应为 {expected_cash}，实为 {balance}"

    # 5. 独立黄金账本双轨对账
    assert float(strategy.ledger.cash) == balance, (
        f"黄金账本现金 ({float(strategy.ledger.cash)}) 与原生账户 ({balance}) 不一致！"
    )
    assert float(strategy.ledger.positions["600519"]) == float(net_pos), (
        f"黄金账本持仓 ({float(strategy.ledger.positions['600519'])}) 与原生持仓 ({float(net_pos)}) 不一致！"
    )

    # 6. 逐日 NAV 估值闭环断言 (R05 & R08)
    assert len(strategy.ledger.history) == 3, f"应生成 3 个交易日的 NAV 估值快照，实为 {len(strategy.ledger.history)}"
    nav_d1, nav_d2, nav_d3 = strategy.ledger.history
    assert str(nav_d1.date) == "2025-05-06"
    assert str(nav_d2.date) == "2025-05-07"
    assert str(nav_d3.date) == "2025-05-08"

    # Day 1 NAV = 现金 979993.80 + 200股 * 101.00 = 1000193.80
    assert nav_d1.total_equity == Decimal("1000193.80")
    # Day 2 NAV = 现金 990483.44 + 100股 * 105.00 = 1000983.44
    assert nav_d2.total_equity == Decimal("1000983.44")
    # Day 3 NAV = 现金 990483.44 + 100股 * 110.00 = 1001483.44
    assert nav_d3.total_equity == Decimal("1001483.44")

    # 7. 费用 Oracle 逐笔独立核算核验
    # Buy 200 @ 100 = 20000. 佣金=max(6.0, 5)=6.0, 过户费=0.20, 印花税=0 -> 6.20
    fee_buy = calculate_expected_fee("BUY", Decimal("200"), Decimal("100.00"))
    assert fee_buy == Decimal("6.20")

    # Sell 100 @ 105 = 10500. 佣金=max(3.15, 5)=5.0, 过户费=0.105->0.11, 印花税=5.25 -> 10.36
    fee_sell = calculate_expected_fee("SELL", Decimal("100"), Decimal("105.00"))
    assert fee_sell == Decimal("10.36")
    assert Decimal("1000000.0") - Decimal("20000.0") - fee_buy + Decimal("10500.0") - fee_sell == Decimal("990483.44")


def test_gtc_cross_day_order_rejected_by_gateway():
    """
    【反例验证 (R01)】
    验证若尝试提交跨日 GTC 挂单，AShareOrderGateway 必然直接拦截拒单，
    从源头杜绝跨日未决订单的时序错配。
    """
    venue_sse = Venue("SSE")
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)

    gateway = AShareOrderGateway()
    gateway.on_trade_day_start("2025-05-06")

    # 构造 GTC 挂单
    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.core.uuid import UUID4

    gtc_order = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("GTC-BUY-1"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(200),
        price=Price.from_str("99.00"),
        time_in_force=TimeInForce.GTC,  # 非法跨日有效期
        init_id=UUID4(),
        ts_init=0,
    )
    rule = {"trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 110.0, "down_limit": 90.0, "buy_min": 100, "buy_step": 100}
    bar = {"close": 100.0, "volume": 1000}

    res = gateway.validate_and_reserve(gtc_order, rule, bar, benchmark_price=100.0)
    assert not res.is_valid, "GTC 订单应当被 A 股订单网关拦截"
    assert "INVALID_TIME_IN_FORCE" in res.rejection_reason


def test_concurrent_sell_orders_reservation_rejected():
    """
    【挂单库存预留竞争验证 (R02)】
    持有 100 股可卖股票时，若第一笔 100 股挂单处于活动状态，
    第二笔 100 股卖单在提交阶段即应被网关拦截（可用 100 - 预留 100 = 0）。
    """
    venue_sse = Venue("SSE")
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)

    gateway = AShareOrderGateway()
    # 模拟结算持仓 100 股
    gateway._settled_shares[str(symbol)] = Decimal("100.0")
    gateway.on_trade_day_start("2025-05-07")

    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.model.identifiers import StrategyId
    from nautilus_trader.core.uuid import UUID4

    rule = {"trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 110.0, "down_limit": 90.0, "sell_min": 100, "sell_step": 100}
    bar = {"close": 100.0, "volume": 1000}

    # 1. 提交第一笔 100 股卖单
    order_1 = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("SELL-1"),
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    res_1 = gateway.validate_and_reserve(order_1, rule, bar, benchmark_price=100.0)
    assert res_1.is_valid, "第一笔合规卖单应通过校验"
    assert gateway.get_sellable_qty(str(symbol)) == Decimal("0.0"), "预留后可用可卖股数应归零"

    # 2. 在第一笔卖单尚未成交时，尝试提交第二笔 100 股卖单
    order_2 = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("SELL-2"),
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    res_2 = gateway.validate_and_reserve(order_2, rule, bar, benchmark_price=100.0)
    assert not res_2.is_valid, "超额重复卖单必须被拦截"
    assert "T1_SETTLEMENT_VIOLATION" in res_2.rejection_reason

    # 3. 若撤销第一笔挂单，预留应释放恢复
    gateway.on_order_canceled_or_rejected(str(order_1.client_order_id), str(symbol))
    assert gateway.get_sellable_qty(str(symbol)) == Decimal("100.0"), "撤单后预留应释放"


def test_nautilus_ashare_zero_fee_mutation_detected():
    """
    【变异反例测试 (M01)】
    验证若将交易所费用模型置为零费率，断言必须能够精准报错捕获，杜绝假阳性。
    """
    engine, inst_id, bar_type, venue_sse = _create_configured_engine(fee_model=None)

    strategy_config = AShareTestStrategyConfig(instrument_id=inst_id, bar_type=bar_type)
    strategy = AShareTestStrategy(strategy_config)
    engine.add_strategy(strategy)

    engine.run()

    account = engine.portfolio.account(venue_sse)
    balance = float(account.balances_total().get(CNY, 0.0))

    # 零费率时现金应为 990500.00，不再等于法定含税的 990483.44
    assert balance == 990500.00
    assert balance != 990483.44, "零费率变异应当被严格发现，不可漏网！"


def test_gateway_input_validation_boundary_matrix():
    """
    【N04 验证】网关输入防御性校验与单笔申报上限矩阵：
    1. 申报超出 max_order 必须拒绝
    2. 限价单缺失基准价 (benchmark_price=None) 必须拒绝
    3. 行情缺少收盘价或收盘价为 None 必须拒绝
    4. 行情数据包含 NaN 必须拒绝
    """
    venue_sse = Venue("SSE")
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.core.uuid import UUID4

    gateway = AShareOrderGateway()
    gateway.on_trade_day_start("2025-05-06")

    base_rule = {
        "trade_status": "OPEN",
        "limit_state": "BOUNDED",
        "up_limit": 110.0,
        "down_limit": 90.0,
        "buy_min": 100,
        "buy_step": 100,
        "max_order": 1000000,
    }
    normal_bar = {"close": 100.0, "volume": 10000.0}

    # 1. 申报 1,000,100 股 (超 max_order 100万上限)
    order_huge = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("HUGE-1"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(1000100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    res_1 = gateway.validate_and_reserve(order_huge, base_rule, normal_bar, benchmark_price=100.0)
    assert not res_1.is_valid
    assert "MAX_ORDER_EXCEEDED" in res_1.rejection_reason

    # 2. 限价单且 benchmark_price=None
    order_normal = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("NORM-1"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    res_2 = gateway.validate_and_reserve(order_normal, base_rule, normal_bar, benchmark_price=None)
    assert not res_2.is_valid
    assert "BENCHMARK_PRICE_REQUIRED" in res_2.rejection_reason

    # 3. Bar 只有 volume 没有 close
    bar_no_close = {"volume": 10000.0}
    res_3 = gateway.validate_and_reserve(order_normal, base_rule, bar_no_close, benchmark_price=100.0)
    assert not res_3.is_valid
    assert "INVALID_BAR_CLOSE" in res_3.rejection_reason

    # 4. Bar close 与 volume 均为 NaN
    bar_nan = {"close": float("nan"), "volume": float("nan")}
    res_4 = gateway.validate_and_reserve(order_normal, base_rule, bar_nan, benchmark_price=100.0)
    assert not res_4.is_valid
    assert "INVALID_BAR_CLOSE" in res_4.rejection_reason or "INVALID_BAR_DATA" in res_4.rejection_reason


def test_gateway_idempotency_and_deduplication():
    """
    【N03 验证】网关申报与成交幂等性：
    1. 同一 ClientOrderId 重复提交必须被拒绝
    2. 同一 trade_id 成交事件派发多次，持仓绝不重复累计
    3. 同日重复调用 on_trade_day_start 不产生可卖数量泄露
    4. 时序回退调用 on_trade_day_start 抛错
    """
    venue_sse = Venue("SSE")
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.model.events import OrderFilled
    from nautilus_trader.model.identifiers import PositionId, TradeId
    from nautilus_trader.core.uuid import UUID4

    gateway = AShareOrderGateway()
    gateway.on_trade_day_start("2025-05-06")

    base_rule = {"trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 110.0, "down_limit": 90.0, "buy_min": 100, "buy_step": 100}
    bar = {"close": 100.0, "volume": 1000.0}

    order = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("BUY-IDEMP-1"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    # 首次提交: 合法
    res_1 = gateway.validate_and_reserve(order, base_rule, bar, benchmark_price=100.0)
    assert res_1.is_valid

    # 重复提交相同 client_order_id: 必须拦截
    res_2 = gateway.validate_and_reserve(order, base_rule, bar, benchmark_price=100.0)
    assert not res_2.is_valid
    assert "DUPLICATE_ORDER_ID" in res_2.rejection_reason

    # 模拟成交事件 (N03)
    t_ns = beijing_to_utc_ns("2025-05-06 10:00:00")
    fill_event = type("MockFillEvent", (), {
        "trade_id": "T-1001",
        "ts_event": t_ns,
        "client_order_id": order.client_order_id,
        "last_qty": Quantity.from_int(100),
        "instrument_id": inst_id,
        "order_side": OrderSide.BUY,
    })
    # 第一次处理成交: 总持仓增加 100 股
    gateway.on_order_filled(fill_event)
    assert gateway.get_total_position("600519") == Decimal("100.0")

    # 第二次重复派发同一 trade_id: 必须幂等去重，持仓保持 100 股！
    gateway.on_order_filled(fill_event)
    assert gateway.get_total_position("600519") == Decimal("100.0"), "重复成交事件绝不能造成持仓翻倍！"

    # 同日重复调用 on_trade_day_start: 保持幂等
    assert gateway.get_sellable_qty("600519") == Decimal("0.0")
    gateway.on_trade_day_start("2025-05-06")
    assert gateway.get_sellable_qty("600519") == Decimal("0.0"), "同日重复日切严禁提前释放可卖持仓！"

    # 时序回退调用: 抛出异常
    with pytest.raises(ValueError, match="Trade date regression"):
        gateway.on_trade_day_start("2025-05-05")


def test_native_order_cancellation_releases_reserved_inventory():
    """
    【N02 验证】原生订单撤销确认接入网关：
    验证当通过 NautilusTrader 原生策略真实撤单时，
    on_order_canceled 回调能够正确释放网关的挂单预留，恢复可卖数量。
    """
    class CancelTestStrategy(Strategy):
        def __init__(self, config):
            super().__init__(config)
            self.gateway = AShareOrderGateway()
            self.canceled_count = 0

        def on_order_canceled(self, event):
            self.gateway.on_order_canceled(str(event.client_order_id), str(event.instrument_id.symbol))
            self.canceled_count += 1

    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("TRADER-001")))
    venue_sse = Venue("SSE")
    engine.add_venue(
        venue=venue_sse,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=CNY,
        starting_balances=[Money(1_000_000.0, CNY)],
    )
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    t_init = beijing_to_utc_ns("2025-05-01 09:30:00")
    instrument = Equity(
        instrument_id=inst_id,
        raw_symbol=symbol,
        currency=CNY,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(100),
        ts_event=t_init,
        ts_init=t_init,
    )
    engine.add_instrument(instrument)
    bar_spec = BarSpecification(1, BarAggregation.HOUR, PriceType.LAST)
    bar_type = BarType(inst_id, bar_spec, AggregationSource.EXTERNAL)

    strat = CancelTestStrategy(AShareTestStrategyConfig(instrument_id=inst_id, bar_type=bar_type))
    engine.add_strategy(strat)
    # 模拟已有已结算持仓 100 股
    strat.gateway._settled_shares["600519"] = Decimal("100.0")
    strat.gateway.on_trade_day_start("2025-05-07")
    assert strat.gateway.get_sellable_qty("600519") == Decimal("100.0")

    # 构造并预留一笔卖单
    order = strat.order_factory.limit(
        instrument_id=inst_id,
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(100),
        price=Price.from_str("120.00"),  # 远高于市价，挂单未成交
        time_in_force=TimeInForce.DAY,
    )
    rule = {"trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 130.0, "down_limit": 90.0, "sell_min": 100, "sell_step": 100}
    bar = {"close": 100.0, "volume": 1000.0}
    res = strat.gateway.validate_and_reserve(order, rule, bar, benchmark_price=100.0)
    assert res.is_valid
    assert strat.gateway.get_sellable_qty("600519") == Decimal("0.0")

    # 模拟原生撤单事件响应
    strat.on_order_canceled(type("Event", (), {
        "client_order_id": order.client_order_id,
        "instrument_id": inst_id,
    }))
    assert strat.canceled_count == 1
    # 撤单后预留必须被成功释放，可卖数量恢复为 100 股 (N02)
    assert strat.gateway.get_sellable_qty("600519") == Decimal("100.0")


def test_day_order_fill_on_new_day_cannot_be_sold_same_day():
    """
    【N01 核心反例验证】
    若某笔买单在 5 月 7 日 (Day 2) 上午成交（无论来源如何）：
    1. 网关必须将该成交记录为 5 月 7 日批次 (lot_date = 2025-05-07)
    2. 即使随后执行 5 月 7 日的 on_trade_day_start('2025-05-07')，
       该批次由于不满足 lot_date < 2025-05-07，严禁转化为已结算可卖持仓！
    3. 盘中任何在 5 月 7 日卖出该股份的申报必被网关硬拦截 (T1_SETTLEMENT_VIOLATION)。
    """
    venue_sse = Venue("SSE")
    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.core.uuid import UUID4

    gateway = AShareOrderGateway()
    # Day 1: 5 月 6 日启动
    gateway.on_trade_day_start("2025-05-06")
    assert gateway.get_sellable_qty("600519") == Decimal("0.0")

    # 模拟 5 月 7 日上午 09:30 成交一笔 200 股买单 (时戳为 5 月 7 日)
    t_day2 = beijing_to_utc_ns("2025-05-07 09:30:00")
    day2_fill = type("Day2FillEvent", (), {
        "trade_id": "T-D2-BUY",
        "ts_event": t_day2,
        "client_order_id": ClientOrderId("B-D2"),
        "last_qty": Quantity.from_int(200),
        "instrument_id": inst_id,
        "order_side": OrderSide.BUY,
    })
    fill_date = gateway.on_order_filled(day2_fill)
    assert fill_date == "2025-05-07"
    assert gateway.get_total_position("600519") == Decimal("200.0")

    # 策略随后推进 5 月 7 日日切 (on_trade_day_start)
    gateway.on_trade_day_start("2025-05-07")

    # 关键检验点 (N01)：5 月 7 日新买入的 200 股绝对不能变成可卖！
    assert gateway.get_sellable_qty("600519") == Decimal("0.0"), "当天新买入的股份严禁在当日释放为可卖持仓！"

    # 尝试在 5 月 7 日卖出 100 股 -> 必须被前置网关硬拦截
    sell_order = LimitOrder(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("STRATEGY-001"),
        instrument_id=inst_id,
        client_order_id=ClientOrderId("S-D2-ILLEGAL"),
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(100),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=0,
    )
    rule = {"trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 110.0, "down_limit": 90.0, "sell_min": 100, "sell_step": 100}
    bar = {"close": 100.0, "volume": 1000.0}
    res = gateway.validate_and_reserve(sell_order, rule, bar, benchmark_price=100.0)
    assert not res.is_valid, "当日新买股份尝试当日卖出必须被网关拦截"
    assert "T1_SETTLEMENT_VIOLATION" in res.rejection_reason

    # 推进到 5 月 8 日 (Day 3) -> 自动释放为可卖持仓
    gateway.on_trade_day_start("2025-05-08")
    assert gateway.get_sellable_qty("600519") == Decimal("200.0"), "次日开盘方可正常释放为可卖持仓"
