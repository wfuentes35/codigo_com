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
    get_all_usdt_symbols, get_step_size, send_telegram_message, update_light_stops
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

                # Saltar si ya se vendió en fase2
                if symbol not in state or exclusion_dict.get(symbol):
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
                stop_abs_usdt   = config.STOP_ABS_USDT
                light_mode      = config.LIGHT_MODE

                rec = state.get(symbol)

                # -------- posición ya sincronizada --------
                if rec and isinstance(rec, dict):
                    if light_mode:
                        triggered = update_light_stops(
                            rec, qty, price, stop_delta_usdt, stop_abs_usdt
                        )

                        if triggered:

                            # vender
                            async with _BIN_SEM:
                                step = await get_step_size(symbol)
                                qty_sell = round_step_size(qty, step)
                                try:
                                    if not DRY_RUN:
                                        sell = await asyncio.to_thread(
                                            client.create_order,
                                            symbol=symbol,
                                            side="SELL",
                                            type="MARKET",
                                            quantity=qty_sell,
                                        )
                                        value = float(sell.get("cummulativeQuoteQty", 0.0))
                                        fee = 0.0
                                        for f in sell.get("fills", []):
                                            comm = float(f["commission"])
                                            if f["commissionAsset"] == "USDT":
                                                fee += comm
                                            elif f["commissionAsset"] == "BNB":
                                                price_bnb = float((await asyncio.to_thread(
                                                    client.get_symbol_ticker,
                                                    symbol="BNBUSDT",
                                                ))["price"])
                                                fee += comm * price_bnb
                                            else:
                                                fee += comm * float(f["price"])
                                    else:
                                        value = price * qty_sell
                                        fee = 0.0
                                    pnl = value - fee - rec.get("entry_value", current_value)
                                    pct = 100 * pnl / rec.get("entry_value", current_value)
                                    texto = (
                                        f"🚨 STOP(sync) {symbol} @ {value/qty_sell:.4f}\n"
                                        f"🔻 Valor vendido: {value:.2f} USDT\n"
                                        f"🧾 Fee: {fee:.4f} USDT\n"
                                        f"📊 PnL: {pnl:.2f} USDT ({pct:.2f}%)"
                                    )
                                    await send_telegram_message(texto)
                                    logger.info(f"SELL {symbol} pnl={pnl:.4f} pct={pct:.2f}")
                                except Exception:
                                    logger.exception(f"Venta sync {symbol} falló")

                            # liberar y repoblar
                            state.pop(symbol, None)
                            exclusion_dict[symbol] = True
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
                await send_telegram_message(
                    f"📡 Sincronizada {symbol} • value={current_value:.2f} USDT"
                )
        except Exception:
            logger.exception("[sync] crash")

        await asyncio.sleep(interval)
