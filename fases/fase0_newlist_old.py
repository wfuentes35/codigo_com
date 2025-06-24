# ======================================================================
#  fases/fase0_pullback.py
#  - Detecta listados < 8 días
#  - Espera caída ≥ 30 %  (pull‑back)
#  - Compra solo tras breakout + volumen fuerte
#  - Inserta el símbolo como "RESERVADA_NUEVA" en state_dict
# ======================================================================

import asyncio, datetime
from collections import defaultdict
from config import (
    logger, NEW_LISTINGS_INTERVAL, MAX_TRACKED_COINS,
    KLINE_INTERVAL_FASE0
)
from utils import (
    get_all_usdt_symbols, get_historical_data,
    send_telegram_message, check_volume, rsi
)

# ------------------------------------------------------------------ #
# lock global para proteger escrituras concurrentes
try:
    from global_lock import global_lock          # si ya tienes uno central
except ModuleNotFoundError:
    global_lock = asyncio.Lock()

# ------------------------------------------------------------------ #
# parámetros ajustables
MIN_DROP_PCT      = 30       # % desde el high inicial
BREAKOUT_LOOKBACK = 12       # velas 30 m para calcular nivel de ruptura
VOLUME_FACTOR     = 3        # vol_actual >= VOLUME_FACTOR * vol_medio_15m
TRACK_EXPIRY_H    = 48       # dejar de observar tras 2 días
MAX_4H_BARS       = 50       # <= 50 velas 4 h ≈ 8 días

# estructura de tracking
tracking = defaultdict(dict)
# tracking[sym] = {
#     "high": float, "low": float, "watch": bool,
#     "started": datetime, "last_check": datetime
# }

# ------------------------------------------------------------------ #
async def watch_new_listings(state_dict, exclusion_dict):
    """
    Bucle principal: examina listados recientes y gestiona el pull‑back tracker.
    """
    while True:
        try:
            symbols = await get_all_usdt_symbols()

            # 1) actualiza / crea trackers
            tasks = [
                asyncio.create_task(handle_symbol(sym, state_dict, exclusion_dict))
                for sym in symbols
            ]
            await asyncio.gather(*tasks)

            # 2) revisa breakouts de los que ya están en tracking
            await check_breakouts(state_dict, exclusion_dict)

        except Exception as e:
            logger.error(f"[fase0] Error general: {e}")

        await asyncio.sleep(NEW_LISTINGS_INTERVAL)

# ------------------------------------------------------------------ #
async def handle_symbol(sym, state_dict, exclusion_dict):
    """
    Crea o actualiza la entrada de tracking para listados < MAX_4H_BARS.
    """
    try:
        # ya está en seguimiento o exclusión?
        async with global_lock:
            if sym in state_dict or sym in exclusion_dict:
                return
            if len(state_dict) >= MAX_TRACKED_COINS:
                return

        df4h = await get_historical_data(sym, "4h", MAX_4H_BARS + 10)
        if df4h is None or len(df4h) > MAX_4H_BARS:
            # demasiado antiguo; si existía en tracking lo quitamos
            tracking.pop(sym, None)
            return

        # inicializar tracker
        t = tracking.get(sym)
        high_now = df4h["high"].max()
        low_now  = df4h["low"].min()

        if t is None:
            tracking[sym] = {
                "high": high_now,
                "low":  low_now,
                "watch": False,
                "started": datetime.datetime.utcnow(),
                "last_check": datetime.datetime.utcnow()
            }
            return

        # actualizar extremos
        if high_now > t["high"]:
            t["high"] = high_now
        if low_now < t["low"]:
            t["low"] = low_now
        t["last_check"] = datetime.datetime.utcnow()

        # activar modo watch si caída ≥ MIN_DROP_PCT
        drop_pct = (t["high"] - low_now) / t["high"] * 100
        if not t["watch"] and drop_pct >= MIN_DROP_PCT:
            t["watch"] = True
            logger.info(f"[fase0] {sym} entra en WATCH (drop {drop_pct:.1f} %)")

        # expira tracker tras TRACK_EXPIRY_H
        if (datetime.datetime.utcnow() - t["started"]).total_seconds() > TRACK_EXPIRY_H * 3600:
            tracking.pop(sym, None)

    except Exception as e:
        logger.error(f"[fase0] Error handle {sym}: {e}")

# ------------------------------------------------------------------ #
async def check_breakouts(state_dict, exclusion_dict):
    """
    Recorre símbolos en tracking con flag watch=True y detecta breakout + volumen.
    """
    symbols_to_add = []

    for sym, t in list(tracking.items()):
        if not t["watch"]:
            continue

        try:
            # nivel de breakout = máx últimos BREAKOUT_LOOKBACK velas 30 m
            df30 = await get_historical_data(sym, KLINE_INTERVAL_FASE0, BREAKOUT_LOOKBACK + 2)
            if df30 is None or df30.empty:
                continue

            breakout_lvl = df30["high"].iloc[-BREAKOUT_LOOKBACK:].max()
            last_close   = df30["close"].iloc[-1]
            vol15m_df    = await get_historical_data(sym, "15m", 40)
            if vol15m_df is None:
                continue
            vol_mean = vol15m_df["volume"].tail(20).mean()
            vol_now  = vol15m_df["volume"].iloc[-1]

            if (last_close >= breakout_lvl) and (vol_now >= vol_mean * VOLUME_FACTOR):
                symbols_to_add.append(sym)

        except Exception as e:
            logger.error(f"[fase0] breakout chk {sym}: {e}")

    # ----- insertar candidatos ganadores -----
    for sym in symbols_to_add:
        try:
            async with global_lock:
                if len(state_dict) >= MAX_TRACKED_COINS:
                    break
                if sym in state_dict or sym in exclusion_dict:
                    continue
                state_dict[sym] = {
                    "status": "RESERVADA_NUEVA",
                    "timestamp": int(datetime.datetime.now().timestamp())
                }

            txt = (f"🚀 Breakout listado nuevo: {sym}\n"
                   f"Cierra ≥ máx {BREAKOUT_LOOKBACK} velas y volumen 15 m x{VOLUME_FACTOR}")
            await send_telegram_message(txt)
            logger.info(f"[fase0] {txt}")

            # quitar del tracker para no procesar de nuevo
            tracking.pop(sym, None)

        except Exception as e:
            logger.error(f"[fase0] insert {sym}: {e}")
