# fases/fase0_cross15m.py – Confirma cruces reales en 15 min
# ==========================================================
"""
Cada 8 min revisa:
  • Todos los símbolos que estén en estado "RESERVADA_PRE".
  • (Opcional) los primeros 200 pares USDT de Binance para no perder cruces.

Si el cruce HMA-8 > EMA-24 ocurrió en la vela actual (bars≤1), cambia el estado
a "RESERVADA" para que Fase 2 pueda comprar.
"""
import asyncio
from config import PAUSED, SHUTTING_DOWN
from config import logger
from utils import (
    get_all_usdt_symbols,      # ← ya tienes esto
    get_historical_data,
    hull_moving_average,
)

# ————————————————————————————————————————————————————————————————
async def phase0_cross15m(state_dict: dict):
    sem = asyncio.Semaphore(10)               # creado dentro del loop

    async def _confirm_cross(sym: str):
        async with sem:
            df = await get_historical_data(sym, "15m", 120)
        if df is None:
            return

        diff = hull_moving_average(df["close"], 8) - df["close"].ewm(span=24).mean()
        crosses = (diff > 0) & (diff.shift(1) <= 0)
        if not crosses.any():
            return                           # aún no cruza

        last_pos = df.index.get_loc(crosses[crosses].index[-1])
        bars_ago = len(df) - 1 - last_pos
        if bars_ago <= 1:
            if isinstance(state_dict.get(sym), str):      # solo si NO es dict
                state_dict[sym] = "RESERVADA"
            logger.info(f"[fase0B] {sym} cruzó 15 m → RESERVADA")

    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        # 1) los que ya están “PRE”
        watch = [s for s, v in state_dict.items() if v == "RESERVADA_PRE"]

        # 2) añade (hasta 200) pares USDT para no perder cruces
        if len(watch) < 200:
            all_syms = await get_all_usdt_symbols()
            watch += all_syms[: 200 - len(watch)]

        watch = list(dict.fromkeys(watch))    # elimina duplicados

        await asyncio.gather(*(_confirm_cross(s) for s in watch))
        logger.debug("[fase0B] cruces 15 m completados")
        await asyncio.sleep(480)              # 8 min
