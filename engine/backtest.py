"""Cash-long A-share research backtests on the official NautilusTrader engine.

Opening quotes are explicit bar-level liquidity approximations, not an auction or
queue reconstruction. A closed bar can only create orders for a later bar open.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, AggregationSource, BarAggregation, OmsType, OrderSide, PriceType, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from contracts import CorporateAction, Fill, Instrument, OrderSide as LedgerSide
from engine.accounting import AShareAccountModule, action_id
from engine.fee_model import AShareFeeModel, calculate_expected_fee
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway
from engine.split_guard import load_bridge
from engine.liquidity import BarLiquidity, LiquidityModule

TZ = ZoneInfo("Asia/Shanghai")
SIM = Venue("ASHARE")
D = Decimal


def nanos(t):
    return int(t.timestamp() * 1_000_000_000)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), TZ)


@dataclass(frozen=True)
class BacktestBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class ResearchState:
    cash: Decimal
    positions: object
    sellable: object
    history: object


@dataclass
class BacktestResult:
    native_cash: Decimal
    ledger_cash: Decimal
    positions: dict
    fills: list
    rejections: list
    daily_nav: list
    account_journal: list
    orders: list
    pending_intents: list
    execution_model: str = "next-bar-open IOC limit for unpriced intents; DAY limit with native OHLC; shared bar volume cap; no queue/auction reconstruction"

    def to_dict(self):
        from dataclasses import asdict
        def convert(x):
            if isinstance(x, Decimal): return str(x)
            if isinstance(x, dict): return {str(k): convert(v) for k,v in x.items()}
            if isinstance(x, (list,tuple)): return [convert(v) for v in x]
            return x
        return convert(asdict(self))


def _validate(instruments, bars, rules, actions, initial_cash, rate, trading_days):
    if not initial_cash.is_finite() or initial_cash <= 0 or not 0 < rate <= 1:
        raise ValueError("Invalid cash or participation_rate")
    master = {i.symbol:i for i in instruments}
    if not master or len(master) != len(instruments) or not bars:
        raise ValueError("Unique instruments and nonempty bars are required")
    for i in instruments:
        if i.exchange.value not in {"SSE","SZSE"} or i.asset_class.value != "EQUITY":
            raise ValueError("Only Shanghai/Shenzhen cash equities are supported")
        if i.currency!='CNY' or i.tick_size!=D('.01') or i.lot_size<=0:
            raise ValueError('CNY equities with 0.01 price tick and positive lot size required')
    bar_days={b.end.astimezone(TZ).date() for b in bars}
    first,last=min(bar_days),max(bar_days)
    sessions = sorted(set(trading_days) if trading_days is not None else {d for s,d in rules if s in master and first<=d<=last} | bar_days)
    if not sessions or not bar_days.issubset(sessions):raise ValueError('Bars outside explicit trading calendar')
    for day in sessions:
        if day.weekday() >= 5: raise ValueError("Invalid exchange session date")
        for symbol, inst in master.items():
            if inst.list_date and day < inst.list_date: continue
            r = rules.get((symbol,day))
            if not isinstance(r,dict): raise ValueError(f"Missing daily rule: {symbol} {day}")
            if r.get('trade_status') not in {'OPEN','SUSPENDED','DAY_BLOCKED'}:
                raise ValueError(f"Invalid daily rule status: {symbol} {day}")
            for key in ('buy_min','buy_step','sell_min','sell_step','max_order'):
                if not isinstance(r.get(key),int) or r[key] <= 0:
                    raise ValueError(f"Invalid daily rule {key}: {symbol} {day}")
            if r.get('limit_state')=='BOUNDED':
                lo,hi=D(str(r.get('down_limit'))),D(str(r.get('up_limit')))
                if not lo.is_finite() or not hi.is_finite() or not 0 < lo < hi:
                    raise ValueError("Invalid price band rule")
            elif r.get('limit_state')!='UNBOUNDED' and r.get('trade_status')=='OPEN':
                raise ValueError("Missing executable price band rule")
    previous={}; actual=set()
    for b in sorted(bars,key=lambda x:(x.start,x.symbol)):
        if b.symbol not in master or b.start.tzinfo is None or b.end.tzinfo is None:
            raise ValueError("Known symbol and explicit timezone required")
        start,end=b.start.astimezone(TZ),b.end.astimezone(TZ)
        if start.date()!=end.date() or not at(start.date(),9,30)<=start<end<=at(start.date(),15):
            raise ValueError("Bar is outside the supported session")
        daily=start==at(start.date(),9,30) and end==at(start.date(),15)
        if not daily and not (end<=at(start.date(),11,30) or start>=at(start.date(),13)):
            raise ValueError("Intraday bar crosses the lunch break")
        if not daily and (end-start).total_seconds()%60:
            raise ValueError("Only daily or whole-minute bars are supported")
        if b.symbol in previous and start<previous[b.symbol]: raise ValueError("Overlapping bars")
        previous[b.symbol]=end; actual.add((b.symbol,end.date()))
        values=[D(str(v)) for v in (b.open,b.high,b.low,b.close)]
        if any(not v.is_finite() or v<=0 for v in values) or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]:
            raise ValueError("Invalid OHLC")
        tick=master[b.symbol].tick_size
        if any(v%tick for v in values): raise ValueError("OHLC violates price tick")
        if not isinstance(b.volume,int) or b.volume<0: raise ValueError("Invalid share volume")
        r=rules[(b.symbol,end.date())]
        if r['trade_status']=='OPEN' and r['limit_state']=='BOUNDED' and not D(str(r['down_limit']))<=values[2]<=values[1]<=D(str(r['up_limit'])):
            raise ValueError('OHLC violates daily price band')
    for (symbol,day),r in rules.items():
        if day in sessions and symbol in master and r['trade_status']=='OPEN' and (symbol,day) not in actual:
            raise ValueError(f"Missing market bar for open instrument: {symbol} {day}")
    for a in actions:
        if a.symbol not in master or a.cash_div < 0 or a.bonus_ratio < 0 or a.reserve_ratio < 0:
            raise ValueError("Invalid corporate action")
        if a.record_date and a.record_date>=a.ex_date: raise ValueError("Invalid corporate action record date")
        if a.cash_div and not a.record_date:raise ValueError('Cash dividend requires record date')
        if a.pay_date and a.pay_date<a.ex_date: raise ValueError("Payment predates ex-date")
        if a.share_listing_date and a.share_listing_date<a.ex_date: raise ValueError("Listing predates ex-date")
        if a.fractional_policy not in {'floor','reject','cash_in_lieu'}: raise ValueError("Invalid fractional policy")
    keys=[action_id(a) for a in actions]
    if len(keys)!=len(set(keys)): raise ValueError("Duplicate/conflicting corporate action")
    return master,sessions


class _ResearchStrategy(Strategy):
    def __init__(self, bars, rules, master, sessions, actions, signal, initial_cash, rate, native, fee_model, cash_module, liquidity):
        super().__init__(StrategyConfig())
        self.bars=bars;self.rules=rules;self.master=master;self.sessions=sessions;self.actions=actions
        self.signal=signal;self.rate=rate;self.native=native;self.fee_model=fee_model;self.cash_module=cash_module
        self.liquidity=liquidity
        self.buy_reservations={}
        self.liquidity.affordable=self._affordable_quantity
        self.ledger=GoldenLedger(initial_cash);self.gateway=AShareOrderGateway()
        self.history=defaultdict(list);self.pending=[];self.last_close={};self.current_day=None;self.closed=set()
        self.fills=[];self.rejections=[];self.nav=[];self.fee_values={};self.native_receivables={}
        self.lookup={(str(native[b.symbol].id),nanos(b.end)):b for b in bars}
        self.by_id={str(i.id):s for s,i in native.items()}
        self.by_day=defaultdict(list)
        for b in bars:self.by_day[b.end.astimezone(TZ).date()].append(b)
        for a in actions:
            self.ledger.register_corporate_action(a)
            if a.record_date and a.record_date<sessions[0]:
                self.ledger.dividend_entitlements[f"{a.symbol}_{a.ex_date}"]=D(0)
                self.cash_module.record_entitlement(a,D(0))

    def on_start(self):
        for b in self.bars:
            inst=self.native[b.symbol]
            daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
            spec=BarSpecification(1,BarAggregation.DAY,PriceType.LAST) if daily else BarSpecification(int((b.end-b.start).total_seconds()/60),BarAggregation.MINUTE,PriceType.LAST)
            bt=BarType(inst.id,spec,AggregationSource.EXTERNAL)
            if str(bt) not in getattr(self,'_subscribed',set()):
                self.subscribe_bars(bt)
                if not hasattr(self,'_subscribed'):self._subscribed=set()
                self._subscribed.add(str(bt))
        for inst in self.native.values():self.subscribe_quote_ticks(inst.id)
        days=set(self.sessions)|{a.ex_date for a in self.actions}|{a.pay_date for a in self.actions if a.pay_date}
        for day in sorted(days):
            if self.sessions[0]<=day<=self.sessions[-1]:
                self.clock.set_time_alert_ns(f"start_{day}",nanos(at(day,9)),callback=self._start_day)
        for day in self.sessions:
            self.clock.set_time_alert_ns(f"close_{day}",nanos(at(day,15)),callback=self._close_day)
        for n,b in enumerate(self.bars):
            if b.volume==0 or self.rules[(b.symbol,b.end.astimezone(TZ).date())]['trade_status']!='OPEN':
                self.clock.set_time_alert_ns(f"inactive_{n}",nanos(b.end),callback=lambda e,b=b:self._inactive_bar(b))

    def _native_quantity(self,symbol):
        return D(str(self.portfolio.net_position(self.native[symbol].id)))

    def _start_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.sessions:
            self.current_day=day;self.gateway.on_trade_day_start(str(day));self.ledger.on_trade_day_start(str(day))
            self.fee_model.set_trade_date(str(day))
        for a in self.actions:
            if a.ex_date==day:
                ratio=D(1)+a.bonus_ratio+a.reserve_ratio
                if ratio>1:
                    qty=self._native_quantity(a.symbol);fraction=qty*ratio-(qty*ratio).to_integral_value(rounding='ROUND_FLOOR')
                    if fraction and a.fractional_policy=='floor':
                        raise ValueError("Strict backtest requires reject or cash_in_lieu for fractional account entitlement")
                    for pos in self.cache.positions_open(instrument_id=self.native[a.symbol].id):
                        load_bridge().apply_split(pos,ratio,event.ts_event,self.native[a.symbol],fractional_policy='cash_in_lieu' if a.fractional_policy=='cash_in_lieu' else 'reject')
                    self.gateway.apply_stock_split(a.symbol,ratio,share_listing_date=a.share_listing_date,trade_date=day)
                    self.portfolio.initialize_positions()
                    if fraction:
                        px=a.fractional_cash_price
                        if px is None: px=self.last_close[a.symbol]/ratio
                        self.cash_module.adjust_cash(action_id(a)+'|fraction',fraction*px)
                qty=self.cash_module.entitlements.get(action_id(a))
                if a.cash_div and qty is None: raise ValueError("Missing native record snapshot")
                if a.cash_div:self.native_receivables[action_id(a)]=(qty*a.cash_div).quantize(D('.01'),rounding=ROUND_HALF_UP)
                if a.symbol in self.last_close:
                    adjusted=(self.last_close[a.symbol]-a.cash_div)/ratio
                    if adjusted<=0:raise ValueError('Invalid ex-date valuation reference')
                    self.last_close[a.symbol]=adjusted
        self.ledger.process_corporate_actions_for_date(str(day))
        for a in self.actions:
            if (a.pay_date or a.ex_date)==day and a.cash_div:
                self.cash_module.pay_dividend(a,str(day));self.native_receivables.pop(action_id(a),None)

    def _state(self):
        return ResearchState(self.ledger.cash,MappingProxyType(dict(self.ledger.positions)),
                             MappingProxyType({s:self.gateway.get_sellable_qty(s) for s in self.master}),
                             MappingProxyType({s:tuple(v) for s,v in self.history.items()}))

    def _signal(self,b,update_price=True):
        if update_price:self.last_close[b.symbol]=D(str(b.close))
        self.history[b.symbol].append(b)
        intents=self.signal(b,self._state()) or []
        for i in intents:
            if not isinstance(i,OrderIntent) or i.symbol not in self.master or i.side.upper() not in {'BUY','SELL'} or not isinstance(i.quantity,int) or i.quantity<=0:
                raise ValueError("Invalid strategy order intent")
            if i.limit_price is not None:
                px=D(str(i.limit_price))
                if not px.is_finite() or px<=0 or px%self.master[i.symbol].tick_size:
                    raise ValueError('Invalid strategy limit price or price tick')
            self.pending.append((nanos(b.end),i))

    def _inactive_bar(self,b):
        day=b.end.astimezone(TZ).date()
        reason='ZERO_VOLUME' if b.volume==0 and self.rules[(b.symbol,day)]['trade_status']=='OPEN' else self.rules[(b.symbol,day)]['trade_status']
        remaining=[]
        for created,intent in self.pending:
            if intent.symbol==b.symbol and created<=nanos(b.start):
                self.rejections.append({'symbol':b.symbol,'reason':reason,'time':nanos(b.end)})
            else:remaining.append((created,intent))
        self.pending=remaining
        if b not in self.history[b.symbol]:self._signal(b,update_price=self.rules[(b.symbol,day)]['trade_status']=='OPEN')

    def on_bar(self,bar):
        self._signal(self.lookup[(str(bar.bar_type.instrument_id),bar.ts_event)])

    def on_quote_tick(self,tick):
        symbol=self.by_id[str(tick.instrument_id)];day=datetime.fromtimestamp(tick.ts_event/1e9,TZ).date()
        previous_volume=next((b.volume for b in reversed(self.history[symbol]) if b.volume>0),0)
        available=int(D(previous_volume)*D(str(self.rate)))
        remaining=[]
        for created,intent in self.pending:
            if intent.symbol!=symbol or created>tick.ts_event:
                remaining.append((created,intent));continue
            if intent.quantity>available:
                self.rejections.append({'symbol':symbol,'reason':'PARTICIPATION_LIMIT','time':tick.ts_init});continue
            side=OrderSide.BUY if intent.side.upper()=='BUY' else OrderSide.SELL
            # IOC at the observed open avoids NT market-order empty-book fallback.
            args=dict(instrument_id=tick.instrument_id,order_side=side,quantity=Quantity.from_int(intent.quantity),time_in_force=TimeInForce.IOC if intent.limit_price is None else TimeInForce.DAY)
            price=tick.ask_price if side==OrderSide.BUY else tick.bid_price
            order=self.order_factory.limit(**args,price=price if intent.limit_price is None else Price.from_str(format(intent.limit_price,'.2f')))
            if side==OrderSide.BUY:
                cost=D(str(order.price))*intent.quantity
                cost+=calculate_expected_fee('BUY',D(intent.quantity),D(str(order.price)),trade_date=str(day))
                if cost>self.ledger.cash-sum((r['amount'] for r in self.buy_reservations.values()),D(0)):
                    self.rejections.append({'symbol':symbol,'reason':'INSUFFICIENT_CASH_WITH_FEES','time':tick.ts_init});continue
            r=self.gateway.validate_and_reserve(order,self.rules[(symbol,day)],{'close':tick.ask_price.as_double(),'volume':max(available,1),'execution_ts':tick.ts_init},tick.ask_price.as_double() if side==OrderSide.BUY else tick.bid_price.as_double())
            if r.is_valid:
                if side==OrderSide.BUY:self.buy_reservations[str(order.client_order_id)]={'amount':cost,'quantity':D(intent.quantity),'price':D(str(order.price))}
                self.submit_order(order);available-=intent.quantity
            else:self.rejections.append({'symbol':symbol,'reason':r.rejection_reason,'time':tick.ts_init})
        self.pending=remaining

    def _affordable_quantity(self,order,price,maximum):
        key=str(order.client_order_id);px=D(str(price))
        cash=self.ledger.cash-sum((r['amount'] for k,r in self.buy_reservations.items() if k!=key),D(0))
        before=self.fee_values.get(key,D(0));lo,hi=0,maximum
        while lo<hi:
            qty=(lo+hi+1)//2
            fee=calculate_expected_fee('BUY',D(qty),px,is_new_order=before==0,trade_date=str(self.current_day),cum_trade_val_before=before)
            if D(qty)*px+fee<=cash:lo=qty
            else:hi=qty-1
        return lo

    def on_order_filled(self,e):
        if str(e.trade_id) in self.gateway._processed_trade_ids:return
        self.liquidity.consume(e.instrument_id,e.last_qty)
        symbol=self.by_id[str(e.instrument_id)];day=self.gateway.on_order_filled(e)
        qty=D(str(e.last_qty));px=D(str(e.last_px));fee=D(str(e.commission.as_double()))
        key=str(e.client_order_id);before=self.fee_values.get(key,D(0))
        expected=calculate_expected_fee(e.order_side.name,qty,px,is_new_order=before==0,trade_date=day,cum_trade_val_before=before)
        if fee!=expected:raise RuntimeError(f"Native fee mismatch: {fee} != {expected}")
        self.fee_values[key]=before+qty*px
        self.ledger.apply_fill(Fill(fill_id=str(e.trade_id),symbol=symbol,side=LedgerSide(e.order_side.name),quantity=qty,price=px,fee=fee,timestamp=datetime.fromtimestamp(e.ts_event/1e9,TZ)),day)
        if key in self.buy_reservations:
            reserve=self.buy_reservations[key];reserve['quantity']-=qty
            if reserve['quantity']==0:self.buy_reservations.pop(key)
            else:
                reserve['amount']=reserve['quantity']*reserve['price']+calculate_expected_fee('BUY',reserve['quantity'],reserve['price'],is_new_order=False,trade_date=day,cum_trade_val_before=self.fee_values[key])
        self.fills.append({'symbol':symbol,'side':e.order_side.name,'quantity':str(qty),'price':str(px),'fee':str(fee),'time':e.ts_event,'order_id':key})

    def on_order_canceled(self,e):
        self.gateway.on_order_canceled(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
    def on_order_expired(self,e):
        self.gateway.on_order_expired(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
    def on_order_rejected(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_rejected(str(e.client_order_id));self.rejections.append({'symbol':self.by_id[str(e.instrument_id)],'reason':str(e.reason),'time':e.ts_event})
    def on_order_denied(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_denied(str(e.client_order_id));self.rejections.append({'symbol':self.by_id[str(e.instrument_id)],'reason':str(e.reason),'time':e.ts_event})

    def _close_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.closed:return
        for inst in self.native.values():self.cancel_all_orders(inst.id)
        self.gateway.on_session_close()
        for b in self.by_day[day]:
            if (b.volume==0 or self.rules[(b.symbol,day)]['trade_status']!='OPEN') and b not in self.history[b.symbol]:self._inactive_bar(b)
        prices={}
        for symbol,qty in self.ledger.positions.items():
            if qty:
                if symbol not in self.last_close:raise ValueError(f"Missing valuation price {symbol}")
                prices[symbol]=self.last_close[symbol]
        # Also snapshot known non-held symbols, needed for record-day and later split valuation.
        prices.update(self.last_close)
        nav=self.ledger.mark_to_market(str(day),prices)
        for a in self.actions:
            if a.record_date==day:self.cash_module.record_entitlement(a,self._native_quantity(a.symbol))
        native_cash=D(str(self.portfolio.account(SIM).balance_total(CNY).as_double()))
        native_positions={s:self._native_quantity(s) for s in self.master}
        positions_match=all(native_positions[s]==self.ledger.positions.get(s,D(0))==self.gateway.get_total_position(s) for s in self.master)
        cash_matches=native_cash==self.ledger.cash
        native_equity=native_cash+sum(q*prices[s] for s,q in native_positions.items() if q)+sum(self.native_receivables.values(),D(0))
        if not positions_match or not cash_matches or abs(native_equity-nav.total_equity)>D('.000001'):
            raise RuntimeError(f"Daily reconciliation failed on {day}: cash={cash_matches}, positions={positions_match}")
        row=nav.model_dump(mode='json');row.update(native_cash=str(native_cash),native_equity=str(native_equity),cash_matches=cash_matches,positions_match=positions_match)
        self.nav.append(row);self.closed.add(day)


def run_backtest(instruments, bars, rules, signal, *, corporate_actions=(), initial_cash=D(100000), participation_rate=.01, trading_days=None):
    """Run one isolated, shared-cash portfolio. Rules keyed by (symbol, local date)."""
    instruments=list(instruments);bars=list(bars);actions=list(corporate_actions);initial_cash=D(str(initial_cash))
    master,sessions=_validate(instruments,bars,rules,actions,initial_cash,participation_rate,trading_days)
    eng=BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId('RESEARCH-001'),logging=LoggingConfig(log_level='ERROR')))
    cash_module=AShareAccountModule();fee=AShareFeeModel(scope_id='isolated_run');liquidity=BarLiquidity()
    try:
        eng.add_venue(venue=SIM,oms_type=OmsType.NETTING,account_type=AccountType.CASH,base_currency=CNY,starting_balances=[Money(float(initial_cash),CNY)],fee_model=fee,fill_model=liquidity,liquidity_consumption=True,modules=[cash_module,LiquidityModule(liquidity)])
        native={}
        for symbol,i in master.items():
            native[symbol]=Equity(instrument_id=InstrumentId(Symbol(symbol),SIM),raw_symbol=Symbol(symbol.split('.')[0]),currency=CNY,price_precision=2,price_increment=Price.from_str('.01'),lot_size=Quantity.from_int(i.lot_size),ts_event=nanos(at(sessions[0],0)),ts_init=nanos(at(sessions[0],0)))
            eng.add_instrument(native[symbol])
        data=[]
        for b in sorted(bars,key=lambda x:(x.start,x.symbol)):
            if b.volume==0 or rules[(b.symbol,b.end.astimezone(TZ).date())]['trade_status']!='OPEN':continue
            inst=native[b.symbol];start,end=nanos(b.start),nanos(b.end)
            cap=int(D(b.volume)*D(str(participation_rate)))
            for ts in (start+1,end):liquidity.events[(str(inst.id),ts)]=((b.symbol,end),cap)
            p=Price.from_str(format(b.open,'.2f'))
            data.append(QuoteTick(inst.id,p,p,Quantity.from_int(cap),Quantity.from_int(cap),start,start+1))
            daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
            spec=BarSpecification(1,BarAggregation.DAY,PriceType.LAST) if daily else BarSpecification(int((b.end-b.start).total_seconds()/60),BarAggregation.MINUTE,PriceType.LAST)
            data.append(Bar(BarType(inst.id,spec,AggregationSource.EXTERNAL),*[Price.from_str(format(v,'.2f')) for v in (b.open,b.high,b.low,b.close)],Quantity.from_int(int(D(b.volume)*D(str(participation_rate)))),end,end))
        if data:eng.add_data(data)
        strategy=_ResearchStrategy(bars,rules,master,sessions,actions,signal,initial_cash,participation_rate,native,fee,cash_module,liquidity)
        eng.add_strategy(strategy)
        eng.run(start=at(sessions[0],0),end=at(sessions[-1],15))
        if len(strategy.nav)!=len(sessions):raise RuntimeError("Missing daily reconciliation")
        native_cash=D(str(eng.portfolio.account(SIM).balance_total(CNY).as_double()))
        return BacktestResult(native_cash,strategy.ledger.cash,{s:strategy.ledger.positions.get(s,D(0)) for s in master},strategy.fills,strategy.rejections,strategy.nav,list(cash_module.journal),[{'id':str(o.client_order_id),'status':o.status.name,'quantity':str(o.quantity),'filled_quantity':str(o.filled_qty)} for o in eng.cache.orders()],[{'symbol':i.symbol,'side':i.side,'quantity':i.quantity,'reason':'END_OF_DATA'} for _,i in strategy.pending])
    finally:eng.dispose()
