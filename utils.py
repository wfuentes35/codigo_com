
"""Funciones auxiliares para el bot.

Contiene indicadores técnicos, wrappers de Binance con control de
concurrencia y un sistema antiflood para Telegram. Se implementan
pequeños cachés con TTL para reducir peticiones repetitivas.
"""

# utils.py – indicadores, Binance helpers y antiflood Telegram
# ============================================================

import asyncio
from typing import Optional

import numpy as np
import pandas as pd
import telegram.error
from binance import exceptions as bexc
from binance.client import Client
import math
from datetime import datetime, timedelta, timezone
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
HIST_TTL = 120      # seg – históricos de precios

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
                              ttl: int = HIST_TTL) -> Optional[pd.DataFrame]:
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


# ─────────────────────────────────────────────────────────────
#  Indicadores técnicos
# ─────────────────────────────────────────────────────────────

def get_bollinger_bands(series: pd.Series, period: int = 20,
                        stddev: float = 2) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Devuelve bandas de Bollinger superior, media e inferior."""
    ma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = ma + stddev * std
    lower = ma - stddev * std
    return upper, ma, lower


def get_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Índice de fuerza relativa."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def get_ema(series: pd.Series, period: int = 9) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def hull_moving_average(series: pd.Series, period: int = 9) -> pd.Series:
    """Hull Moving Average."""
    if period < 1:
        raise ValueError("period must be positive")

    def _wma(s: pd.Series, length: int) -> pd.Series:
        weights = np.arange(1, length + 1)
        return s.rolling(length).apply(
            lambda x: np.dot(x, weights) / weights.sum(),
            raw=True,
        )

    half = int(period / 2)
    sqrt_len = int(np.sqrt(period))

    wma_half = _wma(series, half)
    wma_full = _wma(series, period)

    hma_base = 2 * wma_half - wma_full
    return _wma(hma_base, sqrt_len)


def get_volume_avg(volume_series: pd.Series, period: int = 20) -> float:
    """Volumen promedio de ``period`` barras."""
    if len(volume_series) < period:
        return float(volume_series.mean())
    return float(volume_series.tail(period).mean())

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

def update_light_stops(rec: dict, qty: float, last_price: float,
                       stop_delta_usdt: float) -> bool:
    """Actualiza el trailing Δ-stop y devuelve ``True`` si se activa."""

    value_now = qty * last_price
    active = rec.setdefault("trailing_active", False)
    entry_cost = float(rec.get("entry_cost", rec.get("entry_value", 0.0)))

    # activar solo cuando el valor supere entry_cost + stop_delta + 1
    if not active:
        activation_value = entry_cost + stop_delta_usdt + 1.0
        if value_now >= activation_value:
            rec["trailing_active"] = True
            rec["max_value"] = value_now
            base_stop = entry_cost + 1.0
            rec["stop_delta"] = max(base_stop, value_now - stop_delta_usdt)
        else:
            return False

    if "max_value" not in rec:
        rec["max_value"] = value_now
    if "stop_delta" not in rec or rec["stop_delta"] is None:
        rec["stop_delta"] = rec["max_value"] - stop_delta_usdt
        return False

    if value_now > rec["max_value"]:
        rec["max_value"] = value_now
        rec["stop_delta"] = max(rec["stop_delta"], value_now - stop_delta_usdt)

    return value_now <= rec["stop_delta"]

# ─────────────────────────────────────────────────────────────
#  LOT_SIZE helper (stepSize cache)
# ─────────────────────────────────────────────────────────────
_STEP_CACHE: dict[str, float] = {}

_FILTER_CACHE: dict[str, tuple[float, float]] = {}

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

async def get_market_filters(symbol: str) -> tuple[float, float]:
    """Devuelve ``(step_size, min_notional)`` usando caché."""
    if symbol in _FILTER_CACHE:
        return _FILTER_CACHE[symbol]

    async with await _bin_sem():
        info = await asyncio.to_thread(client.get_symbol_info, symbol=symbol)

    step, min_notional = 0.000001, 0.0
    for flt in info["filters"]:
        if flt["filterType"] == "LOT_SIZE":
            step = float(flt["stepSize"])
        elif flt["filterType"] == "MIN_NOTIONAL":
            min_notional = float(flt.get("minNotional", 0.0))

    _FILTER_CACHE[symbol] = (step, min_notional)
    return step, min_notional


# ─────────────────────────────────────────────────────────────
#  Safe market sell helpers
# ─────────────────────────────────────────────────────────────

async def get_available_qty(client: Client, symbol: str) -> float:
    """Return free balance for ``symbol`` base asset."""
    asset = symbol[:-4]
    bal = await asyncio.to_thread(client.get_asset_balance, asset=asset)
    return float(bal["free"])


async def get_full_market_filters(client: Client, symbol: str):
    """Return ``(stepSize, minQty, minNotional)`` for ``symbol``."""
    info = await asyncio.to_thread(client.get_symbol_info, symbol=symbol)
    lot = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    min_notional = next(
        (f for f in info["filters"] if f["filterType"] == "MIN_NOTIONAL"), None
    )
    return (
        float(lot["stepSize"]),
        float(lot["minQty"]),
        float(min_notional["minNotional"]) if min_notional else 0.0,
    )


async def safe_market_sell(client: Client, symbol: str, raw_qty: float):
    """Safely execute a market sell respecting filters."""
    step, min_qty, min_notional = await get_full_market_filters(client, symbol)
    available = min(raw_qty, await get_available_qty(client, symbol))

    qty = available - (available % step)
    qty = round(qty, int(-round(math.log10(step))))

    if qty < min_qty - 1e-12:
        return False, f"qty<{min_qty} LOT_SIZE (dust)"

    if min_notional:
        price = float((await asyncio.to_thread(
            client.get_symbol_ticker, symbol=symbol))["price"])
        if qty * price < min_notional - 1e-8:
            return False, f"valor<{min_notional} MIN_NOTIONAL"

    try:
        order = await asyncio.to_thread(
            client.create_order,
            symbol=symbol, side="SELL", type="MARKET", quantity=qty
        )
        return True, order
    except bexc.BinanceAPIException as e:
        return False, f"error {e.code}:{e.message}"



import pandas as pd, asyncio, logging
from datetime import datetime
from pathlib import Path
from config import logger

async def log_sale_to_excel(symbol: str, value: float,
                            pnl: float, pct: float):
    """
    Añade una fila a historial_ventas.xlsx.
    Si falla, se deja registro en logger y un aviso por Telegram.
    """
    from utils import send_telegram_message  # import local para evitar ciclos
    fn = Path("historial_ventas.xlsx")
    row = {
        "fecha": datetime.utcnow().isoformat(timespec="seconds"),
        "symbol": symbol,
        "valor": round(value, 2),
        "pnl":   round(pnl, 2),
        "pct":   round(pct, 2),
        "resultado": "positivo" if pnl > 0 else "negativo",
    }
    try:
        if fn.exists():
            df = pd.read_excel(fn)
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        else:
            df = pd.DataFrame([row])
        df.to_excel(fn, index=False)
        logger.info(f"[excel] venta registrada {symbol} valor={value:.2f}")
    except Exception as e:
        logger.exception(f"[excel] error registrando venta {symbol}: {e}")
        await send_telegram_message(
            f"⚠️ Error guardando historial_ventas.xlsx: {e}")

# ─── trailing stop_delta que sólo sube ────────────────────────────────
def trail_stop_delta(rec: dict, value_now: float, delta_usdt: float) -> bool:
    """Incrementa ``rec['stop_delta']`` si ``value_now - delta_usdt`` supera el
    valor almacenado.  Devuelve ``True`` si se movió, ``False`` si no."""
    new_stop = value_now - delta_usdt
    old_stop = rec.get("stop_delta", 0.0)
    if new_stop > old_stop:
        rec["stop_delta"] = new_stop
        return True
    return False


def _parse_expiry_any(ts):
    """
    Acepta:
      - str ISO8601 (con o sin 'Z')
      - datetime (naive o con tz)
      - epoch (int/float, segundos)
    Devuelve datetime UTC naive o None si no se puede parsear.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return (
            ts if ts.tzinfo is None
            else ts.astimezone(timezone.utc).replace(tzinfo=None)
        )
    if isinstance(ts, (int, float)):
        return datetime.utcfromtimestamp(ts)
    if isinstance(ts, str):
        s = ts.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1]
            dt = datetime.fromisoformat(s)
            return (
                dt if dt.tzinfo is None
                else dt.astimezone(timezone.utc).replace(tzinfo=None)
            )
        except Exception:
            return None
    return None


def set_cooldown(exclusion_dict: dict, sym: str, minutes: int) -> str:
    """
    Fija cooldown en ISO8601 (UTC). Centraliza el formato para evitar mezcla de tipos.
    """
    expiry = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
    exclusion_dict[sym] = expiry
    return expiry


def cooldown_active(exclusion_dict: dict, sym: str) -> bool:
    """
    True si el símbolo sigue en cooldown.
    Si ya expiró o el valor es inválido, limpia la entrada y devuelve False.
    """
    ts = exclusion_dict.get(sym)
    if not ts:
        return False
    expires = _parse_expiry_any(ts)
    if expires is None:
        exclusion_dict.pop(sym, None)
        return False
    if datetime.utcnow() > expires:
        exclusion_dict.pop(sym, None)
        return False
    return True
