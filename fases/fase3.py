# fases/fase3.py – Reposición Precandidatos mejorada (near‑cross 30 m)
# --------------------------------------------------------------------
# Usa la misma lógica de Fase 1 para rellenar huecos cuando se liberan
# posiciones.  Limita la búsqueda al top‑N por volumen para ahorrar API.
# --------------------------------------------------------------------
import asyncio
from config import (
    logger,
    PRECANDIDATES_PER_FREED_COIN,
    MAX_TRACKED_COINS,
)
from utils import (
    get_all_usdt_symbols,
    send_telegram_message,
)
from fases.fase1 import _is_candidate   # reutilizamos la función


async def phase3_replenish(state_dict: dict,
                           exclusion_dict: dict,
                           freed_coins: int):
    """Añade precandidatos cuando se liberan posiciones."""
    to_add = min(
        freed_coins * PRECANDIDATES_PER_FREED_COIN,
        MAX_TRACKED_COINS - len(state_dict)
    )
    if to_add <= 0:
        return exclusion_dict

    symbols = await get_all_usdt_symbols()
    # ordena por volumen (ya devuelve volumen descendente)
    added = []

    async def _eval(sym):
        if len(added) >= to_add:
            return
        if sym in state_dict or sym in exclusion_dict:
            return
        ok = await _is_candidate(sym, state_dict)
        if ok:
            state_dict[sym] = "RESERVADA_PRE"
            added.append(sym)
            exclusion_dict[sym] = {"ts": asyncio.get_event_loop().time()}

    await asyncio.gather(*[_eval(s) for s in symbols[:200]])  # solo top 200

    if added:
        await send_telegram_message("Fase 3: nuevos candidatos:\n" + ", ".join(added))
        logger.info(f"Fase 3 añadió {len(added)} símbolos: {added}")
    else:
        logger.info("Fase 3 sin candidatos relevantes.")

    return exclusion_dict

# Alias para compatibilidad con fase2 / telegram_commands
async def phase3_search_new_candidates(state_dict, freed_coins, exclusion_dict):
    return await phase3_replenish(state_dict, exclusion_dict, freed_coins)
