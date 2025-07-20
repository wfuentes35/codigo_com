# config.py  –  parámetros globales y logger
# =====================================================================
import os, logging
from dotenv import load_dotenv
from binance.client import Client
from telegram import Bot
from typing import Optional
import asyncio

load_dotenv()

# ───── Credenciales ────────────────────────────────────────────────
API_KEY        = os.getenv("BINANCE_API_KEY")
API_SECRET     = os.getenv("BINANCE_API_SECRET")
client         = Client(API_KEY, API_SECRET)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
telegram_bot   = Bot(token=TELEGRAM_TOKEN)

# ───── Logger ───────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s - %(levelname)s - %(message)s",
    filename= "app.log",
    filemode= "a")
logger = logging.getLogger(__name__)

# silenciar ruido extra
for noisy in ("httpx", "telegram._network.httpx_backend",
              "telegram.ext._application", "telegram.ext._basehandler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───── Parámetros globales del bot ──────────────────────────────────
DRY_RUN                  = False          # no envía orden real si True
# tamaños
MIN_ENTRY_USDT           = 20
MIN_SYNC_USDT            = 10.0
# stops
TRAILING_USDT            = 2.0            # colchón ATR
STOP_DELTA_USDT          = 1              # trailing fijo en USDT
STOP_ABS_USDT            = 18             # stop absoluto en USDT
STOP_ABS_HIGH_FACTOR     = 51.0           # stop por cantidad si precio alto
STOP_ABS_HIGH_THRESHOLD  = 55.0           # umbral de precio alto USDT
# modo liviano
LIGHT_MODE               = True          # True → Fase2 sólo entradas
# ciclo y slots
CHECK_INTERVAL           = 600            # seg – Fase 2
SYNC_POS_INTERVAL        = 900            # seg – balance sync
NEW_LISTINGS_INTERVAL    = 300            # seg – Fase 0
INITIAL_PRECANDIDATES_LIMIT = 5
MAX_TRACKED_COINS        = 20
PRECANDIDATES_PER_FREED_COIN = 4
COOLDOWN_CANDLES         = 2
# --- control de saldo insuficiente ---
INSUFFICIENT_BALANCE_COOLDOWN = 600      # 10 min
NO_BALANCE_UNTIL = 0.0                   # timestamp; 0 = sin cooldown
# límite de posiciones abiertas simultáneas
MAX_OPERACIONES_ACTIVAS  = 10
# EMA / HMA
EMA_SHORT  = 8
EMA_LONG   = 24
MIN_24H_VOL_USDT = 5_000_000
ETA_MAX_BARS     = 12
RVOL_MIN         = 2.0
CVD_MIN          = 0

# ───── K-line intervalos ────────────────────────────────────────────
KLINE_INTERVAL_FASE0 = Client.KLINE_INTERVAL_15MINUTE
KLINE_INTERVAL_FASE1 = Client.KLINE_INTERVAL_4HOUR
KLINE_INTERVAL_FASE2 = Client.KLINE_INTERVAL_4HOUR
# ─────────────────────────────────────────────────────────────
#  Ajustes DINÁMICOS (modificables vía Telegram) – Fase 0 y tamaño entrada
# ─────────────────────────────────────────────────────────────
FASE0_SETTINGS = {
    "interval":   "5m",      # 1m, 5m, 15m  (tiempo de k-linea que revisa Fase0)
    "min_vol":    250_000,    # volumen mínimo USDT en esa vela
    "min_ratio":  0.85,      # % de compras (0–1)
}

def update_fase0_setting(key: str, value: str) -> Optional[str]:
    """Modifica FASE0_SETTINGS; devuelve None si OK, o mensaje de error."""
    if key not in FASE0_SETTINGS:
        return f"Clave no válida: {key}"
    if key == "interval" and value not in {"1m", "5m", "15m"}:
        return "Interval debe ser 1m, 5m o 15m"

    if key in {"min_vol", "min_ratio"}:
        try:
            value = float(value)
        except ValueError:
            return f"{key} debe ser numérico"

    FASE0_SETTINGS[key] = value
    return None  # sin error

# ------------- tamaño fijo de entrada (MIN_ENTRY_USDT) -------------
def update_min_entry_usdt(new_val: str) -> Optional[str]:
    """Cambia el tamaño fijo de cada compra."""
    global MIN_ENTRY_USDT
    try:
        MIN_ENTRY_USDT = float(new_val)
    except ValueError:
        return "Debe ser numérico"
    return None

# ------------- límite dinámico de operaciones activas -------------
def update_max_operaciones_activas(new_val: str) -> Optional[str]:
    """Cambia el número máximo de operaciones simultáneas."""
    global MAX_OPERACIONES_ACTIVAS
    try:
        MAX_OPERACIONES_ACTIVAS = int(new_val)
    except ValueError:
        return "Debe ser numérico"
    return None

# config.py  (al final del archivo)


PAUSED = asyncio.Event()
PAUSED.set()                 # arranca activo

SHUTTING_DOWN = asyncio.Event()
