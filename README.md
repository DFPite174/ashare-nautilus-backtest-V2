# ashare-nautilus-backtest-V2

> **面向 20 人企业级量化投研团队的 NautilusTrader A 股高保真事件驱动适配底座**  
> 具备 A 股真实微观交易规则（T+1 / 动态多板块涨跌停 / 停复牌 / 板块手数与零股全量清仓）、法定费税（最低5元佣金 / 0.05% 印花税 / 双边过户费）、注册制 2% 申报价格笼子、不可变研究契约（Pydantic v2）、以及独立参考黄金账本（Golden Ledger）双重零漂移核验能力。

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![NautilusTrader 1.231.0](https://img.shields.io/badge/NautilusTrader-1.231.0-brightgreen.svg)](https://nautilustrader.io/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests Passing](https://img.shields.io/badge/tests-25%20passed-brightgreen.svg)]()

---

## 一、 系统架构图 (Rust 撮合核心 + Python 规则桥接)

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
       │  │ Market Rules Engine   │   │ Statutory Fee Model   │ │
       │  │ (T+1/Limits/Collar)   │   │ (Min 5 CNY/Stamp/Xfer)│ │
       │  └───────────────────────┘   └───────────────────────┘ │
       │  ┌───────────────────────┐   ┌───────────────────────┐ │
       │  │ Split/Dividend Native │   │ Independent Golden    │ │
       │  │ (Cython Bridge .pyd)  │   │ Accounting Ledger     │ │
       │  └───────────────────────┘   └───────────────────────┘ │
       └───────────────────────────┬────────────────────────────┘
                                   │ (Direct Callback / AddVenue)
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │               NautilusTrader 1.231.0 Core              │
       │     BacktestEngine | OrderMatching | Shared Cash NAV   │
       │                 (High-Throughput Rust)                 │
       └────────────────────────────────────────────────────────┘
```

---

## 二、 A 股特有规则与落地方位一览表 (Rule-to-File Mapping)

本项目将中国 A 股特有的微观交易结构规则完整解耦并实现在以下模块中：

| 规则项 | 业务定义与微观场景 | 实现文件与核心函数 | 测试断言文件 |
| :---: | :--- | :--- | :--- |
| **Item 1: 板块差异化涨跌停校验** | 主板($\pm 10\%$)、主板ST($\pm 5\%$)、创业/科创($\pm 20\%$)、北交所($\pm 30\%$)、新股前5日无限制、退市整理期首日无限制、法定分位标准四舍五入。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`calculate_price_limits()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item1_board_differentiated_price_limits` |
| **Item 2: T+1 可卖数量与状态流转** | 当日买入股份全量冻结，严禁日内二次卖出；跨日日终结转自动释放；精确支持日内做 T（昨持仓可高抛低吸）与超额卖出硬拦截。 | [`engine/golden_ledger.py`](file:///engine/golden_ledger.py)<br>`on_trade_day_start()`<br>`apply_fill()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item2_t1_available_position_state_machine` |
| **Item 3: 停牌与无行情处理** | 标的停牌拦截申报；日内行情缺失或成交量为 0 时判定为流动性阻断（`ZERO_VOLUME`），杜绝回测假成交。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`block_reason()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item3_suspension_and_no_data_handling` |
| **Item 4: 退市持仓与长停处理** | 长期无行情（如超过 30 交易日）标的触发风险处置：强制残值清算或按折扣（如 50% 折价）计提现金回收。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`handle_delisting_valuation()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item4_delisting_liquidation_and_haircut` |
| **Item 5: 申报数量单位与取整** | 主板 100 股整手买入；科创板 200 股起购+1股递增；送转派息产生的零股支持一次性全量清仓卖出，非全量卖出禁止拆零。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`round_order()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item5_lot_size_and_odd_lot_rules` |
| **Item 6: 法定费税模型** | 最低单笔 5 元券商佣金保底、卖方单边万 5 (0.05%) 印花税、双边十万分之一过户费；继承 NautilusTrader `FeeModel` 原生虚函数。 | [`engine/fee_model.py`](file:///engine/fee_model.py)<br>`AShareFeeModel.get_commission()` | [`tests/test_fee_model.py`](file:///tests/test_fee_model.py)<br>[`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item6_statutory_fee_model` |
| **Item 7: TradingState 联动映射** | 将 A 股 T+1 锁仓状态映射为交易状态：停牌映射为 `HALTED`，当日买入锁仓映射为 `BUY_ONLY`（禁发减仓卖单），已结算持仓映射为 `ACTIVE`。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`get_trading_state()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item7_trading_state_t1_coupling` |
| **Item 8: prev_close 对齐与 2% 价格笼子** | 涨跌停基准以前收盘价 `prev_close` 为锚点；全面注册制 2% 申报价格笼子校验（买单不得高于基准 102%，卖单不得低于基准 98%）。 | [`engine/market_rules.py`](file:///engine/market_rules.py)<br>`validate_price_collar()` | [`tests/test_ashare_all_items_validation.py`](file:///tests/test_ashare_all_items_validation.py)<br>`test_item8_prev_close_and_price_collar` |
| **端到端集成验证** | 真实调用 NautilusTrader Rust 事件循环回测引擎，驱动 `BacktestEngine.add_venue(fee_model=...)` 跑通跨日多 Bar 实测。 | [`tests/test_nautilus_ashare_integration.py`](file:///tests/test_nautilus_ashare_integration.py)<br>`test_nautilus_ashare_integration_e2e()` | 验证最终余额 990,483.44 CNY 精确到分，持仓 100 股零漂移 |

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
│   ├── fee_model.py         # [核心] 法定费税模型 (最低5元/印花税/过户费)
│   ├── golden_ledger.py     # [核心] 独立参考黄金双账本
│   ├── split_guard.py       # 拆股守卫
│   ├── split_native.pyx     # Cython 原生拆股扩展源码
│   └── split_native.cp312-win_amd64.pyd # Windows cp312 二进制构建
├── tests/                   # 工业级单元与集成回归测试套件 (22 项测试全部通过)
│   ├── test_contracts.py
│   ├── test_fee_model.py
│   ├── test_golden_ledger.py
│   ├── test_market_rules.py
│   ├── test_ashare_all_items_validation.py  # A 股 8 大专项规则全面验证
│   └── test_nautilus_ashare_integration.py   # 与 NautilusTrader 引擎端到端集成
├── examples/                # 实战策略示例
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

**预期输出**：
```text
======================= 25 passed, 6 warnings in 1.33s ========================
```

---

## 五、 开源许可

本项目基于 MIT 许可证开源。
欢迎量化同行提交 Issue 与 Pull Request 共同共建高保真 A 股量化研发基础设施！
