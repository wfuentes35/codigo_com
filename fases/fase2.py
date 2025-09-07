"""Fase 2 – validación de pullback y gestión de posiciones.

Cada símbolo marcado como ``RESERVADA_PRE`` por la Fase 1 se
monitorea para detectar un pullback hacia la zona comprendida
entre la banda superior de Bollinger y la EMA(9).  Si el precio
rebota desde esa área se ejecuta una compra de mercado.  Las
posiciones abiertas se cierran si el precio cierra por debajo de
la EMA(9) o por los stops existentes (trailing ATR, Δ-stop y
stop absoluto).
"""

import asyncio
import time
import config
from config import PAUSED, SHUTTING_DOWN
from binance.helpers import round_step_size
from binance import exceptions as bexc
from binance.exceptions import BinanceAPIException
from config import (
    logger, DRY_RUN, LIGHT_MODE,
    KLINE_INTERVAL_FASE2, CHECK_INTERVAL,
)
from utils import (
    get_historical_data, send_telegram_message,
    get_bollinger_bands, get_ema,
    get_market_filters, update_light_stops,
    set_cooldown, fee_to_usdt, process_sell_and_notify,
)
from fases.fase3 import phase3_replenish


async def _buy_market(sym, client, usdt, hint_price):
    if DRY_RUN:
        return dict(qty=usdt / hint_price, price=hint_price,
                    entry_cost=usdt, commission=0.0)
    try:
        o = await asyncio.to_thread(
            client.create_order,
            symbol=sym, side="BUY", type="MARKET", quoteOrderQty=usdt,
        )
    except BinanceAPIException as e:
        if e.code == -2010:   # balance insuficiente
            logger.warning(f"{sym}: saldo insuficiente para {usdt} USDT")
            await send_telegram_message(
                f"⚠️ Sin saldo para comprar {sym}. Ajusta /set entry o recarga USDT."
            )
            # --- activar cooldown global ---
            config.NO_BALANCE_UNTIL = time.time() + config.INSUFFICIENT_BALANCE_COOLDOWN
            return None
        raise

    qty = float(o["executedQty"])
    cost = float(o["cummulativeQuoteQty"])
    fee = await fee_to_usdt(client, o.get("fills", []))
    price = cost / qty if qty else hint_price
    return dict(qty=qty, price=price, entry_cost=cost + fee, commission=fee)


async def _evaluate(sym, state, client, freed, exclusion_dict):
    rec = state.get(sym)
    status = rec if isinstance(rec, str) else rec.get("status")

    # 2.1 Chequeo de límite
    activas = sum(
        1 for rec in state.values()
        if isinstance(rec, dict) and str(rec.get("status", "")).startswith("COMPRADA")
    )
    if activas >= config.MAX_OPERACIONES_ACTIVAS:
        logger.info(
            f"❌ Límite de operaciones ({activas}/{config.MAX_OPERACIONES_ACTIVAS}) alcanzado, no compro {sym}"
        )
        return

    # -------- ENTRADA --------
    if status == "RESERVADA_PRE":
        # 1. Obtener datos suficientes para EMAs largas
        df = await get_historical_data(sym, KLINE_INTERVAL_FASE2, 250)
        if df is None or len(df) < 201:
            return
        close = df["close"].astype(float)

        # 2. Implementar filtro de tendencia (EMA50 > EMA200)
        ema50 = get_ema(close, 50)
        ema200 = get_ema(close, 200)

        if ema50.iloc[-1] <= ema200.iloc[-1]:
            logger.info(f"Filtro tendencia {sym}: EMA50 <= EMA200. Descartado.")
            state.pop(sym, None)  # Eliminar para no reevaluar
            return

        # 3. Continuar con la lógica de pullback si la tendencia es alcista
        high = df["high"].astype(float); low = df["low"].astype(float)

        bb_upper, _, _ = get_bollinger_bands(close)
        ema9 = get_ema(close, 9)

        pull_low = low.iloc[-2]
        in_zone = ema9.iloc[-2] <= pull_low <= bb_upper.iloc[-2]
        rebound = close.iloc[-1] > close.iloc[-2]
        if not (in_zone and rebound):
            return

        step, min_notional = await get_market_filters(sym)
        if config.MIN_ENTRY_USDT < min_notional:
            await send_telegram_message(
                f"⚠️ {sym}: min\u202Fnotional {min_notional:.2f}\u202FUSDT • ajusta /set entry"
            )
            return

        trade = await _buy_market(sym, client, config.MIN_ENTRY_USDT, close.iloc[-1])
        if trade is None:
            state.pop(sym, None)
            return

        state[sym] = {
            "status":      "COMPRADA",
            "entry_price": trade["price"],
            "entry_cost":  trade["entry_cost"],
            "quantity":    trade["qty"],
            "max_value":   trade["entry_cost"],
            "stop_delta":  trade["entry_cost"] - config.STOP_DELTA_USDT,
        }
        await send_telegram_message(
            f"✅ COMPRA {sym} @ {trade['price']:.4f} (Qty {trade['qty']:.4f})\n"
            f"🧾 Coste total: {trade['entry_cost']:.2f} USDT (Fee {trade['commission']:.4f})"
        )
        logger.info(f"BUY {sym} qty={trade['qty']} price={trade['price']} cost={trade['entry_cost']}")
        return

    # -------- GESTIÓN --------
    if isinstance(rec, dict) and rec.get("status", "").startswith("COMPRADA"):
        df = await get_historical_data(sym, KLINE_INTERVAL_FASE2, 12)
        if df is None or df.empty:
            return
        last = float(df["close"].iloc[-1])
        ema9 = get_ema(df["close"].astype(float), 9)
        value_now = rec["quantity"] * last

        # --- disparadores ---
        if last < ema9.iloc[-1]:
            rec["exit_reason"] = "EMA9-EXIT"; freed.append(sym)
        elif update_light_stops(rec, rec["quantity"], last, config.STOP_DELTA_USDT):
            rec["exit_reason"] = "Δ-STOP"; freed.append(sym)
        elif value_now <= config.STOP_ABS_USDT:
            rec["exit_reason"] = "ABS-STOP"; freed.append(sym)

        if sym in freed:
            exit_reason = rec.pop("exit_reason", "EXIT")
            await process_sell_and_notify(
                client, sym, rec, last, exit_reason, exclusion_dict
            )

            state.pop(sym, None)


async def phase2_monitor(state, client, exclusion_dict):
    while True:
        await PAUSED.wait()
        if SHUTTING_DOWN.is_set():
            break
        freed = []
        try:
            await asyncio.gather(*[
                _evaluate(s, state, client, freed, exclusion_dict)
                for s in list(state.keys())
            ])
        except Exception:
            logger.exception("[fase2] crash")
            raise

        if freed:
            await phase3_replenish(state, exclusion_dict, len(freed))
        await asyncio.sleep(CHECK_INTERVAL)
