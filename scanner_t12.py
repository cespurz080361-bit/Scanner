# -*- coding: utf-8 -*-
"""SCANNER T12 EUROPA — ejecución diaria en GitHub Actions con alertas por Telegram.

Señales LARGAS del motor 'Elliott Wave Impulse' (réplica Pine) sobre los 231
constituyentes de DAX 40 + CAC 40 + Euro Stoxx 50 + FTSE 100 + FTSE MIB, con
filtro de caída previa > 20 % (250 velas) y disparador T12
(cierre > T1 y cierre ≥ T2 = P1 − 0,50·W1 dentro de [señal, señal+15]).

Modos de funcionamiento:
  1) GitHub Actions (recomendado; funciona con el PC apagado):
       python scanner_t12.py --una-vez
     Ejecuta UN ciclo completo (actualizar datos → escanear → alertas → salidas).
     La repetición diaria la hace el cron del workflow
     (.github/workflows/scanner.yml) y el estado (alarmas ya enviadas) queda
     guardado en estado_scanner.json, que el workflow commitea al repositorio.

  2) En tu PC (bucle continuo, opcional):
       python scanner_t12.py
     Repite el ciclo completo cada INTERVALO_MINUTOS mientras el PC esté
     encendido. Para uso diario basta el modo GitHub.

Requisitos:  pip install -r requirements.txt   (pandas numpy requests openpyxl)

Credenciales de Telegram (por este orden de prioridad):
  a) Variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.
     En GitHub se configuran como "Secrets" del repositorio (recomendado).
  b) En local, exporta las variables o pégalas en la zona de CONFIGURACIÓN.

Datos: OHLC diario sin ajustar de la API chart de Yahoo (v8), el mismo origen
del backtest validado. El histórico (~8 años por valor) viaja en
datos/constituyentes/ y cada ejecución añade de forma incremental la última
mensualidad (rango '1mo'); si un valor lleva más de REPARAR_SI_ANTIGUO días sin
datos se re-descarga completo. Una vela de HOY anterior a la hora de cierre
seguro (HORA_SESION_CERRADA) se descarta por ser sesión incompleta.

Salidas (carpeta salidas/, se commitea la copia "ultimo"):
  - scanner_T12_EU_{FECHA}.xlsx (5 hojas) y CSVs por categoría (con fecha).
  - Copias fijas scanner_T12_EU_ultimo.* (sin fecha, siempre la última).

Alarmas Telegram (deduplicadas por estado_scanner.json):
  📈 nueva operación (entrada), ⚡ disparo en la última sesión (entrada
  prevista), 👁 señal en vigilancia (ventana abierta, niveles T1/T2),
  🎯/🛑 eventos de cartera (TP1..TP4 alcanzado / SL tocado) y ✅ resumen diario.
La primera ejecución fija la referencia sin enviar avisos (pon
AVISAR_PRIMERA_EJECUCION = True si quieres recibir también lo ya vigente).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ew_impulse as ew
import ew_triggers as tr


# ---------------------- CONFIGURACIÓN ----------------------
INTERVALO_MINUTOS = 60   # Solo se usa en modo bucle (en GitHub programa el workflow).

# Datos de Telegram. Primero se intentan las variables de entorno (Secrets
# de GitHub); si no existen, se usan los valores escritos aquí (solo útil
# si ejecutas el script en tu propio PC).
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")      # ej: "123456:ABC-DEF..."
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")  # ej: "123456789"

# Zona horaria para las horas de los mensajes y el corte de sesión.
ZONA_HORARIA = os.environ.get("TZ_ALERTAS", "Europe/Madrid")

UMBRAL_CAIDA = 20.0        # % de caída previa (caida250_P0) exigido a la señal
DIAS_VALIDACION = 30       # ventana de entradas que valida el scanner (días naturales)
HILOS_DESCARGA = 8         # peticiones simultáneas a Yahoo
RANGO_INICIAL = '8y'       # descarga completa si un valor no tiene histórico
RANGO_INCREMENTAL = '1mo'  # actualización diaria incremental
MIN_VELAS = 260            # histórico mínimo para escanear un valor (250 + margen)
REPARAR_SI_ANTIGUO = 15    # días sin datos → re-descarga completa del valor

ENVIAR_RESUMEN = True             # ✅ mensaje corto de "pulso" en cada ejecución
AVISAR_PRIMERA_EJECUCION = False  # True → la 1ª ejecución envía también lo ya vigente
HORA_SESION_CERRADA = (18, 0)     # una vela de HOY antes de esta hora se descarta
MAX_COLA = 200                    # aviso de seguridad si la cola de envío crece sin fin
# ----------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATOS = BASE_DIR / 'datos'
CONST = DATOS / 'constituyentes'
SALIDAS = BASE_DIR / 'salidas'
ESTADO_PATH = BASE_DIR / 'estado_scanner.json'
LOG_PATH = BASE_DIR / 'scanner_t12.log'

SEC_FIX = {'Industrials': 'Industria', 'Real Estate Investment Trusts': 'Inmobiliario (REIT)',
           'Closed End Investments': 'Inversión colectiva (trusts)'}
EV_RE = re.compile(r'(TP\d|SL P0|SL TStop) (?:alcanzado|tocado) el (\d{4}-\d{2}-\d{2})')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ------------------------------------------------------------------ utilidades
def _tz() -> Any:
    """Devuelve la zona horaria configurada (con reserva segura)."""
    try:
        return ZoneInfo(ZONA_HORARIA)
    except Exception:
        return datetime.now().astimezone().tzinfo


def ahora_local() -> datetime:
    """Fecha/hora actual en la zona horaria configurada."""
    return datetime.now(_tz())


def esc(t: Any) -> str:
    """Escapa texto para Telegram en modo HTML."""
    return html.escape(str(t))


def num(v: Any) -> str:
    """Número con hasta 4 decimales y separador de miles, sin ceros sobrantes."""
    if pd.isna(v):
        return 's/dato'
    return f'{float(v):,.4f}'.rstrip('0').rstrip('.')


# ------------------------------------------------------------------- telegram
def enviar_telegram(mensaje: str) -> bool:
    """Envía un mensaje por Telegram con el Bot API oficial.
    Devuelve True si el envío se completó correctamente."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error(
            "Faltan TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID. En GitHub, crea los "
            "'Secrets' del repositorio con esos nombres. En local, exporta las "
            "variables de entorno o pégalos en la zona de CONFIGURACIÓN del script."
        )
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        respuesta = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensaje,
                "parse_mode": "HTML",  # permite <b>negrita</b>, <code>, etc.
            },
            timeout=15,
        )
    except Exception as exc:
        logging.exception("No se pudo enviar el mensaje de Telegram: %s", exc)
        return False

    if respuesta.ok:
        logging.info("Telegram enviado correctamente.")
        return True

    logging.error(
        "Telegram rechazó el envío (HTTP %s): %s",
        respuesta.status_code,
        respuesta.text[:300],
    )
    return False


# --------------------------------------------------------------------- estado
def estado_nuevo() -> dict[str, Any]:
    return {"actualizado": None, "ultima_sesion": None,
            "notificados": {"entradas": {}, "pendientes": {}, "vigilancia": {}, "eventos": {}},
            "cola_envio": []}


def cargar_estado() -> dict[str, Any]:
    if not ESTADO_PATH.exists():
        return estado_nuevo()
    try:
        with ESTADO_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        base = estado_nuevo()
        base.update(data)
        for cat in base["notificados"]:
            if not isinstance(base["notificados"][cat], dict):
                base["notificados"][cat] = {}
        if not isinstance(base["cola_envio"], list):
            base["cola_envio"] = []
        return base
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("No se pudo leer el estado (%s). Se inicia estado nuevo.", exc)
        return estado_nuevo()


def guardar_estado(estado: dict[str, Any]) -> None:
    temp = ESTADO_PATH.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)
    temp.replace(ESTADO_PATH)


def enviar_pendientes(estado: dict[str, Any]) -> int:
    """Envía la cola de alertas; las que fallen se reintentan en la siguiente ejecución."""
    cola = estado["cola_envio"]
    enviados = 0
    while cola:
        item = cola[0]
        if len(cola) > MAX_COLA:
            logging.warning("Cola de envío con %s items (> %s); se recorta por la cabeza.",
                            len(cola), MAX_COLA)
            del cola[:len(cola) - MAX_COLA]
        if enviar_telegram(item["texto"]):
            cola.pop(0)
            cat, clave = item["clave"].split("#", 1)
            estado["notificados"].setdefault(cat, {})[clave] = \
                ahora_local().isoformat(timespec="seconds")
            guardar_estado(estado)
            enviados += 1
            # Pequeña pausa entre mensajes por cortesía con la API.
            if cola:
                time.sleep(1)
        else:
            # Conserva el evento para reintentarlo en la siguiente vuelta.
            break
    return enviados


# -------------------------------------------------------------- datos (Yahoo)
def descargar_rango(tk: str, rango: str):
    """OHLC diario SIN ajustar de la API chart de Yahoo (v8), como el backtest.
    Normaliza la moneda: Yahoo devuelve los tickers de Londres en peniques (GBp/GBX);
    el histórico del proyecto está en libras, así que en ese caso se divide entre 100.
    Prueba query1/query2 con un reintento. Devuelve (df, moneda) o (None, None)."""
    for intento in range(2):
        for host in ('query1', 'query2'):
            url = (f'https://{host}.finance.yahoo.com/v8/finance/chart/'
                   f'{tk}?range={rango}&interval=1d')
            try:
                r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=45)
                if not r.ok:
                    continue
                res = r.json()['chart']['result'][0]
                q = res['indicators']['quote'][0]
                moneda = str(res.get('meta', {}).get('currency', ''))
                dates = pd.to_datetime(res['timestamp'], unit='s', utc=True
                                       ).tz_convert(ZONA_HORARIA)
                df = pd.DataFrame({'open': q['open'], 'high': q['high'],
                                   'low': q['low'], 'close': q['close']},
                                  index=pd.DatetimeIndex(dates.date).normalize())
                df = df.dropna(subset=['open', 'high', 'low', 'close'])
                df = df[~df.index.duplicated(keep='last')].sort_index()
                if len(df):
                    if moneda in ('GBp', 'GBX'):
                        df = df / 100.0          # peniques → libras (escala del histórico)
                    return df.astype(float), moneda
            except Exception:
                continue
        time.sleep(1.5)
    return None, None


def recorta_sesion_incompleta(df: pd.DataFrame) -> pd.DataFrame:
    """Si la última vela es de HOY y aún no ha pasado la hora de cierre seguro,
    la descarta (sería una sesión en curso, no cerrada)."""
    ahora = ahora_local()
    if (ahora.hour, ahora.minute) < HORA_SESION_CERRADA:
        hoy = pd.Timestamp(ahora.date())
        if len(df) and df.index[-1] >= hoy:
            df = df.iloc[:-1]
    return df


def actualizar_datos(tickers: list[str]) -> str:
    """Fusión incremental de la última mensualidad sobre datos/constituyentes/*.csv.
    Con guarda de escala: si Yahoo cambia de unidades (peniques ↔ libras, el incidente
    del 09-11-2026), la descarga se re-escala comparándola con lo ya almacenado."""
    HOY = pd.Timestamp(ahora_local().date())

    def tarea(tk: str):
        path = CONST / f'{tk}.csv'
        if not path.exists():
            nuevo, moneda = descargar_rango(tk, RANGO_INICIAL)
            if nuevo is None or len(nuevo) < 100:
                return tk, 'SIN_DATOS', 0
            nuevo = recorta_sesion_incompleta(nuevo[nuevo.index <= HOY])
            nuevo.to_csv(path, index_label='date')
            return tk, f'NUEVO({moneda or "?"})', len(nuevo)
        viejo = pd.read_csv(path, parse_dates=['date']).set_index('date').sort_index()
        antiguo = (HOY - viejo.index.max()).days > REPARAR_SI_ANTIGUO
        nuevo, moneda = descargar_rango(tk, RANGO_INICIAL if antiguo else RANGO_INCREMENTAL)
        if nuevo is None:
            return tk, 'FALLO', 0
        nuevo = recorta_sesion_incompleta(nuevo[nuevo.index <= HOY]).astype(float)
        # --- guarda de escala: ratio mediano del cierre en fechas comunes ---
        comunes = nuevo.index.intersection(viejo.index)
        if len(comunes):
            ratios = (nuevo.loc[comunes, 'close'] / viejo.loc[comunes, 'close'])
            ratios = ratios.replace([np.inf, -np.inf], np.nan).dropna()
            med = float(ratios.tail(120).median()) if len(ratios) else 1.0
            if 0.95 <= med <= 1.05:
                pass                                   # escala correcta
            elif 95 <= med <= 105:
                nuevo = nuevo / 100.0                  # Yahoo en peniques pese a la moneda
                logging.info('%s: descarga en peniques (ratio %.1f); dividido por 100.', tk, med)
            elif 0.0095 <= med <= 0.0105:
                nuevo = nuevo * 100.0                  # caso inverso
                logging.info('%s: descarga en libras (ratio %.3f); multiplicado por 100.', tk, med)
            else:
                # Ante cualquier otra anomalía se conserva el histórico y se reintenta mañana.
                return tk, f'ESCALA_ANOMALA({med:.3f})', 0
        base = viejo[viejo.index < nuevo.index.min()]
        fusion = pd.concat([base, nuevo])
        fusion = fusion[~fusion.index.duplicated(keep='last')].sort_index().astype(float)
        fusion.to_csv(path, index_label='date')
        return tk, 'OK', len(fusion) - len(viejo)

    res = []
    with ThreadPoolExecutor(max_workers=HILOS_DESCARGA) as ex:
        futs = {ex.submit(tarea, tk): tk for tk in tickers}
        for f in as_completed(futs):
            try:
                res.append(f.result())
            except Exception as exc:          # noqa: BLE001
                res.append((futs[f], f'ERROR: {exc}', 0))

    r = pd.DataFrame(res, columns=['ticker', 'estado', 'velas'])
    ok = r.estado.str.startswith(('OK', 'NUEVO'))
    fallos = r[~ok].ticker.tolist()
    logging.info('Datos: %s/%s valores actualizados | velas nuevas: %s | fallos: %s',
                 int(ok.sum()), len(r), int(r.loc[ok, 'velas'].sum()),
                 fallos if fallos else 'ninguno')
    valida_saltos(tickers)
    ultima = max(pd.read_csv(CONST / f'{tk}.csv', parse_dates=['date']).date.max()
                 for tk in tickers if (CONST / f'{tk}.csv').exists())
    return str(pd.Timestamp(ultima).date())


def valida_saltos(tickers: list[str]) -> None:
    """Aviso temprano si algún fichero queda con un salto de escala reciente.
    Sólo mira las últimas 60 sesiones (zona donde una fusión puede romper la
    escala); los saltos históricos reales (fusiones, ampliaciones, consolidaciones
    — Stellantis 2021, Saipem 2022...) no generan ruido."""
    malos = []
    for tk in tickers:
        p = CONST / f'{tk}.csv'
        if not p.exists():
            continue
        try:
            d = pd.read_csv(p, parse_dates=['date']).set_index('date').sort_index().tail(60)
        except Exception:
            continue
        ret = d.close.pct_change().abs()
        saltos = ret[ret > 0.40]
        if len(saltos):
            malos.append((tk, len(saltos), str(saltos.index[-1].date())))
    if malos:
        logging.warning('Saltos de escala detectados (|retorno| > 40 %%): %s', malos)
    else:
        logging.info('Validación de escala: sin saltos anómalos en %s ficheros.', len(tickers))


# ----------------------------------------------------------------- el scanner
def fila_base(tk: str, r: Any, div: str, uni_ix: pd.Series, meta: pd.DataFrame) -> dict:
    m_ = meta.loc[tk]
    sec = SEC_FIX.get(m_.sector, m_.sector)
    return dict(pais=m_.pais_cotizacion, sector=sec, nombre=m_.nombre_wiki, ticker=tk,
                indices=uni_ix.get(tk, ''),
                divisa=div,
                fecha_senal=str(pd.Timestamp(r.date).date()),
                P0=round(r.P0, 4), P1=round(r.P1, 4), P2=round(r.P2, 4),
                W1=round(r.W1, 4), W2=round(r.W2, 4),
                caida_pct=round(float(r.caida250_P0), 1),
                fecha_top=str(pd.Timestamp(r.fecha_top).date()) if pd.notna(r.fecha_top) else '',
                SL_P0=round(r.stop, 4), SL_TStop=round(r.tstop, 4),
                TP1=round(r.tp1, 4), TP2=round(r.tp2, 4), TP3=round(r.tp3, 4), TP4=round(r.tp4, 4))


def estado_desde(df: pd.DataFrame, eb: int, sl_p0: float, sl_tstop: float,
                 tps: tuple, entrada: float):
    """Recorre eb→fin y enumera los eventos en orden temporal."""
    l, h, c = df['low'].values, df['high'].values, df['close'].values
    evs = []
    for j in range(eb, len(df)):
        if l[j] <= sl_p0:
            evs.append((j, f'SL P0 tocado el {df.index[j].date()}'))
        if l[j] <= sl_tstop:
            evs.append((j, f'SL TStop tocado el {df.index[j].date()}'))
        for k in range(4):
            if h[j] >= tps[k]:
                evs.append((j, f'TP{k+1} alcanzado el {df.index[j].date()}'))
    evs.sort(key=lambda x: x[0])
    vistos, cadena = set(), []
    for _, txt in evs:
        clave = txt.split(' el ')[0]
        if clave not in vistos:
            vistos.add(clave)
            cadena.append(txt)
    cierre = c[-1]
    var = (cierre / entrada - 1.0) * 100.0
    return ('; '.join(cadena) if cadena else 'sin eventos'), cierre, var


def ejecutar_scan(uni: pd.DataFrame, meta: pd.DataFrame):
    """Barrido completo del universo. Devuelve (ent, pen, vig, univ, n_val)."""
    uni_ix = uni.set_index('ticker').indices
    entradas, pendientes, vigilancia, huecos = [], [], [], []
    senales_tot, senales_filtro, n_val = 0, 0, 0

    for tk in uni.ticker:
        try:
            df = pd.read_csv(CONST / f'{tk}.csv', parse_dates=['date']
                             ).set_index('date').sort_index().astype(float)
        except FileNotFoundError:
            continue
        df.index = pd.DatetimeIndex(df.index)
        n = len(df)
        if n < MIN_VELAS:
            logging.info('%s: histórico insuficiente (%s velas); se omite.', tk, n)
            continue
        n_val += 1
        sig = ew.compute_signals(df, verbose=False)
        sig = sig[sig.direction == 1]
        if not len(sig):
            continue
        lv = tr.add_levels(df, sig)
        gg = sig.merge(lv.reset_index(), on='bar', how='left')
        gg['trig_T12'] = [tr.trig_bar(df, r, 'T12') for r in gg.itertuples()]
        senales_tot += len(gg)
        gg = gg[gg.caida250_P0 > UMBRAL_CAIDA]
        senales_filtro += len(gg)

        div = 'GBP' if tk.endswith('.L') else 'EUR'
        corte = df.index[-1] - pd.Timedelta(days=DIAS_VALIDACION)
        o = df['open'].values

        for r in gg.itertuples():
            tb = int(r.trig_T12)
            if tb == -2:
                continue
            base = fila_base(tk, r, div, uni_ix, meta)
            if tb >= 0:
                eb = tb + 1
                if eb <= n - 1:
                    px = float(o[eb])
                    if px <= base['SL_P0']:
                        huecos.append(dict(ticker=tk, fecha_entrada=str(df.index[eb].date()),
                                           entrada=round(px, 4), SL_P0=base['SL_P0']))
                        continue
                    if df.index[eb] >= corte:
                        est, cierre, var = estado_desde(df, eb, base['SL_P0'], base['SL_TStop'],
                                                        (base['TP1'], base['TP2'],
                                                         base['TP3'], base['TP4']), px)
                        entradas.append({**base,
                                         'fecha_disparo': str(df.index[tb].date()),
                                         'fecha_entrada': str(df.index[eb].date()),
                                         'entrada': round(px, 4),
                                         'hueco_TStop': 'sí' if px <= base['SL_TStop'] else 'no',
                                         'dias_desde_entrada': n - 1 - eb,
                                         'estado': est,
                                         'cierre_actual': round(float(cierre), 4),
                                         'var_desde_entrada_pct': round(var, 2)})
                    continue
                # eb == n → disparo en la última sesión: entrada prevista en la próxima apertura
                pendientes.append({**base, 'fecha_disparo': str(df.index[tb].date()),
                                   'entrada_prevista': 'próxima apertura'})
            else:  # tb == -1: sin disparo ni invalidación; ¿ventana aún abierta?
                if r.bar + tr.TRIG_VALID > n - 1:
                    restantes = r.bar + tr.TRIG_VALID - (n - 1)
                    vigilancia.append({**base,
                                       'nivel_T1': round(float(r.t1_lvl), 4),
                                       'nivel_T2': round(float(r.t2_lvl), 4),
                                       'sesiones_ventana_restantes': int(restantes),
                                       'condicion': 'cierre > T1 y cierre ≥ T2 '
                                                    '(o invalidación: cierre < P2)'})

    ent = pd.DataFrame(entradas).sort_values('fecha_entrada', ascending=False).reset_index(drop=True) \
        if entradas else pd.DataFrame()
    pen = pd.DataFrame(pendientes).sort_values('fecha_disparo', ascending=False).reset_index(drop=True) \
        if pendientes else pd.DataFrame()
    vig = pd.DataFrame(vigilancia).sort_values('fecha_senal', ascending=False).reset_index(drop=True) \
        if vigilancia else pd.DataFrame()

    logging.info('Valores escaneados: %s | señales largas: %s | con caída > %.0f %%: %s',
                 n_val, senales_tot, UMBRAL_CAIDA, senales_filtro)
    logging.info('Entradas últimos %s días: %s | pendientes de apertura: %s | '
                 'en vigilancia: %s | hueco<SL_P0 (excluidas): %s',
                 DIAS_VALIDACION, len(ent), len(pen), len(vig), len(huecos))
    return ent, pen, vig, univ_salidas(uni, meta), n_val


def univ_salidas(uni: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    univ = uni.copy()
    univ['pais'] = univ.ticker.map(meta.pais_cotizacion)
    univ['pais_sede'] = univ.ticker.map(meta.pais_sede).fillna('')
    univ['sector'] = univ.ticker.map(meta.sector).map(lambda s: SEC_FIX.get(s, s))
    univ['fuente_sector'] = univ.ticker.map(meta.fuente_sector)
    return univ


def params_salidas(fecha: str) -> pd.DataFrame:
    return pd.DataFrame({'parametro': [
        'Estrategia', 'Universo', 'Señal', 'Filtro de caída previa', 'Disparador T12',
        'Ventana de disparo', 'Invalidación', 'Entrada',
        'SL escenario P0', 'SL escenario TStop', 'TP1', 'TP2', 'TP3', 'TP4',
        'Tamaño', 'Costes', 'Última sesión analizada', 'Divisas',
        'Composición', 'Backtest de referencia (T12, caída>20 %, 5 años)'],
        'valor': [
        'Elliott Wave Impulse, sólo largos, disparador T12',
        '231 valores: DAX 40 + CAC 40 + Euro Stoxx 50 + FTSE 100 + FTSE MIB',
        'Zigzag 10 velas; patrón W2/W1 ∈ {0,50 · 0,618 · 0,764 · 0,854} ±5 %; confirmación en P2',
        'Caída > 20 %: (máx high de 250 velas anteriores a P0 − P0)/máx · 100',
        'cierre > T1 (último swing menor de la onda 2) Y cierre ≥ T2 = P1 − 0,50·W1',
        '[barra de señal, +15 velas]',
        'cierre < P2 dentro de la ventana (onda 2 rota)',
        'a mercado en la apertura siguiente al cierre disparador',
        'SL_P0 = P0 (origen del impulso)',
        'SL_TStop = P2 − 0,30·W2',
        'TP1 = P2 + 1,618·W2', 'TP2 = P2 + 2,000·W2', 'TP3 = P2 + 2,618·W2', 'TP4 = P2 + 3,236·W2',
        'floor(5.000 € / precio) acciones',
        '0,35 % ida y vuelta',
        str(fecha),
        'GBP para valores de Londres (.L), EUR el resto',
        'Composición ACTUAL de los índices (nov-2026): sesgo de supervivencia no corregido',
        'GLOBAL P0+TP4: 234 ops · win rate 51 % · PF 1,97 | P0+TP3: 238 ops · PF 1,68']})


# -------------------------------------------------------------------- salidas
def al_final(df: pd.DataFrame) -> pd.DataFrame:
    """Detalle de la señal (fecha_senal, P0-P2, W1-W2, caída, top) al final de la hoja.
    Sólo afecta al Excel; los CSV conservan el orden original de columnas."""
    if df.empty:
        return df
    cols = [c for c in df.columns if c not in MOVER] + [c for c in MOVER if c in df.columns]
    return df[cols]


MOVER = ['fecha_senal', 'P0', 'P1', 'P2', 'W1', 'W2', 'caida_pct', 'fecha_top']


def escribir_salidas(ent, pen, vig, univ, params, fecha: str) -> None:
    """Excel (5 hojas) + CSVs fechados y copias fijas scanner_T12_EU_ultimo.*"""
    SALIDAS.mkdir(parents=True, exist_ok=True)
    stem = SALIDAS / f'scanner_T12_EU_{fecha}'

    with pd.ExcelWriter(f'{stem}.xlsx', engine='openpyxl') as xw:
        al_final(ent).to_excel(xw, sheet_name='Entradas_30d', index=False)
        al_final(pen).to_excel(xw, sheet_name='Pendientes_apertura', index=False)
        al_final(vig).to_excel(xw, sheet_name='Vigilancia', index=False)
        univ.to_excel(xw, sheet_name='Universo', index=False)
        params.to_excel(xw, sheet_name='Parametros', index=False)
        for sh in xw.book.sheetnames:
            ws = xw.book[sh]
            ws.freeze_panes = 'A2'
            for col in ws.columns:
                w = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 9), 46)
            for c in ws[1]:
                from openpyxl.styles import Font
                c.font = Font(bold=True)

    csvs = []
    if len(ent):
        ent.to_csv(f'{stem}_entradas_30d.csv', index=False)
        csvs.append('_entradas_30d')
    if len(pen):
        pen.to_csv(f'{stem}_pendientes.csv', index=False)
        csvs.append('_pendientes')
    if len(vig):
        vig.to_csv(f'{stem}_vigilancia.csv', index=False)
        csvs.append('_vigilancia')
    univ.to_csv(f'{stem}_universo.csv', index=False)
    csvs.append('_universo')

    shutil.copyfile(f'{stem}.xlsx', SALIDAS / 'scanner_T12_EU_ultimo.xlsx')
    for suf in csvs:
        shutil.copyfile(f'{stem}{suf}.csv', SALIDAS / f'scanner_T12_EU_ultimo{suf}.csv')
    logging.info('Salidas escritas en %s (fecha %s)', SALIDAS, fecha)


# -------------------------------------------------------------------- alertas
def msg_entrada(r) -> str:
    return (f"📈 <b>NUEVA OPERACIÓN T12</b> — {esc(r.nombre)} (<code>{esc(r.ticker)}</code>)\n"
            f"Entrada el <b>{esc(r.fecha_entrada)}</b> a {num(r.entrada)} {esc(r.divisa)}\n"
            f"Señal {esc(r.fecha_senal)} · caída previa {num(r.caida_pct)} % "
            f"(top {esc(r.fecha_top)})\n"
            f"SL_P0 {num(r.SL_P0)} · SL_TStop {num(r.SL_TStop)}\n"
            f"TP1 {num(r.TP1)} | TP2 {num(r.TP2)} | TP3 {num(r.TP3)} | TP4 {num(r.TP4)}")


def msg_pendiente(r) -> str:
    return (f"⚡ <b>DISPARO T12 EN LA ÚLTIMA SESIÓN</b> — {esc(r.nombre)} "
            f"(<code>{esc(r.ticker)}</code>)\n"
            f"Disparo {esc(r.fecha_disparo)} · entrada prevista en la próxima apertura\n"
            f"Caída previa {num(r.caida_pct)} % · SL_P0 {num(r.SL_P0)} · "
            f"SL_TStop {num(r.SL_TStop)}\n"
            f"TP1 {num(r.TP1)} | TP2 {num(r.TP2)} | TP3 {num(r.TP3)} | TP4 {num(r.TP4)}")


def msg_vigilancia(r) -> str:
    return (f"👁 <b>VIGILANCIA T12</b> — {esc(r.nombre)} (<code>{esc(r.ticker)}</code>)\n"
            f"Señal {esc(r.fecha_senal)} · ventana abierta "
            f"({r.sesiones_ventana_restantes} sesiones restantes)\n"
            f"Dispara si cierre &gt; T1 {num(r.nivel_T1)} y cierre ≥ T2 {num(r.nivel_T2)}\n"
            f"SL_P0 {num(r.SL_P0)} · SL_TStop {num(r.SL_TStop)} · TP1 {num(r.TP1)}")


def msg_evento(r, evento: str, fecha: str) -> str:
    icono = '🛑' if evento.startswith('SL') else '🎯'
    return (f"{icono} <b>{esc(evento)} el {esc(fecha)}</b> — {esc(r.nombre)} "
            f"(<code>{esc(r.ticker)}</code>)\n"
            f"Entrada {num(r.entrada)} {esc(r.divisa)} el {esc(r.fecha_entrada)}\n"
            f"Cierre {num(r.cierre_actual)} ({r.var_desde_entrada_pct:+.2f} % desde entrada)\n"
            f"SL_P0 {num(r.SL_P0)} · SL_TStop {num(r.SL_TStop)} · "
            f"TP1 {num(r.TP1)} · TP4 {num(r.TP4)}")


def marcar_y_encolar(estado: dict[str, Any], ent, pen, vig) -> int:
    """Compara las señales actuales con lo ya notificado y encola las novedades."""
    claves = []
    for r in ent.itertuples():
        claves.append(('entradas', f'{r.ticker}#{r.fecha_entrada}', msg_entrada(r)))
    for r in pen.itertuples():
        claves.append(('pendientes', f'{r.ticker}#{r.fecha_disparo}', msg_pendiente(r)))
    for r in vig.itertuples():
        claves.append(('vigilancia', f'{r.ticker}#{r.fecha_senal}', msg_vigilancia(r)))
    for r in ent.itertuples():
        for evento, fecha in EV_RE.findall(str(r.estado)):
            claves.append(('eventos', f'{r.ticker}#{evento}#{fecha}', msg_evento(r, evento, fecha)))

    if estado['actualizado'] is None and not AVISAR_PRIMERA_EJECUCION:
        for cat, clave, _ in claves:
            estado['notificados'][cat][clave] = 'referencia inicial'
        logging.info('Primera ejecución: %s referencias iniciales fijadas sin aviso '
                     '(AVISAR_PRIMERA_EJECUCION = False).', len(claves))
        return 0

    en_cola = {c['clave'] for c in estado['cola_envio']}
    nuevas = 0
    for cat, clave, texto in claves:
        if clave in estado['notificados'][cat] or clave in en_cola:
            continue
        estado['cola_envio'].append({'clave': f'{cat}#{clave}', 'texto': texto,
                                     'creado': ahora_local().isoformat(timespec='seconds')})
        en_cola.add(clave)
        nuevas += 1
    return nuevas


def msg_resumen(ultima: str, n_val: int, ent, pen, vig, nuevas: int, enviados: int) -> str:
    return (f"✅ <b>Scanner T12 ejecutado</b>\n"
            f"Datos hasta {esc(ultima)} · {n_val} valores analizados\n"
            f"Entradas {DIAS_VALIDACION} d: <b>{len(ent)}</b> | pendientes apertura: "
            f"{len(pen)} | vigilancia: {len(vig)}\n"
            f"Novedades: {nuevas} nuevas · {enviados} enviadas")


# ---------------------------------------------------------------------- ciclo
def cargar_universo():
    uni = pd.read_csv(DATOS / 'universo_constituyentes.csv')
    meta = pd.read_csv(DATOS / 'meta_tickers.csv').set_index('ticker')
    return uni, meta


def ciclo(estado: dict[str, Any]) -> dict[str, Any]:
    """Un ciclo completo: actualizar datos → escanear → salidas → alertas."""
    uni, meta = cargar_universo()
    ultima = actualizar_datos(sorted(uni.ticker))
    ent, pen, vig, univ, n_val = ejecutar_scan(uni, meta)
    escribir_salidas(ent, pen, vig, univ, params_salidas(ultima), ultima)

    if len(ent):
        cols = ['pais', 'sector', 'nombre', 'ticker', 'fecha_entrada', 'entrada', 'SL_P0',
                'SL_TStop', 'TP1', 'TP4', 'caida_pct', 'estado']
        print('\n=== ENTRADAS ÚLTIMOS %s DÍAS ===' % DIAS_VALIDACION)
        print(ent[cols].to_string(index=False))

    nuevas = marcar_y_encolar(estado, ent, pen, vig)
    logging.info('Alertas nuevas detectadas: %s (cola: %s)', nuevas, len(estado['cola_envio']))
    enviados = enviar_pendientes(estado)

    if ENVIAR_RESUMEN:
        enviar_telegram(msg_resumen(ultima, n_val, ent, pen, vig, nuevas, enviados))

    estado['actualizado'] = ahora_local().isoformat(timespec='seconds')
    estado['ultima_sesion'] = ultima
    guardar_estado(estado)
    return dict(ultima=ultima, n_val=len(uni), ent=len(ent), pen=len(pen),
                vig=len(vig), nuevas=nuevas, enviados=enviados)


def main() -> None:
    una_vez = "--una-vez" in sys.argv[1:] or os.environ.get("EJECUCION_UNICA", "") == "1"

    logging.info("=== Scanner T12 Europa (señales → Telegram) ===")
    logging.info("Datos: %s | Salidas: %s", CONST, SALIDAS)
    logging.info("Zona horaria: %s | Umbral caída: %s %% | Validación: %s días",
                 ZONA_HORARIA, UMBRAL_CAIDA, DIAS_VALIDACION)

    if una_vez:
        # Modo GitHub Actions: un ciclo y fuera; el estado queda guardado en
        # estado_scanner.json y el workflow lo sube con un commit.
        logging.info("Modo: un solo ciclo (--una-vez).")
        estado = cargar_estado()
        ciclo(estado)
        return

    # Modo PC: bucle continuo como la versión anterior.
    logging.info("Frecuencia: cada %s minutos (modo bucle local).", INTERVALO_MINUTOS)
    logging.info("Para detener, pulsa Ctrl+C en esta ventana.")

    estado = cargar_estado()
    while True:
        inicio = time.monotonic()
        try:
            ciclo(estado)
        except KeyboardInterrupt:
            logging.info("Programa detenido por el usuario.")
            break
        except Exception:
            logging.exception("Error inesperado en el ciclo.")

        transcurrido = time.monotonic() - inicio
        espera = max(1, INTERVALO_MINUTOS * 60 - int(transcurrido))
        proxima = ahora_local() + timedelta(seconds=espera)
        logging.info("Próxima consulta aproximadamente a las %s", proxima.strftime("%H:%M:%S"))
        try:
            time.sleep(espera)
        except KeyboardInterrupt:
            logging.info("Programa detenido por el usuario.")
            break


if __name__ == "__main__":
    main()
