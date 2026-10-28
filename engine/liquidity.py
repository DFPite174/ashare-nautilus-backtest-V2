"""One shared execution budget per raw bar, including opening and OHLC fills."""
from decimal import Decimal
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import BookType, OrderSide
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.data import BookOrder


class BarLiquidity(FillModel):
    def __init__(self):
        super().__init__()
        self.events={};self.active={};self.remaining={}
        self.affordable=None

    def begin(self,data):
        iid=str(data.instrument_id if isinstance(data,QuoteTick) else data.bar_type.instrument_id)
        key,cap=self.events[(iid,data.ts_init)]
        self.active[iid]=key
        self.remaining.setdefault(key,cap)

    def consume(self,iid,quantity):
        key=self.active[str(iid)]
        self.remaining[key]-=int(Decimal(str(quantity)))
        if self.remaining[key]<0:raise RuntimeError('Native fills exceeded bar participation budget')

    def get_orderbook_for_fill_simulation(self,instrument,order,best_bid,best_ask):
        book=OrderBook(instrument_id=instrument.id,book_type=BookType.L2_MBP)
        qty=self.remaining.get(self.active.get(str(instrument.id)),0)
        if self.affordable is not None and order.side==OrderSide.BUY:
            qty=self.affordable(order,best_ask,min(qty,int(Decimal(str(order.leaves_qty)))))
        if qty:
            book.add(BookOrder(OrderSide.BUY,best_bid,Quantity.from_int(qty),1),0,0)
            book.add(BookOrder(OrderSide.SELL,best_ask,Quantity.from_int(qty),2),0,0)
        return book


class LiquidityModule(SimulationModule):
    def __init__(self,model):
        super().__init__(SimulationModuleConfig())
        self.model=model

    def pre_process(self,data):self.model.begin(data)
    def process(self,ts_now):pass
    def log_diagnostics(self,logger):pass
    def reset(self):self.model.active.clear();self.model.remaining.clear()
