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
    logger, MIN_SYNC_USDT, DRY_RUN,
)
from utils import (
    get_all_usdt_symbols, get_step_size, send_telegram_message,
    update_light_stops, get_historical_data, get_ema,
    safe_market_sell, set_cooldown,
    process_sell_and_notify,
)
from fases.fase3 import phase3_search_new_candidates

_BIN_SEM = asyncio.Semaphore(3)

def asset_ok(asset: str, valid_assets: set[str]) -> bool:
    """Comprueba si *assetUSDT* está listado en Binance usando un set previo."""
    return asset in valid_assets
def _ensure_int(x):
    assert isinstance(x, int), "freed slots debe ser int"
    return x
# ----------------------------------------------------------------------
async def sync_positions(state: dict, client, exclusion_dict: dict, interval: int = 900):
    """Sincroniza balances en tiempo real.

    Aplica trailing con ``update_light_stops`` y vende cuando se activa un stop.
    ``state`` es un diccionario compartido con Fase 2.
    """
    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        try:
            account = await asyncio.to_thread(client.get_account)
            valid_assets = {s[:-4] for s in await get_all_usdt_symbols()}

            # -- recorrer balances --
            for bal in account["balances"]:
                asset = bal["asset"]
                if asset == "USDT":
                    continue

                qty = float(bal["free"]) + float(bal["locked"])
                symbol = f"{asset}USDT"

                # Saltar si se vendió desde otra fase
                if exclusion_dict.get(symbol):
                    continue

                # limpiar si posición vacía
                if qty == 0 or not asset_ok(asset, valid_assets):
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
                light_mode      = config.LIGHT_MODE

                rec = state.get(symbol)

                # -------- posición ya sincronizada --------
                if rec and isinstance(rec, dict):
                    if light_mode:
                        df = await get_historical_data(symbol, config.KLINE_INTERVAL_FASE2, 30)
                        triggers = []
                        if df is not None and not df.empty:
                            closes = df["close"].astype(float)
                            ema_long = get_ema(closes, config.EMA_LONG)
                            if price <= ema_long.iloc[-1]:
                                rec["exit_reason"] = f"EMA{config.EMA_LONG}-EXIT"
                                triggers.append(symbol)

                        value_now = qty * price

                        stop_trigger = update_light_stops(
                            rec, qty, price, stop_delta_usdt
                        )
                        if stop_trigger:
                            rec["exit_reason"] = "Δ-STOP"
                            triggers.append(symbol)
                        elif value_now <= config.STOP_ABS_USDT:
                            rec["exit_reason"] = "ABS-STOP"
                            triggers.append(symbol)

                        if triggers:
                            async with _BIN_SEM:
                                step = await get_step_size(symbol)
                                qty_sell = round_step_size(qty, step)
                                exit_reason = rec.pop("exit_reason", "EXIT")
                                # La cantidad a vender es `qty`, no `qty_sell` para el PnL
                                rec["quantity"] = qty
                                await process_sell_and_notify(
                                    client, symbol, rec, price, exit_reason, exclusion_dict
                                )

                            state.pop(symbol, None)
                            await phase3_search_new_candidates(state, _ensure_int(1), exclusion_dict)
                    continue

                # -------- registrar nueva posición --------
                state[symbol] = {
                    "status":      "COMPRADA_SYNC",
                    "entry_price": price,
                    "entry_cost":  current_value,
                    "quantity":    qty,
                    "max_value":   current_value,
                    "stop_delta":  current_value - config.STOP_DELTA_USDT,
                }
                await send_telegram_message(
                    f"📡 Sincronizada {symbol} • value={current_value:.2f} USDT"
                )
        except Exception:
            logger.exception("[sync] crash")

        await asyncio.sleep(interval)
