"""One shared execution budget per raw bar, including opening and OHLC fills."""
from decimal import Decimal
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType, OrderSide, BookAction
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas


def synthetic_book(instrument,price,quantity,ts):
    """Bar-derived depth, not observed L2: native L2 avoids L1 slip-fill fallback."""
    qty=Quantity.from_int(max(quantity,1))
    return OrderBookDeltas(instrument.id,[
        OrderBookDelta(instrument.id,BookAction.CLEAR,None,0,0,ts,ts),
        OrderBookDelta(instrument.id,BookAction.ADD,BookOrder(OrderSide.BUY,price,qty,1),0,0,ts,ts),
        OrderBookDelta(instrument.id,BookAction.ADD,BookOrder(OrderSide.SELL,price,qty,2),128,0,ts,ts),
    ])


class BarLiquidity(FillModel):
    def __init__(self):
        super().__init__()
        self.events={};self.active={};self.remaining={};self.intrabar={}
        self.affordable=None

    def begin(self,data):
        iid=str(data.instrument_id if hasattr(data,'instrument_id') else data.bar_type.instrument_id)
        key,cap,intrabar=self.events[(iid,data.ts_init)]
        self.active[iid]=key
        self.intrabar[iid]=intrabar
        self.remaining.setdefault(key,cap)

    def consume(self,iid,quantity):
        key=self.active[str(iid)]
        self.remaining[key]-=int(Decimal(str(quantity)))
        if self.remaining[key]<0:raise RuntimeError('Native fills exceeded bar participation budget')

    def get_orderbook_for_fill_simulation(self,instrument,order,best_bid,best_ask):
        book=OrderBook(instrument_id=instrument.id,book_type=BookType.L2_MBP)
        iid=str(instrument.id)
        qty=min(self.remaining.get(self.active.get(iid),0),int(Decimal(str(order.leaves_qty))))
        # Touch inside a bar fills at the submitted limit, never at the bar extreme.
        price=order.price if self.intrabar.get(iid) else (best_ask if order.side==OrderSide.BUY else best_bid)
        if self.affordable is not None and order.side==OrderSide.BUY:
            qty=self.affordable(order,price,qty)
        if qty:
            side=OrderSide.SELL if order.side==OrderSide.BUY else OrderSide.BUY
            book.add(BookOrder(side,price,Quantity.from_int(qty),1),0,0)
        return book


class LiquidityModule(SimulationModule):
    def __init__(self,model):
        super().__init__(SimulationModuleConfig())
        self.model=model

    def pre_process(self,data):self.model.begin(data)
    def process(self,ts_now):pass
    def log_diagnostics(self,logger):pass
    def reset(self):self.model.active.clear();self.model.remaining.clear();self.model.intrabar.clear()
