# main.py – Orquestador con /pausa /reanudar /apagar /restart
# ===========================================================

import asyncio
from datetime import datetime
import sys

import config
from config import (
    logger,
    client,
    SYNC_POS_INTERVAL,
    PAUSED,
    SHUTTING_DOWN,
)

# ─── supervise simple: solo reinicia si crashea ──────────────
async def supervise(coro_factory, *args):
    while not SHUTTING_DOWN.is_set():
        try:
            await coro_factory(*args)            # la fase maneja /pausa
        except Exception:
            logger.exception(f"❌ {coro_factory.__name__} crasheó; reinicio en 5 s")
            await asyncio.sleep(5)

# ─── Importes dependientes de eventos ────────────────────────
from telegram_commands import build_telegram_app

# fases
from fases.fase1 import phase1_search_20_candidates
from fases.fase2 import phase2_monitor
from fases.position_sync import sync_positions
from fases.manual_watcher import watch_manual_file

# ─── Estados compartidos ─────────────────────────────────────
state_dict, exclusion_dict = {}, {}

# ─── sync_positions con retardo ──────────────────────────────
async def delayed_sync():
    await asyncio.sleep(30)
    asyncio.create_task(
        supervise(sync_positions,
                  state_dict, client, exclusion_dict, SYNC_POS_INTERVAL)
    )

# ─── main ────────────────────────────────────────────────────
async def main():
    app = build_telegram_app(state_dict, exclusion_dict, PAUSED, SHUTTING_DOWN)
    await app.initialize(); await app.start()
    asyncio.create_task(app.updater.start_polling())

    asyncio.create_task(supervise(watch_manual_file, state_dict, exclusion_dict))
    asyncio.create_task(delayed_sync())
    asyncio.create_task(supervise(phase2_monitor, state_dict, client, exclusion_dict))
    asyncio.create_task(supervise(phase1_search_20_candidates, state_dict))

    # Heart-beat
    while not SHUTTING_DOWN.is_set():
        await PAUSED.wait()
        await asyncio.sleep(1800)
        logger.info(f"Heartbeat {datetime.utcnow().isoformat(timespec='seconds')}")

# ─── lanzamiento ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot detenido.")
