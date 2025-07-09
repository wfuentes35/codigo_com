# Codigo Com

Bot asincrónico para trading spot en Binance.  Utiliza una estrategia en varias fases:

1. **Fase 1** busca rupturas al alza en todos los pares USDT. Se detecta un
   cierre por encima de la banda superior de Bollinger con volumen elevado y RSI
   positivo.
2. **Fase 2** monitoriza los símbolos marcados y espera un pullback hacia la
   zona comprendida entre la banda superior y la EMA(9).  Si el precio rebota
   desde ese nivel se compra y la posición se gestiona con trailing por ATR,
   Δ‑stop y stop absoluto.
3. **Fase 3** repone candidatos cuando hay huecos disponibles.
4. **Sync** mantiene el estado real de las posiciones y aplica stops en modo
   liviano cuando se opera desde otro dispositivo.

Las notificaciones se envían a Telegram y todas las llamadas a la API de Binance
están limitadas para evitar bloqueos.

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

### Archivo `manual_candidates.txt`

Si creas un fichero llamado `manual_candidates.txt` (un símbolo por línea), el
bot lo vigilará y añadirá esos pares como candidatos "RESERVADA" para que la
Fase 2 valide el pullback.

## Variables de entorno

Se requieren al menos las siguientes variables:

- `BINANCE_API_KEY` y `BINANCE_API_SECRET`
- `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`

Opcionalmente pueden definirse claves adicionales por cuenta con el
prefijo `BINANCE_API_KEY_<CUENTA>_spot`.

### Comando `/set`

Desde Telegram puedes modificar parámetros en caliente. Envía `/set help` para
ver la sintaxis.  Los ajustes disponibles son:

- `/set stop_delta <usd>` – margen usado por el Δ‑stop.
- `/set stop_abs <usd>` – stop absoluto en USDT.
- `/set light on|off` – activa o desactiva el modo ligero.
- `/set entry <usd>` – tamaño de la entrada por operación.
- `/set fase0 <interval|min_vol|min_ratio> <valor>` – parámetros de Fase 0.
- `/set dry on|off` – modo simulación sin órdenes reales.

## Licencia

MIT
