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
import config
from config import PAUSED, SHUTTING_DOWN
from binance.helpers import round_step_size
from binance import exceptions as bexc
from binance.exceptions import BinanceAPIException
from config import (
    logger, DRY_RUN, TRAILING_USDT, LIGHT_MODE,
    KLINE_INTERVAL_FASE2, CHECK_INTERVAL,
)
from utils import (
    get_historical_data, send_telegram_message,
    get_bollinger_bands, get_ema,
    get_step_size, atr_stop, trailing_atr_trigger, delta_stop_trigger,
    absolute_stop_trigger,
)
from fases.fase3 import phase3_replenish

# ----------------------------------------------------------------------
STOP_ATR_MULT = 1.2

def _active_positions(state: dict) -> int:
    return sum(
        1 for r in state.values()
        if isinstance(r, dict) and str(r.get("status", "")).startswith("COMPRADA")
    )


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
            return None
        raise

    qty = float(o["executedQty"])
    cost = float(o["cummulativeQuoteQty"])
    fee = await _fee_to_usdt(client, o.get("fills", []))
    price = cost / qty if qty else hint_price
    return dict(qty=qty, price=price, entry_cost=cost + fee, commission=fee)


async def _evaluate(sym, state, client, freed):
    rec = state.get(sym)
    status = rec if isinstance(rec, str) else rec.get("status")

    # -------- ENTRADA --------
    if status == "RESERVADA_PRE":
        if _active_positions(state) >= config.MAX_OPERACIONES_ACTIVAS:
            return
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

        trade = await _buy_market(sym, client, config.MIN_ENTRY_USDT, close.iloc[-1])
        if trade is None:
            state.pop(sym, None)
            return

        state[sym] = dict(
            status="COMPRADA",
            entry_price=trade["price"],
            entry_cost=trade["entry_cost"],
            quantity=trade["qty"],
            stop=atr_stop(df, trade["price"], STOP_ATR_MULT),
            max_price=trade["price"],
            commission=trade["commission"],
        )
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
        rec["max_price"] = max(rec.get("max_price", rec["entry_price"]), last)

        ema9 = get_ema(df["close"].astype(float), 9)
        if last < ema9.iloc[-1]:
            await send_telegram_message(f"🚨 EMA9-EXIT {sym} @ {last:.4f}")
            freed.append(sym)

        if not LIGHT_MODE and trailing_atr_trigger(rec, last, TRAILING_USDT):
            await send_telegram_message(f"🚨 STOP {sym} @ {last:.4f}")
            freed.append(sym)

        if delta_stop_trigger(rec, last, config.STOP_DELTA_USDT):
            await send_telegram_message(f"🚨 Δ-STOP {sym} @ {last:.4f}")
            freed.append(sym)

        if absolute_stop_trigger(rec["quantity"], last, config.STOP_ABS_USDT):
            await send_telegram_message(f"🚨 ABS-STOP {sym} @ {last:.4f}")
            freed.append(sym)

        if sym in freed:
            if not DRY_RUN:
                step = await get_step_size(sym)
                qty = round_step_size(rec["quantity"], step)
                try:
                    sell = await asyncio.to_thread(
                        client.create_order,
                        symbol=sym, side="SELL", type="MARKET", quantity=qty,
                    )
                    value = float(sell.get("cummulativeQuoteQty", 0.0))
                    fee = await _fee_to_usdt(client, sell.get("fills", []))
                    pnl = value - fee - rec["entry_cost"]
                    pct = 100 * pnl / rec["entry_cost"]

                    await send_telegram_message(
                        f"💰 VENTA {sym} @ {last:.4f}\n"
                        f"🔻 Valor vendido: {value:.2f} USDT\n"
                        f"🧾 Fee venta: {fee:.4f} USDT\n"
                        f"📊 PnL real: {pnl:.3f} USDT ({pct:.2f}%)"
                    )
                    logger.info(f"SELL {sym} pnl={pnl:.4f} pct={pct:.2f}")
                except bexc.BinanceAPIException as e:
                    logger.error(f"Venta {sym} err: {e}")
            else:
                value = last * rec["quantity"]
                fee = 0.0
                pnl = value - fee - rec["entry_cost"]
                pct = 100 * pnl / rec["entry_cost"]
                await send_telegram_message(
                    f"💰 (SIM) VENTA {sym} @ {last:.4f}\n"
                    f"🔻 Valor simulado: {value:.2f} USDT\n"
                    f"📊 PnL simulado: {pnl:.3f} USDT ({pct:.2f}%)"
                )
                logger.info(f"SIM-SELL {sym} pnl={pnl:.4f} pct={pct:.2f}")

            state.pop(sym, None)


async def phase2_monitor(state, client, exclusion_dict):
    while True:
        await PAUSED.wait()
        if SHUTTING_DOWN.is_set():
            break
        freed = []
        try:
            await asyncio.gather(*[
                _evaluate(s, state, client, freed) for s in list(state.keys())
            ])
        except Exception:
            logger.exception("[fase2] crash")
            raise

        if freed:
            await phase3_replenish(state, exclusion_dict, len(freed))
        await asyncio.sleep(CHECK_INTERVAL)
