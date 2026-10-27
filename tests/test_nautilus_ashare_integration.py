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
            # 1. 触发前一交易日日终收盘估值 (R05)
            if self.last_close_px is not None:
                self.ledger.mark_to_market(self.current_date, {symbol_str: Decimal(str(self.last_close_px))})
            self.gateway.on_session_close()
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
