from decimal import Decimal
import importlib.util

def test_local_daily_csv_has_explicit_sessions_and_no_adjustment(tmp_path):
    assert importlib.util.find_spec('engine.data') is not None
    from engine.data import load_bars_csv
    p=tmp_path/'bars.csv'
    p.write_text('ts_code,trade_date,open,high,low,close,volume\n600519.SH,20250506,10,11,9,10.5,10000\n',encoding='utf-8')
    bars=load_bars_csv(p,frequency='DAY')
    assert bars[0].open==Decimal(10) and bars[0].volume==10000
    assert bars[0].start.hour==9 and bars[0].end.hour==15 and bars[0].start.tzinfo is not None

def test_example_csv_rule_contract_and_native_run():
    from pathlib import Path
    from engine.data import load_bars_csv,load_rules_csv
    from engine import run_backtest,OrderIntent
    from contracts import Instrument,Venue
    root=Path(__file__).resolve().parents[1]/'examples/data'
    for frequency,name in [('DAY','bars_daily.csv'),('MINUTE','bars_minute.csv')]:
        bars=load_bars_csv(root/name,frequency=frequency)
        rules=load_rules_csv(root/'rules.csv')
        result=run_backtest([Instrument(symbol='600519.SH',exchange=Venue.SSE)],bars,rules,
            lambda b,s: [OrderIntent(b.symbol,'BUY',100)] if b.end==bars[0].end else [],participation_rate=.1)
        assert len(result.fills)==1 and result.native_cash==result.ledger_cash

def test_tushare_lots_require_explicit_multiplier(tmp_path):
    from engine.data import load_bars_csv
    p=tmp_path/'lots.csv'
    p.write_text('ts_code,trade_date,open,high,low,close,vol\n600519.SH,20250506,10,11,9,10,123.45\n',encoding='utf-8')
    bars=load_bars_csv(p,volume_multiplier=100)
    assert bars[0].volume==12345

def test_minute_csv_without_timezone_rejected(tmp_path):
    import pytest
    from engine.data import load_bars_csv
    p=tmp_path/'minute.csv'
    p.write_text('symbol,start,end,open,high,low,close,volume\n600519.SH,2025-05-06T09:30,2025-05-06T09:31,10,11,9,10,100\n',encoding='utf-8')
    with pytest.raises(ValueError,match='timezone'):load_bars_csv(p,frequency='MINUTE')
