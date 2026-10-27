"""
契约模型单元测试 - 检验不可变性、约束验证及跨平台序列化
"""

import pytest
from datetime import date, datetime
from decimal import Decimal
from pydantic import ValidationError

from contracts import (
    Instrument,
    Venue,
    AssetClass,
    Bar,
    CorporateAction,
    ExecutionAssumptions,
    TargetWeights,
    DailyNAV,
)


def test_instrument_immutability():
    inst = Instrument(
        symbol="600519.SH",
        exchange=Venue.SSE,
        name="贵州茅台",
        board="主板",
        lot_size=100,
        min_order_qty=100,
    )
    assert inst.symbol == "600519.SH"
    # 验证不可变性 (Frozen)
    with pytest.raises(ValidationError):
        inst.lot_size = 200  # type: ignore


def test_bar_immutability_and_types():
    b = Bar(
        symbol="000001.SZ",
        timestamp=datetime(2025, 5, 6, 15, 0, 0),
        open=Decimal("10.50"),
        high=Decimal("10.80"),
        low=Decimal("10.40"),
        close=Decimal("10.70"),
        volume=Decimal("1000000"),
        adj_factor=Decimal("1.25"),
    )
    assert b.close == Decimal("10.70")
    with pytest.raises(ValidationError):
        b.close = Decimal("11.00")  # type: ignore


def test_target_weights_validation():
    tw = TargetWeights(
        signal_date=date(2025, 5, 5),
        execution_date=date(2025, 5, 6),
        weights={"600519.SH": 0.5, "000001.SZ": 0.4},
        strategy_id="momentum_top2",
    )
    assert sum(tw.weights.values()) <= 1.0

    # 1. 内层字典被深冻结，禁止原地篡改 (R06)
    with pytest.raises(TypeError):
        tw.weights["600519.SH"] = 2.0  # type: ignore

    # 2. 拒绝 NaN 与非法权重
    with pytest.raises(ValidationError):
        TargetWeights(
            signal_date=date(2025, 5, 5),
            execution_date=date(2025, 5, 6),
            weights={"600519.SH": float("nan")},
        )

    # 3. 拒绝总和超过 1.0
    with pytest.raises(ValidationError):
        TargetWeights(
            signal_date=date(2025, 5, 5),
            execution_date=date(2025, 5, 6),
            weights={"600519.SH": 0.8, "000001.SZ": 0.5},
        )

    # 4. 拒绝执行日期早于信号日期
    with pytest.raises(ValidationError):
        TargetWeights(
            signal_date=date(2025, 5, 6),
            execution_date=date(2025, 5, 5),
            weights={"600519.SH": 0.5},
        )


def test_execution_assumptions_defaults():
    assump = ExecutionAssumptions()
    assert assump.initial_cash == Decimal("10000000.0")
    assert assump.min_commission == Decimal("5.0")
    assert assump.t_plus_one is True
    assert assump.stamp_duty_rate == Decimal("0.0005")


def test_target_weights_serialization_and_deep_copy():
    """
    【N11 契约序列化与深复制回归检验】
    验证深冻结 TargetWeights 支持 Pydantic model_dump_json()、model_copy(deep=True)、
    以及 pickle 跨进程序列化，同时保持严格不可变性。
    """
    import pickle
    import json

    tw = TargetWeights(
        signal_date=date(2025, 5, 5),
        execution_date=date(2025, 5, 6),
        weights={"600519.SH": 0.6, "000001.SZ": 0.4},
        strategy_id="stat_arb_01",
    )

    # 1. Pydantic model_dump_json() 必须成功并能反序列化
    json_str = tw.model_dump_json()
    assert isinstance(json_str, str)
    data = json.loads(json_str)
    assert data["weights"]["600519.SH"] == 0.6
    assert data["weights"]["000001.SZ"] == 0.4

    # 2. model_copy(deep=True) 必须成功且拷贝对象同样保持不可变
    tw_copied = tw.model_copy(deep=True)
    assert tw_copied.weights["600519.SH"] == 0.6
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        tw_copied.weights["600519.SH"] = 0.9  # type: ignore

    # 3. pickle 序列化与跨进程传输能力
    pickled_bytes = pickle.dumps(tw)
    tw_unpickled = pickle.loads(pickled_bytes)
    assert tw_unpickled.weights == tw.weights
    with pytest.raises(TypeError, match="FrozenDict is immutable"):
        tw_unpickled.weights["000001.SZ"] = 0.1  # type: ignore
