"""
企业级量化投研平台 - 不可变研究契约核心数据模型 (Research Contracts)
遵循 Pydantic v2 与不可变设计原则，支撑跨模块、跨引擎（VectorBT / NautilusTrader）标准化数据流。
"""

from __future__ import annotations
from collections.abc import Mapping
import copy
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_serializer


class FrozenDict(Mapping):
    """不可变字典包装类，支持哈希、Pickle序列化、深拷贝与 Pydantic 导出 (N11)"""
    def __init__(self, data: Mapping | dict):
        self._data = dict(data)
        self._hash = None

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __setitem__(self, key, value):
        raise TypeError("FrozenDict is immutable and cannot be modified")

    def __delitem__(self, key):
        raise TypeError("FrozenDict is immutable and cannot be modified")

    def __repr__(self):
        return repr(self._data)

    def __eq__(self, other):
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return False

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(frozenset(self._data.items()))
        return self._hash

    def __reduce__(self):
        return (FrozenDict, (self._data,))

    def __deepcopy__(self, memo):
        return FrozenDict(copy.deepcopy(self._data, memo))


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    FX = "FX"


class Venue(str, Enum):
    SSE = "SSE"      # 上海证券交易所
    SZSE = "SZSE"    # 深圳证券交易所
    BSE = "BSE"      # 北京证券交易所
    CFFEX = "CFFEX"  # 中金所
    SHFE = "SHFE"    # 上期所
    DCE = "DCE"      # 大商所
    CZCE = "CZCE"    # 郑商所


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Instrument(BaseModel):
    """证券主数据模型 (Securities Master)"""
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(..., description="标准证券代码，例如 '600519.SH' 或 '000001.SZ'")
    exchange: Venue = Field(..., description="所属交易所")
    asset_class: AssetClass = Field(default=AssetClass.EQUITY)
    name: str = Field(default="", description="证券中文简称")
    board: str = Field(default="主板", description="交易板块：主板/创业板/科创板/北交所")
    lot_size: int = Field(default=100, description="买入最小整手单位")
    min_order_qty: int = Field(default=100, description="最低起购股数（如科创板为200股）")
    tick_size: Decimal = Field(default=Decimal("0.01"), description="最小价格变动单位")
    currency: str = Field(default="CNY")
    list_date: Optional[date] = Field(default=None, description="上市日期")
    delist_date: Optional[date] = Field(default=None, description="退市日期")
    is_suspended: bool = Field(default=False, description="当前是否停牌")


class Bar(BaseModel):
    """不可变行情 Bar 数据 (日频/分钟频)"""
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime = Field(..., description="Bar 结束时间 (UTC/Local Explicit)")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Field(..., description="成交量 (股)")
    amount: Optional[Decimal] = Field(default=None, description="成交额 (元)")
    adj_factor: Decimal = Field(default=Decimal("1.0"), description="复权因子")


class CorporateAction(BaseModel):
    """公司行为模型 (分红、送股、转增、拆细)"""
    model_config = ConfigDict(frozen=True)

    symbol: str
    ex_date: date = Field(..., description="除权除息日")
    announcement_date: Optional[date] = Field(default=None, description="公告日")
    record_date: Optional[date] = Field(default=None, description="股权登记日")
    pay_date: Optional[date] = Field(default=None, description="红利派发日/股份到账日")
    cash_div: Decimal = Field(default=Decimal("0.0"), description="每股税前现金红利 (元)")
    bonus_ratio: Decimal = Field(default=Decimal("0.0"), description="每股送股比例 (股)")
    reserve_ratio: Decimal = Field(default=Decimal("0.0"), description="每股转增比例 (股)")
    share_listing_date: Optional[date] = Field(default=None, description="新增股份可交易上市日")
    fractional_policy: str = Field(
        default="floor",
        description="公司行为零碎股处理策略: 'floor' (向下取整，默认), 'reject' (存在零碎股时抛出异常), 'cash_in_lieu' (零碎部分现金折现)",
    )


class ExecutionAssumptions(BaseModel):
    """真实交易与制度假设"""
    model_config = ConfigDict(frozen=True)

    initial_cash: Decimal = Field(default=Decimal("10000000.0"), description="初始资金，默认1000万")
    commission_rate: Decimal = Field(default=Decimal("0.0003"), description="佣金费率，默认万三")
    min_commission: Decimal = Field(default=Decimal("5.0"), description="最低单笔佣金，默认5元")
    stamp_duty_rate: Decimal = Field(default=Decimal("0.0005"), description="卖方单边印花税，默认千分之0.5")
    transfer_fee_rate: Decimal = Field(default=Decimal("0.00001"), description="双边过户费，默认十万分之一")
    slippage: Decimal = Field(default=Decimal("0.01"), description="固定价格滑点")
    volume_limit_ratio: Decimal = Field(default=Decimal("0.01"), description="单日/单Bar最大成交量参与上限")
    t_plus_one: bool = Field(default=True, description="A 股 T+1 交易规则约束")


class TargetWeights(BaseModel):
    """统一目标权重模型 (跨 VectorBT 与 NautilusTrader 共享)"""
    model_config = ConfigDict(frozen=True)

    signal_date: date = Field(..., description="信号生成基准日 (观察时间)")
    execution_date: date = Field(..., description="预期最早执行交易日 (T+1 开盘/收盘)")
    weights: Dict[str, float] = Field(..., description="标的权重映射表 (总和 <= 1.0)")
    strategy_id: str = Field(default="default_strategy")
    params_hash: str = Field(default="")

    @field_serializer("weights", mode="plain")
    def serialize_weights(self, v) -> dict[str, float]:
        return dict(v)

    @model_validator(mode="after")
    def validate_target_weights(self) -> "TargetWeights":
        if self.execution_date < self.signal_date:
            raise ValueError(f"execution_date ({self.execution_date}) cannot precede signal_date ({self.signal_date})")
        for sym, w in self.weights.items():
            if not isinstance(w, (int, float)) or not math.isfinite(w):
                raise ValueError(f"Weight for {sym} must be a finite number, got {w}")
            if w < 0.0 or w > 1.0:
                raise ValueError(f"Weight for {sym} must be between 0.0 and 1.0, got {w}")
        total_w = sum(self.weights.values())
        if total_w > 1.000001:
            raise ValueError(f"Total target weights sum to {total_w:.4f}, exceeding 1.0 limit")
        # 深冻结字典，防止通过内层字典直接篡改数据，同时支持 JSON 与深复制 (N11)
        object.__setattr__(self, "weights", FrozenDict(self.weights))
        return self


class Fill(BaseModel):
    """成交明细记录"""
    model_config = ConfigDict(frozen=True)

    fill_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime

    @model_validator(mode="after")
    def validate_fill(self) -> "Fill":
        if self.quantity <= Decimal("0.0"):
            raise ValueError(f"Fill quantity must be positive, got {self.quantity}")
        if self.price <= Decimal("0.0"):
            raise ValueError(f"Fill price must be positive, got {self.price}")
        if self.fee < Decimal("0.0"):
            raise ValueError(f"Fill fee cannot be negative, got {self.fee}")
        return self


class DailyNAV(BaseModel):
    """每日资产净值与账本快照"""
    model_config = ConfigDict(frozen=True)

    date: date
    cash: Decimal
    positions_value: Decimal
    total_equity: Decimal
    receivable_dividends: Decimal = Field(default=Decimal("0.0"), description="应收现金红利 (除息后、到账前)")
    holdings: Dict[str, Decimal] = Field(default_factory=dict, description="标的持仓股数")
    tradable_holdings: Dict[str, Decimal] = Field(default_factory=dict, description="T+1 可卖股数")


class BacktestSummary(BaseModel):
    """统一回测产物摘要"""
    model_config = ConfigDict(frozen=True)

    engine: str = Field(..., description="'NAUTILUS_TRADER' 或 'VECTORBT'")
    strategy_id: str
    start_date: date
    end_date: date
    trading_days: int
    initial_cash: Decimal
    final_equity: Decimal
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    total_fees: Decimal
    fill_count: int
    direct_match: bool = Field(default=False, description="是否经过独立参考账本精准比对")
    snapshot_id: str = Field(default="")
