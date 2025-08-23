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
    get_step_size, get_market_filters,
    safe_market_sell, log_sale_to_excel,
    set_cooldown,
)
from fases.fase3 import phase3_replenish


async def send_log_message(msg: str):
    """Envía un mensaje a Telegram y lo registra en el log."""
    logger.info(msg)
    await send_telegram_message(msg)


async def _fee_to_usdt(client, fills, quote="USDT") -> float:
    total = 0.0
    for f in fills:
        comm = float(f["commission"])
        asset = f["commissionAsset"]
        if comm == 0:
            continue
        if asset == quote:
            total += comm
        elif asset == "BNB":
            bnb_price = float((await asyncio.to_thread(
                client.get_symbol_ticker, symbol="BNBUSDT"))["price"])
            total += comm * bnb_price
        else:
            total += comm * float(f["price"])
    return total


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
    fee = await _fee_to_usdt(client, o.get("fills", []))
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
        df = await get_historical_data(sym, KLINE_INTERVAL_FASE2, 60)
        if df is None or len(df) < 30:
            return
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

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
            "status": "COMPRADA",
            "entry_price": trade["price"],
            "entry_cost": trade["entry_cost"],      # valor en USDT de la entrada
            "quantity": trade["qty"],
            "max_value": trade["entry_cost"],       # arranca en el valor de entrada
            "trailing_active": False,               # aún NO activo
            "stop_delta": None,                     # se definirá al activarse
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
        ema9 = get_ema(df["close"].astype(float), 9)

        last = float(df["close"].iloc[-1])
        qty = float(rec.get("quantity", 0.0))

        # valor actual de la posición
        value_now = qty * last

        # por compatibilidad, prioriza 'entry_cost'
        entry_cost = rec.get("entry_cost", rec.get("entry_value", 0.0))

        # umbral de activación: entrada + Δ + 1 USDT
        trigger_value = float(entry_cost) + float(config.STOP_DELTA_USDT) + 1.0

        rec.setdefault("trailing_active", False)
        rec.setdefault("stop_delta", None)
        rec.setdefault("exit_reason", None)

        # --- ACTIVACIÓN por valor ---
        if not rec.get("trailing_active") and value_now >= trigger_value:
            rec["trailing_active"] = True
            rec["max_value"] = value_now
            rec["stop_delta"] = value_now - float(config.STOP_DELTA_USDT)
            logger.info(
                f"[{sym}] Trailing ACTIVADO • value={value_now:.2f} • Δ-stop={rec['stop_delta']:.2f}"
            )

        # --- SEGUIMIENTO: solo subir cuando hay nuevo máximo ---
        if rec.get("trailing_active") and value_now > rec.get("max_value", 0.0):
            rec["max_value"] = value_now
            rec["stop_delta"] = value_now - float(config.STOP_DELTA_USDT)

        # --- disparadores ---
        if last < ema9.iloc[-1]:
            rec["exit_reason"] = "EMA9-EXIT"; freed.append(sym)
        elif rec.get("trailing_active") and rec.get("stop_delta") is not None and value_now <= rec["stop_delta"]:
            rec["exit_reason"] = "Δ-STOP"; freed.append(sym)
        elif value_now <= config.STOP_ABS_USDT:
            rec["exit_reason"] = "ABS-STOP"; freed.append(sym)

        if sym in freed:
            if not DRY_RUN:
                ok, sell = await safe_market_sell(client, sym, qty)
                if not ok:
                    logger.warning(f"No se vendió {sym}: {sell}")
                    await send_telegram_message(f"⚠️ Venta {sym} cancelada: {sell}")
                    state.pop(sym, None)
                    set_cooldown(exclusion_dict, sym, config.COOLDOWN_HOURS * 60)
                    return
                value = float(sell.get("cummulativeQuoteQty", 0.0))
                fee = await _fee_to_usdt(client, sell.get("fills", []))
            else:
                value = last * qty
                fee = 0.0
            pnl = value - fee - entry_cost
            pct = 100 * pnl / entry_cost if entry_cost > 0 else 0.0

            exit_type = rec.pop("exit_reason", "EXIT")
            texto = (
                f"🚨 {exit_type} {sym} @ {last:.4f}\n"
                f"🔻 Valor vendido: {value:.2f}\u202FUSDT\n"
                f"🧾 Fee: {fee:.4f}\u202FUSDT\n"
                f"📊 PnL: {pnl:.2f}\u202FUSDT ({pct:.2f}\u202F%)"
            )
            await send_telegram_message(texto)
            if not DRY_RUN:
                await log_sale_to_excel(sym, value, pnl, pct)
            logger.info(f"SELL {sym} pnl={pnl:.4f} pct={pct:.2f}")

            state.pop(sym, None)
            set_cooldown(exclusion_dict, sym, config.COOLDOWN_HOURS * 60)


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
