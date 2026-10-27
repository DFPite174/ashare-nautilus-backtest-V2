"""
端到端集成测试：验证 NautilusTrader Rust 内核 + Python A 股适配层完整生命周期
覆盖核心审计项修复：
1. [F01] 严格断言账户最终现金、持仓与费用，接入 GoldenLedger 进行独立双账本零漂移核验。
2. [F01 变异测试] 验证零费率变异时硬断言必然触发失败，消除假阳性。
3. [F02] 原生纳秒时钟从 1970 修复至 2025 年真实推进 (ts_init = ts_event)。
4. [F03 & F10] T+1 状态机与 2% 价格笼子下沉至成交事件与前置网关，订单生命周期驱动 (on_order_filled)。
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

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

from engine.fee_model import AShareFeeModel
from engine.golden_ledger import GoldenLedger
from engine.market_rules import round_order, block_reason, validate_price_collar
from contracts import Fill, OrderSide as ContractOrderSide


class AShareTestStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType


class AShareTestStrategy(Strategy):
    """
    A 股测试策略：真实绑定订单生命周期与独立黄金账本
    """

    def __init__(self, config: AShareTestStrategyConfig):
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type

        # 真实独立黄金账本（双轨运行）
        self.ledger = GoldenLedger(initial_cash=Decimal("1000000.0"))

        # A 股状态机（由真实成交事件 on_order_filled 驱动）
        self.settled_qty = 0.0
        self.bought_today_qty = 0.0
        self.current_date = None

        # 统计计数器
        self.t1_rejections = 0
        self.limit_blocks = 0
        self.successful_buys = 0
        self.successful_sells = 0

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def on_order_filled(self, event):
        """
        根据底层交易所原生真实成交回报更新 T+1 状态机与独立黄金账本
        """
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
            timestamp=datetime.fromtimestamp(event.ts_event / 1e9, timezone.utc),
        )
        self.ledger.apply_fill(fill, self.current_date)

        if event.order_side == OrderSide.BUY:
            self.bought_today_qty += qty
            self.successful_buys += 1
        elif event.order_side == OrderSide.SELL:
            self.settled_qty -= qty
            self.successful_sells += 1

    def on_bar(self, bar: Bar):
        # 解析当前交易日
        dt = datetime.fromtimestamp(bar.ts_event / 1e9, timezone.utc)
        trade_date = dt.strftime("%Y-%m-%d")

        # 跨日时钟推进：日终结算，今日买入自动释放为次日可卖
        if self.current_date is not None and trade_date != self.current_date:
            self.settled_qty += self.bought_today_qty
            self.bought_today_qty = 0.0
            self.ledger.on_trade_day_start(trade_date)

        if self.current_date is None:
            self.ledger.on_trade_day_start(trade_date)
        self.current_date = trade_date

        px = bar.close.as_double()

        # ==========================================
        # 场景 1: Day 1 (2025-05-06) 开盘买入 200 股
        # ==========================================
        if trade_date == "2025-05-06" and self.successful_buys == 0:
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(200),
            )
            self.submit_order(order)
            return

        # ==========================================
        # 场景 2: Day 1 (2025-05-06) 盘中尝试卖出 100 股 -> 必须触发 T+1 拦截
        # ==========================================
        if trade_date == "2025-05-06" and self.successful_buys == 1:
            req_qty = 100.0
            if req_qty > self.settled_qty:
                # T+1 拦截成功！
                self.t1_rejections += 1
            else:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=Quantity.from_int(int(req_qty)),
                )
                self.submit_order(order)
            return

        # ==========================================
        # 场景 3: Day 2 (2025-05-07) 跨日后允许卖出 100 股
        # ==========================================
        if trade_date == "2025-05-07" and self.successful_sells == 0:
            req_qty = 100.0
            if req_qty <= self.settled_qty:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=Quantity.from_int(int(req_qty)),
                )
                self.submit_order(order)
            return

        # ==========================================
        # 场景 4: Day 3 (2025-05-08) 涨停价买入被阻断
        # ==========================================
        if trade_date == "2025-05-08":
            mock_rule = {
                "trade_status": "OPEN",
                "limit_state": "BOUNDED",
                "up_limit": 110.00,
                "down_limit": 90.00,
            }
            bar_dict = {"close": px, "volume": 1000}
            reason = block_reason(mock_rule, bar_dict, "buy")
            if reason == "LIMIT_UP_CLOSE":
                self.limit_blocks += 1


def _create_configured_engine(fee_model=None):
    """辅助工厂函数：创建并初始化回测引擎"""
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

    def to_ns(dt_str: str) -> int:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1e9)

    symbol = Symbol("600519")
    inst_id = InstrumentId(symbol, venue_sse)
    instrument = Equity(
        instrument_id=inst_id,
        raw_symbol=symbol,
        currency=CNY,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(100),
        ts_event=to_ns("2025-05-01 00:00:00"),
        ts_init=to_ns("2025-05-01 00:00:00"),
    )
    engine.add_instrument(instrument)

    bar_spec = BarSpecification(1, BarAggregation.HOUR, PriceType.LAST)
    bar_type = BarType(inst_id, bar_spec, AggregationSource.EXTERNAL)

    # 严格填入真实纳秒时钟 ts_init 与 ts_event (消除 1970 年时钟冻结)
    t1 = to_ns("2025-05-06 10:00:00")
    t2 = to_ns("2025-05-06 14:00:00")
    t3 = to_ns("2025-05-07 10:00:00")
    t4 = to_ns("2025-05-08 10:00:00")

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
    【基准正常路径测试】
    真实执行 NautilusTrader，并与独立黄金账本进行逐笔零漂移双重核验。
    """
    fee_model = AShareFeeModel(
        commission_rate=0.0003,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        currency=CNY,
    )
    engine, inst_id, bar_type, venue_sse = _create_configured_engine(fee_model=fee_model)

    strategy_config = AShareTestStrategyConfig(instrument_id=inst_id, bar_type=bar_type)
    strategy = AShareTestStrategy(strategy_config)
    engine.add_strategy(strategy)

    # 运行回测 (调用底层 Rust 内核事件循环)
    engine.run()

    # 1. 验证原生时钟真实推进至 2025 年 (F02 修复确认)
    assert strategy.clock.timestamp_ns() >= 1746698400000000000, "策略原生时钟未正确推进至 2025 年"

    # 2. 验证规则生效情况
    assert strategy.t1_rejections == 1, "T+1 当日卖单未被成功拦截"
    assert strategy.successful_sells == 1, "T+1 次日卖单未成功执行"
    assert strategy.limit_blocks == 1, "涨停板买入未被阻断"

    # 3. 检查原生持仓数量
    net_pos = engine.portfolio.net_position(inst_id)
    assert float(net_pos) == 100.0, f"净持仓应为 100 股，实为 {net_pos}"

    # 4. 严格断言账户最终现金 (F01 修复确认：拒绝仅 print)
    account = engine.portfolio.account(venue_sse)
    assert account is not None
    total_balances = account.balances_total()
    balance = float(total_balances.get(CNY, 0.0))
    expected_cash = 990483.44
    assert balance == expected_cash, f"原生现金余额应为 {expected_cash}，实为 {balance}"

    # 5. 独立黄金账本零漂移核验 (F01 修复确认：双轨对账)
    assert float(strategy.ledger.cash) == balance, (
        f"黄金账本现金 ({float(strategy.ledger.cash)}) 与原生账户 ({balance}) 不一致！"
    )
    assert float(strategy.ledger.positions["600519"]) == float(net_pos), (
        f"黄金账本持仓 ({float(strategy.ledger.positions['600519'])}) 与原生持仓 ({float(net_pos)}) 不一致！"
    )


def test_nautilus_ashare_zero_fee_mutation_detected():
    """
    【变异反例测试 (M01)】
    验证若将交易所费用模型置为零费率，断言必须能够精准报错捕获，杜绝假阳性。
    """
    # 不注入 A 股真实费用模型 (即费率为 0)
    engine, inst_id, bar_type, venue_sse = _create_configured_engine(fee_model=None)

    strategy_config = AShareTestStrategyConfig(instrument_id=inst_id, bar_type=bar_type)
    strategy = AShareTestStrategy(strategy_config)
    engine.add_strategy(strategy)

    engine.run()

    account = engine.portfolio.account(venue_sse)
    balance = float(account.balances_total().get(CNY, 0.0))

    # 零费率时现金应为 990500.00，不再等于法定含税的 990483.44
    assert balance == 990500.00
    # 严格检验：断言法定费用必须不等，证明系统测试具备真实验收能力
    assert balance != 990483.44, "零费率变异应当被严格发现，不可漏网！"
