# fases/fase0_precross.py – “Pre-cruce” HMA-8 vs EMA-24 (30 min)
# ==============================================================
"""
Explora todos los pares USDT cada 10 min en marco 30 m.
Si HMA-8 está por debajo de EMA-24 pero subiendo y el cruce parece inminente,
marca el símbolo como "RESERVADA_PRE" para que otra tarea lo vigile.
"""

import asyncio
from config import PAUSED, SHUTTING_DOWN
from config import logger
from utils import get_all_usdt_symbols, get_historical_data, hull_moving_average

# ————————————————————————————————————————————————————————————————
async def phase0_precross(state_dict: dict):
    """Tarea asincrónica: se lanza desde main.py"""
    sem = asyncio.Semaphore(10)              # ‹— creado dentro del loop

    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        symbols = await get_all_usdt_symbols()   # refresca lista cada ciclo

        async def _almost_cross(sym: str):
            async with sem:
                df = await get_historical_data(sym, "30m", 120)
            if df is None:
                return

            close = df["close"].astype(float)
            ema24 = close.ewm(span=24).mean()
            hma8  = hull_moving_average(close, 8)

            diff  = hma8.iloc[-1] - ema24.iloc[-1]
            slope = hma8.diff().iloc[-3:].mean()

            # HMA todavía por debajo pero subiendo con pendiente moderada
            if diff < 0 and 0 < slope < abs(ema24.iloc[-1]) * 0.002:
                state_dict.setdefault(sym, "RESERVADA_PRE")

        await asyncio.gather(*(_almost_cross(s) for s in symbols))
        logger.debug("[fase0A] precross 30m completado")
        await asyncio.sleep(600)                 # 10 min
