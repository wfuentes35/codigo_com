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

import config
from config import PAUSED, SHUTTING_DOWN
from utils import (
    get_all_usdt_symbols,
    get_historical_data,
    send_telegram_message,
    get_bollinger_bands,
    get_rsi,
    get_volume_avg,
)

# ----------------------------------------------------------------------
SCAN_INTERVAL = 900  # segundos
INITIAL_DELAY = 60  # segundos de espera tras el arranque

def _active_positions(state: dict) -> int:
    return sum(
        1 for r in state.values()
        if isinstance(r, dict) and str(r.get("status", "")).startswith("COMPRADA")
    )


async def _is_candidate(sym: str, state: dict) -> bool:
    """Devuelve True si ``sym`` cumple la ruptura inicial."""
    rec = state.get(sym)
    status = rec.get("status") if isinstance(rec, dict) else rec

    if isinstance(status, str):
        if status.startswith("COMPRADA") or status.startswith("RESERVADA_PRE"):
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
    await asyncio.sleep(INITIAL_DELAY)  # espera inicial
    while not SHUTTING_DOWN.is_set():
        await PAUSED.wait()
        if _active_positions(state_dict) >= config.MAX_OPERACIONES_ACTIVAS:
            config.logger.debug("[fase1] límite de operaciones activas alcanzado")
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        symbols = await get_all_usdt_symbols()
        added: list[str] = []

        async def _eval(sym: str):
            try:
                ok = await _is_candidate(sym, state_dict)
                if ok:
                    state_dict[sym] = {"status": "RESERVADA_PRE"}
                    added.append(sym)
            except Exception:
                config.logger.exception(f"[fase1] error evaluando {sym}")

        await asyncio.gather(*[_eval(s) for s in symbols])

        if added:
            msg = "Fase 1 – nuevas rupturas:\n" + ", ".join(added)
            await send_telegram_message(msg)
            config.logger.info(msg)
        await asyncio.sleep(SCAN_INTERVAL)
