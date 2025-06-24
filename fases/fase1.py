# fases/fase1.py – Precandidato Scanner mejorado (30 m near-cross)
# -----------------------------------------------------------------
# Selecciona pares USDT que están a punto de cruzar HMA-8 ↑ EMA-24 en 30 m.
# Coloca el símbolo como "RESERVADA_PRE" para que fase0_precross / phase0_cross15m
# los vigilen y, si procede, fase2 los compre.
# -----------------------------------------------------------------
import asyncio, pandas as pd
from config import (
    logger,
    INITIAL_PRECANDIDATES_LIMIT,
)
from utils import (
    get_all_usdt_symbols,
    get_historical_data,
    send_telegram_message,
    check_volume,
    hull_moving_average,
    rsi,
)
from binance.client import Client
import config

# ─── parámetros ajustables ─────────────────────────────────────────
DEBUG_MODE       = False
MIN_VOLUME_USDT  = 2_000
MIN_RSI_1H       = 50
MAX_SPREAD_PCT   = 0.3        # % máximo entre best ask y best bid
PRECRUCE_MAX_NEG = 0.001      # HMA puede estar hasta −0.1 % por debajo de EMA

client = config.client  # Binance client global

# ------------------------------------------------------------------
async def _spread_pct(sym: str) -> float:
    """Calcula spread % entre oferta y demanda."""
    try:
        order_book = await asyncio.to_thread(client.get_order_book, symbol=sym, limit=5)
        bid = float(order_book["bids"][0][0])
        ask = float(order_book["asks"][0][0])
        return 100 * (ask - bid) / bid if bid else 999.0
    except Exception:
        return 999.0

# ------------------------------------------------------------------
async def _is_candidate(sym: str, state: dict) -> bool:
    if sym in state:
        return False

    # 1) volumen
    if not await check_volume(sym, MIN_VOLUME_USDT):
        return False

    # 2) spread líquido
    if await _spread_pct(sym) > MAX_SPREAD_PCT:
        return False

    # 3) Historicos 30 m
    df30 = await get_historical_data(sym, "30m", 120)
    if df30 is None or len(df30) < 60:
        return False
    close30 = df30["close"].astype(float)
    ema24   = close30.ewm(span=24).mean()
    hma8    = hull_moving_average(close30, 8)

    diff    = hma8.iloc[-1] - ema24.iloc[-1]
    slope   = hma8.diff().iloc[-3:].mean()

    # HMA por debajo pero muy cerca (<0.1 %) y subiendo
    if not (diff < 0 and abs(diff)/close30.iloc[-1] <= PRECRUCE_MAX_NEG and slope > 0):
        return False

    # 4) RSI 1 h > 50 (momentum)
    df1h = await get_historical_data(sym, Client.KLINE_INTERVAL_1HOUR, 120)
    if df1h is None or len(df1h) < 30:
        return False
    rsi1h_val = rsi(df1h["close"].astype(float), 14).iloc[-1]
    if rsi1h_val <= MIN_RSI_1H:
        return False

    # 5) Tendencia 4 h alcista (EMA50 ascendente)
    df4h = await get_historical_data(sym, Client.KLINE_INTERVAL_4HOUR, 120)
    if df4h is None or len(df4h) < 30:
        return False
    ema50_4h = df4h["close"].astype(float).ewm(span=50).mean()
    if ema50_4h.diff().iloc[-1] <= 0:
        return False

    return True

# ------------------------------------------------------------------
async def phase1_search_20_candidates(state_dict: dict):
    """Llena el state_dict hasta INITIAL_PRECANDIDATES_LIMIT símbolos."""
    while len(state_dict) < INITIAL_PRECANDIDATES_LIMIT:
        symbols = await get_all_usdt_symbols()
        selected = []
        reasons  = [] if DEBUG_MODE else None

        async def _eval(sym):
            ok = await _is_candidate(sym, state_dict)
            if ok:
                state_dict[sym] = "RESERVADA_PRE"
                selected.append(sym)
            elif DEBUG_MODE:
                reasons.append(sym)
        await asyncio.gather(*[_eval(s) for s in symbols])

        msg = f"Fase 1: añadidos {len(selected)} – total={len(state_dict)}/{INITIAL_PRECANDIDATES_LIMIT}"
        if selected:
            msg += "\n" + ", ".join(selected)
        await send_telegram_message(msg)
        logger.info(msg)

        if DEBUG_MODE and reasons:
            await send_telegram_message("Descartados: " + ", ".join(reasons[:30]))

        if len(state_dict) < INITIAL_PRECANDIDATES_LIMIT:
            await asyncio.sleep(600)  # espera 10 min antes de nuevo ciclo

    done = f"Fase 1 completada con {len(state_dict)} precandidatos."
    await send_telegram_message(done)
    logger.info(done)
