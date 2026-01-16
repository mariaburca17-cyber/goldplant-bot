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
from fastapi import FastAPI, Request, HTTPException, Response
from starlette.middleware.base import BaseHTTPMiddleware

# --- 1. CONFIGURATION ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DB_URL")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")

if not BOT_TOKEN:
    print("CRITICAL ERROR: BOT_TOKEN not found")
    sys.exit()

if not DB_URL:
    print("CRITICAL ERROR: DB_URL not found")
    sys.exit()

ADMIN_ID = 7430692266  # Your Telegram ID

logging.basicConfig(level=logging.INFO)

# Global variable for the connection pool
db_pool = None

# --- FASTAPI APP ---
app = FastAPI()

# --- FASTAPI APPLICATION EVENTS ---
@app.on_event("startup")
async def startup_event():
    """
    This function runs automatically when the FastAPI server is about to start.
    It's the perfect place to initialize resources like the database and the bot.
    """
    global bot, dp, db_pool  # Declare that you will modify the globals
    print("Starting FastAPI startup event...")
    
    # 1. Initialize the database FIRST
    await init_db()
    print("PostgreSQL database initialized successfully.")
    
    # 2. Initialize the Telegram bot
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    print("Telegram bot initialized.")
    
    # 3. Register all bot handlers and middleware
    dp.message.middleware(BlockedMiddleware())
    dp.message(Command("start"))(cmd_start)
    dp.message(F.text == "📋 Main Menu 📋", StateFilter(None))(main_menu)
    dp.message(F.text == "⚙️Menu⚙️", StateFilter(None))(more_options)
    dp.message(F.text == "🧑‍🤝‍🧑 Referrals")(show_referrals)
    dp.message(F.text == "ℹ️ Information")(about_goldplant)
    dp.message(F.text == "📊 History")(show_history)
    dp.message(F.text == "↩️ Back to Main Menu")(back_to_main_menu)
    dp.message(F.text == "↩️ Back to More Options")(back_to_more_options)
    dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))(process_buy_callback)
    dp.message(F.text == "❌ Cancel", StateFilter(None))(cmd_cancel)
    dp.message(WithdrawState.waiting_for_add_balance_amount)(process_add_balance_amount)
    dp.message(F.text.startswith('/') == False, StateFilter(None))(handle_menu)
    dp.message(WithdrawState.waiting_for_card)(process_card)
    dp.message(WithdrawState.waiting_for_name)(process_name)
    dp.message(F.text == "❌ Cancel Withdrawal")(cancel_withdraw)
    dp.callback_query(lambda c: c.data and c.data.startswith("approve_"))(admin_approve_withdraw)
    dp.callback_query(lambda c: c.data and c.data.startswith("reject_"))(admin_reject_withdraw)
    dp.message(WithdrawState.waiting_for_rejection_reason)(process_rejection_reason)
    dp.message(Command("block"))(cmd_block_user)
    dp.message(Command("unblock"))(cmd_unblock_user)
    dp.message(Command("add"))(cmd_add_balance)
    print("aiogram handlers registered.")
    
    # 4. Configure the Telegram webhook
    await set_webhook()
    print("Telegram webhook configured.")
    
    print("✅ Startup event completed. The server is ready to receive requests.")

# --- Final corrected middleware for the NOWPayments Webhook ---
@app.middleware("http")
async def strip_user_agent_for_nowpayments(request: Request, call_next):
    if request.url.path == "/nowpayments/webhook":
        # We save the raw body without modifying
        body = await request.body()
        request.state.raw_body = body
        response = await call_next(request)
        return response
    else:
        return await call_next(request)

@app.post("/nowpayments/webhook")
async def nowpayments_webhook(request: Request):
    received_signature = request.headers.get("x-nowpayments-sig")
    if not received_signature:
        raise HTTPException(status_code=403, detail="Signature not provided")
    
    # Use the body saved in the state
    body = request.state.raw_body
    NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET")
    if not NOWPAYMENTS_IPN_SECRET:
        raise HTTPException(status_code=500, detail="IPN key not configured on the server")
    
    try:
        # Convert the body to string
        body_str = body.decode('utf-8')
        
        # Calculate the signature correctly according to the documentation
        # The signature should be calculated only over the JSON, without concatenating the key
        calculated_signature = hmac.new(
            key=NOWPAYMENTS_IPN_SECRET.encode(),
            msg=body_str.encode(),  # Only the JSON body, without concatenating the key
            digestmod=hashlib.sha512
        ).hexdigest()
        
        if not hmac.compare_digest(received_signature, calculated_signature):
            print("--- SIGNATURE VERIFICATION ERROR ---")
            print(f"DEBUG - Received signature: {received_signature}")
            print(f"DEBUG - Calculated signature: {calculated_signature}")
            print(f"DEBUG - Received body (raw): {body_str}")
            print("--------------------------------------")
            raise HTTPException(status_code=403, detail="Invalid signature")
        
        # Parse the JSON after verifying the signature
        payment_data = json.loads(body_str)
        
        # We process the payment according to its status
        payment_status = payment_data.get("payment_status")
        print(f"Payment status: {payment_status}")
        
        if payment_status == "finished":
            # We accept both states
            order_id = payment_data.get("order_id")
            user_id = order_id.split('_')[0]
            amount_paid = payment_data.get("actually_paid")
            
            # We verify that the payment is not zero
            if float(amount_paid) > 0:
                print(f"✅ Payment confirmed for user {user_id}. Amount: {amount_paid}")
                
                async def update_user_balance_in_db(user_id, amount):
                    global db_pool
                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                            amount, user_id
                        )
                        print(f"Balance updated for user {user_id}: +${amount}")
                
                await update_user_balance_in_db(int(user_id), float(amount_paid))
                
                try:
                    await bot.send_message(
                        int(user_id),
                        f"✅ Payment received!\n\n${amount_paid:.2f} has been added to your balance. Thank you for your recharge."
                    )
                except Exception as e:
                    print(f"Could not notify user {user_id}: {e}")
            else:
                print(f"⚠️ Zero amount payment for user {user_id}. Balance not updated.")
            
            return Response(status_code=200)
        else:
            print(f"ℹ️ Payment in {payment_status} status. It won't be processed until completed.")
            return Response(status_code=200)  # We respond with 200 to avoid retries
    
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail="Invalid request body")

# --- 2. DATABASE (POSTGRESQL) ---
async def init_db():
    """Initialize the connection and create tables if they don't exist."""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DB_URL, min_size=5, max_size=50)
        async with db_pool.acquire() as conn:
            # Users table
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
                    is_blocked BOOLEAN DEFAULT FALSE
                )
            ''')
            
            # Trees table
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
            
            # Withdrawals table
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
        
        print("PostgreSQL database initialized successfully.")
    except Exception as e:
        print(f"DB Error: {e}")

async def create_nowpayments_invoice(amount: float, user_id: int) -> str:
    """
    Creates an invoice in NowPayments and returns the payment URL.
    """
    order_id = f"{user_id}_{int(datetime.now().timestamp())}"
    invoice_data = {
        "price_amount": amount,
        "price_currency": "USD",
        "order_id": order_id,
        "ipn_callback_url": f"{os.getenv('WEBHOOK_URL')}/nowpayments/webhook",
        "order_description": f"Balance recharge for user {user_id}",
        "success_url": "https://t.me/Goldplant_bot",
        "cancel_url": "https://t.me/Goldplant_bot"
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
            response.raise_for_status()
            result = response.json()
            return result["invoice_url"]
        except httpx.HTTPStatusError as e:
            print(f"HTTP error when creating invoice: {e.response.status_code} - {e.response.text}")
            raise Exception(f"Error contacting NowPayments: {e.response.status_code}")
        except Exception as e:
            print(f"Unexpected error when creating invoice: {e}")
            raise Exception(f"Could not generate payment invoice.")

# --- 3. AUXILIARY FUNCTIONS (ASYNC) ---
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
        try:
            await conn.execute(
                "INSERT INTO users (user_id, username, referred_by) VALUES ($1, $2, $3)",
                user_id, username, referrer_id
            )
            return True
        except asyncpg.UniqueViolationError:
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
    Calculates a user's earnings since their last watering, adds them to their balance in the database,
    and resets the watering counter. Returns the amount of money earned.
    """
    async with db_pool.acquire() as conn:
        user_data = await conn.fetchrow("SELECT last_watered FROM users WHERE user_id = $1", user_id)
        if not user_data:
            return 0.0
        
        trees = await conn.fetch("SELECT daily_return FROM trees WHERE user_id = $1", user_id)
        if not trees:
            return 0.0
        
        last_watered = user_data["last_watered"]
        if not last_watered:
            return 0.0
        
        # Calculate earnings (maximum 24h)
        now = datetime.now()
        elapsed_seconds = min((now - last_watered).total_seconds(), 86400)
        
        total_earnings = 0.0
        for tree in trees:
            total_earnings += float(tree["daily_return"]) * (elapsed_seconds / 86400)
        
        # If there are earnings, update balance and watering date
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

# --- 4. FSM STATES ---
class WithdrawState(StatesGroup):
    waiting_for_card = State()
    waiting_for_name = State()
    waiting_for_rejection_reason = State()
    waiting_for_add_balance_amount = State()

# --- 5. BOT LOGIC ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BlockedMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            user_id = event.from_user.id
            if user_id == ADMIN_ID:
                return await handler(event, data)
            
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT is_blocked FROM users WHERE user_id = $1",
                    user_id
                )
                if row and row["is_blocked"]:
                    await event.answer("❌ You are blocked and cannot use the bot.")
                    return
        
        return await handler(event, data)

# Register middleware
dp.message.middleware(BlockedMiddleware())

@dp.message(Command("block"))
async def cmd_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You are not an admin.")
        return
    
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Correct usage: /block USER_ID")
        return
    
    target_id = int(args[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", target_id)
    
    await message.answer(f"✅ User {target_id} blocked.")

@dp.message(Command("unblock"))
async def cmd_unblock_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You are not an admin.")
        return
    
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Correct usage: /unblock USER_ID")
        return
    
    target_id = int(args[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked = FALSE WHERE user_id = $1", target_id)
    
    await message.answer(f"✅ User {target_id} unblocked.")

# Function for main menu keyboard
def main_menu_keyboard():
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳Add Balance💳"), KeyboardButton(text="💸Withdraw💸")],
            [KeyboardButton(text="🌳My Trees🌳")],
            [KeyboardButton(text="💧Water Tree💧")],
            [KeyboardButton(text="🏦Balance🏦")],
            [KeyboardButton(text="⚙️Menu⚙️"),KeyboardButton(text="💰Buy💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard

tree_map = {
    50: ("$50🌳⭐(4%/DAY)", 0.04),
    100: ("$100🌳⭐⭐(5%/DAY)", 0.05),
    300: ("$300🌳⭐⭐⭐(6%/DAY)", 0.06),
    500: ("$500🌳⭐⭐⭐⭐(7%/DAY)", 0.07),
    1000: ("$1000🌳⭐⭐⭐⭐⭐(7%/DAY)", 0.07)
}

# --- USEFUL FUNCTIONS ---
async def is_user_blocked(user_id: int) -> bool:
    """Check if a user is blocked"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_blocked FROM users WHERE user_id = $1", user_id)
        return row and row['is_blocked']

# --- FUNCTION TO SET THE WEBHOOK ---
async def set_webhook():
    """
    Tells Telegram to send us messages to our URL on Render.
    """
    webhook_path = f"/webhook/{BOT_TOKEN}"
    webhook_url = f"{os.getenv('WEBHOOK_URL')}{webhook_path}"
    
    print(f"Trying to configure webhook at: {webhook_url}")
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"]
    )
    print(f"✅ Telegram webhook configured correctly at: {webhook_url}")

# --- NEW ENDPOINT FOR THE TELEGRAM WEBHOOK ---
@app.post("/webhook/{bot_token}")
async def telegram_webhook(request: Request, bot_token: str):
    """
    This is the entry point for Telegram messages on your server.
    """
    if bot_token != BOT_TOKEN:
        return {"status": "error", "message": "Invalid token"}, 403
    
    update_data = await request.json()
    update = types.Update.model_validate(update_data, context={"bot": bot})
    await dp.feed_update(bot=bot, update=update)
    
    return {"status": "ok"}

# --- GLOBAL HANDLER FOR USERS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    await state.clear()
    
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    
    await register_user_if_new(user_id, username, referrer_id)
    
    # Main menu button at start
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="💳Add Balance💳"), types.KeyboardButton(text="💸Withdraw💸")],
            [types.KeyboardButton(text="🌳My Trees🌳")],
            [types.KeyboardButton(text="💧Water Tree💧")],
            [types.KeyboardButton(text="🏦Balance🏦")],
            [types.KeyboardButton(text="⚙️Menu⚙️"), types.KeyboardButton(text="💰Buy💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    
    await message.answer(
        "╔══════════════╗\n"
        "🌳 Welcome to GOLDPLANT! 🌳\n"
        "╚══════════════╝\n\n"
        f"{username}\n\n"
        "🌳Plant your trees and watch them grow 🌞\n"
        "💧Water them every 24h to earn more 💰\n"
        "🧑‍🤝‍🧑Bring referrals and earn more🚀\n"
        "💳Deposit and withdraw whenever you want💰\n\n"
        "🔥Start your plantation and make it prosper! 🌳🚀",
        reply_markup=keyboard
    )

# --- 1️⃣ Main menu button ---
@dp.message(F.text == "📋 Main Menu 📋", StateFilter(None))
async def main_menu(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="💳Add Balance💳"), types.KeyboardButton(text="💸Withdraw💸")],
            [types.KeyboardButton(text="🌳My Trees🌳")],
            [types.KeyboardButton(text="💧Water Tree💧")],
            [types.KeyboardButton(text="🏦Balance🏦")],
            [types.KeyboardButton(text="⚙️Menu⚙️"), types.KeyboardButton(text="💰Buy💰")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("🌳Select an option:", reply_markup=keyboard)

# --- 2️⃣ More Options button ---
@dp.message(F.text == "⚙️Menu⚙️", StateFilter(None))
async def more_options(message: types.Message):
    keyboard = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🧑‍🤝‍🧑 Referrals")],
            [types.KeyboardButton(text="📊 History")],
            [types.KeyboardButton(text="ℹ️ Information"),types.KeyboardButton(text="↩️ Back to Main Menu")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("🌳 More Options:", reply_markup=keyboard)

# --- 3️⃣ Handling More Options buttons ---
@dp.message(F.text == "🧑‍🤝‍🧑 Referrals")
async def show_referrals(message: types.Message):
    user_id = message.from_user.id
    bot_username = "Goldplant_bot"
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    
    await message.answer(
        "╔════════════╗\n"
        " ⬇️ YOUR REFERRAL LINK ⬇️ \n"
        "╚════════════╝\n"
        f"{ref_link}\n\n"
        "🟢Invite your friends and earn 10% commission when they buy trees.",
    )

@dp.message(F.text == "ℹ️ Information")
async def about_goldplant(message: types.Message):
    await message.answer(
        "Welcome to GOLDPLANT, your gamified investment bot where you can grow virtual trees that generate real earnings. 🌱💰\n\n"
        "How it works:\n\n"
        "🌳Buy a tree🌳: Invest your balance to plant your first tree.\n"
        "💧Water your tree💧: Every 24 hours you can water it to keep it growing and generating earnings.\n"
        "💰Get earnings💰: Your tree produces a daily return (ROI) based on its type and size.\n"
        "💳Deposits💳: Deposits are made by paying with card to recharge your balance.\n"
        "💸Withdrawals💸: The minimum withdrawal amount is $50. To request it, you must enter the 16 digits of your card and the name and surname of the holder.\n"
        "👥Referral system👥: Invite your friends with your unique link and earn 10% of their tree purchases**. More friends, more earnings! 🧑‍🤝‍🧑\n\n"
        "💯Transparency and security💯:\n\n"
        "- All calculations and records are securely stored.\n"
        "- You don't need intermediaries: everything is automatic and reliable.\n\n"
        "💡 Tip: Take care of your trees every day and reinvest your earnings to accelerate your growth.\n\n"
        "Enjoy growing and earning with Goldplant! 🌳🚀"
    )

@dp.message(F.text == "📊 History")
async def show_history(message: types.Message):
    user_id = message.from_user.id
    
    # Here you can get the withdrawal history
    async with db_pool.acquire() as conn:
        withdrawals = await conn.fetch(
            "SELECT amount, status, request_date FROM withdrawals WHERE user_id = $1 ORDER BY request_date DESC LIMIT 10",
            user_id
        )
    
    msg = (
        "╔═════════════╗\n"
        " 📊 <b>WITHDRAWAL HISTORY</b> 📊\n"
        "╚═════════════╝\n\n"
    )
    
    if withdrawals:
        msg += "\n💸 Withdrawals:\n"
        status_map = {
            "approved": "✅",
            "rejected": "❌",
            "pending": "⏳"
        }
        
        for w in withdrawals:
            date_str = w['request_date'].strftime("%d/%m/%Y")
            status_emoji = status_map.get(w['status'], w['status'])
            msg += f"• ${w['amount']} - {status_emoji} - {date_str}\n"
    else:
        msg += "\n💸 Withdrawals: None\n"
    
    await message.answer(msg, parse_mode="HTML")

# --- 4️⃣ Back to main menu ---
@dp.message(F.text == "↩️ Back to Main Menu")
async def back_to_main_menu(message: types.Message):
    await main_menu(message)

# --- 5️⃣ Back to More Options ---
@dp.message(F.text == "↩️ Back to More Options")
async def back_to_more_options(message: types.Message):
    await more_options(message)

@dp.callback_query(lambda c: c.data and c.data.startswith("buy_"))
async def process_buy_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    cost = int(callback_query.data.split("_")[1])
    
    if cost not in tree_map:
        await callback_query.message.answer("❌ Invalid option.")
        await callback_query.answer()
        return
    
    tree_type, roi = tree_map[cost]
    daily_return = cost * roi
    
    # --- PURCHASE TRANSACTION ---
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                user_row = await conn.fetchrow("SELECT balance, referred_by FROM users WHERE user_id = $1 FOR UPDATE", user_id)
                if not user_row:
                    await callback_query.message.answer("❌ Error getting user data.")
                    await callback_query.answer()
                    return
                
                current_balance = user_row['balance']
                referrer_id = user_row['referred_by']
                
                if current_balance < cost:
                    error_message = "❌ Insufficient balance. You need $" + str(cost) + " to buy this tree.\n"
                    await callback_query.message.answer(error_message)
                    await callback_query.answer()
                    return
                
                # 2. Subtract balance and add investment
                await conn.execute("UPDATE users SET total_invested = total_invested + $1, balance = balance - $1 WHERE user_id = $2", cost, user_id)
                
                # 3. Pay commission to referrer if exists
                if referrer_id:
                    commission = cost * 0.10
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", commission, referrer_id)
                    print(f"Commission of {commission} paid to user {referrer_id}")
                
                # 4. Insert the tree
                now = datetime.now()
                await conn.execute("INSERT INTO trees (user_id, cost, daily_return, purchase_date, last_claimed) VALUES ($1, $2, $3, $4, $4)", user_id, cost, daily_return, now)
                
                await callback_query.message.answer("You have purchased a Tree " + str(tree_type))
                await callback_query.answer()
                
                # --- ADMIN NOTIFICATION ---
                try:
                    admin_message = (
                        f"🌳 <b>New tree purchased</b> by a user:\n\n"
                        f"👤 <b>User:</b> @{callback_query.from_user.username}\n"
                        f"💰 <b>Tree price:</b> ${cost}\n"
                        f"🌳 <b>Tree type:</b> {tree_type}\n"
                        f"🔗 <b>User link:</b> https://t.me/{callback_query.from_user.username}\n"
                        f"⏰ <b>Purchase date:</b> {now}\n"
                    )
                    await bot.send_message(ADMIN_ID, admin_message, parse_mode="HTML")
                except Exception as e:
                    print(f"Error sending notification to admin: {e}")
    
    except Exception as e:
        print(f"Purchase error: {e}")
        await callback_query.message.answer("Error processing purchase: " + str(e))
        await callback_query.answer()

# --- Handler to cancel any action and return to main menu ---
@dp.message(F.text == "❌ Cancel", StateFilter(None))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """
    This handler is activated when the user presses the '❌ Cancel' button.
    Clears any active state and returns the user to the main menu.
    """
    await state.clear()  # Clear the state just in case
    await message.answer(
        "✅ Operation cancelled. You have returned to the main menu.",
        reply_markup=main_menu_keyboard()
    )

# --- NEW CORRECT HANDLER ---
@dp.message(WithdrawState.waiting_for_add_balance_amount)
async def process_add_balance_amount(message: types.Message, state: FSMContext):
    """This is only activated when the user is in the waiting amount state."""
    # If the user presses cancel, we clear the state and return to the menu
    if message.text == "❌ Cancel":
        await state.clear()
        await message.answer("✅ Operation cancelled. You have returned to the main menu.", reply_markup=main_menu_keyboard())
        return
    
    try:
        amount = float(message.text)
        if amount <= 0:
            await message.answer("❌ The amount must be a positive number. Try again.")
            return
        
        user_id = message.from_user.id
        await message.answer("🔁 Generating your payment link, please wait...")
        
        # Call our function to create the invoice
        payment_url = await create_nowpayments_invoice(amount, user_id)
        
        # Create the inline button so the user can pay directly
        payment_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 Pay Now", url=payment_url)]
        ])
        
        await message.answer(
            f"✅ Ready! To add ${amount:.2f} to your balance, click the button below.\n\n"
            f"⏳ Once the payment is completed, your balance will be updated automatically.",
            reply_markup=payment_keyboard
        )
        
        # IMPORTANT: We clear the state and return to the main menu
        await state.clear()
        await message.answer("🏦 Main Menu:", reply_markup=main_menu_keyboard())
    
    except ValueError:
        await message.answer("❌ Invalid format. Please write just a number (e.g., 50 or 25.5).")
    except Exception as e:
        print(f"Error creating payment for user {message.from_user.id}: {e}")
        await message.answer("❌ An error occurred while generating the payment. Please try again later.")
        await state.clear()  # We also clear the state in case of error

@dp.message(F.text.startswith('/') == False, StateFilter(None))
async def handle_menu(message: types.Message, state: FSMContext):
    text = message.text
    user_id = message.from_user.id
    username = message.from_user.username
    if username is None:
        username = message.from_user.first_name or "User"
    
    if text == "💰Buy💰":
        buttons = []
        for cost, (name, roi) in tree_map.items():
            daily_profit = cost * roi
            label = f"{name}\n💰 Earnings: ${daily_profit:.2f}/day"
            buttons.append([types.InlineKeyboardButton(text=label, callback_data=f"buy_{cost}")])
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "╔═════════════╗\n"
            "<b>🌳 SHOP 🌳</b>\n"
            "╚═════════════╝\n\n",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    elif text == "💳Add Balance💳":
        # We create a keyboard with the cancel option
        cancel_keyboard = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancel")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        
        await message.answer(
            "💳 Please write the amount of USD you want to add to your balance (e.g., 10, 25, 50):\n\n"
            "✍️ Simply send me the number as a message.\n\n"
            "Or press the button to cancel.",
            reply_markup=cancel_keyboard
        )
        
        # <-- KEY CHANGE: WE PUT THE USER IN THE NEW STATE
        await state.set_state(WithdrawState.waiting_for_add_balance_amount)
    
    elif text == "🌳My Trees🌳":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT cost, daily_return, purchase_date FROM trees WHERE user_id = $1", user_id)
        
        if not rows:
            await message.answer("🍂 You don't have trees yet. Buy one to start harvesting.")
            return
        
        msg = (
            "╔═════════════╗\n"
            "<b>🌳 YOUR PERSONAL FOREST 🌳</b>\n"
            "╚═════════════╝\n\n"
        )
        
        total_daily = 0
        for row in rows:
            cost = row['cost']
            daily = row['daily_return']
            purchase_date = row['purchase_date'].strftime("%d/%m/%Y")
            total_daily += daily
            
            tree_name = "Unknown Tree"
            if cost == 50:
                tree_name = "🌳⭐"
            elif cost == 100:
                tree_name = "🌳⭐⭐"
            elif cost == 300:
                tree_name = "🌳⭐⭐⭐"
            elif cost == 500:
                tree_name = "🌳⭐⭐⭐⭐"
            elif cost == 1000:
                tree_name = "🌳⭐⭐⭐⭐⭐"
            
            msg += f"• {tree_name} (${cost})\n └ Generates: ${daily:.2f}/day\n └ Purchased: {purchase_date}\n\n"
        
        msg += f"💰 <b>Total Daily Earnings:</b> ${total_daily:.2f}"
        
        await send_long_message(message, msg, parse_mode="HTML")
    
    elif text == "💧Water Tree💧":
        # Get user data
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("❌ Error loading profile.")
            return
        
        # Get the user's trees
        async with db_pool.acquire() as conn:
            trees = await conn.fetch("SELECT id FROM trees WHERE user_id = $1", user_id)
        
        if not trees:
            await message.answer("❌ You have no trees to water.")
            return
        
        now = datetime.now()
        last_watered = user_data.get("last_watered")
        
        # Check if 24h have passed since the last watering
        if not last_watered or (now - last_watered).total_seconds() >= 86400:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET last_watered = $1 WHERE user_id = $2",
                    now, user_id
                )
            
            await message.answer(
                f"💧 All your trees have been watered.\n"
                "⏱️ They will start generating earnings automatically for 24 hours."
            )
        else:
            # Calculate remaining time for next watering
            remaining_seconds = 86400 - (now - last_watered).total_seconds()
            hours = int(remaining_seconds // 3600)
            minutes = int((remaining_seconds % 3600) // 60)
            
            await message.answer(
                "🌳 Your trees are already watered💧.\n"
                f"⌛ Next watering available in: {hours}h {minutes}min"
            )
    
    elif text == "💸Withdraw💸":
        user_id = message.from_user.id
        
        # --- STEP 1: Update balance with pending earnings ---
        earned = await claim_tree_earnings(user_id)
        if earned > 0:
            print(f"User {user_id}: Earnings of ${earned:.2f} added before withdrawal.")
        
        # --- STEP 2: Checks and withdrawal flow (without changes) ---
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("Error loading your profile.")
            return
        
        balance = float(user_data["balance"])
        min_withdraw = 50
        
        if balance < min_withdraw:
            await message.answer(f"❌ You need to accumulate at least ${min_withdraw:.2f} to withdraw. Your current balance is ${balance:.2f}")
            return
        
        if await check_pending_withdrawals(user_id):
            await message.answer("⏳ You already have a withdrawal request in process.")
            return
        
        # Start withdrawal flow
        cancel_kb = types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancel Withdrawal")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        
        await message.answer(
            "💳 Enter the 16 digits of your debit or credit card:",
            reply_markup=cancel_kb
        )
        
        await state.set_state(WithdrawState.waiting_for_card)
    
    elif text == "🏦Balance🏦":
        user_data = await get_user_data(user_id)
        if not user_data:
            await message.answer("Error loading profile.")
            return
        
        base_balance = float(user_data["balance"])
        last_watered = user_data["last_watered"]
        
        # --- EARNINGS CALCULATION (same as you had) ---
        async with db_pool.acquire() as conn:
            trees = await conn.fetch("SELECT daily_return FROM trees WHERE user_id = $1", user_id)
        
        now = datetime.now()
        generated_earnings = 0.0
        
        if last_watered:
            elapsed = (now - last_watered).total_seconds()
            if elapsed > 86400:  # maximum 24h
                elapsed = 86400
            
            for tree in trees:
                generated_earnings += float(tree["daily_return"]) * (elapsed / 86400)
        
        # If there are earnings, SAVE THEM in the database
        if generated_earnings > 0:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, last_watered = $2 WHERE user_id = $3",
                    generated_earnings, now, user_id
                )
            
            # We also update the local balance to show it
            base_balance += generated_earnings
        
        # --- GET THE REST OF THE DATA (same as you had) ---
        async with db_pool.acquire() as conn:
            total_withdrawn = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id = $1 AND status = 'approved'",
                user_id
            )
            
            total_referral_earnings = await conn.fetchval(
                "SELECT COALESCE(SUM(t.cost * 0.10), 0) FROM trees t JOIN users u ON t.user_id = u.user_id WHERE u.referred_by = $1",
                user_id
            )
        
        # --- SHOW THE MESSAGE (now with the real balance) ---
        username = message.from_user.username or message.from_user.first_name
        
        await message.answer(
            f"👤 <b>@{username}</b>\n\n"
            f"💰 <b>Balance:</b> ${base_balance:.2f}\n"  # We use base_balance which is already the real one
            f"💸 <b>Total Withdrawn:</b> ${float(total_withdrawn):.2f}\n"
            f"💵 <b>Referral Earnings:</b> ${float(total_referral_earnings):.2f}\n\n"
            f"<i>💧 Earnings have been added to your balance.</i>",
            parse_mode="HTML"
        )

# --- HANDLER FOR CARD ENTRY ---
@dp.message(WithdrawState.waiting_for_card)
async def process_card(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    # ⚠️ If the user presses Cancel Withdrawal
    if text == "❌ Cancel Withdrawal":
        await state.clear()
        await message.answer(
            "❌ Withdrawal cancelled. You can use the buttons normally.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    # Validate card number
    card_number = text.replace(" ", "")
    if not card_number.isdigit() or len(card_number) != 16:
        await message.answer(
            "❌ Invalid number. Make sure to write exactly 16 numbers without letters or spaces."
        )
        return
    
    # Save card number and move to the next state
    await state.update_data(card_number=card_number)
    
    await message.answer(
        "✅ Valid card. Now write your Full Name:",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Cancel Withdrawal")]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    
    await state.set_state(WithdrawState.waiting_for_name)

# --- HANDLER FOR FULL NAME ENTRY ---
@dp.message(WithdrawState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    # ⚠️ If the user presses Cancel Withdrawal
    if text == "❌ Cancel Withdrawal":
        await state.clear()
        await message.answer(
            "❌ Withdrawal cancelled. You can use the buttons normally.",
            reply_markup=main_menu_keyboard()
        )
        return
    
    full_name = text
    if len(full_name) < 3:
        await message.answer("❌ Name too short. Try again.")
        return
    
    user_id = message.from_user.id
    username = message.from_user.username
    data = await state.get_data()
    card_number = data.get("card_number")
    
    # Get user data
    user_data = await get_user_data(user_id)
    if not user_data:
        await message.answer("Error getting user data.")
        await state.clear()
        return
    
    balance = user_data['balance']
    
    if balance <= 0:
        await message.answer("❌ You have no balance available to withdraw.")
        await state.clear()
        return
    
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                now = datetime.now()
                
                # Insert withdrawal request
                await conn.execute(
                    "INSERT INTO withdrawals (user_id, amount, card_number, full_name, status, request_date) VALUES ($1, $2, $3, $4, 'pending', $5)",
                    user_id, balance, card_number, full_name, now
                )
                
                # Subtract balance
                await conn.execute(
                    "UPDATE users SET balance = balance - $1 WHERE user_id = $2",
                    balance, user_id
                )
                
                # Update payment data
                await conn.execute(
                    "UPDATE users SET card_number = $1, full_name = $2 WHERE user_id = $3",
                    card_number, full_name, user_id
                )
                
                # Notify admin
                try:
                    admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                        [types.InlineKeyboardButton(text="✅ Approve Withdrawal", callback_data=f"approve_{user_id}")],
                        [types.InlineKeyboardButton(text="❌ Reject Withdrawal", callback_data=f"reject_{user_id}")]
                    ])
                    
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔔 <b>New Withdrawal Request</b>\n\n"
                        f"👤 <b>User:</b> @{username} ({full_name})\n"
                        f"👤 User ID: {user_id}\n"
                        f"⏰ <b>Request date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"👤 Name: {full_name}\n"
                        f"💳 Card: {card_number}\n"
                        f"💰 Amount: ${balance:.2f}",
                        parse_mode="HTML",
                        reply_markup=admin_keyboard
                    )
                except Exception as e:
                    print(f"Could not notify admin: {e}")
                
                await message.answer(
                    f"✅ Request received.\n\nA request of ${balance:.2f} "
                    f"has been sent to the card ending in ****{card_number[-4:]}.\n\n⏳ The payment will be processed in less than 24 hours.",
                    reply_markup=main_menu_keyboard()
                )
                
                await state.clear()
    
    except Exception as e:
        print(f"Error saving data: {e}")
        await message.answer(f"Error processing request: {e}", reply_markup=main_menu_keyboard())
        await state.clear()

@dp.message(F.text == "❌ Cancel Withdrawal")
async def cancel_withdraw(message: types.Message, state: FSMContext):
    # Clear the state
    await state.clear()
    
    # Return to the main menu
    await message.answer(
        "❌ Withdrawal cancelled. You can use the buttons normally.",
        reply_markup=main_menu_keyboard()
    )

# --- 8. ADMIN CALLBACK ---
@dp.callback_query(lambda c: c.data and c.data.startswith("approve_"))
async def admin_approve_withdraw(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ You don't have permissions.")
        return
    
    try:
        user_id = int(callback_query.data.split("_")[1])
        
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Find last pending withdrawal
                row = await conn.fetchrow(
                    "SELECT id, amount FROM withdrawals WHERE user_id = $1 AND status = 'pending' ORDER BY id DESC LIMIT 1",
                    user_id
                )
                
                if row:
                    withdraw_id = row['id']
                    amount = row['amount']
                    now = datetime.now()
                    
                    # Mark as approved
                    await conn.execute(
                        "UPDATE withdrawals SET status = 'approved', processed_date = $1 WHERE id = $2",
                        now, withdraw_id
                    )
                    
                    # Notify user
                    await bot.send_message(user_id, f"✅ Your withdrawal request of ${amount} has been approved.")
                    await callback_query.message.edit_text(f"✅ Withdrawal of ID {user_id} for ${amount} approved.")
                    await callback_query.answer()
                else:
                    await callback_query.message.edit_text("❌ No pending withdrawal found for this user.")
                    await callback_query.answer()
    
    except Exception as e:
        print(f"Approval error: {e}")
        await callback_query.answer("Internal error approving.")

# --- ADMIN CALLBACK TO REJECT WITHDRAWALS ---
@dp.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def admin_reject_withdraw(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ You don't have permissions.")
        return
    
    try:
        user_id = int(callback_query.data.split("_")[1])
        
        # We save the ID of the user whose withdrawal will be rejected
        await state.update_data(target_user_id=user_id)
        
        # We change the admin's state to capture their reason
        await state.set_state(WithdrawState.waiting_for_rejection_reason)
        
        await callback_query.message.edit_text(
            "❌ You have rejected the withdrawal. Please reply to this message with the reason for rejection."
        )
        
        await callback_query.answer()
    
    except Exception as e:
        print(f"Rejection error: {e}")
        await callback_query.answer("Internal error rejecting withdrawal.")

# --- REJECTION REASON HANDLER ---
@dp.message(WithdrawState.waiting_for_rejection_reason)
async def process_rejection_reason(message: types.Message, state: FSMContext):
    rejection_reason = message.text.strip()
    
    if len(rejection_reason) < 5:
        await message.answer("❌ The reason is too short. Try again.")
        return
    
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    
    if not target_user_id:
        await message.answer("❌ Error: Could not identify the user. State restarted.")
        await state.clear()
        return
    
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Get last pending withdrawal from the user
                row = await conn.fetchrow(
                    "SELECT id, amount FROM withdrawals WHERE user_id = $1 AND status = 'pending' ORDER BY id DESC LIMIT 1",
                    target_user_id
                )
                
                if row:
                    withdraw_id = row['id']
                    amount = row['amount']
                    now = datetime.now()
                    
                    # Mark withdrawal as rejected and add reason
                    await conn.execute(
                        "UPDATE withdrawals SET status = 'rejected', rejection_reason = $1, processed_date = $2 WHERE id = $3",
                        rejection_reason, now, withdraw_id
                    )
                    
                    # Return money to user
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        amount, target_user_id
                    )
                    
                    # Notify user
                    await bot.send_message(
                        target_user_id,
                        f"❌ Your withdrawal request of ${amount:.2f} has been rejected.\n\nReason: {rejection_reason}\n\nThe amount has been returned to your balance."
                    )
                    
                    await message.answer(f"✅ Withdrawal from user {target_user_id} rejected. Reason sent and money returned.")
                else:
                    await message.answer("❌ No pending withdrawal found for this user.")
    
    except Exception as e:
        print(f"Error processing rejection reason: {e}")
        await message.answer("Error processing rejection request.")
        await state.clear()

# --- 9. /add COMMAND (ADMIN ONLY) ---
# --- BLOCK USER ---
@dp.message(Command("block"))
async def cmd_block_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You are not an admin.")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /block USER_ID")
            return
        
        target_id = int(args[1])
        
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", target_id)
        
        await message.answer(f"✅ User {target_id} blocked.")
    
    except Exception as e:
        await message.answer(f"Error: {e}")

# --- UNBLOCK USER ---
@dp.message(Command("unblock"))
async def cmd_unblock_user(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You are not an admin.")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /unblock USER_ID")
            return
        
        target_id = int(args[1])
        
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET is_blocked = FALSE WHERE user_id = $1", target_id)
        
        await message.answer(f"✅ User {target_id} unblocked.")
    
    except Exception as e:
        await message.answer(f"Error: {e}")

@dp.message(Command("add"))
async def cmd_add_balance(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You are not an admin.")
        return
    
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Usage: /add ID AMOUNT")
            return
        
        target_id = int(args[1])
        amount = float(args[2])
        
        await add_balance_admin(target_id, amount)
        await message.answer(f"✅ ${amount} sent to user {target_id}")
    
    except ValueError:
        await message.answer("Error: The ID must be an integer and the amount a decimal number.")
    except Exception as e:
        await message.answer(f"Error: {e}")

# --- CORRECTED MAIN FUNCTION ---
async def main():
    print("🚀 STARTING WEB SERVER")
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Application stopped.")