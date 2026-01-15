import asyncio
import asyncpg
import logging
import os
import hmac
import hashlib
import json
import sys
import aiohttp
import uvicorn
import httpx
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from decimal import Decimal
from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from fastapi import FastAPI, Request, HTTPException

# --- 1. CONFIGURACIÓN ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DB_URL")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY") 
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

NOWPAYMENTS_IPS = ["127.0.0.1"]
IPN_SECRET_KEY = os.getenv("NOWPAYMENTS_IPN_KEY")

if not BOT_TOKEN:
    print("ERROR CRÍTICO: No se encontró el BOT_TOKEN")
    sys.exit()

if not DB_URL:
    print("ERROR CRÍTICO: No se encontró la DB_URL")
    sys.exit()

ADMIN_ID = 7430692266  # Tu ID de Telegram

logging.basicConfig(level=logging.INFO)

# Variable global para el pool de conexiones
db_pool = None

# --- FASTAPI APP ---
app = FastAPI()

# --- EVENTOS DE LA APLICACIÓN FASTAPI ---
@app.on_event("startup")
async def startup_event():
    """
    Esta función se ejecuta automáticamente cuando el servidor FastAPI está a punto de iniciar.
    Es el lugar perfecto para inicializar recursos como la base de datos y el bot.
    """
    global bot, dp, db_pool # Declara que vas a modificar las globales

    print("Iniciando evento de startup de FastAPI...")

    # 1. Inicializar la base de datos PRIMERO
    await init_db()
    print("Base de datos PostgreSQL inicializada correctamente.")

    # 2. Inicializar el bot de Telegram
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    print("Bot de Telegram inicializado.")

    # 3. Registrar todos los handlers y middleware del bot
    # (Mueve todo el registro de handlers aquí para que se hagan sobre el dp correcto)
    dp.message.middleware(BlockedMiddleware())
    dp.message(Command("start"))(cmd_start)
    dp.message(F.text == "📋 Menú Principal 📋", StateFilter(None))(main_menu)
    dp.message(F.text == "⚙️Menu⚙️", StateFilter(None))(more_options)
    dp.message(F.text == "🧑‍🤝‍🧑 Referidos")(show_referrals)
    dp.message(F.text == "ℹ️ información")(about_goldplant)
    dp.message(F.text == "📊 Historial")(show_history)
    dp.message(F.text == "↩️ Volver al Menú Principal")(back_to_main_menu)
    dp.message(F.text == "↩️ Volver a Más Opciones")(back_to_more_options)
    dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))(process_buy_callback)
    dp.message(F.text == "❌ Cancelar", StateFilter(None))(cmd_cancel)
    dp.message(WithdrawState.waiting_for_add_balance_amount)(process_add_balance_amount)
    dp.message(F.text.startswith('/') == False, StateFilter(None))(handle_menu)
    dp.message(WithdrawState.waiting_for_card)(process_card)
    dp.message(WithdrawState.waiting_for_name)(process_name)
    dp.message(F.text == "❌ Cancelar Retiro")(cancel_withdraw)
    dp.callback_query(lambda c: c.data and c.data.startswith("approve_"))(admin_approve_withdraw)
    dp.callback_query(lambda c: c.data and c.data.startswith("reject_"))(admin_reject_withdraw)
    dp.message(WithdrawState.waiting_for_rejection_reason)(process_rejection_reason)
    dp.message(Command("block"))(cmd_block_user)
    dp.message(Command("unblock"))(cmd_unblock_user)
    dp.message(Command("add"))(cmd_add_balance)
    print("Handlers de aiogram registrados.")

    # 4. Configurar el webhook de Telegram
    await set_webhook()
    print("Webhook de Telegram configurado.")

    print("✅ Evento de startup completado. El servidor está listo para recibir peticiones.")

@app.post("/nowpayments_webhook", headers={"User-Agent": "*"})
async def nowpayments_webhook(request: Request):
    # 1. Obtener la firma que envía NOWPayments
    received_signature = request.headers.get("x-nowpayments-sig")
    if not received_signature:
        raise HTTPException(status_code=403, detail="Firma no proporcionada")

    # 2. Leer el cuerpo de la petición como bytes directamente
    body = await request.body()

    # 3. Calcular tu propia firma para comparar
    # Tanto la clave como el mensaje DEBEN estar en bytes.
    ipn_secret = os.getenv("NOWPAYMENTS_IPN_KEY")
    if not ipn_secret:
        raise HTTPException(status_code=500, detail="Clave IPN no configurada en el servidor")

    # --- CORRECCIÓN CLAVE ---
    # Asegúrate de que el cuerpo esté codificado en UTF-8 antes de calcular el hash.
    # Aunque `body` ya es bytes, a veces puede haber problemas si no se especifica la codificación.
    # Forzar UTF-8 es la práctica más segura.
    calculated_signature = hmac.new(
        ipn_secret.encode('utf-8'),  # La clave secreta en bytes
        body,                        # El cuerpo también en bytes (¡sin decode!)
        hashlib.sha256
    ).hexdigest()

    # 4. Comparar las firmas de forma segura
    if not hmac.compare_digest(received_signature, calculated_signature):
        # Para depurar, puedes imprimir las firmas y el cuerpo
        # print(f"DEBUG - Firma recibida: {received_signature}")
        # print(f"DEBUG - Firma calculada: {calculated_signature}")
        # print(f"DEBUG - Cuerpo recibido (bytes): {body}")
        # print(f"DEBUG - Cuerpo recibido (string): {body.decode('utf-8')}")
        raise HTTPException(status_code=403, detail="Firma inválida")

    # 5. Si la firma es válida, procesar el pago
    # Ahora es seguro parsear el JSON porque ya pasamos la verificación de seguridad
    payment_data = await request.json()

    # Verificar que el pago está 'finished'
    if payment_data.get("payment_status") == "finished":
        order_id = payment_data.get("order_id")
        user_id = order_id.split('_')[0]  # Extraer el user_id del order_id
        amount_paid = payment_data.get("actually_paid")

        # Aquí va tu lógica para actualizar la base de datos
        print(f"✅ Pago confirmado para usuario {user_id}. Cantidad: {amount_paid}")

        # --- ¡AQUÍ ESTABA EL FALTA QUE PROVOCA QUE NO SE ACTUALICE EL BALANCE! ---
        # Necesitas implementar esta función para que el saldo realmente se añada.
        async def update_user_balance_in_db(user_id, amount):
            global db_pool
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    amount, user_id
                )
                print(f"Balance actualizado para el usuario {user_id}: +${amount}")

        # Llama a la función para actualizar el balance
        await update_user_balance_in_db(int(user_id), float(amount_paid))

        # Opcional: Notificar al usuario
        try:
            await bot.send_message(
                int(user_id),
                f"✅ ¡Pago recibido!\n\nSe han añadido ${amount_paid:.2f} a tu balance. Gracias por tu recarga."
            )
        except Exception as e:
            print(f"No se pudo notificar al usuario {user_id}: {e}")

    # Devuelve una respuesta 200 OK a NOWPayments para confirmar que recibiste el webhook
    return Response(status_code=200)

# --- 2. BASE DE DATOS (POSTGRESQL) ---

async def init_db():
    """Inicializa la conexión y crea las tablas si no existen."""
    global db_pool
    try:
        # Crear el pool de conexiones
        db_pool = await asyncpg.create_pool(DB_URL, min_size=5, max_size=50)
        async with db_pool.acquire() as conn:
            # Tabla users (CORREGIDA)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance NUMERIC(15, 2) DEFAULT 0.0,
                    total_invested NUMERIC(15, 2) DEFAULT 0.0,
                    referred_by BIGINT,
                    last_watered TIMESTAMP,
                    card_number TEXT,
                    full_name TEXT,
                    is_blocked BOOLEAN DEFAULT FALSE -- <-- AÑADE ESTA LÍNEA
                )
            ''')
            
            # Tabla trees
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trees (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    cost NUMERIC(15, 2),
                    daily_return NUMERIC(15, 2),
                    purchase_date TIMESTAMP,
                    last_claimed TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Tabla withdrawals
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount NUMERIC(15, 2),
                    card_number TEXT,
                    full_name TEXT,
                    status TEXT DEFAULT 'pending',
                    request_date TIMESTAMP,
                    processed_date TIMESTAMP,
                    rejection_reason TEXT DEFAULT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            ''')
            
        print("Base de datos PostgreSQL inicializada correctamente.")
    except Exception as e:
        print(f"Error DB: {e}")

async def create_nowpayments_invoice(amount: float, user_id: int) -> str:
    """
    Crea una factura en NowPayments y devuelve la URL de pago.
    """
    # Creamos un order_id único para rastrear el pago
    order_id = f"{user_id}_{int(datetime.now().timestamp())}"
    
    invoice_data = {
        "price_amount": amount,
        "price_currency": "USD",  # Moneda en la que quieres recibir
        "order_id": order_id,
        "ipn_callback_url": f"{os.getenv('WEBHOOK_URL')}/nowpayments_webhook", # Usa la variable de entorno
        "order_description": f"Recarga de saldo para el usuario {user_id}",
        "success_url": "https://t.me/Goldplant_bot", # Opcional: a dónde redirige tras pagar
        "cancel_url": "https://t.me/Goldplant_bot"   # Opcional: a dónde redirige si cancela
    }

    headers = {
        "x-api-key": os.getenv("NOWPAYMENTS_API_KEY"),
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://api.nowpayments.io/v1/invoice",
                json=invoice_data,
                headers=headers
            )
            response.raise_for_status()  # Lanza un error si la petición falló (código 4xx o 5xx)
            
            result = response.json()
            return result["invoice_url"]

        except httpx.HTTPStatusError as e:
            print(f"Error HTTP al crear factura: {e.response.status_code} - {e.response.text}")
            raise Exception(f"Error al contactar NowPayments: {e.response.status_code}")
        except Exception as e:
            print(f"Error inesperado al crear factura: {e}")
            raise Exception(f"No se pudo generar la factura de pago.")

# --- 3. FUNCIONES AUXILIARES (ASYNC) ---
async def send_long_message(message: types.Message, text: str, parse_mode="HTML"):
    MAX_LENGTH = 4096
    for i in range(0, len(text), MAX_LENGTH):
        await message.answer(text[i:i + MAX_LENGTH], parse_mode=parse_mode)

async def get_user_data(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT balance, total_invested, last_watered, referred_by, card_number, full_name FROM users WHERE user_id = $1",
            user_id
        )

async def register_user_if_new(user_id, username, referrer_id=None):
    async with db_pool.acquire() as conn:
        # Usamos INSERT ... ON CONFLICT para evitar race conditions o errores de duplicados
        # PostgreSQL no tiene "IF NOT EXISTS" en INSERT directo, pero ON CONFLICT DO NOTHING simula esto.
        # Sin embargo, necesitamos saber si era nuevo. Hacemos una query primero o comprobamos filas afectadas.
        # Estrategia: Intentar insertar, si error de duplicado, ignorar.
        
        try:
            await conn.execute(
                "INSERT INTO users (user_id, username, referred_by) VALUES ($1, $2, $3)",
                user_id, username, referrer_id
            )
            return True
        except asyncpg.UniqueViolationError:
            # El usuario ya existe
            return False

async def update_last_watered(user_id):
    now = datetime.now()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_watered = $1 WHERE user_id = $2",
            now, user_id
        )

async def check_pending_withdrawals(user_id):
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT id FROM withdrawals WHERE user_id = $1 AND status = 'pending'", 
            user_id
        )
        return result is not None

async def claim_tree_earnings(user_id: int) -> float:
    """
    Calcula las ganancias de un usuario desde su último riego, las añade a su balance
    en la base de datos y reinicia el contador de riego.
    Devuelve la cantidad de dinero ganada.
    """
    async with db_pool.acquire() as conn:
        # Obtener fecha de último riego y los árboles
        user_data = await conn.fetchrow("SELECT last_watered FROM users WHERE user_id = $1", user_id)
        if not user_data:
            return 0.0

        trees = await conn.fetch("SELECT daily_return FROM trees WHERE user_id = $1", user_id)
        if not trees:
            return 0.0

        last_watered = user_data["last_watered"]
        if not last_watered:
            return 0.0

        # Calcular ganancias (máximo 24h)
        now = datetime.now()
        elapsed_seconds = min((now - last_watered).total_seconds(), 86400)
        total_earnings = 0.0
        for tree in trees:
            total_earnings += float(tree["daily_return"]) * (elapsed_seconds / 86400)

        # Si hay ganancias, actualizar balance y fecha de riego
        if total_earnings > 0:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, last_watered = $2 WHERE user_id = $3",
                    total_earnings, now, user_id
                )
        return total_earnings

async def add_balance_admin(user_id, amount):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user_id)

# --- 4. ESTADOS FSM ---
class WithdrawState(StatesGroup):
    waiting_for_card = State()
    waiting_for_name = State()
    waiting_for_rejection_reason = State()
    waiting_for_add_balance_amount = State()

# --- 5. LÓGICA DEL BOT ---

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BlockedMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            user_id = event.from_user.id
            if user_id == ADMIN_ID:  # Admin nunca se bloquea
                return await handler(event, data)

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT is_blocked FROM users WHERE user_id = $1",
                    user_id
                )
                if row and row["is_blocked"]:
                    await event.answer("❌ Estás bloqueado y no puedes usar el bot.")
                    return  # bloquea sin llamar al handler

        return await handler(event, data)

# Registrar middleware
dp.message.middleware(BlockedMiddleware())
@dp.message(Command("block"))
async def cmd_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ No eres admin.")
        return

    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Uso correcto: /block ID_USUARIO")
        return

    target_id = int(args[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", target_id)
    await message.answer(f"✅ Usuario {target_id} bloqueado.")

@dp.message(Command("unblock"))
async def cmd_unblock_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ No eres admin.")
        return

    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Uso correcto: /unblock ID_USUARIO")
        return

    target_id = int(args[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked = FALSE WHERE user_id = $1", target_id)
    await message.answer(f"✅ Usuario {target_id} desbloqueado.")

# Función para teclado del menú principal
def main_menu_keyboard():
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳Añadir Saldo💳"), KeyboardButton(text="💸Retirar💸")],
            [KeyboardButton(text="🌳Mis Árboles🌳")],
            [KeyboardButton(text="💧Regar Árbol💧")],
            [KeyboardButton(text="🏦Balance🏦")],
            [KeyboardButton(text="⚙️Menu⚙️"),KeyboardButton(text="💰Comprar💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard

tree_map = {
    50: ("$50🌳⭐(4%/DIA)", 0.04),
    100: ("$100🌳⭐⭐(5%/DIA)", 0.05),
    300: ("$300🌳⭐⭐⭐(6%/DIA)", 0.06),
    500: ("$500🌳⭐⭐⭐⭐(7%/DIA)", 0.07),
    1000: ("$1000🌳⭐⭐⭐⭐⭐(7%/DIA)", 0.07)
}

# --- FUNCIONES ÚTILES ---
async def is_user_blocked(user_id: int) -> bool:
    """Revisa si un usuario está bloqueado"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_blocked FROM users WHERE user_id = $1", user_id)
        return row and row['is_blocked']

# --- FUNCIÓN PARA ESTABLECER EL WEBHOOK ---
async def set_webhook():
    """ Le dice a Telegram que nos envíe los mensajes a nuestra URL en Render. """
    # La URL de Render más una ruta segura con el token del bot
    webhook_path = f"/webhook/{BOT_TOKEN}"
    webhook_url = f"{os.getenv('WEBHOOK_URL')}{webhook_path}"

    print(f"Intentando configurar el webhook en: {webhook_url}")

    # Borra cualquier configuración anterior para evitar errores
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Establece la nueva dirección
    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"]
    )
    
    print(f"✅ Webhook de Telegram configurado correctamente en: {webhook_url}")

# --- NUEVO ENDPOINT PARA EL WEBHOOK DE TELEGRAM ---
@app.post("/webhook/{bot_token}")
async def telegram_webhook(request: Request, bot_token: str):
    """
    Esta es la puerta de entrada de los mensajes de Telegram en tu servidor.
    """
    # 1. Seguridad: verifica que el token sea el correcto
    if bot_token != BOT_TOKEN:
        return {"status": "error", "message": "Token inválido"}, 403

    # 2. Recibe el mensaje de Telegram
    update_data = await request.json()

    # 3. Le pasa el mensaje a aiogram para que lo procese como siempre
    # --- CORRECCIÓN DEFINITIVA ---
    # El dispatcher necesita la instancia del bot para procesar la actualización.
    # La pasamos como primer argumento.
    update = types.Update.model_validate(update_data, context={"bot": bot})
    await dp.feed_update(bot=bot, update=update)
    
    return {"status": "ok"}

# --- HANDLER GLOBAL PARA USUARIOS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Desconocido"

    await state.clear()

    # Registro de usuario
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    await register_user_if_new(user_id, username, referrer_id)

    # Botón de menú principal al iniciar
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="💳Añadir Saldo💳"), types.KeyboardButton(text="💸Retirar💸")],
            [types.KeyboardButton(text="🌳Mis Árboles🌳")],
            [types.KeyboardButton(text="💧Regar Árbol💧")],
            [types.KeyboardButton(text="🏦Balance🏦")],
            [types.KeyboardButton(text="⚙️Menu⚙️"), types.KeyboardButton(text="💰Comprar💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

    await message.answer(
        "╔══════════════╗\n"
        "🌳 ¡Bienvenido a GOLDPLANT! 🌳\n"
        "╚══════════════╝\n\n"
        f"{username}\n\n"
        "🌳Planta tus árboles y míralos crecer 🌞\n"
        "💧Riégalos cada 24h para ganar más 💰\n"
        "🧑‍🤝‍🧑Trae referidos y gana mas🚀\n"
        "💳Deposita y retira cuando quieras💰\n\n"
        "🔥¡Comienza tu plantación y hazla prosperar! 🌳🚀",
        reply_markup=keyboard
    )

# --- 1️⃣ Botón de menú principal ---
@dp.message(F.text == "📋 Menú Principal 📋", StateFilter(None))
async def main_menu(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
        [types.KeyboardButton(text="💳Añadir Saldo💳"), types.KeyboardButton(text="💸Retirar💸")],
            [types.KeyboardButton(text="🌳Mis Árboles🌳")],
            [types.KeyboardButton(text="💧Regar Árbol💧")],
            [types.KeyboardButton(text="🏦Balance🏦")],
            [types.KeyboardButton(text="⚙️Menu⚙️"), types.KeyboardButton(text="💰Comprar💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("🌳Selecciona una opción:", reply_markup=keyboard)

# --- 2️⃣ Botón Más Opciones ---
@dp.message(F.text == "⚙️Menu⚙️", StateFilter(None))
async def more_options(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🧑‍🤝‍🧑 Referidos")],
            [types.KeyboardButton(text="📊 Historial")],
            [types.KeyboardButton(text="ℹ️ información"),types.KeyboardButton(text="↩️ Volver al Menú Principal")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("🌳 Más Opciones:", reply_markup=keyboard)

# --- 3️⃣ Manejo de los botones de Más Opciones ---
@dp.message(F.text == "🧑‍🤝‍🧑 Referidos")
async def show_referrals(message: types.Message):
    user_id = message.from_user.id
    bot_username = "Goldplant_bot"
    ref_link = f"https://t.me/{bot_username}?start={user_id}"

    await message.answer(
        "╔════════════╗\n"
        " ⬇️ TU LINK DE REFERIDO ⬇️ \n"
        "╚════════════╝\n"
        f"{ref_link}\n\n"
        "🟢Invita a tus amigos y gana comisiones del 10% cuando compren árboles.",
    )

@dp.message(F.text == "ℹ️ información")
async def about_goldplant(message: types.Message):

    await message.answer(
        "Bienvenido a GOLDPLANT, tu bot de inversión gamificado donde puedes cultivar árboles virtuales que generan ganancias reales. 🌱💰\n\n"
        "Cómo funciona:\n\n"
        "🌳Compra un árbol🌳: Invierte tu saldo para plantar tu primer árbol.\n"
        "💧Riega tu árbol💧: Cada 24 horas puedes regarlo para que siga creciendo y generando ganancias.\n"
        "💰Obtén ganancias💰: Tu árbol produce un retorno diario (ROI) basado en su tipo y tamaño.\n"
        "💳Depósitos💳: Los depósitos se realizan pagando con tarjeta para recargar tu saldo.\n"
        "💸Retiros💸: El monto mínimo de retiro es de $50. Para solicitarlo debes ingresar los 16 números de tu tarjeta y el nombre y apellido del titular.\n"
        "👥Sistema de referidos👥: Invita a tus amigos con tu link único y gana un 10% de las compras de sus árboles**. ¡Más amigos, más ganancias! 🧑‍🤝‍🧑\n\n"
        "💯Transparencia y seguridad💯:\n\n"
        "- Todos los cálculos y registros se guardan de forma segura.\n"
        "- No necesitas intermediarios: todo es automático y confiable.\n\n"
        "💡 Tip: Cuida tus árboles todos los días y reinvierte tus ganancias para acelerar tu crecimiento.\n\n"
        "¡Disfruta cultivando y ganando con Goldplant! 🌳🚀"
    )

@dp.message(F.text == "📊 Historial")
async def show_history(message: types.Message):
    user_id = message.from_user.id

    # Aquí puedes obtener el historial de retiros
    async with db_pool.acquire() as conn:
        trees = await conn.fetch("SELECT cost, purchase_date FROM trees WHERE user_id = $1", user_id)
        withdrawals = await conn.fetch(
            "SELECT amount, status, request_date FROM withdrawals WHERE user_id = $1 ORDER BY request_date DESC LIMIT 10",
            user_id
        )

    msg = (
    "╔═════════════╗\n"
    " 📊 <b>HISTORIAL DE RETIROS</b> 📊\n"
    "╚═════════════╝\n\n"
)

    if withdrawals:
        msg += "\n💸 Retiros:\n"
    
        # Mapeo de estados a emojis
        status_map = {
            "approved": "✅",
            "rejected": "❌",
            "pending": "⏳"
    }
    
        for w in withdrawals:
            # Solo mostrar fecha, sin hora
            date_str = w['request_date'].strftime("%d/%m/%Y")
        
            # Obtener emoji según estado
            status_emoji = status_map.get(w['status'], w['status'])
        
            msg += f"• ${w['amount']} - {status_emoji} - {date_str}\n"
    else:
        msg += "\n💸 Retiros: Ninguno\n"

    await message.answer(msg, parse_mode="HTML")

# --- 4️⃣ Volver al menú principal ---
@dp.message(F.text == "↩️ Volver al Menú Principal")
async def back_to_main_menu(message: types.Message):
    await main_menu(message)

# --- 5️⃣ Volver a Más Opciones ---
@dp.message(F.text == "↩️ Volver a Más Opciones")
async def back_to_more_options(message: types.Message):
    await more_options(message)

@dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def process_buy_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cost = int(callback_query.data.split("_")[1])
    
    if cost not in tree_map:
        await callback_query.message.answer("❌ Opción inválida.")
        await callback_query.answer()
        return

    tree_type, roi = tree_map[cost]
    daily_return = cost * roi

    # --- TRANSACCIÓN PARA COMPRA ---
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction(): # Iniciamos transacción
                # 1. Verificar saldo y obtener referidor
                user_row = await conn.fetchrow("SELECT balance, referred_by FROM users WHERE user_id = $1 FOR UPDATE", user_id)
                
                if not user_row:
                    await callback_query.message.answer("❌ Error al obtener datos del usuario.")
                    await callback_query.answer()
                    return
                
                current_balance = user_row['balance']
                referrer_id = user_row['referred_by']

                if current_balance < cost:
                    mensaje_error = "❌ Saldo insuficiente. Necesitas $" + str(cost) + " para comprar este árbol.\n"
                    await callback_query.message.answer(mensaje_error)
                    await callback_query.answer()
                    return

                # 2. Restar saldo y agregar inversión
                await conn.execute("UPDATE users SET total_invested = total_invested + $1, balance = balance - $1 WHERE user_id = $2", cost, user_id)

                # 3. Pagar comisión al referidor si existe
                if referrer_id:
                    commission = cost * 0.10
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", commission, referrer_id)
                    print(f"Comisión de {commission} pagada al usuario {referrer_id}")

                # 4. Insertar el árbol
                now = datetime.now()
                await conn.execute("INSERT INTO trees (user_id, cost, daily_return, purchase_date, last_claimed) VALUES ($1, $2, $3, $4, $4)", user_id, cost, daily_return, now)

        await callback_query.message.answer("Has comprado un Arbol " + str(tree_type))
        await callback_query.answer()

        # --- NOTIFICACIÓN AL ADMIN ---
        try:
            admin_message = (
                f"🌳 <b>Nuevo árbol comprado</b> por un usuario:\n\n"
                f"👤 <b>Usuario:</b> @{callback_query.from_user.username}\n"
                f"💰 <b>Precio del árbol:</b> ${cost}\n"
                f"🌳 <b>Tipo de árbol:</b> {tree_type}\n"
                f"🔗 <b>Link del usuario:</b> https://t.me/{callback_query.from_user.username}\n"
                f"⏰ <b>Fecha de compra:</b> {now}\n"
            )
            await bot.send_message(ADMIN_ID, admin_message, parse_mode="HTML")
        except Exception as e:
            print(f"Error al enviar notificación al admin: {e}")

    except Exception as e:
        print(f"Error en compra: {e}")
        await callback_query.message.answer("Error al procesar la compra: " + str(e))
        await callback_query.answer()

# --- Handler para cancelar cualquier acción y volver al menú principal ---
@dp.message(F.text == "❌ Cancelar", StateFilter(None))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """
    Este handler se activa cuando el usuario presiona el botón '❌ Cancelar'.
    Limpia cualquier estado activo y devuelve al usuario al menú principal.
    """
    await state.clear() # Limpia el estado por si acaso
    await message.answer(
        "✅ Operación cancelada. Has vuelto al menú principal.",
        reply_markup=main_menu_keyboard()
    )

# --- NUEVO HANDLER CORRECTO ---
@dp.message(WithdrawState.waiting_for_add_balance_amount)
async def process_add_balance_amount(message: types.Message, state: FSMContext):
    """Se activa solo cuando el usuario está en el estado de espera de cantidad."""
    
    # Si el usuario presiona cancelar, limpiamos el estado y volvemos al menú
    if message.text == "❌ Cancelar":
        await state.clear()
        await message.answer("✅ Operación cancelada. Has vuelto al menú principal.", reply_markup=main_menu_keyboard())
        return

    try:
        amount = float(message.text)
        if amount <= 0:
            await message.answer("❌ La cantidad debe ser un número positivo. Inténtalo de nuevo.")
            return

        user_id = message.from_user.id
        await message.answer("🔁 Generando tu enlace de pago, espera un momento...")

        # Llamar a nuestra función para crear la factura
        payment_url = await create_nowpayments_invoice(amount, user_id)

        # Crear el botón inline para que el usuario pueda pagar directamente
        payment_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 Pagar Ahora", url=payment_url)]
        ])

        await message.answer(
            f"✅ ¡Listo! Para añadir ${amount:.2f} a tu balance, haz clic en el botón de abajo.\n\n"
            f"⏳ Una vez completado el pago, tu saldo se actualizará automáticamente.",
            reply_markup=payment_keyboard
        )

        # IMPORTANTE: Limpiamos el estado y volvemos a mostrar el menú principal
        await state.clear()
        await message.answer("🏦 Menú Principal:", reply_markup=main_menu_keyboard())

    except ValueError:
        await message.answer("❌ Formato inválido. Por favor, escribe solo un número (ej: 50 o 25.5).")
    except Exception as e:
        print(f"Error al crear el pago para el usuario {message.from_user.id}: {e}")
        await message.answer("❌ Ocurrió un error al generar el pago. Por favor, intenta más tarde.")
        await state.clear() # Limpiamos el estado también en caso de errorPor favor, intenta más tarde.")

@dp.message(F.text.startswith('/') == False, StateFilter(None))
async def handle_menu(message: types.Message, state: FSMContext):
    text = message.text
    user_id = message.from_user.id
    username = message.from_user.username
    
    if username is None:
        username = message.from_user.first_name or "Usuario"

    if text == "💰Comprar💰":
        buttons = []
        for cost, (name, roi) in tree_map.items():
            daily_profit = cost * roi
            label = f"{name}\n💰 Ganancia: ${daily_profit:.2f}/día"
            buttons.append([types.InlineKeyboardButton(text=label, callback_data=f"buy_{cost}")])
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "╔═════════════╗\n"
            "<b>🌳                  TIENDA                  🌳</b>\n"
            "╚═════════════╝\n\n",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    elif text == "💳Añadir Saldo💳":
        # Creamos un teclado con la opción de cancelar
        cancel_keyboard = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancelar")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.answer(
            "💳 Por favor, escribe la cantidad de USD que deseas agregar a tu balance (ej: 10, 25, 50):\n\n"
            "✍️ Simplemente envíame el número como un mensaje.\n\n"
            "O presiona el botón para cancelar.",
            reply_markup=cancel_keyboard
        )
         # <-- CAMBIO CLAVE: PONEMOS AL USUARIO EN EL NUEVO ESTADO
        await state.set_state(WithdrawState.waiting_for_add_balance_amount)

    elif text == "🌳Mis Árboles🌳":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT cost, daily_return, purchase_date FROM trees WHERE user_id = $1", user_id)
        
        if not rows:
            await message.answer("🍂 Aún no tienes árboles. Compra uno para empezar a cosechar.")
            return

        msg = (
            "╔═════════════╗\n"
            "<b>🌳 TU BOSQUE PERSONAL 🌳</b>\n"
            "╚═════════════╝\n\n"
        )
        total_daily = 0
        for row in rows:
            cost = row['cost']
            daily = row['daily_return']
            purchase_date = row['purchase_date'].strftime("%d/%m/%Y")
            total_daily += daily
            
            tree_name = "Árbol Desconocido"
            if cost == 50: tree_name = "🌳⭐"
            elif cost == 100: tree_name = "🌳⭐⭐"
            elif cost == 300: tree_name = "🌳⭐⭐⭐"
            elif cost == 500: tree_name = "🌳⭐⭐⭐⭐"
            elif cost == 1000: tree_name = "🌳⭐⭐⭐⭐⭐"
            
            msg += f"• {tree_name} (${cost})\n └ Genera: ${daily:.2f}/día\n └ Comprado: {purchase_date}\n\n"
        
        msg += f"💰 <b>Rentabilidad Total Diaria:</b> ${total_daily:.2f}"
        await send_long_message(message, msg, parse_mode="HTML")

    elif text == "💧Regar Árbol💧":
        # Obtener datos del usuario
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("❌ Error al cargar perfil.")
            return

        # Traer los árboles del usuario
        async with db_pool.acquire() as conn:
            trees = await conn.fetch("SELECT id FROM trees WHERE user_id = $1", user_id)

        if not trees:
            await message.answer("❌ No tienes árboles para regar.")
            return

        now = datetime.now()
        last_watered = user_data.get("last_watered")
    
        # Revisar si han pasado 24h desde el último riego
        if not last_watered or (now - last_watered).total_seconds() >= 86400:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET last_watered = $1 WHERE user_id = $2",
                    now, user_id
                )
            await message.answer(
                f"💧 Todos tus árboles han sido regados.\n"
                "⏱️ Empezarán a generar ganancias automáticamente durante 24 horas."
            )
        else:
            # Calcular tiempo restante para el próximo riego
            remaining_seconds = 86400 - (now - last_watered).total_seconds()
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)
            await message.answer(
                "🌳 Tus árboles ya están regados💧.\n"
                f"⌛ Próximo riego disponible en: {hours}h {minutes}min"
            )

    elif text == "💸Retirar💸":
        user_id = message.from_user.id
            
        # --- PASO 1: Actualizar balance con las ganancias pendientes ---
        earned = await claim_tree_earnings(user_id)
        if earned > 0:
            print(f"Usuario {user_id}: Ganancias de ${earned:.2f} añadidas antes de retirar.")

        # --- PASO 2: Verificaciones y flujo de retiro (sin cambios) ---
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("Error al cargar tu perfil.")
            return
            
        balance = float(user_data["balance"])
        min_withdraw = 50

        if balance < min_withdraw:
            await message.answer(f"❌ Necesitas acumular al menos ${min_withdraw:.2f} para retirar. Tu balance actual es ${balance:.2f}")
            return

        if await check_pending_withdrawals(user_id):
            await message.answer("⏳ Ya tienes una solicitud de retiro en proceso.")
            return

        # Iniciar flujo de retiro
        cancel_kb = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancelar Retiro")]], 
            resize_keyboard=True, 
            one_time_keyboard=True
        )
        await message.answer(
            "💳 Ingresa los 16 números de tu tarjeta de débito o crédito:",
            reply_markup=cancel_kb
        )
        await state.set_state(WithdrawState.waiting_for_card)

    elif text == "🏦Balance🏦":
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("Error al cargar perfil.")
            return
            
        base_balance = float(user_data["balance"])
        last_watered = user_data["last_watered"]

        # --- CÁLCULO DE GANANCIAS (igual que tenías) ---
        async with db_pool.acquire() as conn:
            trees = await conn.fetch("SELECT daily_return FROM trees WHERE user_id = $1", user_id)
            
        now = datetime.now()
        generated_earnings = 0.0
        if last_watered:
            elapsed = (now - last_watered).total_seconds()
            if elapsed > 86400: # máximo 24h
                elapsed = 86400
            for tree in trees:
                generated_earnings += float(tree["daily_return"]) * (elapsed / 86400)

        # --- ¡LA PARTE CLAVE QUE FALTABA! ---
        # Si hay ganancias, GUARDARLAS en la base de datos
        if generated_earnings > 0:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, last_watered = $2 WHERE user_id = $3",
                    generated_earnings, now, user_id
                )
            # Actualizamos el balance local también para mostrarlo
            base_balance += generated_earnings

        # --- OBTENER EL RESTO DE DATOS (igual que tenías) ---
        async with db_pool.acquire() as conn:
            total_withdrawn = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id = $1 AND status = 'approved'", user_id
            )
            total_referral_earnings = await conn.fetchval(
                "SELECT COALESCE(SUM(t.cost * 0.10), 0) FROM trees t JOIN users u ON t.user_id = u.user_id WHERE u.referred_by = $1", user_id
            )
            
        # --- MOSTRAR EL MENSAJE (ahora con el balance real) ---
        username = message.from_user.username or message.from_user.first_name
        await message.answer(
            f"👤 <b>@{username}</b>\n\n"
            f"💰 <b>Balance:</b> ${base_balance:.2f}\n" # Usamos base_balance que ya es el real
            f"💸 <b>Total Retirado:</b> ${float(total_withdrawn):.2f}\n"
            f"💵 <b>Ganancias de Referidos:</b> ${float(total_referral_earnings):.2f}\n\n"
            f"<i>💧 Las ganancias han sido añadidas a tu balance.</i>",
            parse_mode="HTML"
        )

# --- HANDLER PARA INGRESO DE TARJETA ---
@dp.message(WithdrawState.waiting_for_card)
async def process_card(message: types.Message, state: FSMContext):
    text = message.text.strip()

    # ⚠️ Si el usuario presiona Cancelar Retiro
    if text == "❌ Cancelar Retiro":
        await state.clear()
        await message.answer(
            "❌ Retiro cancelado. Puedes usar los botones normalmente.",
            reply_markup=main_menu_keyboard()
        )
        return

    # Validar número de tarjeta
    card_number = text.replace(" ", "")
    if not card_number.isdigit() or len(card_number) != 16:
        await message.answer(
            "❌ Número inválido. Asegúrate de escribir exactamente 16 números sin letras ni espacios."
        )
        return

    # Guardar número de tarjeta y pasar al siguiente estado
    await state.update_data(card_number=card_number)
    await message.answer(
        "✅ Tarjeta válida. Ahora escribe tu Nombre y Apellido:",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancelar Retiro")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    await state.set_state(WithdrawState.waiting_for_name)

# --- HANDLER PARA INGRESO DE NOMBRE COMPLETO ---
@dp.message(WithdrawState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    text = message.text.strip()

    # ⚠️ Si el usuario presiona Cancelar Retiro
    if text == "❌ Cancelar Retiro":
        await state.clear()
        await message.answer(
            "❌ Retiro cancelado. Puedes usar los botones normalmente.",
            reply_markup=main_menu_keyboard()
        )
        return

    full_name = text
    if len(full_name) < 3:
        await message.answer("❌ Nombre demasiado corto. Inténtalo de nuevo.")
        return

    user_id = message.from_user.id
    username = message.from_user.username
    data = await state.get_data()
    card_number = data.get("card_number")

    # Obtener datos del usuario
    user_data = await get_user_data(user_id)
    if not user_data:
        await message.answer("Error al obtener datos de usuario.")
        await state.clear()
        return

    balance = user_data['balance']
    if balance <= 0:
        await message.answer("❌ No tienes saldo disponible para retirar.")
        await state.clear()
        return

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                now = datetime.now()

                # Insertar solicitud de retiro
                await conn.execute(
                    "INSERT INTO withdrawals (user_id, amount, card_number, full_name, status, request_date) VALUES ($1, $2, $3, $4, 'pending', $5)",
                    user_id, balance, card_number, full_name, now
                )

                # Descontar saldo
                await conn.execute(
                    "UPDATE users SET balance = balance - $1 WHERE user_id = $2",
                    balance, user_id
                )

                # Actualizar datos de pago
                await conn.execute(
                    "UPDATE users SET card_number = $1, full_name = $2 WHERE user_id = $3",
                    card_number, full_name, user_id
                )

        # Notificar al admin
        try:
            admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Aprobar Retiro", callback_data=f"approve_{user_id}")],
                [types.InlineKeyboardButton(text="❌ Rechazar Retiro", callback_data=f"reject_{user_id}")]
            ])
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>Nueva Solicitud de Retiro</b>\n\n"
                f"👤 <b>Usuario:</b> @{username} ({full_name})\n"
                f"👤 Usuario ID: {user_id}\n"
                f"⏰ <b>Fecha de solicitud:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"👤 Nombre: {full_name}\n"
                f"💳 Tarjeta: {card_number}\n"
                f"💰 Monto: ${balance:.2f}",
                parse_mode="HTML",
                reply_markup=admin_keyboard
            )
        except Exception as e:
            print(f"No se pudo notificar al admin: {e}")

        await message.answer(
            f"✅ Solicitud recibida.\n\nSe ha enviado una solicitud de ${balance:.2f} "
            f"a la tarjeta terminada en ****{card_number[-4:]}.\n\n⏳ El pago se procesará en menos de 24 horas.",
            reply_markup=main_menu_keyboard()
        )
        await state.clear()

    except Exception as e:
        print(f"Error al guardar datos: {e}")
        await message.answer(f"Error al procesar la solicitud: {e}", reply_markup=main_menu_keyboard())
        await state.clear()

@dp.message(F.text == "❌ Cancelar Retiro")
async def cancel_withdraw(message: types.Message, state: FSMContext):
    # Limpiar el estado
    await state.clear()

    # Volver a mostrar el menú principal
    await message.answer(
        "❌ Retiro cancelado. Puedes usar los botones normalmente.",
        reply_markup=main_menu_keyboard()
    )

# --- 8. CALLBACK DEL ADMIN ---

@dp.callback_query(lambda c: c.data and c.data.startswith("approve_"))
async def admin_approve_withdraw(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ No tienes permisos.")
        return

    try:
        user_id = int(callback_query.data.split("_")[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Buscar último retiro pendiente
                row = await conn.fetchrow(
                    "SELECT id, amount FROM withdrawals WHERE user_id = $1 AND status = 'pending' ORDER BY id DESC LIMIT 1", 
                    user_id
                )
                
                if row:
                    withdraw_id = row['id']
                    amount = row['amount']
                    now = datetime.now()
                    
                    # Marcar como aprobado
                    await conn.execute(
                        "UPDATE withdrawals SET status = 'approved', processed_date = $1 WHERE id = $2", 
                        now, withdraw_id
                    )
                    
                    # Notificar al usuario
                    await bot.send_message(user_id, f"✅ Tu solicitud de retiro de ${amount} ha sido aprobada.")
                    await callback_query.message.edit_text(f"✅ Retiro de ID {user_id} por ${amount} aprobado.")
                    await callback_query.answer()
                else:
                    await callback_query.message.edit_text("❌ No se encontró retiro pendiente para este usuario.")
                    await callback_query.answer()

    except Exception as e:
        print(f"Error aprobación: {e}")
        await callback_query.answer("Error interno al aprobar.")

# --- CALLBACK DEL ADMIN PARA RECHAZAR RETIROS ---
@dp.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def admin_reject_withdraw(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ No tienes permisos.")
        return

    try:
        user_id = int(callback_query.data.split("_")[1])
        
        # Guardamos el ID del usuario cuyo retiro se va a rechazar
        await state.update_data(target_user_id=user_id)
        
        # Cambiamos el estado del admin para capturar su motivo
        await state.set_state(WithdrawState.waiting_for_rejection_reason)
        
        await callback_query.message.edit_text(
            "❌ Has rechazado el retiro. Por favor, responde a este mensaje con el motivo del rechazo."
        )
        await callback_query.answer()
        
    except Exception as e:
        print(f"Error en el rechazo: {e}")
        await callback_query.answer("Error interno al rechazar el retiro.")


# --- MANEJADOR DEL MOTIVO DE RECHAZO ---
@dp.message(WithdrawState.waiting_for_rejection_reason)
async def process_rejection_reason(message: types.Message, state: FSMContext):
    rejection_reason = message.text.strip()
    if len(rejection_reason) < 5:
        await message.answer("❌ El motivo es demasiado corto. Inténtalo de nuevo.")
        return

    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    
    if not target_user_id:
        await message.answer("❌ Error: No se pudo identificar el usuario. Estado reiniciado.")
        await state.clear()
        return

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Obtener último retiro pendiente del usuario
                row = await conn.fetchrow(
                    "SELECT id, amount FROM withdrawals WHERE user_id = $1 AND status = 'pending' ORDER BY id DESC LIMIT 1",
                    target_user_id
                )
                
                if row:
                    withdraw_id = row['id']
                    amount = row['amount']
                    now = datetime.now()
                    
                    # Marcar retiro como rechazado y agregar motivo
                    await conn.execute(
                        "UPDATE withdrawals SET status = 'rejected', rejection_reason = $1, processed_date = $2 WHERE id = $3",
                        rejection_reason, now, withdraw_id
                    )
                    
                    # Devolver dinero al usuario
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        amount, target_user_id
                    )
                    
                    # Notificar al usuario
                    await bot.send_message(
                        target_user_id,
                        f"❌ Tu solicitud de retiro de ${amount:.2f} ha sido rechazada.\n\nMotivo: {rejection_reason}\n\nEl monto ha sido devuelto a tu balance."
                    )
                    
                    await message.answer(f"✅ Retiro del usuario {target_user_id} rechazado. Motivo enviado y dinero devuelto.")
                else:
                    await message.answer("❌ No se encontró un retiro pendiente para este usuario.")

    except Exception as e:
        print(f"Error al procesar el motivo de rechazo: {e}")
        await message.answer("Error al procesar la solicitud de rechazo.")
    
    await state.clear()


# --- 9. COMANDO /add (SOLO ADMIN) ---
# --- BLOQUEAR USUARIO ---
@dp.message(Command("block"))
async def cmd_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ No eres admin.")
        return

    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Uso: /block USER_ID")
            return

        target_id = int(args[1])

        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", target_id)

        await message.answer(f"✅ Usuario {target_id} bloqueado.")
    except Exception as e:
        await message.answer(f"Error: {e}")

# --- DESBLOQUEAR USUARIO ---
@dp.message(Command("unblock"))
async def cmd_unblock_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ No eres admin.")
        return

    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Uso: /unblock USER_ID")
            return

        target_id = int(args[1])

        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blocked = FALSE WHERE user_id = $1", target_id)

        await message.answer(f"✅ Usuario {target_id} desbloqueado.")
    except Exception as e:
        await message.answer(f"Error: {e}")

@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ No eres admin.")
        return

    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Uso: /add ID CANTIDAD")
            return

        target_id = int(args[1])
        amount = float(args[2])
        
        await add_balance_admin(target_id, amount)
        await message.answer(f"✅ Enviados ${amount} al usuario {target_id}")

    except ValueError:
        await message.answer("Error: El ID debe ser un número entero y la cantidad un número decimal.")
    except Exception as e:
        await message.answer(f"Error: {e}")

# --- FUNCIÓN PRINCIPAL CORREGIDA ---
async def main():
    print("🚀 INICIANDO SERVIDOR WEB")
    # La inicialización ahora se maneja en el evento 'startup' de FastAPI.
    # Esta función solo se encarga de iniciar el servidor.
    config = uvicorn.Config(
        app, # Tu aplicación FastAPI
        host="0.0.0.0", # Necesario para que sea accesible desde internet
        port=int(os.getenv("PORT", 10000)), # Usa el puerto que Render te da
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Aplicación detenida.")