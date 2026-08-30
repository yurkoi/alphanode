"""Paper trading портфеля {Bollinger, MAverage} на живых данных Binance.

Идея: чтобы live не расходился с бэктестом, прогоняем ТУ ЖЕ симуляцию до последнего
закрытого дня и берём целевые позиции из последней строки portfolio_df.

Состояние бумажного счёта (позиции, equity, история) хранится в paper_state.json.
Каждый запуск = один торговый шаг: mark-to-market -> ребаланс -> комиссии -> лог.
Запускать раз в сутки (после закрытия дневной свечи, т.е. после 00:00 UTC).

Запуск: python paper_trade.py
"""
import os
import sys
import json
import time
import pickle
import warnings
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')             # чистый лог: гасим безвредные RuntimeWarning движка
np.seterr(divide='ignore', invalid='ignore')

from strategies import Bollinger, MAverage, Donchian, RSI
from quantpylib.simulator.alpha import Portfolio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'alphanode'))
import vision_klines as vk                     # noqa: E402  fapi → data.binance.vision fallback

# --- конфиг ---
START = datetime(2019, 9, 5)
STRATS = [Bollinger, MAverage, Donchian, RSI]   # состав портфеля (как в eval_strategies.py, Sharpe 1.04)
PORTFOLIO_VOL = 0.50
EXEC = 0.001                       # комиссия, доля от оборота (10 б.п.)
START_CAPITAL = 10_000.0
BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # абсолютные пути -> не зависят от cwd (важно для systemd/cron)
STATE_FILE = os.path.join(BASE_DIR, 'paper_state.json')
TRADES_LOG = os.path.join(BASE_DIR, 'paper_trades.csv')
DATA_FILE = os.path.join(BASE_DIR, 'data.pickle')
DUST = 1.0                         # игнорируем сделки/позиции меньше $1
KLINES = 'https://fapi.binance.com/fapi/v1/klines'


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fetch_json(url, retries=4):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_klines(symbol, start_ms, end_ms):
    """Дневные свечи symbol, БЕЗ незакрытой сегодняшней (closeTime уже прошёл).
    fapi недоступен (гео-блок 451 в США) → архив data.binance.vision: те же бары, лаг ~10-30ч."""
    out = vk.fetch_rows(symbol, start_ms, end_ms, '1d')
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=['openTime', 'open', 'high', 'low', 'close', 'volume',
                                    'closeTime', 'qav', 'trades', 'tbb', 'tbq', 'ig'])
    df = df[df['closeTime'] <= now_ms()]                       # выкинуть незакрытую свечу
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = df[c].astype(float)
    df['datetime'] = pd.to_datetime(df['openTime'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
    return df[~df.index.duplicated()]


def fresh(dfs):
    return {t: df.copy() for t, df in dfs.items()}


def compute_targets(tickers, dfs, end):
    """Прогон стратегий до end; вернуть целевые веса w и плечо из последней строки."""
    sub = []
    for cls in STRATS:
        a = cls(insts=tickers, dfs=fresh(dfs), start=START, end=end,
                portfolio_vol=PORTFOLIO_VOL, execrates=EXEC)
        sub.append(a.run_simulation())
    pf = Portfolio(insts=tickers, dfs=fresh(dfs), start=START, end=end,
                   stratdfs=sub, portfolio_vol=PORTFOLIO_VOL, execrates=EXEC)
    last = pf.run_simulation().iloc[-1]
    lev = float(last.get('leverage', 0.0))
    weights = {t: float(last.get(f'{t} w', 0.0)) for t in tickers}
    return weights, lev


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
        dd = {}                                    # дедуп истории по дате (чистим прежние повторы)
        for h in state.get('history', []):
            dd[h['date']] = h
        state['history'] = [dd[d] for d in sorted(dd)]
        return state
    return {'equity': START_CAPITAL, 'positions': {}, 'prices': {},
            'last_run': None, 'history': []}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def log_trades(date, trades, prices):
    new = not os.path.exists(TRADES_LOG)
    with open(TRADES_LOG, 'a', encoding='utf-8') as f:
        if new:
            f.write('date,ticker,side,units,notional_usd\n')
        for t, d in trades.items():
            f.write(f'{date},{t},{"BUY" if d > 0 else "SELL"},{d:.6f},{abs(d*prices[t]):.2f}\n')


def main():
    force = 'force' in sys.argv                   # ручное применение (напр. смена состава портфеля)
    state = load_state()
    # ранний выход: новый дневной бар ещё не закрылся (свеча за день D закрывается в D+1 00:00 UTC)
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    if not force and state.get('last_run') == expected:
        print(f'Новый дневной бар ещё не закрылся (последний закрытый {expected} уже отработан; '
              f'следующий — после 00:00 UTC). Шаг пропущен.')
        print(f'  Счёт: equity ${state["equity"]:,.2f} | позиций {len(state["positions"])} '
              f'| last_run {state["last_run"]}')
        return

    with open(DATA_FILE, 'rb') as f:
        tickers, _ = pickle.load(f)

    print(f'Тяну свежие дневные данные по {len(tickers)} тикерам...')
    dfs, ok = {}, []
    for t in tickers:
        try:
            df = fetch_klines(t, int(pd.Timestamp(START, tz='UTC').timestamp() * 1000), now_ms())
            if len(df) > 60:
                dfs[t], _ = df, ok.append(t)
        except Exception as e:
            print(f'  {t}: пропущен ({type(e).__name__})')
    tickers = ok
    last_date = max(df.index[-1] for df in dfs.values())
    prices = {t: float(dfs[t]['close'].iloc[-1]) for t in tickers}
    end = datetime(last_date.year, last_date.month, last_date.day)
    print(f'Последний закрытый день: {end:%Y-%m-%d} | активов: {len(tickers)}\n')

    if not force and state.get('last_run') == f'{end:%Y-%m-%d}':   # данные ещё не обновились с прошлого шага
        print(f'Бар {end:%Y-%m-%d} уже отработан. Шаг пропущен.')
        print(f'  Счёт: equity ${state["equity"]:,.2f} | позиций {len(state["positions"])}')
        return

    print('Считаю целевые позиции (прогон стратегий до сегодня)...')
    weights, lev = compute_targets(tickers, dfs, end)

    equity = state['equity']
    positions = state['positions']
    prev_prices = state['prices']

    # 1) mark-to-market: P&L от удержания позиций с прошлого запуска
    pnl = sum(positions.get(t, 0.0) * (prices[t] - prev_prices.get(t, prices[t]))
              for t in tickers if t in prices)
    equity += pnl

    # 2) целевые позиции в юнитах под текущий equity
    target = {t: (weights.get(t, 0.0) * lev * equity / prices[t]) if prices[t] > 0 else 0.0
              for t in tickers}

    # 3) сделки и комиссии
    trades, turnover = {}, 0.0
    for t in tickers:
        d = target[t] - positions.get(t, 0.0)
        if abs(d * prices[t]) > DUST:
            trades[t] = d
            turnover += abs(d * prices[t])
    cost = turnover * EXEC
    equity -= cost

    # 4) применить, сохранить
    positions = {t: target[t] for t in tickers if abs(target[t] * prices[t]) > DUST}
    same_day = state['last_run'] == f'{end:%Y-%m-%d}'
    state.update({'equity': equity, 'positions': positions,
                  'prices': {t: prices[t] for t in tickers},
                  'last_run': f'{end:%Y-%m-%d}'})
    entry = {'date': f'{end:%Y-%m-%d}', 'equity': round(equity, 2),
             'pnl': round(pnl, 2), 'leverage': round(lev, 3)}
    if state['history'] and state['history'][-1]['date'] == entry['date']:
        state['history'][-1] = entry              # не дублируем тот же день
    else:
        state['history'].append(entry)
    save_state(state)
    if trades:
        log_trades(f'{end:%Y-%m-%d}', trades, prices)

    # --- отчёт ---
    longs = {t: v for t, v in positions.items() if v > 0}
    shorts = {t: v for t, v in positions.items() if v < 0}
    gross = sum(abs(v * prices[t]) for t, v in positions.items())
    ret_tot = equity / START_CAPITAL - 1
    print('\n' + '=' * 60)
    print(f'  PAPER ACCOUNT — {end:%Y-%m-%d}' + ('  (повтор за сегодня)' if same_day else ''))
    print('=' * 60)
    print(f'  Equity          : ${equity:,.2f}   (старт ${START_CAPITAL:,.0f}, {ret_tot*100:+.1f}%)')
    print(f'  P&L с пр. запуска: ${pnl:+,.2f}')
    print(f'  Комиссии шага    : ${cost:,.2f}   (оборот ${turnover:,.0f})')
    print(f'  Плечо (target)   : {lev:.2f}   валовая экспозиция ${gross:,.0f}')
    print(f'  Позиции          : {len(longs)} лонг / {len(shorts)} шорт')
    top = sorted(positions.items(), key=lambda kv: -abs(kv[1] * prices[kv[0]]))[:6]
    print('  Топ по номиналу  :')
    for t, u in top:
        notion = u * prices[t]
        print(f'      {t:12s} {"LONG " if u > 0 else "SHORT"} ${abs(notion):>9,.0f}')
    if trades:
        print(f'\n  СДЕЛКИ ШАГА ({len(trades)}):')
        for t, d in sorted(trades.items(), key=lambda kv: -abs(kv[1] * prices[kv[0]]))[:12]:
            print(f'      {"BUY " if d > 0 else "SELL"} {t:12s} ${abs(d*prices[t]):>8,.0f}')
        if len(trades) > 12:
            print(f'      ... ещё {len(trades)-12}')
    else:
        print('\n  Сделок нет (позиции уже на целях).')
    print('=' * 60)
    print(f'Стейт: {STATE_FILE} | лог сделок: {TRADES_LOG}')


if __name__ == '__main__':
    main()
