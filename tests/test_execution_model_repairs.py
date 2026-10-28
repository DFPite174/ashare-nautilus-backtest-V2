"""Native execution regressions for shared budgets, limit prices and suspensions."""
from dataclasses import replace
from decimal import Decimal as D
from datetime import timedelta
import pytest
from contracts import Instrument,Venue
from engine.backtest import OrderIntent,run_backtest
from test_regular_backtest import fixture

INST=Instrument(symbol='600000.SH',exchange=Venue.SSE)

def market(days=4):
    bars,rules=fixture(days=days,symbol=INST.symbol)
    bars=[replace(b,open=D(10),high=D('10.6'),low=D('9.8'),close=D(10)) for b in bars]
    for r in rules.values():r.update(up_limit=11.,down_limit=9.)
    return bars,rules

@pytest.mark.parametrize('cap',[0,1,50,150,199,200])
@pytest.mark.parametrize('limit',[D('10.00'),D('10.20')])
def test_competing_buys_never_exceed_budget_or_limit(cap,limit):
    bars,rules=market();bars[1]=replace(bars[1],volume=cap)
    def signal(b,s):return [OrderIntent(b.symbol,'BUY',100,limit)]*2 if b==bars[0] else []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    assert sum(D(x['quantity']) for x in r.fills)==min(cap,200)
    assert all(D(x['price'])<=limit for x in r.fills)
    assert all(x['cash_matches'] and x['positions_match'] for x in r.daily_nav)
    if 0<cap<200:assert any(x['status']=='CANCELED' for x in r.orders)

@pytest.mark.parametrize('cap',[0,1,50,150,199,200])
def test_competing_sells_share_budget(cap):
    bars,rules=market();bars[2]=replace(bars[2],volume=cap)
    def signal(b,s):
        if b==bars[0]:return [OrderIntent(b.symbol,'BUY',200)]
        if b==bars[1]:return [OrderIntent(b.symbol,'SELL',100,D('9.80'))]*2
        return []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    sold=[x for x in r.fills if x['side']=='SELL']
    assert sum((D(x['quantity']) for x in sold),D(0))==min(cap,200)
    assert all(D(x['price'])>=D('9.80') for x in sold)
    assert r.positions[INST.symbol]==200-min(cap,200)

def test_resting_buy_fills_at_limit_not_bar_low():
    bars,rules=market()
    r=run_backtest([INST],bars,rules,lambda b,s:[OrderIntent(b.symbol,'BUY',100,D('9.90'))] if b==bars[0] else [],participation_rate=1)
    assert [D(x['price']) for x in r.fills]==[D('9.90')]
    assert 'resting limits at limit' in r.execution_model

def test_resting_sell_fills_at_limit_not_bar_high():
    bars,rules=market()
    def signal(b,s):
        if b==bars[0]:return [OrderIntent(b.symbol,'BUY',100)]
        if b==bars[1]:return [OrderIntent(b.symbol,'SELL',100,D('10.55'))]
        return []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    assert [D(x['price']) for x in r.fills]==[D(10),D('10.55')]

def test_halt_rejects_pending_intent_with_or_without_placeholder():
    bars,rules=market(days=5);calendar=[b.end.date() for b in bars]
    for b in bars[1:3]:rules[(b.symbol,b.end.date())]['trade_status']='SUSPENDED'
    def signal(b,s):return [OrderIntent(b.symbol,'BUY',100,D('10.20'))] if b==bars[0] else []
    missing=run_backtest([INST],bars[:1]+bars[3:],rules,signal,participation_rate=1,trading_days=calendar)
    zero=[replace(b,volume=0) if i in [1,2] else b for i,b in enumerate(bars)]
    placeholder=run_backtest([INST],zero,rules,signal,participation_rate=1,trading_days=calendar)
    assert not missing.fills and not placeholder.fills
    assert missing.rejections==placeholder.rejections
    assert [r['reason'] for r in missing.rejections]==['SUSPENDED']


def test_open_and_intrabar_orders_share_one_budget():
    bars,rules=market();bars[1]=replace(bars[1],volume=150)
    def signal(b,s):
        return [OrderIntent(b.symbol,'BUY',100,D('10.20')),OrderIntent(b.symbol,'BUY',100,D('9.90'))] if b==bars[0] else []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    assert [(D(f['quantity']),D(f['price'])) for f in r.fills]==[(D(100),D(10)),(D(50),D('9.90'))]


def test_partial_day_order_can_fill_on_adjacent_minute_without_budget_leak():
    bars,rules=market(days=1);first=replace(bars[0],end=bars[0].start+timedelta(minutes=1))
    minutes=[first]+[replace(first,start=first.start+timedelta(minutes=i),end=first.end+timedelta(minutes=i),volume=50) for i in [1,2]]
    def signal(b,s):return [OrderIntent(b.symbol,'BUY',100,D('10.20'))] if b==first else []
    r=run_backtest([INST],minutes,rules,signal,participation_rate=1)
    assert [D(f['quantity']) for f in r.fills]==[D(50),D(50)]
    assert len(r.orders)==1 and r.orders[0]['status']=='FILLED'
