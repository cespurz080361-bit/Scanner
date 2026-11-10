# -*- coding: utf-8 -*-
"""
Disparadores T2 / T3 / T12 + filtro de caída previa para señales LARGAS del
indicador 'Elliott Wave Impulse' (réplica). Módulo compartido por el backtest
de acciones del IBEX 35 y el de índices europeos.

Definiciones (todas causales, sin información futura):
  Caída previa  : (máximo high de las 250 velas anteriores al inicio de la onda 1
                   —ventana [P0bar-249, P0bar]— − P2) / máximo × 100.
                  Requiere ≥ 60 velas disponibles en la ventana; si no, NaN.
  T2            : cierre ≥ P1 − 0.50·W1  (recupera el 50 % de retracción de la onda 1)
  T3            : cierre > P1            (supera el extremo de la onda 1)
  T1            : cierre > último swing menor interno de la onda 2
                  (pivote high simétrico de 5 velas, estrictamente entre P1bar y P2bar;
                   si no hay ninguno, el máximo high de esa ventana; si la ventana está
                   vacía, P2 + 0.50·W2)
  T12           : T1 y T2 cumplidas a la vez (cierre > T1  Y  cierre ≥ T2)
  T0            : referencia — entrada por mercado en la apertura siguiente a la
                  confirmación del patrón (sin esperar disparador).

  Ventana de disparo: velas [bar_señal, bar_señal+15]. Invalidación si algún
  cierre de la ventana queda por debajo de P2 (la onda 2 quedaría rota).
  Entrada: apertura de la vela siguiente al cierre disparador (orden a mercado).
           Si esa apertura cruza el stop → patrón invalidado, no se entra.
  Salidas: idénticas al backtest original ({P0, TStop} × {TP1..TP4}), stop primero
           en la ambigüedad intradía, huecos ejecutados en apertura, cierre forzado
           al último cierre.
  Tamaño : acciones floor(5000/precio); índices fraccional 5000/precio (CFD).
  Costes : 0,35 % ida y vuelta. Una posición por valor.
"""
import math
import numpy as np
import pandas as pd

TRIG_VALID = 15        # velas máximas para que salte el disparador tras la señal
CAIDA_VENTANA = 250    # velas para medir la caída previa (≈ 1 año de mercado)
CAIDA_MIN_BARS = 60
CAPITAL = 5000.0
COST_RT = 0.0035       # 0,35 % ida y vuelta
SCENARIOS = [(s, k) for s in ('P0', 'TStop') for k in (1, 2, 3, 4)]
SC_NAME = {('P0', 1): 'P0+TP1', ('P0', 2): 'P0+TP2', ('P0', 3): 'P0+TP3', ('P0', 4): 'P0+TP4',
           ('TStop', 1): 'TStop+TP1', ('TStop', 2): 'TStop+TP2', ('TStop', 3): 'TStop+TP3',
           ('TStop', 4): 'TStop+TP4'}


# --------------------------------------------------------------------- niveles
def add_levels(df, sigs):
    """Por cada señal larga: caída previa, niveles T1/T2/T3. Devuelve df indexado por 'bar'."""
    h = df['high'].values
    n = len(df)
    out = []
    for s in sigs.itertuples():
        p0b, p1b, p2b = int(s.P0bar), int(s.P1bar), int(s.P2bar)
        P0, P1, P2 = float(s.P0), float(s.P1), float(s.P2)
        W2 = float(s.W2)
        r = dict(bar=int(s.bar))
        # --- caída previa (máximo de ~1 año anterior al inicio de la onda 1 → P2) ---
        ini = max(0, p0b - CAIDA_VENTANA + 1)
        win_hi = h[ini:p0b + 1]
        if len(win_hi) >= CAIDA_MIN_BARS:
            top = float(win_hi.max())
            r['top250'] = top
            r['caida250'] = (top - P2) / top * 100.0
            r['caida250_P0'] = (top - P0) / top * 100.0
            r['fecha_top'] = df.index[ini + int(np.argmax(win_hi))]
        else:
            r['top250'] = r['caida250'] = r['caida250_P0'] = np.nan
            r['fecha_top'] = pd.NaT
        ini2 = max(0, p0b - 119)
        win2 = h[ini2:p0b + 1]
        r['caida120'] = ((float(win2.max()) - P2) / float(win2.max()) * 100.0
                         if len(win2) >= 30 else np.nan)
        # --- niveles de disparo ---
        r['t2_lvl'] = P1 - 0.5 * float(s.W1)
        r['t3_lvl'] = P1
        lo, hi = p1b + 1, p2b - 1          # interior de la onda 2
        sw = None
        for k in range(max(lo, 2), min(hi, n - 3) + 1):
            if h[k] == h[k - 2:k + 3].max():
                sw = k                      # nos quedamos con el ÚLTIMO swing
        if sw is not None:
            r['t1_lvl'] = float(h[sw])
        elif hi >= lo:
            r['t1_lvl'] = float(h[lo:hi + 1].max())
        else:
            r['t1_lvl'] = P2 + 0.5 * W2
        out.append(r)
    return pd.DataFrame(out).set_index('bar')


# ------------------------------------------------------------------- disparo
def trig_bar(df, s, kind):
    """Barra (índice) del cierre que dispara el trigger.
    ≥ bar_señal si dispara; -1 = no disparó en la ventana; -2 = onda 2 rota por cierre."""
    c = df['close'].values
    n = len(df)
    b = int(s.bar)
    P2 = float(s.P2)
    if kind == 'T0':
        return b + 1 if b + 1 <= n - 1 else -1
    fin = min(b + TRIG_VALID, n - 1)
    for j in range(b, fin + 1):
        if c[j] < P2:
            return -2                       # la onda 2 quedaría rota
        if kind == 'T2':
            ok = c[j] >= float(s.t2_lvl)
        elif kind == 'T3':
            ok = c[j] > float(s.t3_lvl)
        else:                               # T12
            ok = (c[j] > float(s.t1_lvl)) and (c[j] >= float(s.t2_lvl))
        if ok:
            return j
    return -1


# ----------------------------------------------------------------- simulador
def simulate(df, sigs, trig_col, stop_mode, tp_k, fractional=False,
             capital=CAPITAL, cost_rt=COST_RT):
    """Backtest con entrada a mercado en la apertura siguiente al disparo.
    Sólo señales largas (la hipótesis es de recuperación tras caída)."""
    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    dates = df.index
    n = len(df)

    evs, n_disp, n_cad, n_inv = [], 0, 0, 0
    for s in sigs.itertuples():
        tb = int(getattr(s, trig_col))
        if tb == -1:
            n_cad += 1
            continue
        if tb == -2:
            n_inv += 1
            continue
        eb = tb + 1
        if eb > n - 1:
            n_cad += 1
            continue
        n_disp += 1
        evs.append(dict(bar=int(s.bar), entry_bar=eb, entry_px=float(o[eb]),
                        symbol=s.symbol, nombre=getattr(s, 'nombre', s.symbol),
                        fecha_senal=str(pd.Timestamp(s.date).date()),
                        stop=float(s.stop if stop_mode == 'P0' else s.tstop),
                        tp=float(getattr(s, f'tp{tp_k}')), P2=float(s.P2)))
    evs.sort(key=lambda d: d['bar'])

    trades = []
    diag = dict(senales=int(len(sigs)), disparadas=n_disp, caducadas=n_cad,
                invalidadas=n_inv, descartadas_pos=0, reemplazadas=0,
                hueco_invalido=0, no_llenadas=0)

    pos = None
    sched = None
    k = 0

    def cerrar(i, px, motivo):
        e, u = pos['entry'], pos['units']
        gross = u * (px - e)
        cost = cost_rt / 2 * u * e + cost_rt / 2 * u * px
        trades.append(dict(
            symbol=pos['symbol'], nombre=pos['nombre'], dir='LARGO',
            fecha_senal=pos['fecha_senal'], fecha_entrada=dates[pos['entry_bar']],
            fecha_salida=dates[i], motivo=motivo, dias=(i - pos['entry_bar']),
            precio_entrada=e, unidades=u, nominal_entrada=u * e,
            precio_salida=px, pnl_bruto=gross, costes=cost, pnl_neto=gross - cost))

    for i in range(n):
        # ---- posición abierta de barras anteriores ----
        if pos is not None and pos['entry_bar'] < i:
            ex = reason = None
            if o[i] <= pos['sl']:
                ex, reason = o[i], 'Stop_hueco'
            elif o[i] >= pos['tp']:
                ex, reason = o[i], 'TP_hueco'
            elif l[i] <= pos['sl'] and h[i] >= pos['tp']:
                ex, reason = pos['sl'], 'Stop'
            elif l[i] <= pos['sl']:
                ex, reason = pos['sl'], 'Stop'
            elif h[i] >= pos['tp']:
                ex, reason = pos['tp'], 'TP'
            if ex is not None:
                cerrar(i, ex, reason)
                pos = None
        # ---- entrada programada (apertura) ----
        if sched is not None and sched['entry_bar'] == i:
            if pos is not None:
                diag['descartadas_pos'] += 1
                sched = None
            else:
                px = sched['entry_px']
                if px <= sched['stop']:          # hueco a través del stop: patrón roto
                    diag['hueco_invalido'] += 1
                    sched = None
                else:
                    units = (capital / px) if fractional else math.floor(capital / px)
                    if units <= 0:
                        sched = None
                    else:
                        pos = dict(symbol=sched['symbol'], nombre=sched['nombre'],
                                   entry_bar=i, entry=px, units=units,
                                   fecha_senal=sched['fecha_senal'],
                                   sl=sched['stop'], tp=sched['tp'])
                        sched = None
                        # salida en la misma barra de entrada (stop primero)
                        ex = reason = None
                        if l[i] <= pos['sl'] and h[i] >= pos['tp']:
                            ex, reason = pos['sl'], 'Stop'
                        elif l[i] <= pos['sl']:
                            ex, reason = pos['sl'], 'Stop'
                        elif h[i] >= pos['tp']:
                            ex, reason = pos['tp'], 'TP'
                        if ex is not None:
                            cerrar(i, ex, reason)
                            pos = None
        elif sched is not None and sched['entry_bar'] < i:
            diag['descartadas_pos'] += 1         # se perdió por posición abierta
            sched = None
        # ---- señales al cierre ----
        while k < len(evs) and evs[k]['bar'] == i:
            e = evs[k]
            k += 1
            if pos is not None:
                diag['descartadas_pos'] += 1
            elif sched is not None:
                diag['reemplazadas'] += 1
                sched = e
            else:
                sched = e

    if pos is not None:
        cerrar(n - 1, c[n - 1], 'Fin_datos')
    elif sched is not None:
        diag['no_llenadas'] += 1
    return trades, diag


def resumen(trades_df, diag, escenario, trigger, subset):
    r = dict(trigger=trigger, subset=subset, escenario=escenario)
    r.update(diag)
    if len(trades_df) == 0:
        r.update(ops=0, win_rate=np.nan, pnl_neto=0.0, pnl_medio=0.0, pf=np.nan,
                 dias_medio=np.nan, max_dd=0.0, tp_hit=0, stop_hit=0, fin_datos=0)
        return r
    g = trades_df
    wins = g[g.pnl_neto > 0].pnl_neto.sum()
    losses = -g[g.pnl_neto <= 0].pnl_neto.sum()
    eq = g.sort_values('fecha_salida').pnl_neto.cumsum()
    dd = (eq - eq.cummax()).min()
    mot = g.motivo.value_counts()
    r.update(ops=len(g), win_rate=(g.pnl_neto > 0).mean(), pnl_neto=g.pnl_neto.sum(),
             pnl_medio=g.pnl_neto.mean(), pf=(wins / losses if losses > 0 else np.inf),
             dias_medio=g.dias.mean(), max_dd=dd,
             tp_hit=int(mot.get('TP', 0) + mot.get('TP_hueco', 0)),
             stop_hit=int(mot.get('Stop', 0) + mot.get('Stop_hueco', 0)),
             fin_datos=int(mot.get('Fin_datos', 0)))
    return r


BUCKETS = [('<10%', 0.0, 10.0), ('10-15%', 10.0, 15.0), ('15-20%', 15.0, 20.0),
           ('>20%', 20.0, np.inf)]


def bucket_de(x):
    if pd.isna(x):
        return 's/dato'
    for lbl, a, b in BUCKETS:
        if a <= x < b:
            return lbl
    return 's/dato'
