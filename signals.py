# signals.py
import os
import json
import math
import asyncio
import logging
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
telegram_bot = Bot(token=TELEGRAM_TOKEN)

async def send_telegram_message(message: str):
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Mensaje enviado a Telegram: {message}")
    except Exception as e:
        logger.error(f"Error al enviar mensaje por Telegram: {e}")
    finally:
        await asyncio.sleep(2.0)

async def comprar_criptomoneda(account_name: str, symbol: str) -> dict:
    """Orden de compra Spot usando quoteOrderQty."""
    try:
        api_key = os.getenv(f"BINANCE_API_KEY_{account_name}_spot")
        api_secret = os.getenv(f"BINANCE_API_SECRET_{account_name}_spot")
        if not api_key or not api_secret:
            logger.error(f"Claves API no encontradas para {account_name} en SPOT.")
            return {}
        client = Client(api_key, api_secret)

        # Leer monto_por_orden
        monto_por_orden = 0
        try:
            with open('montos_por_orden.json','r') as f:
                data = json.load(f)
                monto_por_orden = data[account_name]['spot']
        except Exception as e:
            logger.error(f"Error al leer 'montos_por_orden.json': {e}")
            return {}

        if monto_por_orden <= 0:
            logger.error(f"Monto por orden inválido para {account_name}: {monto_por_orden}")
            return {}

        orden = await asyncio.to_thread(client.order_market_buy, symbol=symbol, quoteOrderQty=monto_por_orden)
        fills = orden.get('fills', [])
        if fills:
            fill = fills[0]
            price = fill.get('price','N/A')
            qty = fill.get('qty','N/A')
            commission = fill.get('commission','N/A')
        else:
            price = 'N/A'
            qty = 'N/A'
            commission = 'N/A'

        msg = (
            f"Orden de COMPRA ejecutada para {symbol} en {account_name}:\n"
            f"Precio: {price}\n"
            f"Cantidad: {qty}\n"
            f"Comisión: {commission}"
        )
        await send_telegram_message(msg)
        logger.info(msg)
        return orden
    except Exception as e:
        logger.error(f"Error al comprar {symbol} en {account_name}: {e}")
        return {}

def obtener_balance(client: Client, asset: str) -> float:
    try:
        balance_info = client.get_asset_balance(asset=asset)
        if balance_info:
            return float(balance_info['free'])
        else:
            return 0.0
    except Exception as e:
        logger.error(f"Error al obtener balance de {asset}: {e}")
        return 0.0

async def vender_criptomoneda(account_name: str, symbol: str) -> dict:
    """Vende todo el saldo disponible en Spot en una sola orden."""
    try:
        api_key = os.getenv(f"BINANCE_API_KEY_{account_name}_spot")
        api_secret = os.getenv(f"BINANCE_API_SECRET_{account_name}_spot")
        if not api_key or not api_secret:
            logger.error(f"Claves API no encontradas para {account_name} en SPOT.")
            return {}
        client = Client(api_key, api_secret)

        asset = symbol.replace("USDT","")
        cantidad_a_vender = obtener_balance(client, asset)
        if cantidad_a_vender <= 0:
            logger.info(f"No hay suficiente {asset} para vender en {account_name}.")
            return {}

        info = client.get_symbol_info(symbol)
        if info is None:
            logger.error(f"No se encontró info del símbolo {symbol}.")
            return {}

        lot_size_filter = next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        step_size = float(lot_size_filter['stepSize'])
        min_qty = float(lot_size_filter['minQty'])
        precision = int(round(-math.log10(step_size))) if step_size != 0 else 0

        cantidad_a_vender = cantidad_a_vender - (cantidad_a_vender % step_size)
        cantidad_a_vender = round(cantidad_a_vender, precision)

        if cantidad_a_vender < min_qty:
            logger.warning(f"La cantidad a vender ({cantidad_a_vender}) < minQty ({min_qty}). Abortando.")
            return {}

        orden = await asyncio.to_thread(client.order_market_sell, symbol=symbol, quantity=cantidad_a_vender)
        fills = orden.get('fills', [])
        if fills:
            fill = fills[0]
            price = fill.get('price','N/A')
            qty = fill.get('qty','N/A')
            commission = fill.get('commission','N/A')
        else:
            price = 'N/A'
            qty = cantidad_a_vender
            commission = 'N/A'
        cummulative_quote_qty = orden.get('cummulativeQuoteQty','N/A')

        msg = (
            f"Orden de VENTA ejecutada para {symbol} en {account_name}:\n"
            f"Precio: {price}\n"
            f"Cantidad: {qty}\n"
            f"Comisión: {commission}\n"
            f"Total USDT: {cummulative_quote_qty}"
        )
        await send_telegram_message(msg)
        logger.info(msg)
        return orden
    except Exception as e:
        logger.error(f"Error al vender {symbol} en {account_name}: {e}")
        return {}
