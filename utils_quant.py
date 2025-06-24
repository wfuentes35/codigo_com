"""Helpers exclusivos del radar cuant ETA ≤ 30 m."""
import asyncio, time, numpy as np, logging
from binance.client import Client
from config import (
    MIN_24H_VOL_USDT, EMA_SHORT, EMA_LONG, ETA_MAX_BARS,
    RVOL_MIN, CVD_MIN, TELEGRAM_CHAT_ID
)
from utils import send_telegram_message  # re‑usa helper sync

logger = logging.getLogger(__name__)
client = Client()

async def list_usdt_symbols():
    info = await asyncio.to_thread(client.get_exchange_info)
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]

async def get_klines(symbol: str, interval: str = "30m", limit: int = 60):
    import pandas as pd
    kl = await asyncio.to_thread(client.get_klines,
                                 symbol=symbol,
                                 interval=interval,
                                 limit=limit)
    df = pd.DataFrame(kl, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"])
    df[["close","volume"]] = df[["close","volume"]].astype(float)
    return df

async def aggtrade_delta(symbol: str, minutes: int = 5) -> float:
    end = int(time.time()*1000)
    start = end - minutes*60_000
    trades = await asyncio.to_thread(client.get_aggregate_trades,
                                     symbol=symbol,
                                     startTime=start,
                                     endTime=end)
    buy = sell = 0.0
    for t in trades:
        notional = float(t["q"])*float(t["p"])
        if t["m"]:
            sell += notional
        else:
            buy  += notional
    return buy - sell

async def check_liquidity(symbol: str) -> bool:
    tick = await asyncio.to_thread(client.get_ticker, symbol=symbol)
    return float(tick["quoteVolume"]) > MIN_24H_VOL_USDT

def _eta_to_cross(diff):
    x = np.arange(len(diff))
    slope,_ = np.polyfit(x, diff, 1)
    return np.inf if slope >= 0 else abs(diff[-1]/slope)

async def is_quant_candidate(symbol: str) -> bool:
    if not await check_liquidity(symbol):
        return False
    df = await get_klines(symbol)
    if df.empty:
        return False
    df["ema_s"] = df["close"].ewm(span=EMA_SHORT).mean()
    df["ema_l"] = df["close"].ewm(span=EMA_LONG).mean()
    diff = (df["ema_s"] - df["ema_l"]).tail(6).values
    if _eta_to_cross(diff) > ETA_MAX_BARS:
        return False
    if df["volume"].iloc[-1] < df["volume"].iloc[-20:-1].mean()*RVOL_MIN:
        return False
    if await aggtrade_delta(symbol) < CVD_MIN:
        return False
    return True