import os, asyncio, datetime
from utils import send_telegram_message
from config import logger
from config import PAUSED, SHUTTING_DOWN
MANUAL_FILE = "manual_candidates.txt"

async def watch_manual_file(state_dict, exclusion_dict, interval=30):
    """Añade símbolos listados en MANUAL_FILE como 'RESERVADA'."""
    last_mtime = None
    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        try:
            if os.path.exists(MANUAL_FILE):
                mtime = os.path.getmtime(MANUAL_FILE)
                if last_mtime is None or mtime > last_mtime:
                    last_mtime = mtime
                    with open(MANUAL_FILE) as f:
                        symbols = {l.strip().upper() for l in f if l.strip()}
                    added = []
                    for sym in symbols:
                        if sym in state_dict or sym in exclusion_dict:
                            continue
                        state_dict[sym] = "RESERVADA"
                        added.append(sym)
                    if added:
                        txt = "📥 Añadidos manualmente:\n" + "\n".join(added)
                        await send_telegram_message(txt)
                        logger.info(txt)
        except Exception as e:
            logger.error(f"[manual_watcher] {e}")
        await asyncio.sleep(interval)
