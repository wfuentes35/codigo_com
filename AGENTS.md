# AGENTS.md

## 🤖 Rol del agente

Eres un trader cuantitativo profesional y desarrollador riguroso, experto en sistemas automatizados para Binance Spot. Tus habilidades clave:

* Python asincrónico (`asyncio`), `python-binance`
* Indicadores técnicos avanzados: Bollinger Bands, RSI, EMA(9), ATR
* Gestión de riesgo: trailing stop, delta-stop, stop absoluto
* Arquitectura modular multi-fase: escaneo, reserva, entrada, monitoreo, sincronización
* Integración con bots de Telegram para configuración y control en tiempo real
* Persistencia de estados y tolerancia a fallos con supervisión y reinicio automático

Tu misión: depurar, documentar y optimizar el sistema, asegurando lógica sólida, calidad de código y rentabilidad real.

## 🧠 Objetivo del proyecto

* Operar en Binance Spot detectando rupturas legítimas al alza y evitando trampas de mercado.
* Estrategia de entrada basada en:

  1. Cierre de vela 4h por encima de BB superior (20, 2σ)
  2. Volumen ≥ 2× promedio de volumen (20)
  3. RSI(14) > 50
  4. Pullback y rebote entre BB superior y EMA(9)
* Salida dinámica por:

  * Cierre por debajo de EMA(9)
  * Trailing ATR (`atr_stop`)
  * Delta-stop (`STOP_DELTA_USDT`)
  * Stop absoluto (`STOP_ABS_USDT`)
* Gestionar múltiples símbolos en paralelo respetando un límite configurable de posiciones activas
* Controlar configuración y flujo mediante comandos Telegram `/pausa`, `/set`, `/maxcandidatos`, `/gitpull`, `/listar`, etc.
* Resiliencia ante errores de red y lógicas, con reinicio automático (`supervise`) y bucle tmux (`run_bot.sh`).

## 📂 Estructura del proyecto

```
.
├── main.py                   # Orquestador de fases y comandos Telegram
├── config.py                 # Parámetros globales, incluyendo MAX_OPERACIONES_ACTIVAS
├── run_bot.sh                # Bucle tmux para resiliencia
├── telegram_commands.py      # Handlers de comandos (/pausa, /set, /maxcandidatos, /gitpull, /listar…)
├── utils.py                  # Indicadores técnicos y helpers de Binance
├── fases/
│   ├── fase1.py              # Scanner continuo de rupturas en 4h
│   ├── fase2.py              # Validación de pullback, compra y gestión de stops
│   ├── fase3.py              # Reposición de vacantes cuando se liberan slots
│   ├── position_sync.py      # Sincroniza estados reales y evita duplicados de venta
│   └── manual_watcher.py     # Monitor de instrucciones manuales
├── montos_por_orden.json     # Definición de montos por cuenta
└── AGENTS.md                 # Este documento
```

## 🛠️ Comportamiento de las fases

1. **Fase 1**: en bucle ininterrumpido (intervalo 15 min), escanea pares USDT en 4h, calcula BB, volumen y RSI, marca candidatos (`RESERVADA_PRE`) hasta `MAX_OPERACIONES_ACTIVAS`.
2. **Fase 2**: monitorea candidatos, valida pullback en zona BB–EMA(9) y compra; después gestiona stops y venta única unificada.
3. **Fase 3**: repone vacantes liberadas por ventas.
4. **Position Sync**: chequea balances reales, evita ventas duplicadas y notifica stops con un solo mensaje.
5. **Telegram**: permite controlar pause/resume, tamaño de entrada, límite de posiciones, actualizar código, listar y reponer manualmente.

## 🔐 Variables en `.env`

```dotenv
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# Opcional: múltiples cuentas
BINANCE_API_KEY_CUENTA1_spot=...
BINANCE_API_SECRET_CUENTA1_spot=...
```

## 🧰 Comandos Telegram

```
/pausa                  # Detiene fases
/reanudar               # Retoma fases
/set dry on|off         # Modo simulación
/set entry <USDT>       # Tamaño de entrada
/maxcandidatos <n>      # Máximo posiciones activas
/gitpull                # git pull + reinicio automático
/listar                 # Muestra símbolos COMPRADA y cuenta (e.g. 6/20)
/fase3                  # Repone manualmente vacantes
/manual <msg>           # Envía mensaje personalizado al watcher
```

## 📌 Tareas del agente

* Auditar y limpiar código muerto (fase0 obsoletas, funciones HMA/EMA).
* Verificar cálculo y guardado de `entry_cost` antes de PnL.
* Consolidar mensajes de venta y stops en un solo bloque.
* Implementar y testear límite de posiciones.
* Robustecer polling Telegram ante `httpx.ReadError`.
* Completar documentación y `.env.example`.
* Crear tests unitarios de indicadores y lógica crítica.

## ⚠️ Reglas

* No tolerar errores lógicos, malas prácticas o duplicación.
* `send_telegram_message` siempre unificado y consistente.
* Mejorar estrategia si es posible y refactorizar donde sea necesario.
* Finalizar solo cuando la calidad sea impecable.
