# Regular A-share Backtest Implementation Plan

> For agentic workers: execute inline using executing-plans and test-driven-development. The user's repair authorization includes the sixth audit's concrete repair directions. Do not publish changes or add live trading.

**Goal:** 官方NautilusTrader驱动可复用的沪深日线/分钟线常规策略回测，真实账户与独立账本一致。

**Architecture:** 修复现有规则/账本/网关，增加共享原生回测运行入口及公司行为账户模块。输入明确的未复权Bar、证券日规则和事件，输出成交、拒单、逐日NAV及对账。

**Tech Stack:** Python3.12、NT1.231.0、Pydantic2；不增加运行依赖。

## Global Constraints

- 基线ef26e8b268fe5dd6fb638e65c4e0ce3e301ae9d6，独立分支codex/complete-ashare-backtest。
- 沪深普通股票、现金多头、日线/分钟线、多日持有；未复权价格和明确点时规则。
- 不连接实盘、不索取凭证、不伪造真实数据/收益；保留旧审计证据。
- 修复后缺上下文必须拒绝；有意义的原断言不得删除。

## Task 1: 强化公共边界

Files: engine/order_gateway.py, engine/market_rules.py, engine/golden_ledger.py, contracts/models.py, tests/test_backtest_repairs.py and existing affected tests.

- [x] 写失败测试：15:00DAY/IOC、缺时间、未知阶段、无涨跌幅收盘范围、完整InstrumentId、负价格/未来缓存、失败不改状态。
- [x] 运行新测试保存RED日志；确认失败来自审计问题。
- [x] 实现时间/阶段与身份检查，现金替代严格验证，账本失败原子性与可交易批次语义；迁移旧测试的无效时间输入。
- [x] 新测试GREEN并运行受影响原测试。

Tests express real interfaces, e.g. `with pytest.raises(ValueError): ledger.apply_stock_split(... fractional_cash_price=Decimal('-1'))`; `assert not gateway.validate_and_reserve(order_at_close, rule, bar_without_ts, 100).is_valid`.

## Task 2: 原生账户与公司行为

Files: engine/accounting.py, tests/test_regular_backtest.py, tests/test_corporate_actions.py.

- [x] 增加分红支付后原生现金必须100200且与参考相等的失败测试。
- [x] 使用SimulationModule注册的exchange.adjust_account(Money(delta,CNY))，事件ID去重，独立参考计算并检查现金；测试重复支付及到账后可用资金。
- [x] 测试送转原生位置、缓存/组合、网关和参考账本在同一引擎中一致。

## Task 3: 可复用回测入口

Files: engine/backtest.py, engine/data.py, engine/__init__.py, tests/test_regular_backtest.py, examples/regular_backtest.py.

- [x] 先写日线/分钟线真实买卖、next-bar执行、T+1和分红再投资验收测试。
- [x] 定义InputBar/OrderIntent/BacktestResult与run_backtest，使用开盘可得报价和收盘Bar驱动真实NT；收盘回调只生成下一开盘意图。
- [x] 公司行为、逐日现金/持仓/NAV对账、DAY终态处理共用一套生命周期；缺规则、缺时点、无效数据失败。
- [x] 提供本地CSV读取、JSON审计产物和可运行示例；测试无未来价格、停牌/零量/涨跌停与取消状态。

## Task 4: 完整验证与交付

Files: README.md, docs/validation.md, outputs evidence and portable archive.

- [x] 运行全部测试、示例与审计修复反例，保留红绿日志及环境/版本。
- [x] 自审调用路径和失败边界，核对原生资金、库存、应收、账本逐日一致。
- [x] 更新能力范围/数据契约/运行命令；输出可移植源码包、git补丁及修复验收报告，不推送远端。

Run from the isolated checkout with the available Python3.12 runtime and an appended existing dependency path: `pytest tests -q -s`. The evidence runner records exact invocation and exit code. Commit only verified local work; no push/PR without authorization.
