"""
公司行为 (Corporate Actions) 全链路集成与会计核验测试
全面覆盖审计项 N06, N07, N08, N09：
1. [N06] 新增股份上市日 (share_listing_date)：除权日扩大持仓，但送转股份严格在上市日才解冻为可卖。
2. [N07] 除息至派息期间应收分红 (Receivable Dividends) 机制：确保除权除息价格下跌时总 NAV 经济价值严格守恒不虚降。
3. [N08] 严格登记日权益快照防护：声明登记日的公司行为绝不静默按派息日现存仓位派发。
4. [N09] 拆股整数股规整 (ROUND_FLOOR) 与历史批次 lots 精确守恒；Cython 原生 split_native 结合 NautilusTrader Position 状态联动。
"""

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarSpecification, BarType
from nautilus_trader.model.enums import (
    AccountType,
    AggregationSource,
    BarAggregation,
    OmsType,
    OrderSide,
    PositionSide,
    PriceType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Symbol, Venue, TraderId
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.execution import TestExecStubs
from nautilus_trader.test_kit.stubs.events import TestEventStubs
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from engine.split_guard import load_bridge
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway
from contracts import CorporateAction, Fill, OrderSide as ContractOrderSide


def test_split_native_loading_and_execution():
    """
    【R09 & N09】验证 Cython 原生拆股扩展加载及对原生 Position 的安全调整
    """
    bridge = load_bridge()
    assert hasattr(bridge, "apply_split"), "split_native 应提供 apply_split 接口"

    inst = TestInstrumentProvider.equity()
    order = TestExecStubs.market_order(instrument=inst)
    fill = TestEventStubs.order_filled(
        order=order,
        instrument=inst,
        last_qty=Quantity.from_int(100),
        last_px=Price.from_str("100.00"),
        position_id=PositionId("P-001"),
    )
    pos = Position(inst, fill)
    assert float(pos.quantity) == 100.0
    assert pos.avg_px_open == 100.0

    # 执行 10 送 10 (拆股比例 2.0)
    split_event_time = pos.ts_last + 1000
    res = bridge.apply_split(pos, 2.0, split_event_time, inst)

    assert res["ratio"] == "2.0"
    # 持仓股数应由 100 变为 200
    assert float(pos.quantity) == 200.0
    assert pos.signed_qty == 200.0
    # 开仓均价应由 100.0 调整为 50.0
    assert pos.avg_px_open == 50.0
    # 经济价值守恒：200 股 * 50.0 元 = 100 股 * 100.0 元 = 10,000 元
    assert float(pos.quantity) * pos.avg_px_open == 10000.0


def test_corporate_action_share_listing_date_delays_tradable():
    """
    【N06 验证】新增送转股份上市日 (share_listing_date) 控制：
    除权日增加总持仓，但送转新增股份必须等到 share_listing_date 当天开盘才解冻为可卖持仓！
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-ca-list-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册 10 送 10，除权日 2025-05-08，但新增股份上市日为 2025-05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 12),
        bonus_ratio=Decimal("1.0"),
        share_listing_date=date(2025, 5, 12),
    )
    ledger.register_corporate_action(ca)

    # 1. 2025-05-07 登记日：原 1000 股转为可卖
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")
    assert ledger.positions["600519"] == Decimal("1000.0")
    assert ledger.tradable_positions["600519"] == Decimal("1000.0")

    # 2. 2025-05-08 除权日：总持仓变为 2000 股，但可卖持仓必须依然是 1000 股 (新增 1000 股未上市)
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")
    assert ledger.positions["600519"] == Decimal("2000.0"), "总持仓应在除权日转增至 2000 股"
    assert ledger.tradable_positions["600519"] == Decimal("1000.0"), "新增股份尚未上市，可卖持仓严禁提前释放"

    # 3. 2025-05-12 上市日：新增 1000 股正式解冻，可卖持仓转为 2000 股
    ledger.on_trade_day_start("2025-05-12")
    ledger.process_corporate_actions_for_date("2025-05-12")
    assert ledger.tradable_positions["600519"] == Decimal("2000.0"), "新增股份上市日应当正式解冻为可卖"


def test_corporate_action_receivable_dividend_nav_conservation():
    """
    【N07 验证】应收分红会计机制与总 NAV 平滑守恒：
    除息日股价从 100 元下跌至 98 元 (每股派 2 元)，在派现日现金到账前，
    应收分红计入 NAV，杜绝除息日净值虚假塌陷。
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-ca-div-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("1000.0"),
        price=Decimal("100.0"),
        fee=Decimal("30.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册派现 2 元，登记日 05-07，除息日 05-08，派息日 05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 12),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # 1. 2025-05-07 登记日
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")
    nav_d2 = ledger.mark_to_market("2025-05-07", {"600519": Decimal("100.0")})
    # NAV = 现金 399970 + 股票 100000 = 499970
    assert nav_d2.total_equity == Decimal("499970.00")

    # 2. 2025-05-08 除息日：收盘价除息至 98.0 元
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")
    nav_d3 = ledger.mark_to_market("2025-05-08", {"600519": Decimal("98.0")})

    # 关键断言 (N07)：市值 98000 + 现金 399970 + 应收分红 2000 = 499970.00 (NAV 严格守恒，绝不虚降 2000 元！)
    assert nav_d3.receivable_dividends == Decimal("2000.00")
    assert nav_d3.total_equity == Decimal("499970.00"), "除息日至派息日期间 NAV 必须保持权益守恒"

    # 3. 2025-05-12 派息日：现金 2000 元到账，应收分红结清
    ledger.on_trade_day_start("2025-05-12")
    ledger.process_corporate_actions_for_date("2025-05-12")
    nav_d4 = ledger.mark_to_market("2025-05-12", {"600519": Decimal("98.0")})
    assert nav_d4.receivable_dividends == Decimal("0.00")
    assert nav_d4.cash == Decimal("399970.00") + Decimal("2000.00")
    assert nav_d4.total_equity == Decimal("499970.00")


def test_corporate_action_missing_record_date_entitlement_fails():
    """
    【N08 验证】声明了登记日的公司行为，若快照丢失或漏调度，派息日严禁静默回退
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 9),
        cash_div=Decimal("2.0"),
    )
    ledger.register_corporate_action(ca)

    # 故意跳过 05-07 登记日调度，直接在 05-08 除息日触发
    ledger.on_trade_day_start("2025-05-08")
    with pytest.raises(RuntimeError, match="Missing entitlement snapshot"):
        ledger.process_corporate_actions_for_date("2025-05-08")


def test_corporate_action_integer_quantization_and_lot_conservation():
    """
    【N09 验证】拆股必须按整数股向下取整 (ROUND_FLOOR)，杜绝产生小数股并保持批次守恒
    """
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    ledger.on_trade_day_start("2025-05-06")

    # 买入两笔，各 1 股
    f1 = Fill(fill_id="f1", symbol="600519", side=ContractOrderSide.BUY, quantity=Decimal("1.0"), price=Decimal("10.0"), fee=Decimal("5.0"), timestamp=datetime(2025, 5, 6, 9, 30))
    f2 = Fill(fill_id="f2", symbol="600519", side=ContractOrderSide.BUY, quantity=Decimal("1.0"), price=Decimal("10.0"), fee=Decimal("5.0"), timestamp=datetime(2025, 5, 6, 9, 31))
    ledger.apply_fill(f1, "2025-05-06")
    ledger.apply_fill(f2, "2025-05-06")

    assert ledger.positions["600519"] == Decimal("2.0")

    # 执行 1.25 拆细比例：2 股 * 1.25 = 2.5 股 -> A 股法定向下取整为 2 股整股，杜绝 2.5 股小数！
    ledger.apply_stock_split("600519", Decimal("1.25"), "2025-05-07")
    assert ledger.positions["600519"] == Decimal("2.0")
    assert isinstance(ledger.positions["600519"], Decimal)
    # 批次总和与总持仓严格一致
    sum_lots = sum(q for _, q in ledger.lots["600519"])
    assert sum_lots == ledger.positions["600519"]


def test_corporate_action_bonus_shares_listing_fifo_sell_no_ghost_inventory():
    """
    【Q02 验证】新增股份上市日卖出全部持仓后，FIFO 必须彻底清除所有批次，
    次日绝不产生幽灵可用持仓，杜绝二次卖出导致持仓变为负数。
    """
    ledger = GoldenLedger(initial_cash=Decimal("500000.0"))
    ledger.on_trade_day_start("2025-05-06")

    # 买入 100 股
    fill = Fill(
        fill_id="f-ca-q02-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("5.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册 10 送 10，除权日 2025-05-08，新增股份上市日为 2025-05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        pay_date=date(2025, 5, 12),
        bonus_ratio=Decimal("1.0"),
        share_listing_date=date(2025, 5, 12),
    )
    ledger.register_corporate_action(ca)

    # 推进至 2025-05-07 登记日
    ledger.on_trade_day_start("2025-05-07")
    ledger.process_corporate_actions_for_date("2025-05-07")

    # 推进至 2025-05-08 除权日：总持仓 200，可卖 100
    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")

    # 推进至 2025-05-12 上市日：送转股份解冻，总持仓 200，可卖 200
    ledger.on_trade_day_start("2025-05-12")
    ledger.process_corporate_actions_for_date("2025-05-12")
    assert ledger.positions["600519"] == Decimal("200.0")
    assert ledger.tradable_positions["600519"] == Decimal("200.0")

    # 在 2025-05-12 当天全额卖出 200 股
    sell_fill = Fill(
        fill_id="f-ca-q02-sell",
        symbol="600519",
        side=ContractOrderSide.SELL,
        quantity=Decimal("200.0"),
        price=Decimal("50.0"),
        fee=Decimal("10.0"),
        timestamp=datetime(2025, 5, 12, 14, 0, 0),
    )
    ledger.apply_fill(sell_fill, "2025-05-12")

    # 关键断言：持仓归零，批次清空
    assert ledger.positions["600519"] == Decimal("0.0")
    assert ledger.tradable_positions["600519"] == Decimal("0.0")
    assert ledger.lots["600519"] == []

    # 推进至次日 2025-05-13：杜绝产生幽灵库存！
    ledger.on_trade_day_start("2025-05-13")
    assert ledger.tradable_positions.get("600519", Decimal("0.0")) == Decimal("0.0")
    assert ledger.positions.get("600519", Decimal("0.0")) == Decimal("0.0")

    # 尝试再次卖出必抛出 T+1 违规，严禁持仓变为负数
    sell_fill_2 = Fill(
        fill_id="f-ca-q02-sell-2",
        symbol="600519",
        side=ContractOrderSide.SELL,
        quantity=Decimal("200.0"),
        price=Decimal("50.0"),
        fee=Decimal("10.0"),
        timestamp=datetime(2025, 5, 13, 9, 30, 0),
    )
    with pytest.raises(ValueError, match="Ledger T\\+1 violation"):
        ledger.apply_fill(sell_fill_2, "2025-05-13")


def test_corporate_action_fractional_share_policies():
    """
    【Q07, V02 验证】公司行为零碎股处理策略 (floor, reject, cash_in_lieu)：
    - floor: 向下取整
    - reject: 存在零碎股时严格抛出异常
    - cash_in_lieu: 严格按除权基准参考价 (或指定折算价) 折算现金入账，严禁使用虚构价格或旧单位价格
    - 未知非法策略: 严格报错拦截，绝不静默降级为 floor
    """
    # 1. 默认 floor 策略
    l_floor = GoldenLedger(initial_cash=Decimal("100000.0"))
    l_floor.positions["600519"] = Decimal("105.0")
    l_floor.tradable_positions["600519"] = Decimal("105.0")
    l_floor.lots["600519"] = [("2025-05-01", Decimal("105.0"))]
    l_floor.apply_stock_split("600519", Decimal("1.5"), "2025-05-06", fractional_policy="floor")
    # 105 * 1.5 = 157.5 -> 向下取整为 157
    assert l_floor.positions["600519"] == Decimal("157.0")

    # 2. reject 策略：遇零碎股严格报错
    l_reject = GoldenLedger(initial_cash=Decimal("100000.0"))
    l_reject.positions["600519"] = Decimal("105.0")
    with pytest.raises(ValueError, match="Fractional share detected"):
        l_reject.apply_stock_split("600519", Decimal("1.5"), "2025-05-06", fractional_policy="reject")

    # 3. 未知非法策略 (如拼写错误 TYPO)：严格报错，绝不静默降级为 floor (V02)
    l_typo = GoldenLedger(initial_cash=Decimal("100000.0"))
    l_typo.positions["600519"] = Decimal("105.0")
    with pytest.raises(ValueError, match="Invalid fractional_policy 'TYPO'"):
        l_typo.apply_stock_split("600519", Decimal("1.5"), "2025-05-06", fractional_policy="TYPO")

    # 4. cash_in_lieu 策略在缺乏有效估值价格时必须报错拒绝 (V02)
    l_nopx = GoldenLedger(initial_cash=Decimal("100000.0"))
    l_nopx.positions["600519"] = Decimal("105.0")
    with pytest.raises(ValueError, match="Missing valid valuation price"):
        l_nopx.apply_stock_split("600519", Decimal("1.5"), "2025-05-06", fractional_policy="cash_in_lieu")

    # 5. cash_in_lieu 策略：严格使用除权基准价 (拆前价 / 拆分比例) 折算现金，保持经济价值严格守恒 (V02)
    # 原 105 股 * 10.0 元 = 1050.0 元；拆分比例 1.5，拆后除权基准价 = 10.0 / 1.5 = 6.6667 元/股
    # 拆后新股 157.5 股 -> 整股 157 股，零碎 0.5 股 * 6.6667 元 = 3.33 元现金补足入账
    # 总财富 = 157 股 * 6.6667 + 3.33 = 1046.67 + 3.33 = 1050.00 元！
    l_cash = GoldenLedger(initial_cash=Decimal("100000.0"))
    l_cash.positions["600519"] = Decimal("105.0")
    l_cash.tradable_positions["600519"] = Decimal("105.0")
    l_cash.lots["600519"] = [("2025-05-01", Decimal("105.0"))]
    l_cash.latest_prices["600519"] = (Decimal("10.00"), "2025-05-05")
    l_cash.apply_stock_split("600519", Decimal("1.5"), "2025-05-06", fractional_policy="cash_in_lieu")
    assert l_cash.positions["600519"] == Decimal("157.0")
    assert l_cash.cash == Decimal("100003.33")


def test_corporate_action_e2e_engine_split_and_reconciliation():
    """
    【Q08, V04 验证】端到端全链路闭环：
    BacktestEngine 原生撮合买入 -> 登记日收盘自动锁定分红权益快照 -> 策略与 Position 原生拆股 -> 网关与黄金账本多轨同步 -> 派息日分红入账 -> 原生撮合卖出全部持仓 -> 现金额度与持仓零漂移对账
    """
    from engine.accounting import AShareAccountModule
    cash_module = AShareAccountModule()
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
    def b2u(s):
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI_TZ)
        return int(dt.astimezone(timezone.utc).timestamp() * 1e9)

    class SplitE2EStrategy(Strategy):
        def __init__(self, c, bt, inst):
            super().__init__(c)
            self.bt = bt
            self.inst = inst
            self.ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
            self.gateway = AShareOrderGateway()
            self.bridge = load_bridge()
            self.split_applied = False
            # 包含 10 送 10 (比例 1.0) 及每股派现 2.0 元 (V04)
            self.ca = CorporateAction(
                symbol="600519",
                record_date=date(2025, 5, 6),
                ex_date=date(2025, 5, 7),
                pay_date=date(2025, 5, 8),
                bonus_ratio=Decimal("1.0"),
                cash_div=Decimal("2.0"),
            )
            self.ledger.register_corporate_action(self.ca)

        def on_start(self):
            self.subscribe_bars(self.bt)

        def on_order_filled(self, event):
            fdate = self.gateway.on_order_filled(event)
            side = ContractOrderSide.BUY if event.order_side == OrderSide.BUY else ContractOrderSide.SELL
            fill = Fill(
                fill_id=str(event.trade_id),
                symbol="600519",
                side=side,
                quantity=Decimal(str(event.last_qty.as_double())),
                price=Decimal(str(event.last_px.as_double())),
                fee=Decimal(str(event.commission.as_double())),
                timestamp=datetime.fromtimestamp(event.ts_event / 1e9, tz=SHANGHAI_TZ),
            )
            self.ledger.apply_fill(fill, fdate)

        def on_bar(self, bar: Bar):
            dt = datetime.fromtimestamp(bar.ts_event / 1e9, tz=SHANGHAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            rule = {"trade_status": "OPEN", "limit_state": "UNBOUNDED", "buy_min": 100, "buy_step": 100, "sell_min": 100, "sell_step": 100, "max_order": 1000000}
            bar_dict = {"close": bar.close.as_double(), "volume": 10000.0}

            if d_str == "2025-05-06":
                # 登记日开盘：申报买入 100 股
                self.gateway.on_trade_day_start(d_str)
                self.ledger.on_trade_day_start(d_str)
                if bar.ts_event == b2u("2025-05-06 09:30:00"):
                    order = self.order_factory.limit(self.inst.id, OrderSide.BUY, Quantity.from_int(100), Price.from_str("100.00"), TimeInForce.DAY)
                    assert self.gateway.validate_and_reserve(order, rule, bar_dict, benchmark_price=100.0).is_valid
                    self.submit_order(order)
                elif bar.ts_event >= b2u("2025-05-06 15:00:00"):
                    # 登记日收盘盯市：自动锁定登记日收盘持仓快照 (100 股) 作为合法分红权益基准 (V04)
                    self.ledger.mark_to_market(d_str, {"600519": Decimal("100.00")})
                    cash_module.record_entitlement(self.ca, self.portfolio.net_position(self.inst.id))

            elif d_str == "2025-05-07":
                if not self.split_applied:
                    self.gateway.on_trade_day_start(d_str)
                    self.ledger.on_trade_day_start(d_str)
                    # 容错：若未在昨日收市盯市，在跨日除权前封存昨日登记日收盘快照 (V04)
                    if self.ledger.last_valuation_date != "2025-05-06":
                        self.ledger.mark_to_market("2025-05-06", {"600519": Decimal("100.00")})
                        cash_module.record_entitlement(self.ca, self.portfolio.net_position(self.inst.id))
                    # 除权除息日处理：100 股确认 200 元应收分红，且送转拆股 1:1
                    self.ledger.process_corporate_actions_for_date(d_str)
                    self.gateway.apply_stock_split("600519", 2.0)
                    for pos in self.cache.positions_open():
                        self.bridge.apply_split(pos, 2.0, bar.ts_event, self.inst)
                    self.split_applied = True
                    self.ledger.mark_to_market(d_str, {"600519": Decimal("50.00")})

            elif d_str == "2025-05-08":
                self.gateway.on_trade_day_start(d_str)
                self.ledger.on_trade_day_start(d_str)
                # 派息日：200 元现金分红到账转入可用现金 (V04)
                self.ledger.process_corporate_actions_for_date(d_str)
                cash_module.pay_dividend(self.ca, d_str)
                # 卖出全部 200 股 @ 50.00
                order = self.order_factory.limit(self.inst.id, OrderSide.SELL, Quantity.from_int(200), Price.from_str("50.00"), TimeInForce.DAY)
                assert self.gateway.validate_and_reserve(order, rule, bar_dict, benchmark_price=50.0).is_valid
                self.submit_order(order)
                self.ledger.mark_to_market(d_str, {"600519": Decimal("50.00")})

    cfg = BacktestEngineConfig(trader_id=TraderId("TRADER-001"), logging=LoggingConfig(log_level="ERROR"))
    eng = BacktestEngine(config=cfg)
    v = Venue("SSE")
    eng.add_venue(venue=v, oms_type=OmsType.NETTING, account_type=AccountType.CASH, base_currency=CNY, starting_balances=[Money(100000.0, CNY)], modules=[cash_module])
    sym = Symbol("600519")
    inst_id = InstrumentId(sym, v)
    t0 = b2u("2025-05-01 09:30:00")
    inst = Equity(instrument_id=inst_id, raw_symbol=sym, currency=CNY, price_precision=2, price_increment=Price.from_str("0.01"), lot_size=Quantity.from_int(100), ts_event=t0, ts_init=t0)
    eng.add_instrument(inst)
    bt = BarType(inst_id, BarSpecification(1, BarAggregation.HOUR, PriceType.LAST), AggregationSource.EXTERNAL)
    eng.add_data([
        Bar(bt, Price.from_str("100.00"), Price.from_str("100.00"), Price.from_str("100.00"), Price.from_str("100.00"), Quantity.from_int(10000), b2u("2025-05-06 09:30:00"), b2u("2025-05-06 09:30:00")),
        Bar(bt, Price.from_str("100.00"), Price.from_str("100.00"), Price.from_str("100.00"), Price.from_str("100.00"), Quantity.from_int(10000), b2u("2025-05-06 15:00:00"), b2u("2025-05-06 15:00:00")),
        Bar(bt, Price.from_str("50.00"), Price.from_str("50.00"), Price.from_str("50.00"), Price.from_str("50.00"), Quantity.from_int(10000), b2u("2025-05-07 09:30:00"), b2u("2025-05-07 09:30:00")),
        Bar(bt, Price.from_str("50.00"), Price.from_str("50.00"), Price.from_str("50.00"), Price.from_str("50.00"), Quantity.from_int(10000), b2u("2025-05-08 09:30:00"), b2u("2025-05-08 09:30:00")),
    ])
    strat = SplitE2EStrategy(StrategyConfig(), bt, inst)
    eng.add_strategy(strat)
    eng.run()

    # 原生持仓归零
    assert float(eng.portfolio.net_position(inst_id)) == 0.0
    # 黄金账本持仓归零
    assert strat.ledger.positions["600519"] == Decimal("0.0")
    assert strat.ledger.tradable_positions["600519"] == Decimal("0.0")
    assert strat.gateway.get_total_position("600519") == Decimal("0.0")
    assert strat.ledger.lots["600519"] == []
    # 核心分红断言 (V04)：登记日持有 100 股产生 200 元现金分红，派息日已全额入账！
    assert strat.ledger.dividend_entitlements["600519_2025-05-07"] == Decimal("100.0")
    assert strat.ledger.receivable_dividends == {}
    # 现金对账：初始 100,000 - 买入 10,000 + 卖出 10,000 + 现金分红 200 = 100,200.0 元！
    assert strat.ledger.cash == Decimal("100200.0")
    assert float(eng.portfolio.account(v).balances_total()[CNY]) == 100200.
    assert cash_module.pay_dividend(strat.ca, "2025-05-08") == Decimal(0)


def test_gateway_and_ledger_split_allocation_parity_multi_lot():
    """
    【V03 验证】多批次持仓在非整数拆股比例下的网关与黄金账本分配一致性：
    - 200 股持仓 (批次 1: 100 股已结算，批次 2: 100 股未结算)
    - 拆股比例 1.005，理论账户总权益 200 * 1.005 = 201 股
    - 验证网关与黄金账本均以发生时间优先原则补足舍入差额，两者总持仓严格等于 201 股，零漂移！
    """
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    gateway = AShareOrderGateway()

    # 批次 1: 5月5日买入 100 股 (已结算)
    f1 = Fill(
        fill_id="f-parity-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("0.0"),
        timestamp=datetime(2025, 5, 5, 10, 0, 0),
    )
    ledger.apply_fill(f1, "2025-05-05")
    gateway._settled_shares["600519"] = Decimal("100.0")

    # 批次 2: 5月6日买入 100 股 (未结算)
    f2 = Fill(
        fill_id="f-parity-2",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("0.0"),
        timestamp=datetime(2025, 5, 6, 10, 0, 0),
    )
    ledger.apply_fill(f2, "2025-05-06")
    gateway._bought_lots["600519"] = [("2025-05-06", Decimal("100.0"))]

    assert gateway.get_total_position("600519") == Decimal("200.0")
    assert ledger.positions["600519"] == Decimal("200.0")

    # 执行 1.005 比例拆股
    split_ratio = Decimal("1.005")
    ledger.apply_stock_split("600519", split_ratio, "2025-05-07")
    gateway.apply_stock_split("600519", split_ratio)

    # 核心断言：网关与账本均严格得到 201 股，绝不在网关丢失 1 股！
    assert ledger.positions["600519"] == Decimal("201.0")
    assert gateway.get_total_position("600519") == Decimal("201.0")
    assert gateway.get_sellable_qty("600519") == Decimal("101.0")


def test_golden_ledger_same_day_repeat_day_start_idempotency():
    """
    【V07 验证】黄金账本上市日重复调用 on_trade_day_start 具备严格幂等性：
    - 100 股原仓送 100 股，5月12日上市
    - 首次推进至 5月12日：可卖持仓释放为 200 股
    - 同一交易日 (5月12日) 再次重复调用：可卖持仓严格保持 200 股，绝不回退至 100 股！
    - 次日 (5月13日) 正常推进：可卖持仓保持 200 股
    """
    ledger = GoldenLedger(initial_cash=Decimal("100000.0"))
    ledger.on_trade_day_start("2025-05-06")

    fill = Fill(
        fill_id="f-v07-1",
        symbol="600519",
        side=ContractOrderSide.BUY,
        quantity=Decimal("100.0"),
        price=Decimal("100.0"),
        fee=Decimal("0.0"),
        timestamp=datetime(2025, 5, 6, 9, 30, 0),
    )
    ledger.apply_fill(fill, "2025-05-06")

    # 注册 10 送 10，除权日 2025-05-08，上市日 2025-05-12
    ca = CorporateAction(
        symbol="600519",
        record_date=date(2025, 5, 7),
        ex_date=date(2025, 5, 8),
        share_listing_date=date(2025, 5, 12),
        bonus_ratio=Decimal("1.0"),
    )
    ledger.register_corporate_action(ca)

    ledger.on_trade_day_start("2025-05-07")
    ledger.mark_to_market("2025-05-07", {"600519": Decimal("100.0")})

    ledger.on_trade_day_start("2025-05-08")
    ledger.process_corporate_actions_for_date("2025-05-08")

    # 5月8日总持仓 200 股，可卖仅 100 股
    assert ledger.positions["600519"] == Decimal("200.0")
    assert ledger.tradable_positions["600519"] == Decimal("100.0")

    # 首次推进至上市日 2025-05-12
    ledger.on_trade_day_start("2025-05-12")
    assert ledger.tradable_positions["600519"] == Decimal("200.0")

    # 同日 (2025-05-12) 重复调用日切，严格保持 200 股可卖持仓！
    ledger.on_trade_day_start("2025-05-12")
    assert ledger.tradable_positions["600519"] == Decimal("200.0")

    # 次日推进至 2025-05-13
    ledger.on_trade_day_start("2025-05-13")
    assert ledger.tradable_positions["600519"] == Decimal("200.0")
