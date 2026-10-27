"""Corporate-action cash applied to the real NT simulated account, once per event."""
from decimal import Decimal, ROUND_HALF_UP
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.objects import Money


def action_id(action):
    return f"{action.symbol}|{action.ex_date}|{action.pay_date or action.ex_date}"


class AShareAccountModule(SimulationModule):
    def __init__(self):
        super().__init__(SimulationModuleConfig())
        self.entitlements = {}
        self.payments = {}
        self.journal = []

    def pre_process(self, data):
        pass

    def process(self, ts_now):
        pass

    def log_diagnostics(self, logger):
        pass

    def reset(self):
        self.entitlements.clear()
        self.payments.clear()
        self.journal.clear()

    def record_entitlement(self, action, quantity):
        qty = Decimal(str(quantity))
        if not qty.is_finite() or qty < 0:
            raise ValueError("Invalid native dividend entitlement")
        key = action_id(action)
        if key in self.entitlements and self.entitlements[key] != qty:
            raise ValueError("Conflicting native entitlement snapshot")
        self.entitlements[key] = qty

    def adjust_cash(self, event_id, amount):
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Corporate-action cash must be non-negative and finite")
        amount = amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if event_id in self.payments:
            if self.payments[event_id] != amount:
                raise ValueError("Conflicting corporate-action cash replay")
            return Decimal(0)
        if self.exchange is None or self.exchange.get_account() is None:
            raise RuntimeError("Native simulated account is not initialized")
        before = self.exchange.get_account().balance_total(CNY).as_double()
        if amount:
            self.exchange.adjust_account(Money(float(amount), CNY))
        after = self.exchange.get_account().balance_total(CNY).as_double()
        if abs((after - before) - float(amount)) > .000001:
            raise RuntimeError("Native account did not apply corporate-action cash")
        self.payments[event_id] = amount
        self.journal.append({"event_id": event_id, "amount": str(amount)})
        return amount

    def pay_dividend(self, action, trade_date):
        if str(trade_date) < str(action.pay_date or action.ex_date):
            raise ValueError("Dividend payment precedes its payment date")
        key = action_id(action)
        if key not in self.entitlements:
            raise RuntimeError("Missing native record-date entitlement")
        amount = self.entitlements[key] * action.cash_div
        return self.adjust_cash(key + "|dividend", amount)
