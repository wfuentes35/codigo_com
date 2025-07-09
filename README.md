# Codigo Com

Bot asincrónico para trading spot en Binance.  Utiliza una estrategia en varias fases:

1. **Fase 1** detecta rupturas de la banda superior de Bollinger con volumen elevado y marca los símbolos como precandidatos.
2. **Fase 2** espera un pullback a la zona entre esa banda y la EMA(9), compra tras el rebote y gestiona la posición con trailing ATR, Δ‑stop y stop absoluto.
3. **Fase 3** repone candidatos cuando hay huecos disponibles.
4. **Sync** mantiene el estado real de las posiciones y aplica stops en modo
   liviano cuando se opera desde otro dispositivo.

Las notificaciones se envían a Telegram y todas las llamadas a la API de Binance
están limitadas para evitar bloqueos.

El bot mantiene un sistema de **caché con TTL** para el listado de pares y los
históricos de precios, reduciendo el tráfico hacia Binance. Las notificaciones
al chat se centralizan mediante `send_telegram_message` con control antiflood.

## Características principales

- Estrategia de ruptura con Bollinger Bands y confirmación de volumen.
- Gestión de posiciones con EMA(9), trailing ATR y stops dinámicos.
- Sincronización periódica de balances para detectar operaciones externas.
- Comandos de Telegram para ajustar parámetros en caliente.

## Uso rápido

1. Copia `.env.example` a `.env` y completa tus claves.
2. Instala dependencias:

```bash
pip install -r requirements.txt
```

3. Ejecuta `./run_bot.sh` para iniciar el bot en un bucle de reinicio
automático.

Los parámetros pueden modificarse en caliente a través de comandos de Telegram
(`/set`, `/add`, `/elimina`, `/pausa`, etc.).

## Variables de entorno

Se requieren al menos las siguientes variables:

- `BINANCE_API_KEY` y `BINANCE_API_SECRET`
- `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`

Opcionalmente pueden definirse claves adicionales para `signals.py` con el
prefijo `BINANCE_API_KEY_<CUENTA>_spot`.

Consulta `.env.example` para ver todas las variables necesarias.

## Licencia

MIT
