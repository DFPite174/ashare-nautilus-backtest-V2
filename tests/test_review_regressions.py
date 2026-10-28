from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import pytest
from contracts import Instrument, Venue, CorporateAction
from engine.backtest import run_backtest, OrderIntent
from test_regular_backtest import fixture

INST = Instrument(symbol='600519.SH', exchange=Venue.SSE)

def buy_first(bars):
    return lambda b,s: [OrderIntent(b.symbol,'BUY',100)] if b.symbol==INST.symbol and b.end==bars[0].end else []

def test_missing_whole_open_session_rejected():
    bars,rules=fixture()
    with pytest.raises(ValueError,match='Missing market bar'):
        run_backtest([INST],[bars[0],bars[2]],rules,buy_first(bars))

def test_out_of_band_ohlc_rejected():
    bars,rules=fixture()
    bars[1]=replace(bars[1],high=D(20),close=D(20))
    with pytest.raises(ValueError,match='price band'):
        run_backtest([INST],bars,rules,buy_first(bars))

def test_actual_executing_bar_volume_caps_fills():
    bars,rules=fixture()
    bars[1]=replace(bars[1],volume=1)
    r=run_backtest([INST],bars,rules,buy_first(bars),participation_rate=1)
    assert sum(D(x['quantity']) for x in r.fills)<=1

def test_competing_orders_share_open_and_ohlc_volume():
    bars,rules=fixture()
    bars[1]=replace(bars[1],volume=150)
    def signal(b,s):
        if b.end==bars[0].end:return [OrderIntent(b.symbol,'BUY',100,D('10.5')),OrderIntent(b.symbol,'BUY',100,D('10.5'))]
        return []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    assert sum(D(x['quantity']) for x in r.fills)==150
    assert len(r.fills)>=2
    assert all(x['cash_matches'] and x['positions_match'] for x in r.daily_nav)

def test_resting_limit_fills_from_native_intrabar_low():
    bars,rules=fixture()
    bars[1]=replace(bars[1],low=D('10.1'))
    def signal(b,s):
        return [OrderIntent(b.symbol,'BUY',100,D('10.2'))] if b.end==bars[0].end else []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1)
    assert sum(D(x['quantity']) for x in r.fills)==100
    assert all(D(x['price'])<=D('10.2') for x in r.fills)

def test_split_on_suspended_day_preserves_value():
    bars,rules=fixture()
    other,extra=fixture(symbol='000001.SZ')
    rules.update(extra)
    rules[(INST.symbol,bars[2].end.date())]['trade_status']='SUSPENDED'
    ca=CorporateAction(symbol=INST.symbol,record_date=bars[1].end.date(),ex_date=bars[2].end.date(),bonus_ratio=D(1))
    r=run_backtest([INST,Instrument(symbol='000001.SZ',exchange=Venue.SZSE)],bars[:2]+other,rules,buy_first(bars),corporate_actions=[ca],participation_rate=1)
    assert D(r.daily_nav[2]['total_equity'])==D(r.daily_nav[1]['total_equity'])
    stale=replace(bars[2],open=bars[1].close,high=bars[1].close,low=bars[1].close,close=bars[1].close,volume=0)
    with_bar=run_backtest([INST,Instrument(symbol='000001.SZ',exchange=Venue.SZSE)],bars[:2]+[stale]+other,rules,buy_first(bars),corporate_actions=[ca],participation_rate=1)
    assert with_bar.daily_nav[-1]['total_equity']==r.daily_nav[-1]['total_equity']

@pytest.mark.parametrize('price',[D('10.005'),D('NaN'),D(-1)])
def test_invalid_intent_price_is_not_silently_rounded(price):
    bars,rules=fixture()
    with pytest.raises(ValueError,match='limit price'):
        run_backtest([INST],bars,rules,lambda b,s:[OrderIntent(b.symbol,'BUY',100,price)],participation_rate=1)

def test_odd_share_dividend_half_cent_rounding():
    bars,rules=fixture(days=5)
    actions=[CorporateAction(symbol=INST.symbol,record_date=bars[1].end.date(),ex_date=bars[2].end.date(),bonus_ratio=D('.01')),
             CorporateAction(symbol=INST.symbol,record_date=bars[3].end.date(),ex_date=bars[4].end.date(),cash_div=D('.005'))]
    r=run_backtest([INST],bars,rules,buy_first(bars),corporate_actions=actions,participation_rate=1)
    assert D(r.account_journal[-1]['amount'])==D('.51')

def test_cash_dividend_requires_record_date_before_run():
    bars,rules=fixture()
    ca=CorporateAction(symbol=INST.symbol,ex_date=bars[-1].end.date(),cash_div=D('.1'))
    with pytest.raises(ValueError,match='record date'):
        run_backtest([INST],bars,rules,buy_first(bars),corporate_actions=[ca])

def test_equivalent_timestamp_timezones_preserve_session():
    from datetime import timezone,timedelta
    bars,rules=fixture()
    alternate=[replace(b,start=b.start.astimezone(timezone(timedelta(hours=-8))),end=b.end.astimezone(timezone(timedelta(hours=-8)))) for b in bars]
    r=run_backtest([INST],alternate,rules,buy_first(alternate),participation_rate=1)
    assert len(r.daily_nav)==3 and len(r.fills)==1

def test_explicit_calendar_detects_missing_rules_and_bars():
    bars,rules=fixture()
    rules.pop((INST.symbol,bars[1].end.date()))
    with pytest.raises(ValueError,match='Missing daily rule'):
        run_backtest([INST],[bars[0],bars[2]],rules,buy_first(bars),trading_days=[b.end.date() for b in bars])

@pytest.mark.parametrize('cash,expected',[(D(1050),0),(D('1055.01'),100)])
def test_buy_cash_budget_includes_fees_without_stopping(cash,expected):
    bars,rules=fixture()
    r=run_backtest([INST],bars,rules,buy_first(bars),participation_rate=1,initial_cash=cash)
    assert sum(D(x['quantity']) for x in r.fills)==expected
    assert r.native_cash==r.ledger_cash>=0 and len(r.daily_nav)==3
    if not expected:assert any(x['reason']=='INSUFFICIENT_CASH_WITH_FEES' for x in r.rejections)

def test_competing_resting_buys_reserve_cash_with_fees():
    bars,rules=fixture()
    bars[1]=replace(bars[1],low=D('10.4'))
    def signal(b,s):
        return [OrderIntent(b.symbol,'BUY',100,D('10.4')),OrderIntent(b.symbol,'BUY',100,D('10.4'))] if b.end==bars[0].end else []
    r=run_backtest([INST],bars,rules,signal,participation_rate=1,initial_cash=D(2085))
    assert sum(D(x['quantity']) for x in r.fills)==100
    assert r.native_cash==r.ledger_cash>=0
    assert any(x['reason']=='INSUFFICIENT_CASH_WITH_FEES' for x in r.rejections)
