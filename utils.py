# utils.py – indicadores, Binance helpers y antiflood Telegram
# ============================================================

import asyncio
import pandas as pd
import numpy as np
import telegram.error
from binance import exceptions as bexc
from config import client, logger, telegram_bot, TELEGRAM_CHAT_ID

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
async def get_all_usdt_symbols():
    async with await _bin_sem():
        info = await asyncio.to_thread(client.get_exchange_info)

    excluded = {"BUSD", "USDC", "TUSD", "EUR", "AUD", "BRL", "IDRT",
                "PAX", "USDP", "DAI", "XUSD", "USD1", "VIDT", "FDUSD"}

    return [
        s["symbol"] for s in info["symbols"]
        if (
            s["status"] == "TRADING"
            and s["isSpotTradingAllowed"]
            and s["quoteAsset"] == "USDT"
            and s["baseAsset"] not in excluded
        )
    ]

async def get_historical_data(symbol: str, interval: str, limit: int = 100):
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
