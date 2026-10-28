"""Black-box acceptance: real NT orders, next-bar execution and account parity."""
from datetime import datetime, date, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest
import engine
from contracts import Instrument, Venue, CorporateAction

TZ = ZoneInfo("Asia/Shanghai")
def api():
    assert hasattr(engine, "run_backtest"), "a reusable backtest entry point is required"
    from engine.backtest import BacktestBar, OrderIntent, run_backtest
    return BacktestBar, OrderIntent, run_backtest

def fixture(frequency="DAY", days=3, symbol="600519.SH"):
    Bar, _, _ = api()
    bars=[]
    day=date(2025,5,6)
    for n in range(days):
        while day.weekday()>=5:day+=timedelta(days=1)
        start=datetime.combine(day,datetime.min.time(),TZ).replace(hour=9,minute=30)
        end=start.replace(hour=15,minute=0) if frequency=="DAY" else start+timedelta(minutes=1)
        px=D(10)+D('.5')*n
        bars.append(Bar(symbol,start,end,px,px+D('.1'),px-D('.1'),px,10000))
        day+=timedelta(days=1)
    rules={}
    prev=D(10)
    for b in bars:
        rules[(symbol,b.end.date())]=dict(trade_status="OPEN",limit_state="BOUNDED",up_limit=float((prev*D('1.1')).quantize(D('.01'))),down_limit=float((prev*D('.9')).quantize(D('.01'))),buy_min=100,buy_step=100,sell_min=100,sell_step=100,max_order=1000000)
        prev=b.close
    return bars,rules

@pytest.mark.parametrize("frequency", ["DAY","MINUTE"])
def test_regular_buy_sell_next_bar_and_independent_daily_parity(frequency):
    _, Intent, run = api();bars,rules=fixture(frequency)
    def signal(bar,state):
        assert max(x.end for x in state.history[bar.symbol]) <= bar.end
        if bar.end.date()==date(2025,5,6):return [Intent(bar.symbol,"BUY",100)]
        if bar.end.date()==date(2025,5,7):return [Intent(bar.symbol,"SELL",100)]
        return []
    r=run([Instrument(symbol="600519.SH",exchange=Venue.SSE)],bars,rules,signal,initial_cash=D(100000),participation_rate=1.)
    assert [x['side'] for x in r.fills]==["BUY","SELL"]
    assert [D(x['price']) for x in r.fills]==[D('10.5'),D(11)]
    assert r.native_cash==r.ledger_cash
    assert r.positions["600519.SH"]==0
    assert len(r.daily_nav)==3 and all(x['cash_matches'] and x['positions_match'] for x in r.daily_nav)

def test_minute_same_day_t1_and_competing_sell_reservations():
    Bar, Intent, run=api();bars,rules=fixture("MINUTE",days=2)
    first=bars[0]
    bars.insert(1,Bar(first.symbol,first.end,first.end+timedelta(minutes=1),D(10),D(11),D(9),D(10),10000))
    bars.insert(2,Bar(first.symbol,first.end+timedelta(minutes=1),first.end+timedelta(minutes=2),D(10),D(11),D(9),D(10),10000))
    bars.append(Bar(first.symbol,bars[-1].end,bars[-1].end+timedelta(minutes=1),D(11),D(11),D(10),D(11),10000))
    def signal(bar,state):
        if bar.end.date()==date(2025,5,6) and bar.end.minute==31:return [Intent(bar.symbol,"BUY",100)]
        return [Intent(bar.symbol,"SELL",100),Intent(bar.symbol,"SELL",100)]
    r=run([Instrument(symbol=first.symbol,exchange=Venue.SSE)],bars,rules,signal,participation_rate=1.)
    assert len([x for x in r.fills if x['side']=="SELL"])<=1
    assert any('T1_' in x['reason'] for x in r.rejections)
    assert all(q>=0 for q in r.positions.values())

def test_dividend_cash_reaches_native_and_can_fund_reinvestment():
    _,Intent,run=api();bars,rules=fixture(days=4)
    ca=CorporateAction(symbol="600519.SH",record_date=date(2025,5,7),ex_date=date(2025,5,8),pay_date=date(2025,5,9),cash_div=D(2))
    def signal(bar,state):
        if bar.end.date() in {date(2025,5,6),date(2025,5,8)}:return [Intent(bar.symbol,"BUY",100)]
        return []
    r=run([Instrument(symbol="600519.SH",exchange=Venue.SSE)],bars,rules,signal,corporate_actions=[ca],initial_cash=D(2200),participation_rate=1.)
    assert len(r.fills)==2 and D(r.fills[-1]['price'])==D('11.5')
    assert r.native_cash==r.ledger_cash
    assert sum(D(x['amount']) for x in r.account_journal if 'dividend' in x['event_id'])==D(200)
    assert r.daily_nav[2]['receivable_dividends']=="200.00"

def test_delayed_listing_freezes_bonus_then_native_full_sale():
    Bar,Intent,run=api();bars,rules=fixture(days=5)
    adjusted=[]
    for b in bars:
        if b.end.date()>=date(2025,5,8):
            adjusted.append(Bar(b.symbol,b.start,b.end,b.open/2,b.high/2,b.low/2,b.close/2,20000))
            rules[(b.symbol,b.end.date())]['up_limit']=round(rules[(b.symbol,b.end.date())]['up_limit']/2,2)
            rules[(b.symbol,b.end.date())]['down_limit']=round(rules[(b.symbol,b.end.date())]['down_limit']/2,2)
        else:adjusted.append(b)
    ca=CorporateAction(symbol="600519.SH",record_date=date(2025,5,7),ex_date=date(2025,5,8),share_listing_date=date(2025,5,12),bonus_ratio=D(1))
    def signal(b,s):
        if b.end.date()==date(2025,5,6):return [Intent(b.symbol,"BUY",100)]
        if b.end.date() in {date(2025,5,8),date(2025,5,9)}:return [Intent(b.symbol,"SELL",200)]
        return []
    r=run([Instrument(symbol="600519.SH",exchange=Venue.SSE)],adjusted,rules,signal,corporate_actions=[ca],participation_rate=1.)
    assert any('T1_' in x['reason'] for x in r.rejections)
    assert [x['side'] for x in r.fills]==['BUY','SELL']
    assert D(r.fills[-1]['quantity'])==200 and r.positions['600519.SH']==0

@pytest.mark.parametrize('blocked', ['SUSPENDED','ZERO_VOLUME','LIMIT_UP'])
def test_blocked_open_cannot_fill(blocked):
    Bar,Intent,run=api();bars,rules=fixture()
    b=bars[1]
    if blocked=='SUSPENDED':
        rules[(b.symbol,b.end.date())]['trade_status']='SUSPENDED'
        bars[1]=Bar(b.symbol,b.start,b.end,D(10),D(10),D(10),D(10),0)
    elif blocked=='ZERO_VOLUME':bars[1]=Bar(b.symbol,b.start,b.end,b.open,b.high,b.low,b.close,0)
    else:
        px=D(str(rules[(b.symbol,b.end.date())]['up_limit']))
        bars[1]=Bar(b.symbol,b.start,b.end,px,px,px,px,10000)
    def signal(b,s):return [Intent(b.symbol,'BUY',100)] if b.end.date()==date(2025,5,6) else []
    r=run([Instrument(symbol=b.symbol,exchange=Venue.SSE)],bars,rules,signal,participation_rate=1.)
    assert r.fills==[] and r.positions[b.symbol]==0

def test_shared_cash_for_shanghai_and_shenzhen():
    _,Intent,run=api();a,ra=fixture(symbol="600519.SH");b,rb=fixture(symbol="000001.SZ")
    def signal(bar,state):
        if bar.end.date()==date(2025,5,6):return [Intent(bar.symbol,"BUY",100)]
        return []
    r=run([Instrument(symbol="600519.SH",exchange=Venue.SSE),Instrument(symbol="000001.SZ",exchange=Venue.SZSE)],a+b,ra|rb,signal,initial_cash=D(100000),participation_rate=1.)
    assert len(r.fills)==2
    assert r.native_cash==r.ledger_cash < D(100000)
    assert len(r.daily_nav)==3

def test_missing_rule_and_invalid_ohlc_fail_before_running():
    Bar,_,run=api();bars,rules=fixture()
    inst=[Instrument(symbol="600519.SH",exchange=Venue.SSE)]
    with pytest.raises(ValueError,match="rule"):
        run(inst,bars,{},lambda b,s:[])
    bad=Bar(bars[0].symbol,bars[0].start,bars[0].end,D(10),D(9),D(8),D(10),100)
    with pytest.raises(ValueError,match="OHLC"):
        run(inst,[bad],rules,lambda b,s:[])
