"""
ashare-nautilus-backtest-engine
NautilusTrader A 股事件驱动回测适配核心引擎
"""

from engine.market_rules import (
    DailyMarketRule,
    validate_rules,
    block_reason,
    round_order,
    calculate_price_limits,
    validate_price_collar,
    get_trading_state,
)
from engine.fee_model import (
    AShareFeeModel,
    calculate_expected_fee,
    get_ashare_rates_for_date,
)
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway, OrderValidationResult
from engine.backtest import BacktestBar, OrderIntent, BacktestResult, run_backtest

__all__ = [
    "DailyMarketRule",
    "validate_rules",
    "block_reason",
    "round_order",
    "calculate_price_limits",
    "validate_price_collar",
    "get_trading_state",
    "AShareFeeModel",
    "calculate_expected_fee",
    "get_ashare_rates_for_date",
    "GoldenLedger",
    "AShareOrderGateway",
    "OrderValidationResult",
    "BacktestBar", "OrderIntent", "BacktestResult", "run_backtest",
]
