# -*- coding: utf-8 -*-
"""
Réplica fiel en Python del indicador 'Elliot Wave - Impulse' (Pine Script v4)
© HeWhoMustNotBeNamed — fuente pegada por el usuario.

Lógica replicada exactamente:
  1) pivots(length): máximo/mínimo rodante de `length` velas (incluida la actual).
     dir = 1 si la vela es el máximo (y no el mínimo), -1 si es el mínimo (y no el
     máximo), en otro caso arrastra el dir de la vela anterior.
  2) zigzag(): mantiene una pila de pivotes (máx 10). Si dir no cambia respecto a la
     vela anterior, sustituye el último pivote por el extremo más extremo. Si el nuevo
     extremo supera al pivote previo de su misma dirección, su dirección se marca 2
     (o -2): pivote "impulsivo".
  3) ew_impulse() con waitForConfirmation=true (start=1):
       Point2 = piv[1], Point1 = piv[2], Point0 = piv[3]
       W1=|P1-P0|, W2=|P2-P1|, r2=W2/W1
       patrón: r2 ∈ {0.50, 0.618, 0.764, 0.854} × (1 ± 5%)
       dirMatched: (P1D==2 y P2D==-1) o (P1D==-2 y P2D==1)
       ignore: si el patrón comparte alguno de los 3 puntos con el último dibujado
       niveles: dir=P0>P1?-1:1
         Entry = P2 + dir*0.30*W2     Stop = P0     T.Stop = P2 - dir*0.30*W2
         TP1..TP4 = P2 + dir*{1.618, 2.0, 2.618, 3.236}*W2
"""
import json
import numpy as np
import pandas as pd

ZIGZAG_LEN = 10
ERROR_PCT = 5.0
ENTRY_PCT = 30.0
MAX_PIVOTS = 10
RATIOS = (0.5, 0.618, 0.764, 0.854)
TP_MULTS = (1.618, 2.0, 2.618, 3.236)

DATA_JSON = '/home/z/my-project/upload/ibex_yahoo.json'
OUT_DIR = '/home/z/my-project/results'


def load_data(path=DATA_JSON):
    """OHLC diario del ^IBEX (Yahoo Finance, EUR)."""
    with open(path) as f:
        d = json.load(f)
    res = d['chart']['result'][0]
    ts = res['timestamp']
    q = res['indicators']['quote'][0]
    dates = pd.to_datetime(ts, unit='s', utc=True).tz_convert('Europe/Madrid')
    df = pd.DataFrame({
        'open': q['open'], 'high': q['high'], 'low': q['low'], 'close': q['close'],
    }, index=pd.DatetimeIndex(dates.date).normalize())
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df[~df.index.duplicated(keep='last')].sort_index()
    assert (df['high'] >= df['low']).all(), 'high < low detectado'
    assert (df[['open', 'high', 'low', 'close']] > 0).all().all(), 'precios <= 0'
    return df.astype(float)


def compute_signals(df, verbose=True):
    """Ejecuta el motor Pine replicado barra a barra. Devuelve DataFrame de señales."""
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(df)
    L = ZIGZAG_LEN

    rollmax = pd.Series(high).rolling(L, min_periods=L).max().values
    rollmin = pd.Series(low).rolling(L, min_periods=L).min().values
    phigh = np.where(high == rollmax, high, np.nan)
    plow = np.where(low == rollmin, low, np.nan)

    # Serie dir calculada en TODAS las barras (como en Pine, donde dir arrastra)
    dir_s = np.zeros(n, dtype=int)
    prev = 0
    for i in range(n):
        h = not np.isnan(phigh[i])
        l = not np.isnan(plow[i])
        if h and not l:
            d = 1
        elif l and not h:
            d = -1
        else:
            d = prev
        dir_s[i] = d
        prev = d

    piv_vals, piv_bars, piv_dirs = [], [], []
    signals = []
    last_drawn = (None, None, None)
    err_min = (100 - ERROR_PCT) / 100.0
    err_max = (100 + ERROR_PCT) / 100.0
    entry_range = ENTRY_PCT / 100.0
    n_updates = 0

    for i in range(n):
        has_h = not np.isnan(phigh[i])
        has_l = not np.isnan(plow[i])
        if not (has_h or has_l):
            continue  # ew_impulse no puede cambiar sin actualizar la pila de pivotes
        n_updates += 1

        # ---------- zigzag() ----------
        d = int(dir_s[i])
        value = phigh[i] if d == 1 else plow[i]
        bar = i
        dirchanged = (i > 0) and (dir_s[i] != dir_s[i - 1])
        if (not dirchanged) and len(piv_vals) >= 1:
            pivot = piv_vals.pop(0)
            pivotbar = piv_bars.pop(0)
            pivotdir = piv_dirs.pop(0)
            if value * pivotdir < pivot * pivotdir:
                value = pivot
                bar = pivotbar
        if len(piv_vals) >= 2:
            last_point = piv_vals[1]
            if d * value > d * last_point:
                d = d * 2
        piv_vals.insert(0, value)
        piv_bars.insert(0, bar)
        piv_dirs.insert(0, d)
        if len(piv_vals) > MAX_PIVOTS:
            piv_vals.pop()
            piv_bars.pop()
            piv_dirs.pop()

        # ---------- ew_impulse() (start = waitForConfirmation ? 1 : 0 = 1) ----------
        if len(piv_vals) >= 4:
            P2, P1, P0 = piv_vals[1], piv_vals[2], piv_vals[3]
            P2D, P1D = piv_dirs[1], piv_dirs[2]
            W1 = abs(P1 - P0)
            W2 = abs(P2 - P1)
            if W1 > 0:
                r2 = W2 / W1
                pattern = any(err_min * f < r2 < err_max * f for f in RATIOS)
                dirm = (P1D == 2 and P2D == -1) or (P1D == -2 and P2D == 1)
                e0, e1, e2 = last_drawn
                ignore = False
                if e0 is not None and (e0 == P0 or e1 == P1 or e2 == P2):
                    ignore = True
                if pattern and dirm and not ignore:
                    direction = -1 if P0 > P1 else 1
                    entry = P2 + direction * entry_range * W2
                    stop = P0
                    tstop = P2 - direction * entry_range * W2
                    tps = [P2 + direction * k * W2 for k in TP_MULTS]
                    signals.append(dict(
                        bar=i, date=df.index[i], direction=direction,
                        P0=P0, P1=P1, P2=P2,
                        P0bar=piv_bars[3], P1bar=piv_bars[2], P2bar=piv_bars[1],
                        W1=W1, W2=W2, r2=r2,
                        entry=entry, stop=stop, tstop=tstop,
                        tp1=tps[0], tp2=tps[1], tp3=tps[2], tp4=tps[3]))
                    last_drawn = (P0, P1, P2)

    sig = pd.DataFrame(signals)
    if verbose:
        print(f'Barras: {n} | actualizaciones de pivote: {n_updates} | señales: {len(sig)}')
        if len(sig):
            print('por dirección:', sig['direction'].value_counts().to_dict())
    return sig


# Invariantes estructurales para verificar la réplica
def check_invariants(sig, df):
    ok = True
    for _, s in sig.iterrows():
        if s.direction == 1:      # impulso alcista
            conds = [
                s.P2 >= s.P0,               # onda 2 no rompe bajo el origen (dir -1)
                s.P2 < s.P1,                # onda 2 es corrección
                s.P0 < s.P1,
                s.P0 < s.entry < s.P1,      # entry entre P2(+30%W2) y P1
                s.tp1 > s.P1,               # objetivos proyectados desde P2
                s.tstop > s.P2,             # T.Stop bajo el mínimo de onda 2... (para largo: tstop < P2)
            ]
            conds[5] = s.tstop < s.P2      # largo: tstop = P2 - 0.3W2 < P2
        else:                     # impulso bajista
            conds = [
                s.P2 <= s.P0,
                s.P2 > s.P1,
                s.P0 > s.P1,
                s.P1 < s.entry < s.P2,
                s.tp1 < s.P1,
                s.tstop > s.P2,
            ]
        if not all(conds):
            ok = False
            print('INVARIANTE FALLIDA en', s['date'], dict(s))
            break
    print('Invariantes estructurales:', 'OK' if ok else 'FALLO')
    return ok


if __name__ == '__main__':
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_data()
    print(f'Datos: {df.index[0].date()} → {df.index[-1].date()} ({len(df)} barras)')
    sig = compute_signals(df)
    check_invariants(sig, df)

    df.to_csv(f'{OUT_DIR}/ibex_ohlc.csv')
    sig.to_csv(f'{OUT_DIR}/signals.csv', index=False, float_format='%.6f')

    if len(sig):
        sig_y = sig.copy()
        sig_y['year'] = sig_y['date'].dt.year
        print('\nSeñales por año:')
        t = sig_y.pivot_table(index='year', columns='direction', values='bar', aggfunc='count').fillna(0).astype(int)
        t.columns = ['corto' if c == -1 else 'largo' for c in t.columns]
        print(t.to_string())
        print('\nr2 (retargeting onda2/onda1) de las señales: min %.3f max %.3f' % (sig.r2.min(), sig.r2.max()))
