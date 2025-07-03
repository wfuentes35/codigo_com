
"""Funciones auxiliares para el bot.

Contiene indicadores técnicos, wrappers de Binance con control de
concurrencia y un sistema antiflood para Telegram.  Se implementan
pequeños cachés con TTL para reducir peticiones repetitivas.
"""

# utils.py – indicadores, Binance helpers y antiflood Telegram
# ============================================================

import asyncio
import pandas as pd
import numpy as np
import telegram.error
from binance import exceptions as bexc
from config import (
    client, logger, telegram_bot, TELEGRAM_CHAT_ID,
    STOP_ABS_HIGH_FACTOR, STOP_ABS_HIGH_THRESHOLD,
)

# ─────────────────────────────────────────────────────────────
#  Cachés simples con TTL
# ─────────────────────────────────────────────────────────────

_SYMBOLS_CACHE: dict[str, tuple[float, list[str]]] = {}
_HIST_CACHE: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}

# TTL por defecto
SYMBOLS_TTL = 1800  # seg – listado de pares USDT
HIST_TTL    = 120   # seg – históricos de precios

# ─────────────────────────────────────────────────────────────
#  Cachés simples con TTL
# ─────────────────────────────────────────────────────────────

_SYMBOLS_CACHE: dict[str, tuple[float, list[str]]] = {}
_HIST_CACHE: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}

# TTL por defecto
SYMBOLS_TTL = 1800  # seg – listado de pares USDT
HIST_TTL    = 120   # seg – históricos de precios

# ─────────────────────────────────────────────────────────────
#  Antiflood Telegram
# ─────────────────────────────────────────────────────────────
_LOCKS_BY_LOOP: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

async def _tg_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _LOCKS_BY_LOOP.setdefault(loop, asyncio.Lock())

async def send_telegram_message(msg: str):
    async with await _tg_lock():
        for att in range(3):
            try:
                await telegram_bot.send_message(TELEGRAM_CHAT_ID, text=msg)
                break
            except telegram.error.TimedOut:
                logger.warning(f"TG TimedOut {att+1}/3")
                await asyncio.sleep(4)
            except Exception as e:
                logger.error(f"TG error: {e}")
                break
        await asyncio.sleep(2.5)

# ─────────────────────────────────────────────────────────────
#  Binance semáforo (un semáforo por event-loop)
# ─────────────────────────────────────────────────────────────
_BIN_SEM_BY_LOOP: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

async def _bin_sem() -> asyncio.Semaphore:
    """
    Devuelve un semáforo (máx. 5 peticiones concurrentes) ligado
    al event-loop actual.  Evita “Future attached to a different loop”.
    """
    loop = asyncio.get_running_loop()
    return _BIN_SEM_BY_LOOP.setdefault(loop, asyncio.Semaphore(5))

# ─────────────────────────────────────────────────────────────
#  Binance helpers
# ─────────────────────────────────────────────────────────────
async def get_all_usdt_symbols(ttl: int = SYMBOLS_TTL) -> list[str]:
    """Lista de pares *USDT* filtrados. Usa caché con TTL en segundos."""
    now = asyncio.get_event_loop().time()
    ts, cached = _SYMBOLS_CACHE.get("ts", 0.0), _SYMBOLS_CACHE.get("data")
    if cached and now - ts < ttl:
        return cached

    async with await _bin_sem():
        info = await asyncio.to_thread(client.get_exchange_info)

    excluded = {"BUSD", "USDC", "TUSD", "EUR", "AUD", "BRL", "IDRT",
                "PAX", "USDP", "DAI", "XUSD", "USD1", "VIDT", "FDUSD"}

    symbols = [
        s["symbol"] for s in info["symbols"]
        if (
            s["status"] == "TRADING"
            and s["isSpotTradingAllowed"]
            and s["quoteAsset"] == "USDT"
            and s["baseAsset"] not in excluded
        )
    ]
    _SYMBOLS_CACHE["ts"] = now
    _SYMBOLS_CACHE["data"] = symbols
    return symbols

async def get_historical_data(symbol: str, interval: str, limit: int = 100,
                              ttl: int = HIST_TTL) -> pd.DataFrame | None:
    """Obtiene klines de Binance usando caché con TTL."""
    key = (symbol, interval, limit)
    now = asyncio.get_event_loop().time()
    if key in _HIST_CACHE:
        ts, cached = _HIST_CACHE[key]
        if now - ts < ttl:
            return cached

    try:
        async with await _bin_sem():
            klines = await asyncio.to_thread(
                client.get_klines,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "tbbav", "tbqav", "ignore"
        ])
        df[["open", "high", "low", "close", "volume"]] = (
            df[["open", "high", "low", "close", "volume"]].astype(float)
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        _HIST_CACHE[key] = (now, df)
        return df

    except bexc.BinanceAPIException as e:
        logger.error(f"BinanceAPIException {symbol}: {e}")
    except Exception as e:
        logger.error(f"Históricos {symbol}: {e}")

    return None

async def check_volume(symbol: str, min_usdt: float = 300) -> bool:
    try:
        async with await _bin_sem():
            ticker = await asyncio.to_thread(client.get_ticker, symbol=symbol)
        return float(ticker["quoteVolume"]) > min_usdt
    except Exception as e:
        logger.error(f"Volumen {symbol}: {e}")
        return False

# ─────────────────────────────────────────────────────────────
#  Indicadores técnicos
# ─────────────────────────────────────────────────────────────
def hull_moving_average(series, window: int):
    def wma(x, n):
        weights = np.arange(1, n + 1)
        return x.rolling(n).apply(
            lambda v: np.dot(v, weights) / weights.sum(), raw=True
        )

    half_w = max(1, window // 2)
    root_w = max(1, int(np.sqrt(window)))
    return wma(2 * wma(series, half_w) - wma(series, window), root_w)

def rsi(series, period: int = 14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)

# ─────────────────────────────────────────────────────────────
#  Stops y triggers
# ─────────────────────────────────────────────────────────────
def atr_stop(df: pd.DataFrame, price: float, mult: float = 1.2, period: int = 14) -> float:
    """Calcula stop basado en ATR para ``price``."""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return price - mult * atr

def trailing_atr_trigger(rec: dict, last: float, buffer: float) -> bool:
    """Actualiza ``rec['stop']`` y devuelve ``True`` si se activa."""
    if last > rec["entry_price"] + buffer:
        rec["stop"] = max(rec["stop"], last - buffer)
    return last < rec["stop"]

def delta_stop_trigger(rec: dict, last: float, delta_usdt: float) -> bool:
    """Devuelve ``True`` si el precio cae más de ``delta_usdt`` desde el máximo."""
    return last < rec["max_price"] - delta_usdt

def absolute_stop_trigger(qty: float, last: float, stop_abs_usdt: float) -> bool:
    """Devuelve ``True`` si el valor de la posición es menor que ``stop_abs_usdt``."""
    return qty * last < stop_abs_usdt

def update_light_stops(rec: dict, qty: float, price: float,
                       stop_delta_usdt: float, stop_abs_usdt: float) -> bool:
    """Actualiza max_value, Δ-stop y stop_abs. Devuelve ``True`` si se activa."""
    value = qty * price
    rec["max_value"] = max(rec.get("max_value", 0.0), value)
    rec["stop_delta"] = rec["max_value"] - stop_delta_usdt
    rec["stop_abs"] = (
        STOP_ABS_HIGH_FACTOR * qty
        if price >= STOP_ABS_HIGH_THRESHOLD
        else stop_abs_usdt
    )
    return value < rec["stop_delta"] or value < rec["stop_abs"]

# ─────────────────────────────────────────────────────────────
#  LOT_SIZE helper (stepSize cache)
# ─────────────────────────────────────────────────────────────
_STEP_CACHE: dict[str, float] = {}

async def get_step_size(symbol: str) -> float:
    if symbol in _STEP_CACHE:
        return _STEP_CACHE[symbol]

    async with await _bin_sem():
        info = await asyncio.to_thread(client.get_symbol_info, symbol=symbol)

    for flt in info["filters"]:
        if flt["filterType"] == "LOT_SIZE":
            _STEP_CACHE[symbol] = float(flt["stepSize"])
            break

    return _STEP_CACHE.get(symbol, 0.000001)

# ─────────────────────────────────────────────────────────────
#  Compat: save_log() usado por fase1/fase3
# ─────────────────────────────────────────────────────────────
def save_log(message: str) -> None:
    """Wrapper simple para mantener compatibilidad con fase1/3."""
    logger.info(message)
