# fases/fase2.py – Compra tras cruce HMA‑8 ↑ EMA‑24, trailing, PnL real y salida por cruce bajista
# =================================================================================================
# Archivo completo. Sustituye tu fases/fase2.py por este contenido.
"""Fase 2 – ejecución y gestión de operaciones.

Detecta el cruce alcista HMA‑8/EMA‑24, abre una posición y aplica distintos
métodos de salida: trailing por ATR, Δ‑stop y stop absoluto.  Reporta el PnL real
o simulado vía Telegram.
"""

import config, asyncio, pandas as pd
from config import PAUSED, SHUTTING_DOWN
from binance.client import Client
from binance.helpers import round_step_size
from binance import exceptions as bexc
from binance.exceptions import BinanceAPIException
from config import (
    logger, DRY_RUN, TRAILING_USDT, LIGHT_MODE,
    EMA_SHORT, EMA_LONG, KLINE_INTERVAL_FASE2, CHECK_INTERVAL,
)
from utils import (
    get_historical_data, send_telegram_message, hull_moving_average, rsi,
    get_step_size, atr_stop, trailing_atr_trigger, delta_stop_trigger,
    absolute_stop_trigger,
)
from fases.fase3 import phase3_replenish

# ───── Parámetros ────────────────────────────────────────────────────────
CRUCE_MAX_BARS = 3               # velas máximo desde el cruce para comprar
SLOPE_BARS     = 2               # barras positivas requeridas en la EMA
TREND_TF       = Client.KLINE_INTERVAL_2HOUR
TREND_PERIOD   = 50
STOP_ATR_MULT  = 1.2             # trailing ATR cuando LIGHT_MODE=False

# ───── Conversión de comisión a USDT ─────────────────────────────────────
async def _fee_to_usdt(client, fills, quote="USDT") -> float:
    total = 0.0
    for f in fills:
        comm  = float(f["commission"])
        asset = f["commissionAsset"]
        if comm == 0:
            continue
        if asset == quote:
            total += comm
        elif asset == "BNB":
            bnb_price = float((await asyncio.to_thread(
                client.get_symbol_ticker, symbol="BNBUSDT"))["price"])
            total += comm * bnb_price
        else:  # asset == base
            total += comm * float(f["price"])
    return total

# ───── Helpers técnicos ─────────────────────────────────────────────────

def ema_slope_positive(series: pd.Series, bars=SLOPE_BARS) -> bool:
    return (series.diff().tail(bars) > 0).all()

# devuelve True si la EMA de mayor tiempo está en tendencia alcista
def ema_htf_up(close: pd.Series) -> bool:
    return close.ewm(span=TREND_PERIOD).mean().diff().iloc[-1] > 0

# ───── Market BUY wrapper ───────────────────────────────────────────────
async def _buy_market(sym, client, usdt, hint_price):
    if DRY_RUN:
        return dict(qty=usdt/hint_price, price=hint_price,
                    entry_cost=usdt, commission=0.0)
    try:
        o = await asyncio.to_thread(
            client.create_order,
            symbol=sym, side="BUY", type="MARKET", quoteOrderQty=usdt,
        )
    except BinanceAPIException as e:
        if e.code == -2010:   # balance insuficiente
            logger.warning(f"{sym}: saldo insuficiente para {usdt} USDT – se descarta")
            await send_telegram_message(
                f"⚠️ Sin saldo para comprar {sym}. Ajusta /set entry o recarga USDT."
            )
            return None
        raise  # otros errores siguen siendo críticos

    qty   = float(o["executedQty"])
    cost  = float(o["cummulativeQuoteQty"])
    fee   = await _fee_to_usdt(client, o.get("fills", []))
    price = cost/qty if qty else hint_price
    return dict(qty=qty, price=price, entry_cost=cost+fee, commission=fee)
# ───── Evaluador por símbolo ────────────────────────────────────────────
async def _evaluate(sym, state, client, freed):
    """Gestiona entrada y salida individual de ``sym``."""

    rec = state.get(sym)

    # -------- ENTRADA --------
    if isinstance(rec, str) and rec.startswith("RESERVADA"):
        df = await get_historical_data(sym, KLINE_INTERVAL_FASE2, 200)
        if df is None or len(df) < 100:
            return
        close = df["close"].astype(float)
        ema24 = close.ewm(span=EMA_LONG).mean()
        hma8  = hull_moving_average(close, EMA_SHORT)
        rsi14 = rsi(close, 14)

        diff  = hma8 - ema24
        cross = (diff > 0) & (diff.shift(1) <= 0)
        if not cross.any():
            return
        bars_since = len(df)-1 - df.index.get_loc(cross[cross].index[-1])
        if bars_since > CRUCE_MAX_BARS:
            if diff.iloc[-1] > 0:
                state.pop(sym, None)
            return
        if not ema_slope_positive(ema24):
            return
        d_htf = await get_historical_data(sym, TREND_TF, 120)
        if d_htf is None or not ema_htf_up(d_htf["close"]):
            return
        if rsi14.iloc[-1] <= 50:
            return

        trade = await _buy_market(sym, client, config.MIN_ENTRY_USDT, close.iloc[-1])
        if trade is None:          # fallo por saldo
            state.pop(sym, None)   # quita el símbolo y sigue
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
    if isinstance(rec, dict) and rec["status"].startswith("COMPRADA"):
        df = await get_historical_data(sym, KLINE_INTERVAL_FASE2, 12)
        if df is None or df.empty:
            return
        last = float(df["close"].iloc[-1])
        rec["max_price"] = max(rec.get("max_price", rec["entry_price"]), last)

        # Salida por cruce bajista
        close = df["close"].astype(float)
        ema24 = close.ewm(span=EMA_LONG).mean()
        hma8  = hull_moving_average(close, EMA_SHORT)
        if hma8.iloc[-1] - ema24.iloc[-1] <= 0 and hma8.iloc[-2] - ema24.iloc[-2] > 0:
            await send_telegram_message(f"🚨 CROSS-EXIT {sym} cruce bajista @ {last:.4f}")
            freed.append(sym)

        # Trailing ATR
        if not LIGHT_MODE and trailing_atr_trigger(rec, last, TRAILING_USDT):
            await send_telegram_message(f"🚨 STOP {sym} @ {last:.4f}")
            freed.append(sym)

        # Δ-stop
        if delta_stop_trigger(rec, last, config.STOP_DELTA_USDT):
            await send_telegram_message(f"🚨 Δ-STOP {sym} @ {last:.4f}")
            freed.append(sym)

        # stop absoluto
        if absolute_stop_trigger(rec["quantity"], last, config.STOP_ABS_USDT):
            await send_telegram_message(f"🚨 ABS-STOP {sym} @ {last:.4f}")
            freed.append(sym)

        # -------- VENTA --------
        # -------- VENTA --------
        if sym in freed:
            if not DRY_RUN:                       # ← venta real
                step = await get_step_size(sym)
                qty  = round_step_size(rec["quantity"], step)
                try:
                    sell = await asyncio.to_thread(
                        client.create_order,
                        symbol=sym, side="SELL", type="MARKET", quantity=qty,
                    )
                    value = float(sell.get("cummulativeQuoteQty", 0.0))
                    fee   = await _fee_to_usdt(client, sell.get("fills", []))
                    pnl   = value - fee - rec["entry_cost"]
                    pct   = 100 * pnl / rec["entry_cost"]

                    await send_telegram_message(
                        f"💰 VENTA {sym} @ {last:.4f}\n"
                        f"🔻 Valor vendido: {value:.2f} USDT\n"
                        f"🧾 Fee venta: {fee:.4f} USDT\n"
                        f"📊 PnL real: {pnl:.3f} USDT ({pct:.2f}%)"
                    )
                    logger.info(f"SELL {sym} pnl={pnl:.4f} pct={pct:.2f}")
                except bexc.BinanceAPIException as e:
                    logger.error(f"Venta {sym} err: {e}")

            else:                                 # ← simulación
                value = last * rec["quantity"]
                fee   = 0.0
                pnl   = value - fee - rec["entry_cost"]
                pct   = 100 * pnl / rec["entry_cost"]

                await send_telegram_message(
                    f"💰 (SIM) VENTA {sym} @ {last:.4f}\n"
                    f"🔻 Valor simulado: {value:.2f} USDT\n"
                    f"📊 PnL simulado: {pnl:.3f} USDT ({pct:.2f}%)"
                )
                logger.info(f"SIM-SELL {sym} pnl={pnl:.4f} pct={pct:.2f}")

            state.pop(sym, None)                  # ← dentro del bloque 'if sym in freed'

# ───── Loop principal ────────────────────────────────────────────────────
async def phase2_monitor(state, client, exclusion_dict):
    while True:
        await PAUSED.wait()                     # ← respeta /pausa
        if SHUTTING_DOWN.is_set():              # ← sale en /apagar
            break
        freed = []
        try:
            await asyncio.gather(*[
                _evaluate(s, state, client, freed) for s in list(state.keys())
            ])
        except Exception:
            logger.exception("[fase2] crash")
            raise  # supervisor reiniciará

        if freed:
            await phase3_replenish(state, exclusion_dict, len(freed))
        await asyncio.sleep(CHECK_INTERVAL)
