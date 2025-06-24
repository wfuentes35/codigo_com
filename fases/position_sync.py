# fases/position_sync.py – sincroniza balances, trailing y repuebla Fase 3
# =====================================================================
"""
Sincroniza el balance spot → state_dict, aplica trailing Δ‑stop en USDT y un stop
absoluto dinámico por símbolo.  Tras cada venta repuebla precandidatos con Fase 3.
Todos los parámetros (`STOP_DELTA_USDT`, `STOP_ABS_USDT`, `LIGHT_MODE`) se leen
directamente desde el módulo *config* en cada ciclo para que cambios vía /set se
reflejen sin reiniciar el bot.
"""
from typing import Iterable
from config import PAUSED, SHUTTING_DOWN
import asyncio
import config                    # ← leer valores en caliente
from binance import exceptions as bexc
from binance.helpers import round_step_size
from config import (
    logger, telegram_bot, TELEGRAM_CHAT_ID,
    MIN_SYNC_USDT,
)
from utils import get_all_usdt_symbols, get_step_size
from fases.fase3 import phase3_search_new_candidates

_BIN_SEM = asyncio.Semaphore(3)

async def asset_ok(asset: str) -> bool:
    """Comprueba rápidamente si *assetUSDT* está listado en Binance."""
    _valid = {s[:-4] for s in await get_all_usdt_symbols()}
    return asset in _valid
def _ensure_int(x):
    assert isinstance(x, int), "freed slots debe ser int"
    return x
# ----------------------------------------------------------------------
async def sync_positions(state: dict, client, exclusion_dict: dict, interval: int = 900):
    """Loop de sincronización con trailing y stops dinámicos."""
    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        try:
            account = await asyncio.to_thread(client.get_account)

            # -- recorrer balances --
            for bal in account["balances"]:
                asset = bal["asset"]
                if asset == "USDT":
                    continue

                qty    = float(bal["free"]) + float(bal["locked"])
                symbol = f"{asset}USDT"

                # limpiar si posición vacía
                if qty == 0 or not await asset_ok(asset):
                    if not (isinstance(state.get(symbol), str) and state[symbol].startswith("RESERVADA")):
                        state.pop(symbol, None)
                    continue

                # precio puntual
                try:
                    price = float((await asyncio.to_thread(
                        client.get_symbol_ticker, symbol=symbol))["price"])
                except bexc.BinanceAPIException:
                    continue

                current_value = qty * price
                if current_value < MIN_SYNC_USDT:
                    state.pop(symbol, None)
                    continue

                # leer parámetros vivos
                stop_delta_usdt = config.STOP_DELTA_USDT
                stop_abs_usdt   = config.STOP_ABS_USDT
                light_mode      = config.LIGHT_MODE

                rec = state.get(symbol)

                # -------- posición ya sincronizada --------
                if rec and isinstance(rec, dict):
                    if light_mode:
                        rec["max_value"]  = max(rec.get("max_value", 0.0), current_value)
                        rec["stop_delta"] = rec["max_value"] - stop_delta_usdt
                        rec["stop_abs"]   = 51.0 * qty if price >= 55 else stop_abs_usdt

                        # trigger de stop
                        if current_value < rec["stop_delta"] or current_value < rec["stop_abs"]:
                            msg = (
                                f"🚨 STOP(sync) {symbol} • value={current_value:.2f} USDT / "
                                f"Δ={rec['stop_delta']:.2f} • abs={rec['stop_abs']:.2f}"
                            )
                            await telegram_bot.send_message(TELEGRAM_CHAT_ID, msg)
                            logger.info(msg)

                            # vender
                            async with _BIN_SEM:
                                step = await get_step_size(symbol)
                                qty_sell = round_step_size(qty, step)
                                try:
                                    await asyncio.to_thread(
                                        client.create_order,
                                        symbol=symbol, side="SELL", type="MARKET",
                                        quantity=qty_sell,
                                    )
                                except Exception:
                                    logger.exception(f"Venta sync {symbol} falló")

                            # liberar y repoblar
                            state.pop(symbol, None)
                            await phase3_search_new_candidates(state, _ensure_int(1), exclusion_dict)
                    continue

                # -------- registrar nueva posición --------
                state[symbol] = {
                    "status":      "COMPRADA_SYNC",
                    "entry_price": price,
                    "entry_value": current_value,
                    "quantity":    qty,
                    "max_value":   current_value,
                    "stop_delta":  current_value - stop_delta_usdt,
                    "stop_abs":    stop_abs_usdt,
                }
                await telegram_bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"📡 Sincronizada {symbol} • value={current_value:.2f} USDT",
                )
        except Exception:
            logger.exception("[sync] crash")

        await asyncio.sleep(interval)
