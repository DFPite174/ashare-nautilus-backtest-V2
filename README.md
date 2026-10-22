# ashare-nautilus-backtest-V2

> **面向企业级量化投研平台的 NautilusTrader A 股事件驱动适配版 V2**  
> 具备 A 股真实交易规则（T+1 / 动态涨跌停 / 停复牌 / 板块手数与零股）、法定费税（最低5元佣金 / 0.05% 印花税 / 过户费）、不可变研究契约（Pydantic v2）、除权除息与拆股处理、以及独立参考黄金账本（Golden Ledger）双重核验能力。

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![NautilusTrader 1.231.0](https://img.shields.io/badge/NautilusTrader-1.231.0-brightgreen.svg)](https://nautilustrader.io/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 一、 V2 核心演进与架构升级

相较于 V1 原型版本，V2 进行了全方位工业级重构：

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
       │  │ (T+1/Limits/Lot-size) │   │ (Min 5 CNY/Stamp/Xfer)│ │
       │  └───────────────────────┘   └───────────────────────┘ │
       │  ┌───────────────────────┐   ┌───────────────────────┐ │
       │  │ Split/Dividend Native │   │ Independent Golden    │ │
       │  │ (Cython Bridge .pyd)  │   │ Accounting Ledger     │ │
       │  └───────────────────────┘   └───────────────────────┘ │
       └───────────────────────────┬────────────────────────────┘
                                   │
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │               NautilusTrader 1.231.0 Core              │
       │     BacktestEngine | OrderMatching | Shared Cash NAV   │
       └────────────────────────────────────────────────────────┘
```

1. **强类型不可变契约**：摒弃传统弱类型字典传参，全面采用 Pydantic v2 冻结实体 (`contracts.models`)，消除跨模块静默传参错误。
2. **中国 A 股真实微观结构适配**：
   - **T+1 交易规则**：精确按日跟踪股份买入时间戳，严格阻止同日买入股份的二次卖出。
   - **交易数量约束**：主板 100 股整数倍买入、科创板 200 股起购；支持全量清仓时卖出零股（避免碎股沉淀）。
   - **动态涨跌停与停复牌**：涨停收盘价拦截买单、跌停收盘价拦截卖单、停牌标的顺延时钟。
   - **法定交易成本**：最低 5 元单笔券商佣金阈值、卖方单边 0.05% 印花税、双边十万分之一过户费与价格滑点。
3. **独立参考黄金账本 (Golden Ledger)**：
   - 独立于回测引擎之外运行的高精度 Decimal 会计账本，逐日比对现金流、持仓演进与分红到账，确保 NautilusTrader 计算结果零漂移。
4. **公司行为与拆股桥接**：
   - 保留并规范化 Cython 原生拆股事件桥接 (`split_native`)，精确处理分红现金入账与送转股上市日锁定。

---

## 二、 目录结构

```text
ashare-nautilus-backtest-V2/
├── contracts/               # 不可变研究契约定义 (Instrument, Bar, TargetWeights 等)
│   ├── __init__.py
│   └── models.py
├── engine/                  # A 股事件驱动核心适配引擎
│   ├── __init__.py
│   ├── market_rules.py      # A 股市场规则引擎 (手数规整/涨跌停/停牌)
│   ├── fee_model.py         # 法定费税模型 (最低5元/印花税)
│   ├── golden_ledger.py     # 独立参考黄金账本
│   ├── split_guard.py       # 拆股守卫
│   ├── split_native.pyx     # Cython 原生拆股扩展源码
│   └── split_native.cp312-win_amd64.pyd # Windows cp312 二进制构建
├── tests/                   # 工业级单元与回归测试套件
│   ├── test_contracts.py
│   ├── test_fee_model.py
│   ├── test_golden_ledger.py
│   └── test_market_rules.py
├── examples/                # 实战策略示例
├── pyproject.toml           # 项目元数据与依赖定义
├── requirements.txt         # 核心环境依赖
└── README.md
```

---

## 三、 快速上手

### 1. 安装与环境要求
- **Python**: `>= 3.12` (推荐 Python 3.12.x 64-bit)
- **平台**: Windows x64 / Linux

```bash
# 克隆仓库
git clone https://github.com/DFPite174/ashare-nautilus-backtest-V2.git
cd ashare-nautilus-backtest-V2

# 创建并激活虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装核心依赖
pip install -r requirements.txt
```

### 2. 执行自动化测试
```bash
pytest tests/ -v
```

---

## 四、 贡献与开源许可

本项目基于 MIT 许可证开源。
欢迎提交 Issue 与 Pull Request 共同完善中国 A 股及跨市场高保真量化事件驱动回测生态！
