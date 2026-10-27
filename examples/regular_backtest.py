"""Local CSV smoke strategy; synthetic example returns are not investment evidence."""
import argparse
import json
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contracts import Instrument,Venue
from engine import OrderIntent,run_backtest
from engine.data import load_bars_csv,load_rules_csv


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bars',type=Path,required=True)
    p.add_argument('--rules',type=Path,required=True)
    p.add_argument('--frequency',choices=['DAY','MINUTE'],default='DAY')
    p.add_argument('--volume-multiplier',type=int,default=1)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    bars=load_bars_csv(args.bars,frequency=args.frequency,volume_multiplier=args.volume_multiplier)
    rules=load_rules_csv(args.rules)
    symbols=sorted({b.symbol for b in bars})
    instruments=[]
    for s in symbols:
        if not s.endswith(('.SH','.SZ')):raise ValueError('Use Shanghai .SH or Shenzhen .SZ symbols')
        instruments.append(Instrument(symbol=s,exchange=Venue.SSE if s.endswith('.SH') else Venue.SZSE))
    days=sorted({d for s,d in rules})
    def signal(b,state):
        from engine.backtest import TZ
        day=b.end.astimezone(TZ).date()
        if day==days[0]:return [OrderIntent(b.symbol,'BUY',100)]
        if len(days)>1 and day==days[1]:return [OrderIntent(b.symbol,'SELL',100)]
        return []
    r=run_backtest(instruments,bars,rules,signal,initial_cash=Decimal(100000),participation_rate=.1,trading_days=days)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(r.to_dict(),ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'{len(r.fills)} native fills; {len(r.daily_nav)} reconciled sessions; cash={r.native_cash}; output={args.output}')


if __name__=='__main__':main()
