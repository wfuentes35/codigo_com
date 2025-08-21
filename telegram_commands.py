# telegram_commands.py – control por Telegram
# ==========================================
import asyncio, os, sys, signal, subprocess, config

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from config import (
    TELEGRAM_TOKEN,
    logger,
    FASE0_SETTINGS,
    update_fase0_setting,
    update_min_entry_usdt,
    update_max_operaciones_activas,
)

from utils import get_step_size, send_telegram_message
from binance.helpers import round_step_size
from binance import exceptions as bexc


# ────────────────────────────────────────────────────────────────
async def _liquidate_all(client):
    """Vende todo el balance spot (excepto USDT) y reporta fallos."""
    account = await asyncio.to_thread(client.get_account)
    tasks = []
    for bal in account["balances"]:
        asset = bal["asset"]
        qty   = float(bal["free"]) + float(bal["locked"])
        if asset == "USDT" or qty == 0:
            continue
        sym  = f"{asset}USDT"
        step = await get_step_size(sym)
        qty  = round_step_size(qty, step)
        tasks.append(asyncio.to_thread(
            client.create_order,
            symbol=sym, side="SELL", type="MARKET", quantity=qty
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    sold, failed = [], []
    for res in results:
        if isinstance(res, dict):
            sold.append(res["symbol"])
        else:
            failed.append(str(res))

    if sold:
        await send_telegram_message("✅ Vendido: " + ", ".join(sold))
    if failed:
        await send_telegram_message("⚠️ Falló venta de algunas posiciones:\n" +
                                    "\n".join(failed))
    return not failed          # True si todo OK

# ----------------------------------------------------------------------
def build_telegram_app(
        state_dict: dict,
        exclusion_dict: dict,
        paused_event,             # ← PAUSED  (asyncio.Event)
        shutdown_event):          # ← SHUTTING_DOWN (asyncio.Event)
    """
    Devuelve la Application de python-telegram-bot con todos los handlers.
    """
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # ---------- errores de red ----------
    from telegram.error import NetworkError
    async def tg_error_handler(update, context):
        if isinstance(context.error, NetworkError):
            logger.debug(f"TG network hiccup: {context.error}")
        else:
            logger.exception("Unhandled TG error")
    app.add_error_handler(tg_error_handler)

    # ---------- comandos de control ----------
    async def pause_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if paused_event.is_set():
            paused_event.clear()
            await update.message.reply_text("⏸️ Bot en pausa.")
        else:
            await update.message.reply_text("Ya estaba en pausa.")

    async def resume_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not paused_event.is_set():
            paused_event.set()
            await update.message.reply_text("▶️ Bot reanudado.")
        else:
            await update.message.reply_text("Ya estaba activo.")

    async def shutdown_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("♻️ Vendiendo todo y apagando…")
        shutdown_event.set()             # avisa a los loops que salgan
        try:
            await _liquidate_all(config.client)
        except Exception as e:
            await update.message.reply_text(f"Error vendiendo: {e}")
        await asyncio.sleep(2)
        sys.exit(0)                      # proceso terminará; tmux / systemd lo maneja

    async def restart_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("♻️ Reiniciando proceso…")
        logger.warning("/restart solicitado")
        sys.exit(0)                      # run_bot.sh o systemd relanzan

    # ---------- /add ----------
    async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text("Uso: /add BTC   (o BTCUSDT)")
        raw = ctx.args[0].upper()
        sym = raw if raw.endswith("USDT") else f"{raw}USDT"
        if sym in state_dict:
            msg = f"{sym} ya está en lista."
        else:
            state_dict[sym] = "RESERVADA"
            msg = f"{sym} añadido a Fase 2."
        await update.message.reply_text(msg)
        logger.info(f"/add {sym}")

    # ---------- /elimina ----------
    async def del_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text("Uso: /elimina BTC")
        raw = ctx.args[0].upper()
        sym = raw if raw.endswith("USDT") else f"{raw}USDT"
        if state_dict.pop(sym, None) is not None:
            exclusion_dict.pop(sym, None)
            msg = f"{sym} eliminado."
        else:
            msg = f"{sym} no estaba en lista."
        await update.message.reply_text(msg)
        logger.info(f"/elimina {sym}")
    # ---------- /listar ----------
    async def listar_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        activos = [
            (sym, rec) for sym, rec in state_dict.items()
            if isinstance(rec, dict) and rec.get("status", "").startswith("COMPRADA")
        ]
        reservadas = [
            s for s, r in state_dict.items()
            if isinstance(r, dict) and r.get("status") == "RESERVADA_PRE"
        ]

        header = (
            f"🎯 {len(activos)}/{config.MAX_OPERACIONES_ACTIVAS} operaciones activas\n"
            f"Δ‑stop={config.STOP_DELTA_USDT}  stop_abs={config.STOP_ABS_USDT}"
        )

        body = []
        if activos:
            body.append("💰 Posiciones abiertas:")
            for sym, rec in activos:
                qty = float(rec.get("quantity", 0.0))
                tkr = await asyncio.to_thread(config.client.get_symbol_ticker, symbol=sym)
                last = float(tkr["price"])
                entry_cost = float(rec.get("entry_cost", 0.0))
                pnl = last * qty - entry_cost
                pct = 100 * pnl / entry_cost if entry_cost > 0 else 0.0
                delta = rec.get("stop_delta")
                delta_txt = f"{delta:.2f}" if isinstance(delta, (int, float)) else "—"
                line = f"{sym}: PnL={pnl:+.2f}\u202F({pct:+.2f}\u202F%) | Δ-stop={delta_txt}"
                body.append(line)

        if reservadas:
            body.append("\n⏳ Reservadas:")
            body.append("  " + ", ".join(reservadas))

        await update.message.reply_text("\n".join([header, *body]))
    # ---------- /fase3 ----------
    async def phase3_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from fases.fase3 import phase3_search_new_candidates
        await phase3_search_new_candidates(state_dict, 1, exclusion_dict)
        await update.message.reply_text("Fase 3 encolada / ejecutada.")
        logger.info("/fase3 manual")

    # ---------- /set ----------
    async def set_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            return await update.message.reply_text(
                "Uso:\n"
                "  /set stop_delta <usd>\n"
                "  /set stop_abs   <usd>\n"
                "  /set light on|off\n"
                "  /set entry <usd>\n"
                "  /set fase0 <interval|min_vol|min_ratio> <valor>"
                "  /set dry on|off\n"
            )
        sub = ctx.args[0].lower()

         # dentro de set_cmd
        # activar / desactivar DRY_RUN
        if sub == "dry" and ctx.args[1].lower() in {"on", "off"}:
            config.DRY_RUN = ctx.args[1].lower() == "on"
            await update.message.reply_text(f"✅ DRY_RUN = {config.DRY_RUN}")
            logger.info(f"/set dry {config.DRY_RUN}")
            return
        # stop_delta / stop_abs
        if sub in {"stop_delta", "stop_abs"} and len(ctx.args) == 2:
            try:
                val = float(ctx.args[1])
            except ValueError:
                return await update.message.reply_text("Debe ser numérico.")
            if sub == "stop_delta":
                config.STOP_DELTA_USDT = val
            else:
                config.STOP_ABS_USDT = val
            await update.message.reply_text(f"✅ {sub} = {val}")
            logger.info(f"/set {sub} {val}")
            return

        # light mode
        if sub == "light" and ctx.args[1].lower() in {"on", "off"}:
            config.LIGHT_MODE = ctx.args[1].lower() == "on"
            await update.message.reply_text(f"✅ LIGHT_MODE = {config.LIGHT_MODE}")
            logger.info(f"/set light {config.LIGHT_MODE}")
            return

        # tamaño de entrada
        if sub in {"entry", "size"} and len(ctx.args) == 2:
            try:
                val = float(ctx.args[1])
                if val < 5:
                    raise ValueError
            except ValueError:
                return await update.message.reply_text("Debe ser un número ≥ 5 USDT.")

            config.MIN_ENTRY_USDT = val   # cambia en caliente
            await update.message.reply_text(f"✅ Tamaño entrada = {config.MIN_ENTRY_USDT}")
            logger.info(f"/set entry {val}")
            return

        # parámetros de Fase 0
        if sub == "fase0" and len(ctx.args) == 3:
            err = update_fase0_setting(ctx.args[1], ctx.args[2])
            val = FASE0_SETTINGS.get(ctx.args[1], "?")
            msg = "❌ " + err if err else f"✅ Fase0 {ctx.args[1]} = {val}"
            return await update.message.reply_text(msg)
        
        


        await update.message.reply_text("Parámetros incorrectos. Usa /set help")

    # ---------- /maxcandidatos ----------
    async def maxcandidatos_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            return await update.message.reply_text(
                f"Uso: /maxcandidatos <número> (actual {config.MAX_OPERACIONES_ACTIVAS})"
            )
        err = update_max_operaciones_activas(ctx.args[0])
        if err:
            return await update.message.reply_text(err)
        await update.message.reply_text(
            f"✅ MAX_OPERACIONES_ACTIVAS = {config.MAX_OPERACIONES_ACTIVAS}"
        )

    # ---------- /gitpull ----------
    async def gitpull_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Actualizando código…")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "pull", "origin", "main"],
            capture_output=True,
            text=True,
        )
        out = (proc.stdout + proc.stderr).strip()
        if out:
            await update.message.reply_text(f"`{out}`", parse_mode="Markdown")
        await update.message.reply_text("✅ Repositorio actualizado; reiniciando…")
        await restart_cmd(update, ctx)

    # ---------- registro ----------
    app.add_handler(CommandHandler("add",      add_cmd))
    app.add_handler(CommandHandler("elimina",  del_cmd))
    app.add_handler(CommandHandler("listar",   listar_cmd))
    app.add_handler(CommandHandler("maxcandidatos", maxcandidatos_cmd))
    app.add_handler(CommandHandler("gitpull",  gitpull_cmd))
    app.add_handler(CommandHandler("fase3",    phase3_cmd))
    app.add_handler(CommandHandler("set",      set_cmd))

    app.add_handler(CommandHandler("pausa",    pause_cmd))
    app.add_handler(CommandHandler("reanudar", resume_cmd))
    app.add_handler(CommandHandler("apagar",   shutdown_cmd))
    app.add_handler(CommandHandler("restart",  restart_cmd))

    return app
