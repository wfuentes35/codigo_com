# fases/fase0_newlist.py
# ================================================================
#  Escáner de “spikes” de compra que alimenta la Fase 2
#  – timeframe dinámico (1 m / 5 m / 15 m)
#  – umbrales dinámicos (min_vol, min_ratio) vía /set fase0 …
# ================================================================
import asyncio, datetime
from collections import defaultdict
from binance.client import Client

from config import (
    logger,
    MAX_TRACKED_COINS,
    FASE0_SETTINGS,          # interval / min_vol / min_ratio
)
from utils import (
    get_all_usdt_symbols,
    get_historical_data,
    send_telegram_message,
)

# ───────────── mapping de intervalos ─────────────
_INTERVAL_MAP = {
    "1m":  Client.KLINE_INTERVAL_1MINUTE,
    "5m":  Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
}

# ───────────── helpers ─────────────
def _current_params():
    """Lee settings actuales y devuelve (interval_const, min_vol, min_ratio)."""
    s = FASE0_SETTINGS
    return (
        _INTERVAL_MAP[s["interval"]],
        s["min_vol"],
        s["min_ratio"],
    )

def _dynamic_sleep_seconds() -> int:
    """Intervalo de espera en segundos según el timeframe elegido."""
    tf = int(FASE0_SETTINGS["interval"].rstrip("m"))  # 1, 5, 15
    return max(30, tf * 30)  # 30 s (1 m), 150 s (5 m), 450 s (15 m)

# ───────────── constantes internas ─────────────
COOLDOWN_MIN = 15                                   # evita spam duplicado
last_trigger = defaultdict(lambda: datetime.datetime.min)

# ----------------------------------------------------------------------
async def _procesar_simbolo(sym: str,
                            state: dict,
                            excl: dict) -> None:
    """Evalúa un símbolo y lo añade como RESERVADA_NUEVA si cumple filtros."""
    interval, min_vol, min_ratio = _current_params()

    # descartes rápidos
    if sym in state or sym in excl:
        return
    if (datetime.datetime.utcnow() - last_trigger[sym]).seconds < COOLDOWN_MIN * 60:
        return

    df = await get_historical_data(sym, interval, limit=1)
    if df is None or df.empty:
        return

    row       = df.iloc[-1]
    quote_vol = float(row["qav"])    # volumen total (USDT)
    buy_vol   = float(row["tbqav"])  # volumen comprador
    if quote_vol < min_vol:
        return

    buy_ratio = buy_vol / quote_vol if quote_vol else 0.0
    if buy_ratio < min_ratio:
        return

    # ── spike detectado ──
    state[sym] = "RESERVADA_NUEVA"
    last_trigger[sym] = datetime.datetime.utcnow()

    txt = (f"📈 Spike de COMPRA • {sym}\n"
           f"Vol {FASE0_SETTINGS['interval']} ≈ {quote_vol:,.0f} USDT "
           f"⁄ {buy_ratio * 100:,.0f}% compras\n"
           "Marcado como RESERVADA_NUEVA → lo vigilará Fase 2.")
    await send_telegram_message(txt)
    logger.info(f"[fase0_spike] {txt}")

# ----------------------------------------------------------------------
async def watch_new_listings(state_dict: dict, exclusion_dict: dict):
    """Bucle principal de Fase 0."""
    while True:
        try:
            # Limita si ya alcanzamos el máximo de monedas seguidas
            if len(state_dict) >= MAX_TRACKED_COINS:
                await asyncio.sleep(_dynamic_sleep_seconds())
                continue

            symbols = await get_all_usdt_symbols()  # pares xxxUSDT (filtrados)
            tasks = [
                _procesar_simbolo(sym, state_dict, exclusion_dict)
                for sym in symbols
            ]
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"[fase0_spike] error global: {e}")

        await asyncio.sleep(_dynamic_sleep_seconds())
