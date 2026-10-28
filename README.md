# ashare-nautilus-backtest-V2：沪深常规策略回测

当前实现面向沪深普通股票、现金多头、多日持有、日线/分钟线研究。公共入口 `engine.run_backtest` 使用官方 NautilusTrader 1.231.0 的 BacktestEngine、原生订单/撮合/账户/持仓，Python 层负责交易规则、生命周期与独立参考账本。

这不是纯 Rust 主程序；官方 Python 包的执行路径含 Cython 与 Rust 组件。本阶段不包含实盘接入。

## 安装与运行

本次实际验证环境：Windows x64、Python 3.12、NautilusTrader 1.231.0。公司行为送转使用仓库已有的固定 ABI Cython `.pyd`；`split_guard.py` 校验二进制和依赖指纹，跨平台或版本漂移会明确失败。

在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt
python -m pytest tests -q
python examples/regular_backtest.py --bars examples/data/bars_daily.csv --rules examples/data/rules.csv --output daily-result.json
python examples/regular_backtest.py --bars examples/data/bars_minute.csv --frequency MINUTE --rules examples/data/rules.csv --output minute-result.json
```

示例是三天合成行情，仅用于工程验收。分钟样例每天只有一根 Bar，不代表全天覆盖。实际研究必须自行核验分钟数据完整性及供应商时间戳约定。

## 公共接口

```python
from decimal import Decimal
from contracts import Instrument, Venue
from engine import run_backtest, OrderIntent
from engine.data import load_bars_csv, load_rules_csv

bars = load_bars_csv('bars.csv', frequency='DAY')
rules = load_rules_csv('rules.csv')
# 正式研究应从权威交易日历传入，包含整个回测区间内的所有交易日。
trading_days = sorted({day for symbol, day in rules})

def signal(bar, state):
    # state.history 仅包含已经结束的原始 Bar；state.cash / positions / sellable 可只读访问。
    # 在这里实现选股/择时；以下仅说明返回契约。
    return []  # 例如 [OrderIntent(bar.symbol, 'BUY', 100)]

result = run_backtest(
    [Instrument(symbol='600519.SH', exchange=Venue.SSE)],
    bars, rules, signal,
    initial_cash=Decimal('100000'), participation_rate=.01,
    trading_days=trading_days, corporate_actions=[],
)
print(result.to_dict())
```

`signal(bar,state)` 每根 Bar 结束调用一次；生成的意图最早在该证券下一根 Bar 开始时执行。多证券同一时刻按固定数据顺序依次回调，横截面策略应收齐所需证券的结束 Bar 后统一决策。没有自动目标权重优化器，数量由策略返回。

- 无价格意图：观察下一 Bar 开盘价后提交该价格的原生 IOC 限价单，允许部分成交，未成交余量取消；并非交易所市价指令的完整模拟。
- 显式 `limit_price`：原生 DAY 限价单，可在后续原生 OHLC 撮合中成交，日终统一撤单。
- 无下一 Bar 的意图记入 `pending_intents`，原因 `END_OF_DATA`；不强制在最后收盘虚构平仓。
- 挂单卖出数量先预留，撤单/拒单/到期释放；可卖数量受 T+1 和新增股上市日限制。
- 沪深证券共享一个模拟 `ASHARE` 场所和一个 CNY 现金账户；真实交易所保留于证券主数据。该模拟场所不是实盘路由。

## 数据契约

1. **未复权 OHLC**：成交、持仓和估值使用原始价格，公司行为另行输入。不得将前/后复权行情直接送入资金回测；因子研究可独立计算复权信号。
2. **时间**：显式时区的 `BacktestBar.start/end`，按上海时区解释交易日。日线为 09:30–15:00；分钟线须在连续交易时段内，不能跨午休或彼此重叠。
3. **成交量**：单位为股、非负整数。Tushare 日线 `vol` 若以手计，CSV 读取时明确传 `volume_multiplier=100`；不得猜测单位。分钟接口须另核验单位与起止标签。
4. **交易日历**：正式研究传 `trading_days`，源自交易所日历（例如已落库的 Tushare `trade_cal`）。不传时，默认使用行情首末日期内的规则日期并集；它只能发现已声明交易日的缺口，不能识别行情和规则同时漏掉的日期。
5. **证券日规则**：`rules[(symbol, date)]` 要有 `trade_status`（OPEN/SUSPENDED/DAY_BLOCKED）、`limit_state`（BOUNDED/UNBOUNDED）以及 buy_min/buy_step/sell_min/sell_step/max_order。BOUNDED 必须提供 down_limit/up_limit；可交易 OHLC 必须落在价格边界内。规则必须使用对应历史时点的板块、ST/上市状态和官方价格边界，不能用今天的证券状态回填历史。
6. **缺失与停牌**：每个交易日、每个已上市标的必须有规则。OPEN 缺整天 Bar 直接失败，不能当成停牌；SUSPENDED 可无 Bar。零量不成交。分钟内部缺口仍需在数据预处理阶段核验。
7. **公司行为**：`CorporateAction` 接收登记日、除权日、派息日、新增股份上市日；现金红利必须提供登记日。每股现金红利为税前，分币 ROUND_HALF_UP；处理应收及实际到账。送转同时更新原生持仓、网关、参考账本；上市日前新增股不可卖。除权日无行情时按 `(上一估值价-每股现金红利)/(1+送转比例)` 调整估值单位。
8. **零碎股**：公共运行器遇到账户级非整数权益时不默许丢失，要求 `reject` 或显式 `cash_in_lieu`；现金替代是研究假设，不等同于各登记结算规则的复现。`floor` 仅兼容旧独立账本接口，不适合作为普遍严格政策。

本地 CSV 格式见 `examples/data`。规则加载器还可接收 prev_close/issue_price，供无涨跌幅阶段的申报范围检查。仓库不会联网补齐缺失规则或静默替换数据源。

## 成交与资金约束

每根 Bar 的执行预算为 `floor(实际成交量 × participation_rate)`，开盘报价和后续 OHLC 撮合、所有方向和所有订单共享，成交后扣减。策略看到的仍只有已结束行情；执行模拟器可用当前 Bar 的事后成交量限制结果，这是一项 Bar 级模型假设。

提交数量另受上一根非零 Bar 已知成交量的参与率预算约束，防止策略从成交反馈直接获得完整未来 Bar 容量。实际当根 Bar 容量小于申报量时允许部分成交。不开启无限流动性回退。

涨停买入、跌停卖出采取保守拦截；价格笼子在提交时以前开盘可得报价检查。Bar 只有 OHLCV，无法重建逐笔最优报价、队列、集合竞价成交分配、盘中临停或撤单时间细节；09:30 的开盘报价只是一项研究近似。OHLC 价格路径由 NT 原生撮合决定。

当前费用模型为仓库既有的券商佣金最低额、订单累计部分成交、印花税和过户费及其已编码历史日期，不代表任意券商或任意历史时期费率。个人投资者持有期差别化股息税、融资融券、配股、要约收购和退市结算等特殊事件未纳入公共运行器；含这些事件的样本必须另行建模后才能使用其净收益。

## 可核验结果

结果包含真实原生成交与订单终态、规则/原生拒单、持仓、原生与参考现金、公司行为现金日志、逐日 NAV、应收红利和期末未执行意图。

每日核对：原生现金=参考现金；原生持仓=网关持仓=参考持仓；原生现金+持仓估值+独立应收=参考 NAV。不相等直接报错，不生成“通过”结果。估值价格是共用输入；对账不是对原始数据真实性或市场成交真实性的证明。

测试与限制详见 `docs/validation.md`。当前目标是这份明确输入契约下的常规 A 股策略资金回测，不是全市场、全资产、高频、集合竞价或实盘生产认证。

买单会预留申报上限本金与预计费用，部分成交后重算，取消/拒绝/过期释放；资金不足记录 INSUFFICIENT_CASH_WITH_FEES 并继续。成交前再次按实际价与累计费用限制可支付量，以覆盖部分成交舍入差异。
