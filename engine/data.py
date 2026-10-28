"""Explicit local CSV contracts. Input prices must be raw; volume is always shares."""
import csv
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from engine.backtest import BacktestBar

TZ=ZoneInfo('Asia/Shanghai')

def load_bars_csv(path, *, frequency='DAY', volume_multiplier=1):
    """Daily: symbol/ts_code, trade_date, OHLC, volume. Minute: explicit start/end.

    Tushare daily `vol` is lots, so explicitly pass volume_multiplier=100 for that
    field. Do not guess units or the minute timestamp convention from a vendor.
    """
    if frequency not in {'DAY','MINUTE'} or volume_multiplier<=0:
        raise ValueError('Invalid frequency or explicit volume multiplier')
    result=[]
    with open(path,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            symbol=row.get('symbol') or row.get('ts_code')
            if not symbol:raise ValueError('Missing symbol')
            if frequency=='DAY':
                value=row['trade_date'].replace('-','')
                d=datetime.strptime(value,'%Y%m%d').replace(tzinfo=TZ)
                start=d.replace(hour=9,minute=30);end=d.replace(hour=15)
            else:
                start=datetime.fromisoformat(row['start']);end=datetime.fromisoformat(row['end'])
                if start.tzinfo is None or end.tzinfo is None:raise ValueError('Minute CSV start/end require timezone')
            volume=Decimal(row.get('volume') or row['vol'])*Decimal(str(volume_multiplier))
            if not volume.is_finite() or volume<0 or volume!=volume.to_integral_value():raise ValueError('Volume must be integer shares')
            result.append(BacktestBar(symbol,start,end,*[Decimal(row[k]) for k in ('open','high','low','close')],int(volume)))
    return result

def load_rules_csv(path):
    """Each (symbol,date) requires status, limits and board-specific quantity rules."""
    result={}
    with open(path,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            symbol=row.get('symbol') or row.get('ts_code')
            day=datetime.strptime(row['trade_date'].replace('-',''),'%Y%m%d').date()
            key=(symbol,day)
            if not symbol or key in result:raise ValueError('Missing symbol or duplicate daily rule')
            r={k:row[k] for k in ('trade_status','limit_state')}
            for k in ('up_limit','down_limit','prev_close','issue_price'):
                if row.get(k):r[k]=float(row[k])
            for k in ('buy_min','buy_step','sell_min','sell_step','max_order'):r[k]=int(row[k])
            result[key]=r
    return result
