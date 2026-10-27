# ashare-nautilus-backtest-V2

> **面向企业级量化投研团队的 NautilusTrader A 股高保真事件驱动回测适配底座**  
> 深度对接 NautilusTrader 事件驱动框架，提供中国 A 股全套法定与微观市场交易规则：T+1 状态机流转、前置订单风控网关、挂单库存原子预留、动态多板块涨跌幅（含 2026 年主板 ST 改革制度）、注册制 2% 申报价格笼子、整手与零股清仓规则、法定费税（订单级最低 5 元佣金 / 印花税 / 过户费）、不可变研究契约（Pydantic v2）、Cython 原生拆股支持、以及独立参考黄金账本（Golden Ledger）双轨零漂移核验。

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![NautilusTrader 1.231.0](https://img.shields.io/badge/NautilusTrader-1.231.0-brightgreen.svg)](https://nautilustrader.io/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests Passing](https://img.shields.io/badge/tests-43%20passed-brightgreen.svg)]()

---

## 一、 系统架构图

本项目基于 NautilusTrader 1.231.0 架构，由 Python/Cython 回测撮合引擎、Cython 原生加速扩展、Python A 股制度与风控适配层、以及独立参考黄金账本共同构成：

```text
       ┌────────────────────────────────────────────────────────┐
       │              Research Contracts (Pydantic v2)          │
       │    Instrument | Bar | TargetWeights | CorporateAction  │
       └───────────────────────────┬────────────────────────────┘
                                   │
       ┌───────────────────────────┴────────────────────────────┐
       │             ashare-nautilus-backtest-V2 Core           │
       │                                                        │
       │  ┌───────────────────────┐   ┌───────────────────────┐ │
       │  │ Pre-Trade Gateway     │   │ Statutory Fee Model   │ │
       │  │ (Collar/Limits/T+1)   │   │ (Min 5 CNY/Stamp/Xfer)│ │
       │  └───────────────────────┘   └───────────────────────┘ │
       │  ┌───────────────────────┐   ┌───────────────────────┐ │
       │  │ Split/Dividend Native │   │ Independent Golden    │ │
       │  │ (Cython Bridge .pyd)  │   │ Accounting Ledger     │ │
       │  └───────────────────────┘   └───────────────────────┘ │
       └───────────────────────────┬────────────────────────────┘
                                   │ (Direct Callback / AddVenue)
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │               NautilusTrader 1.231.0 Engine            │
       │    BacktestEngine | OrderMatchingEngine | Portfolio    │
       │       (Cython Execution Engine with Rust Core)         │
       └────────────────────────────────────────────────────────┘
```

---

## 二、 A 股特有规则与落地方位一览表 (Rule-to-File Mapping)

本项目将中国 A 股特有的微观交易结构与制度规则解耦并实现在以下模块中：

| 规则项 | 业务定义与微观场景 | 实现文件与核心函数 | 测试断言文件 |
| :---: | :--- | :--- | :--- |
| **板块差异化涨跌停校验** | 沪深主板 ($\pm 10\%$)、主板 ST 标的（2026-07-06 前 $\pm 5\%$，2026-07-06 起调整为 $\pm 10\%$）、创业板/科创板 ($\pm 20\%$)、北交所（上市首日无限制，次日起 $\pm 30\%$）、主板与双创前 5 日无限制、退市整理期首日无限制、法定分位四舍五入。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`calculate_price_limits()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item1_board_differentiated_price_limits` |
| **前置订单风控网关与挂单预留** | 申报前不可绕过的前置校验网关：检查 2% 价格笼子、整手规范、时钟有效期（强制 `TimeInForce.DAY`，拒绝跨日 GTC 挂单）；卖单申报时即时原子预留卖出份额，撤单/拒单/到期安全释放预留。 | [`engine/order_gateway.py`](file:///engine/order_gateway.py)<br>`AShareOrderGateway`<br>`validate_and_reserve()` | [`tests/test_nautilus_ashare_integration.py`](file:///tests/test_nautilus_ashare_integration.py)<br>`test_gtc_cross_day_order_rejected_by_gateway`<br>`test_concurrent_sell_orders_reservation_rejected` |
| **T+1 可卖数量与状态流转** | 成交归属日严格依真实纳秒时戳（`Asia/Shanghai`）确定；当日买入股份锁定，跨日开盘自动释放为可卖；精准支持日内做 T（昨日持仓可高抛低吸）与超额卖出硬拦截。 | [`engine/golden_ledger.py`](file:///engine/golden_ledger.py)<br>[`engine/order_gateway.py`](file:///engine/order_gateway.py)<br>`on_trade_day_start()`<br>`apply_fill()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item2_t1_available_position_state_machine` |
| **停牌与流动性阻断** | 标的停牌拦截申报；日内行情缺失或成交量为 0 时判定为流动性阻断（`ZERO_VOLUME`），杜绝撮合假成交。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`block_reason()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item3_suspension_and_no_data_handling` |
| **退市长停资产减值评估** | 长期无行情（如超过 30 交易日）标的触发财务减值评估（Haircut 计提），真实反映流动性缺失风险，不虚构现金流入。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`handle_delisting_valuation()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item4_delisting_liquidation_and_haircut` |
| **申报数量单位与零股清仓** | 主板 100 股整手买入；科创板 200 股起购 + 1 股递增；送转派息产生的零股支持一次性全量清仓卖出，非全量卖出禁止拆零。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`round_order()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item5_lot_size_and_odd_lot_rules` |
| **法定费税与订单级累计** | 单笔母单最低 5 元券商佣金保底（部分成交不重复扣收 5 元）、卖方单边万 5 (0.05%) 印花税（支持 2023-08-28 历史减半切换）、双边十万分之一过户费；支持多维复合命名空间隔离与独立费用 Oracle。 | [`engine/fee_model.py`](file:///engine/fee_model.py)<br>`AShareFeeModel`<br>`calculate_expected_fee()` | [`tests/test_fee_model.py`](file:///tests/test_fee_model.py)<br>[`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py) |
| **TradingState 状态联动** | 将 A 股持仓状态映射为交易状态：停牌映射为 `HALTED`，当日买入锁仓映射为 `BUY_ONLY`，已结算持仓映射为 `ACTIVE`。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`get_trading_state()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item7_trading_state_t1_coupling` |
| **2% 申报价格笼子机制** | 注册制限价申报价格笼子：买单不得高于基准价格 102% 与基准价 +0.10 元之较大者；卖单不得低于基准价格 98% 与基准价 -0.10 元之较小者。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`validate_price_collar()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item8_prev_close_and_price_collar` |
| **公司行为与原生持仓调整** | Cython 原生扩展 `split_native` 高效调整 NautilusTrader 原生持仓与开仓均价（经济价值严格守恒）；黄金账本完整实现股权登记日权益锁定、除权除息日拆股、派现日现金到账全流程。 | [`engine/split_native.pyx`](file:///engine/split_native.pyx)<br>[`engine/golden_ledger.py`](file:///engine/golden_ledger.py) | [`tests/test_corporate_actions.py`](file:///tests/test_corporate_actions.py) |
| **单调时序估值与逐日 NAV** | 黄金账本具备单调时间序列与价格归属时间保护，杜绝逆时估值与未来价格泄露；日终收盘自动生成 NAV 快照，核验持仓、现金与总资产双轨零漂移。 | [`engine/golden_ledger.py`](file:///engine/golden_ledger.py)<br>`mark_to_market()` | [`tests/test_golden_ledger.py`](file:///tests/test_golden_ledger.py)<br>[`tests/test_nautilus_ashare_integration.py`](file:///tests/test_nautilus_ashare_integration.py) |

---

## 三、 目录结构

```text
ashare-nautilus-backtest-V2/
├── contracts/               # 不可变研究契约定义 (Instrument, Bar, TargetWeights 等)
│   ├── __init__.py
│   └── models.py
├── engine/                  # A 股事件驱动核心适配引擎
│   ├── __init__.py
│   ├── market_rules.py      # [核心] 涨跌停/手数规整/价格笼子/停退市规则
│   ├── order_gateway.py     # [核心] 前置订单风控网关与挂单库存预留
│   ├── fee_model.py         # [核心] 法定费税模型 (最低5元/印花税/过户费/独立Oracle)
│   ├── golden_ledger.py     # [核心] 独立参考黄金双账本 (逐日NAV/时序保护/公司行为)
│   ├── split_guard.py       # 拆股扩展加载守卫
│   ├── split_native.pyx     # Cython 原生拆股扩展源码
│   └── split_native.cp312-win_amd64.pyd # Windows cp312 二进制构建
├── tests/                   # 自动化测试套件 (43 项测试全量通过)
│   ├── test_contracts.py                 # 研究契约深不可变与边界校验测试
│   ├── test_fee_model.py                 # 订单级费用累计、缓存隔离与 Oracle 测试
│   ├── test_golden_ledger.py             # 黄金账本幂等性、单调时序与公司行为测试
│   ├── test_market_rules.py              # 市场规则引擎单元测试
│   ├── test_corporate_actions.py         # Cython 原生拆股扩展与公司行为端到端测试
│   ├── test_ashare_all_items_validation.py # A 股多板块交易规则专项测试
│   └── test_nautilus_ashare_integration.py  # NautilusTrader 全生命周期端到端集成测试
├── pyproject.toml           # 项目元数据与依赖定义
├── requirements.txt         # 核心环境依赖
└── README.md
```

---

## 四、 快速上手与回归测试

### 1. 安装核心依赖
- **Python**: `>= 3.12`
```bash
pip install -r requirements.txt
```

### 2. 执行全量自动化回归测试
```bash
pytest tests/ -v
```

**运行结果**：
```text
======================= 43 passed, 8 warnings in 1.39s ========================
```

---

## 五、 开源许可

本项目基于 MIT 许可证开源。
欢迎量化同行提交 Issue 与 Pull Request 共同共建高保真 A 股量化研发基础设施！
