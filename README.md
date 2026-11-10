# Scanner T12 Europa — señales diarias por Telegram (GitHub Actions)

Scanner diario de **señales largas Elliott Wave Impulse** (disparador **T12**,
caída previa **> 20 %** en 250 sesiones) sobre los **231 constituyentes** de
DAX 40 + CAC 40 + Euro Stoxx 50 + FTSE 100 + FTSE MIB, con **alertas por
Telegram** y ejecución automática en **GitHub Actions** (funciona con el PC
apagado).

Es la misma lógica validada del scanner T12 (backtest de referencia: P0+TP4,
234 ops · win rate 51 % · PF 1,97 en 5 años).

---

## Qué hace cada ejecución diaria

1. **Actualiza los datos**: fusión incremental de la última mensualidad de
   Yahoo Finance (OHLC diario sin ajustar, la misma fuente del backtest) sobre
   el histórico que viaja en `datos/constituyentes/`. Una vela de "hoy"
   anterior a las 18:00 (España) se descarta por ser sesión incompleta.
   **Guarda de escala**: a veces Yahoo devuelve los valores de Londres en
   peniques (GBp) en vez de libras (le pasó al histórico el 09-11-2026); cada
   descarga se normaliza por `meta.currency` y se coteja con lo almacenado
   (ratio mediano en fechas comunes), con validación final de saltos anómalos.
2. **Escanea los 231 valores** con el motor `ew_impulse` + `ew_triggers`.
3. **Escribe las salidas** en `salidas/`:
   - `scanner_T12_EU_{FECHA}.xlsx` (5 hojas: Entradas_30d, Pendientes_apertura,
     Vigilancia, Universo, Parametros) + CSVs por categoría.
   - Copias fijas `scanner_T12_EU_ultimo.*` (siempre la última ejecución).
4. **Envía las novedades por Telegram** (deduplicadas con `estado_scanner.json`):

   | Alerta | Significado |
   |---|---|
   | 📈 **Nueva operación** | Entrada dentro de los últimos 30 días (con SL, TP y niveles) |
   | ⚡ **Disparo T12** | El cierre de la última sesión disparó; entrada prevista en la próxima apertura |
   | 👁 **Vigilancia** | Señal con ventana de disparo abierta aún sin disparar (niveles T1/T2) |
   | 🎯 **TPx alcanzado** | Un objetivo (TP1..TP4) de una operación vigente fue alcanzado |
   | 🛑 **SL tocado** | El stop (SL_P0 o SL_TStop) de una operación vigente fue tocado |
   | ✅ **Resumen** | Pulso diario: última sesión, nº de entradas/pendientes/vigilancia |

5. **Commitea el estado y las salidas** al repositorio (`estado_scanner.json`,
   `salidas/*_ultimo.*` y los CSV de datos actualizados), de modo que la
   siguiente ejecución parte de donde quedó la anterior. Además sube
   `salidas/` como *artifact* descargable (90 días).

---

## Puesta en marcha (una sola vez, ~10 minutos)

### 1. Crear el repositorio y subir estos archivos

Crea un repo (privado recomendado) y sube TODO el contenido de esta carpeta,
incluidas las carpetas ocultas (`datos/`, `salidas/`, `.github/`). El
histórico de `datos/constituyentes/` pesa ~31 MB: súbelo tal cual y cada
ejecución diaria solo añadirá una fila por valor.

```bash
git init
git add -A
git commit -m "Scanner T12 Europa"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/scanner-t12.git
git push -u origin main
```

> Si prefieres la web de GitHub: "Upload files" y arrastra todo el contenido
> (GitHub acepta arrastrar carpetas completas).

### 2. Crear el bot de Telegram y obtener las credenciales

1. Habla con **@BotFather** en Telegram → `/newbot` → elige nombre y usuario.
2. Copia el **token** que te da (formato `123456789:AAE...`).
3. Manda cualquier mensaje a tu bot nuevo (p. ej. "hola") y luego abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → busca `"chat":{"id":`
   → ese número (p. ej. `123456789`) es tu **chat_id**.
   (Alternativa: escribe a **@userinfobot** y te da tu id.)

### 3. Configurar los Secrets del repositorio

En tu repo: **Settings → Secrets and variables → Actions → New repository
secret**. Crea dos:

| Name | Secret |
|---|---|
| `TELEGRAM_TOKEN` | el token del BotFather |
| `TELEGRAM_CHAT_ID` | tu chat_id |

### 4. Activar Actions

Pestaña **Actions** del repo → si te lo pide, "I understand my workflows, go
ahead and enable them".

### 5. Primera prueba

En **Actions → Scanner T12 Europa → Run workflow**. Verás el log completo y,
si todo va bien, el mensaje ✅ de resumen en Telegram.

**Importante**: este repositorio ya incluye un `estado_scanner.json` con la
referencia de las señales vigentes a 2026-11-10 (9 entradas, 1 vigilancia,
6 eventos), de modo que la primera ejecución en GitHub sólo te avisará de lo
**nuevo**. Si quieres recibir además lo ya vigente al arrancar, borra ese
archivo antes del primer push (o pon `AVISAR_PRIMERA_EJECUCION = True` y
bórralo). Sin estado, la primera ejecución fija la referencia en silencio.

### 6. Horario

El cron está en `.github/workflows/scanner.yml`:

```yaml
- cron: '30 17 * * 1-5'   # L-V a las 17:30 UTC = 18:30 España (invierno) / 19:30 (verano)
```

Puedes cambiarlo; los mercados europeos cierran a las 17:30 locales, así que
cualquier hora ≥ 18:00 España es segura. GitHub puede retrasar el cron unos
minutos: es normal y no afecta al resultado.

---

## Uso local (opcional)

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python scanner_t12.py --una-vez   # un ciclo y fuera
python scanner_t12.py             # bucle cada INTERVALO_MINUTOS (60 min)
```

En local se reutilizan los mismos datos y el mismo `estado_scanner.json`.

---

## Ajustes frecuentes (`scanner_t12.py`, zona CONFIGURACIÓN)

| Parámetro | Por defecto | Qué hace |
|---|---|---|
| `UMBRAL_CAIDA` | 20.0 | Caída previa mínima (%) para aceptar la señal |
| `DIAS_VALIDACION` | 30 | Ventana de entradas que muestra el scanner |
| `ENVIAR_RESUMEN` | True | Mensaje ✅ diario aunque no haya novedades |
| `AVISAR_PRIMERA_EJECUCION` | False | Que la 1ª ejecución envíe lo ya vigente |
| `HORA_SESION_CERRADA` | (18, 0) | Hora (España) a partir de la cual la vela de hoy es fiable |
| `REPARAR_SI_ANTIGUO` | 15 | Días sin datos de un valor → re-descarga completa |
| `INTERVALO_MINUTOS` | 60 | Solo modo bucle local |

Los niveles y fórmulas (SL_P0 = P0, SL_TStop = P2 − 0,30·W2,
TP1..TP4 = P2 + {1,618 · 2,0 · 2,618 · 3,236}·W2, T2 = P1 − 0,50·W1) son los
del indicador y no se tocan.

---

## Estructura del repositorio

```
├── .github/workflows/scanner.yml   # cron diario + commit de estado + artifact
├── scanner_t12.py                  # programa principal (datos → scan → Telegram)
├── ew_impulse.py                   # motor de señales (réplica del indicador Pine)
├── ew_triggers.py                  # disparadores T1/T2/T12, caída previa, niveles
├── datos/
│   ├── universo_constituyentes.csv # 231 tickers + índices
│   ├── meta_tickers.csv            # nombre, país, sector
│   └── constituyentes/*.csv        # OHLC diario histórico (se actualiza a diario)
├── salidas/                        # Excel + CSVs (se commitean las copias "ultimo")
├── estado_scanner.json             # alarmas ya enviadas (deduplicación)
├── requirements.txt
└── README.md
```

---

## Problemas frecuentes

- **No llegan mensajes** → revisa los dos Secrets (nombres exactos), que hayas
  escrito un mensaje al bot, y el log del run (busca "Telegram rechazó" o
  "Faltan TELEGRAM_TOKEN"). Las alertas que fallan quedan en cola y se
  reenvían en la siguiente ejecución.
- **"Sin cambios que commitear"** en el log → ejecución correcta; simplemente
  no había nada nuevo (ni señales ni datos).
- **Un valor con datos antiguos** → el propio script lo detecta
  (más de `REPARAR_SI_ANTIGUO` días) y lo re-descarga completo.
- **Actions deja de ejecutarse** → GitHub desactiva los cron tras 60 días sin
  actividad en el repo; entra y dale a "Run workflow" para reactivarlos.
- **Yahoo bloquea peticiones puntualmente** → el script reintenta (query1/
  query2 × 2) y mantiene el histórico anterior; el scan continúa con datos de
  la última sesión disponible.
