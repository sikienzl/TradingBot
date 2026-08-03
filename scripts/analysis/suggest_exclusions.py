#!/usr/bin/env python3
import sys
import pandas as pd
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('data/remote_exports/trade_journal.csv')
if not path.exists():
    print('File not found:', path)
    raise SystemExit(2)

df = pd.read_csv(path)
closed = df[df['action'].str.lower() == 'sell']

grp = closed.groupby('coin')['pnl_base'].agg(['sum','count'])
# select coins with negative total pnl and at least N trades
min_trades = int(sys.argv[2]) if len(sys.argv) > 2 else 10
lossmakers = grp[(grp['sum'] < 0) & (grp['count'] >= min_trades)].sort_values('sum')

if lossmakers.empty:
    print('# No clear lossmakers found (threshold: count >= {})'.format(min_trades))
    raise SystemExit(0)

coins = ','.join(lossmakers.index.astype(str))
print('# Suggested EXCLUDED_COINS (lossmakers with >= {} trades):'.format(min_trades))
print('EXCLUDED_COINS={}'.format(coins))
print('\n# Details:')
print(lossmakers.to_string())

# Optionally write to .env.tuned
out = Path('.env.tuned')
with out.open('w', encoding='utf-8') as f:
    f.write('# Tuned environment recommendations\n')
    f.write('EXCLUDED_COINS={}\n'.format(coins))

print('\nWrote .env.tuned')
