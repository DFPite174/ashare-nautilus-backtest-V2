"""Regressions for the five independently reproduced acceptance failures."""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import subprocess
import sys
from pathlib import Path

import pytest
from contracts import CorporateAction, Instrument, Venue
from engine.backtest import OrderIntent, run_backtest
from test_regular_backtest import fixture


@pytest.mark.parametrize('dates',[
    {'list_date':date(2025,5,9)},
    {'delist_date':date(2025,5,5)},
])
def test_open_rules_outside_security_lifetime_are_rejected(dates):
    bars,rules=fixture()
    inst=Instrument(symbol=bars[0].symbol,exchange=Venue.SSE,**dates)
    with pytest.raises(ValueError,match='lifecycle'):
        run_backtest([inst],bars,rules,lambda b,s:[OrderIntent(b.symbol,'BUY',100)])


@pytest.mark.parametrize('dates',[
    {'list_date':date(2025,5,9)},
    {'delist_date':date(2025,5,5)},
])
def test_blocked_rule_cannot_hide_bar_outside_security_lifetime(dates):
    bars,rules=fixture()
    for r in rules.values():r['trade_status']='DAY_BLOCKED'
    inst=Instrument(symbol=bars[0].symbol,exchange=Venue.SSE,**dates)
    with pytest.raises(ValueError,match='lifecycle'):
        run_backtest([inst],bars,rules,lambda b,s:[])


def test_list_and_delist_boundaries_remain_usable():
    bars,rules=fixture()
    inst=Instrument(symbol=bars[0].symbol,exchange=Venue.SSE,list_date=bars[0].start.date(),delist_date=bars[-1].end.date())
    def signal(b,s):
        if b==bars[0]:return [OrderIntent(b.symbol,'BUY',100)]
        if b==bars[1]:return [OrderIntent(b.symbol,'SELL',100)]
        return []
    result=run_backtest([inst],bars,rules,signal,participation_rate=1)
    assert [f['side'] for f in result.fills]==['BUY','SELL']


@pytest.mark.parametrize('boundary',['list_date','delist_date'])
def test_inactive_security_needs_no_fabricated_bars_or_open_rules(boundary):
    bars,rules=fixture()
    other,other_rules=fixture(symbol='000001.SZ')
    kept=bars[-1] if boundary=='list_date' else bars[0]
    inst=Instrument(symbol=kept.symbol,exchange=Venue.SSE,**{boundary:kept.end.date()})
    valid_rules={(kept.symbol,kept.end.date()):rules[(kept.symbol,kept.end.date())],**other_rules}
    result=run_backtest([inst,Instrument(symbol='000001.SZ',exchange=Venue.SZSE)],[kept]+other,valid_rules,lambda b,s:[],trading_days=[b.end.date() for b in other])
    assert len(result.daily_nav)==3 and not result.fills


def test_inverted_lifecycle_is_rejected():
    bars,rules=fixture()
    inst=Instrument(symbol=bars[0].symbol,exchange=Venue.SSE,list_date=date(2025,5,9),delist_date=date(2025,5,5))
    with pytest.raises(ValueError,match='lifecycle'):
        run_backtest([inst],bars,rules,lambda b,s:[])


@pytest.mark.parametrize('field',['rights_ratio','cash_dividend'])
def test_unknown_corporate_action_fields_are_rejected(field):
    with pytest.raises(ValueError,match=field):
        CorporateAction.model_validate({'symbol':'600519.SH','ex_date':'2025-05-08',field:'0.2'})


@pytest.mark.parametrize('field',['start','end'])
def test_mixed_naive_and_aware_bars_have_explicit_validation_error(field):
    bars,rules=fixture()
    bars[0]=replace(bars[0],**{field:getattr(bars[0],field).replace(tzinfo=None)})
    with pytest.raises(ValueError,match='timezone'):
        run_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],bars,rules,lambda b,s:[])


def test_failed_signal_stops_before_dispose_and_preserves_exception():
    # NT logs through native streams; use a child process to observe teardown.
    code="""
from decimal import Decimal as D
from contracts import Instrument,Venue
from engine.backtest import run_backtest,OrderIntent
from test_regular_backtest import fixture
bars,rules=fixture()
inst=Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)
calls=[]
def signal(b,s):
    calls.append(b.end)
    return [OrderIntent(b.symbol,'BUY',100,D('NaN'))]
try:
    run_backtest([inst],bars,rules,signal)
except ValueError as error:
    assert 'limit price' in str(error),str(error)
else:
    raise AssertionError('Expected strategy validation failure')
assert len(calls)==1,'Failed run must not flush future signal callbacks'
result=run_backtest([inst],bars,rules,lambda b,s:[])
assert result.native_cash==D(100000) and not result.fills
print('RECOVERED')
"""
    root=Path(__file__).resolve().parents[1]
    bootstrap="import sys;sys.path[:0]="+repr([str(root),str(root/'tests')])+";"+code
    child=subprocess.run([sys.executable,'-B','-X','utf8','-c',bootstrap],capture_output=True,text=True,encoding='utf-8',timeout=30)
    output=child.stdout+child.stderr
    assert child.returncode==0,output
    assert 'RECOVERED' in output
    assert 'InvalidStateTrigger' not in output,output
