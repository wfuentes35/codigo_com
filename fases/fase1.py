"""Fase 1 – escáner continuo de rupturas.

Revisa de forma periódica todos los pares USDT en busca de
cierres por encima de la banda superior de Bollinger con
volumen elevado y RSI positivo.  Los símbolos que cumplan
los requisitos se marcan como ``RESERVADA_PRE`` para que la
fase 2 valide el pullback y ejecute la entrada.
"""

import asyncio
from binance.client import Client
import pandas as pd

from config import logger, PAUSED, SHUTTING_DOWN, MAX_CANDIDATOS_ACTIVOS
from utils import (
    get_all_usdt_symbols,
    get_historical_data,
    send_telegram_message,
    get_bollinger_bands,
    get_rsi,
    get_volume_avg,
    count_active_candidates,
)

# ----------------------------------------------------------------------
SCAN_INTERVAL = 900  # segundos


async def _is_candidate(sym: str, state: dict) -> bool:
    """Devuelve True si ``sym`` cumple la ruptura inicial."""
    rec = state.get(sym)
    if isinstance(rec, dict) and rec.get("status") in {"COMPRADA", "RESERVADA_PRE"}:
        return False
    if isinstance(rec, str) and rec.startswith("RESERVADA_PRE"):
        return False

    df = await get_historical_data(sym, Client.KLINE_INTERVAL_4HOUR, 40)
    if df is None or len(df) < 25:
        return False

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    bb_upper, _, _ = get_bollinger_bands(close)
    rsi_val = get_rsi(close).iloc[-1]
    vol_avg = get_volume_avg(volume)

    last_close = close.iloc[-1]
    last_vol = volume.iloc[-1]

    if last_close > bb_upper.iloc[-1] and last_vol >= 2 * vol_avg and rsi_val > 50:
        return True
    return False


async def phase1_search_20_candidates(state_dict: dict):
    """Escanea continuamente en busca de rupturas."""
    while not SHUTTING_DOWN.is_set():
        await PAUSED.wait()
        if count_active_candidates(state_dict) >= MAX_CANDIDATOS_ACTIVOS:
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        symbols = await get_all_usdt_symbols()
        added: list[str] = []

        for sym in symbols:
            if count_active_candidates(state_dict) >= MAX_CANDIDATOS_ACTIVOS:
                break
            try:
                ok = await _is_candidate(sym, state_dict)
                if ok:
                    state_dict[sym] = {"status": "RESERVADA_PRE"}
                    added.append(sym)
            except Exception:
                logger.exception(f"[fase1] error evaluando {sym}")

        if added:
            msg = "Fase 1 – nuevas rupturas:\n" + ", ".join(added)
            await send_telegram_message(msg)
            logger.info(msg)
        await asyncio.sleep(SCAN_INTERVAL)
