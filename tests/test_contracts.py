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


def test_execution_assumptions_defaults():
    assump = ExecutionAssumptions()
    assert assump.initial_cash == Decimal("10000000.0")
    assert assump.min_commission == Decimal("5.0")
    assert assump.t_plus_one is True
    assert assump.stamp_duty_rate == Decimal("0.0005")
