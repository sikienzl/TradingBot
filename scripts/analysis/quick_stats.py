#!/usr/bin/env python3
import sys
import pandas as pd
from pathlib import Path

p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('data/remote_exports/trade_journal.csv')
if not p.exists():
    print('File not found:', p)
    sys.exit(2)

df = pd.read_csv(p)
# Basic expected columns: side, pnl, realized_pnl, sell_price, buy_price, closed
# adapt defensively
print('Rows:', len(df))

# Use action==sell rows as closed trades
closed = df[df['action'].str.lower() == 'sell'] if 'action' in df.columns else df
closed_count = len(closed)
print('Closed trades:', closed_count)

# Compute realized PnL from 'pnl_base' if available
if 'pnl_base' in closed.columns:
    realized = closed['pnl_base'].sum()
    avg_pnl = closed['pnl_base'].mean()
    wins = closed[closed['pnl_base'] > 0]
    losses = closed[closed['pnl_base'] <= 0]
    win_rate = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins)+len(losses))>0 else None
    gross_win = wins['pnl_base'].sum()
    gross_loss = -losses['pnl_base'].sum()
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float('inf')

    print(f'Win rate: {win_rate:.2f}%')
    print(f'Realized PnL: {realized:.6f}')
    print(f'Avg PnL per closed trade: {avg_pnl:.6f}')
    print(f'Profit factor: {profit_factor:.4f}')

    # Recent N closed trades
    N = 120
    recent = closed.tail(N)
    if len(recent) > 0:
        recent_win = (recent['pnl_base'] > 0).sum() / len(recent) * 100
        print(f'Recent {len(recent)} closed trades win rate: {recent_win:.2f}%')

    # Top coins by performance
    if 'coin' in closed.columns:
        grp = closed.groupby('coin')['pnl_base'].agg(['sum','count']).sort_values('sum', ascending=False)
        print('\nTop coins by realized PnL:')
        print(grp.head(10).to_string())

print('\nDone')
