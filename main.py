# main.py
# -*- coding: utf-8 -*-

import logging
import os
import datetime
import time
import asyncio
from threading import Thread
from flask import Flask
from html import escape as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, ApplicationHandlerStop
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Conflict

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# Import game modules
import blackjack # <<< IMPORT BLACKJACK MODULE

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL: raise ValueError("DATABASE_URL environment variable not set")

INITIAL_BALANCE = 100.0
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
LEADERBOARD_LIMIT = 10

# --- Logging Setup ---
# Configure logging format and level
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)
# Set higher logging levels for libraries that produce too much noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Keep PTB logs at INFO
logging.getLogger('werkzeug').setLevel(logging.WARNING) # Reduce Flask logging noise
# Get the root logger for this application
logger = logging.getLogger(__name__) # Use __name__ for the main module logger

# --- Web Server for Keep-Alive ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home(): return "Bot is alive!"
def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    # Disable Flask's default reloader when running in production/deployment
    use_reloader = os.environ.get("FLASK_ENV") == "development"
    logger.info(f"Starting keep-alive web server on port {port} (reloader: {use_reloader})...")
    try:
        keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=use_reloader)
    except Exception as e:
        logger.error(f"Keep-alive web server failed: {e}", exc_info=True)

def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server thread started.")

# --- Database Interaction (Shared) ---
def get_db_conn():
    """Establishes and returns a database connection."""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True # Set autocommit for simplicity here
        logger.debug("Database connection established.")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"DB connection error: {e}")
        raise # Reraise critical connection errors
    except Exception as e:
        logger.error(f"Unexpected DB connection error: {e}")
        raise

def get_or_create_user(user_id: int) -> dict | None:
    """Gets user data or creates a new user with initial balance."""
    # Removed duplicate logging entry
    sql_select = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    sql_insert = "INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, NULL) ON CONFLICT (user_id) DO NOTHING;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql_select, (user_id,))
            user_data = cur.fetchone()
            if not user_data:
                logger.info(f"User {user_id} not found, creating...")
                cur.execute(sql_insert, (user_id, INITIAL_BALANCE))
                # Fetch again after insert attempt
                cur.execute(sql_select, (user_id,))
                user_data = cur.fetchone()
                if user_data:
                    logger.info(f"New user {user_id} created with balance {INITIAL_BALANCE}.")
                else: # Should not happen with ON CONFLICT DO NOTHING if insert worked or user already existed
                    logger.error(f"Failed to create or find user {user_id} after insert attempt.")
                    return None # Indicate failure clearly

            # Ensure last_bonus is datetime or None (handle potential string storage)
            if user_data and user_data.get('last_bonus'):
                 if isinstance(user_data['last_bonus'], str):
                     try: user_data['last_bonus'] = datetime.datetime.fromisoformat(user_data['last_bonus'])
                     except ValueError: user_data['last_bonus'] = None
                 elif not isinstance(user_data['last_bonus'], datetime.datetime):
                     user_data['last_bonus'] = None

            # logger.debug(f"Fetched user data for {user_id}: {user_data}")
            return user_data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}", exc_info=True)
        return None

# --- Balance and Bonus Functions (Shared) ---
async def update_balance(user_id: int, change: float) -> float | None:
    """Updates user balance by the given change amount. Returns the new balance or None on error."""
    # Using await get_db_conn() if it were async, but psycopg2 is sync
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id))
            result = cur.fetchone()
            if result:
                new_balance = result[0]
                logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                return float(new_balance) # Ensure float conversion
            else:
                logger.warning(f"Update balance failed for user {user_id} (user not found or DB issue).")
                return None
    except Exception as e:
        logger.error(f"DB Error (update_balance) for {user_id}: {e}", exc_info=True)
        return None

async def get_balance(user_id: int) -> float | None:
    """Gets the current balance for a user."""
    user_data = get_or_create_user(user_id) # This is sync, no await needed
    if user_data and 'balance' in user_data:
        # logger.debug(f"Retrieved balance for user {user_id}: {user_data['balance']}")
        return float(user_data['balance']) # Ensure float
    else:
        logger.warning(f"Could not retrieve balance for user {user_id}.")
        return None

async def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    """Updates the last bonus timestamp for a user."""
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    # Ensure timezone info is removed if DB doesn't handle timestamptz well
    ts_naive = ts_utc.replace(tzinfo=None) if ts_utc else None
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts_naive, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {ts_naive}")
    except Exception as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}", exc_info=True)

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Gets the last bonus time for a user (returns naive datetime)."""
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None
    # Already handled conversion in get_or_create_user
    if last_bonus and not isinstance(last_bonus, datetime.datetime):
         logger.warning(f"Invalid last_bonus type for user {user_id} after fetch: {type(last_bonus)}. Returning None.")
         return None
    # logger.debug(f"Retrieved last bonus time for user {user_id}: {last_bonus}")
    return last_bonus

# --- Leaderboard Function (Shared) ---
def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    """Fetches leaderboard data from the database."""
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            leaders = cur.fetchall()
            # logger.debug(f"Fetched leaderboard: {leaders}")
            return leaders
    except Exception as e:
        logger.error(f"DB Error (get_leaderboard): {e}", exc_info=True)
        return []

# --- Helper to get User Mention (HTML) ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600 # 1 hour cache

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """Gets user mention HTML, using a cache."""
    now = time.monotonic()
    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
            # logger.debug(f"Using cached mention for {user_id}")
            return cached['mention']

    # logger.debug(f"Fetching mention for {user_id} from Telegram API.")
    mention = f"User {user_id}" # Default fallback
    try:
        user_chat = await context.bot.get_chat(user_id)
        mention = user_chat.mention_html() or f"User {user_id}" # Use mention_html if available
    except BadRequest as e:
         if "chat not found" in str(e).lower() or "user not found" in str(e).lower():
              logger.warning(f"Could not get chat for user {user_id} (likely deleted/invalid): {e}")
         else:
              logger.warning(f"Failed to get mention for {user_id} due to BadRequest: {e}")
    except Exception as e:
        logger.warning(f"Failed to get mention for {user_id} (unexpected error): {e}", exc_info=True)

    # Update cache
    async with _cache_lock:
        _user_mention_cache[user_id] = {'mention': mention, 'ts': now}
        # logger.debug(f"Cached mention for {user_id}: {mention}")
    return mention

# --- Job Data Helper ---
def get_job_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Safely retrieves job data."""
    if context.job and hasattr(context.job, 'data'):
        return context.job.data
    logger.warning("Attempted to get job data but context.job or context.job.data is missing.")
    return {}

# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id) # Ensure user exists
    balance = await get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {html_escape(user.first_name)}! 👋\n"
        f"Ваш баланс: <b>{balance_str}</b> фишек.\n\n"
        f"Чтобы сыграть в Блекджек, используйте /blackjack.\n" # Simplified command
        # Add other games here later: f"Для игры в ... используйте /..."
        f"Для справки по командам введите /help.",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/help command from user {user.id}")
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>Общие команды:</b>\n"
        "/start - Начать и проверить баланс\n"
        "/balance - Показать текущий баланс\n"
        "/bonus - Получить бонус (раз в {BONUS_COOLDOWN_HOURS} часов, только в ЛС)\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение\n\n"
        "<b>Игры (только в ЛС):</b>\n"
        "/blackjack - Начать игру в Блекджек\n"
        # Add "/<game_command> - Начать игру в <Game Name>" here later
        "\n<i>Играйте ответственно! Удачи!</i>"
    )
    await update.message.reply_text(help_text.format(BONUS_COOLDOWN_HOURS=BONUS_COOLDOWN_HOURS), parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/balance command from user {user.id}")
    balance = await get_balance(user.id)
    if balance is not None:
        await update.message.reply_text(f"Ваш текущий баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/bonus command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Получить бонус можно только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return

    # get_or_create_user is sync, no await needed here
    if not get_or_create_user(user.id):
        await update.message.reply_text("Ошибка: Не удалось найти или создать ваш профиль.")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_naive = get_last_bonus_time(user.id) # Returns naive
    last_bonus_utc = None
    if last_bonus_naive:
        last_bonus_utc = last_bonus_naive.replace(tzinfo=datetime.timezone.utc) # Make aware for comparison

    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_utc and (now_utc < last_bonus_utc + cooldown):
        time_diff = last_bonus_utc + cooldown - now_utc
        hours, remainder = divmod(time_diff.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(
            f"⏳ Бонус уже был получен. Попробуйте снова через: <b>{int(hours)} ч {int(minutes)} мин</b>." ,
            parse_mode=ParseMode.HTML
        )
        return

    new_balance = await update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        await update_last_bonus_time(user.id, now_utc) # Pass aware timestamp
        await update.message.reply_text(
            f"🎉 Поздравляем! Вы получили бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Ваш новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ Ошибка при начислении бонуса. Пожалуйста, попробуйте позже.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/leaderboard command from user {user.id}")

    leaders = get_leaderboard(LEADERBOARD_LIMIT) # Sync function
    if not leaders:
        await update.message.reply_text("🏆 Таблица лидеров пока пуста.")
        return

    leaderboard_text = f"🏆 <b>Таблица Лидеров (Топ {LEADERBOARD_LIMIT})</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]

    mentions = await asyncio.gather(*(get_user_mention(context, leader['user_id']) for leader in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        name = mentions[i] if i < len(mentions) else f"User {leader['user_id']}"
        balance_str = f"{leader.get('balance', 0):.2f}" # Use .get with default
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"

    try:
        await update.message.reply_text(
            leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров.")

# --- Main Callback Query Router ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ Handles all button presses and routes to the appropriate game module. """
    query = update.callback_query
    data = query.data
    user = query.from_user

    if not data:
        await query.answer() # Answer immediately if no data
        return

    logger.debug(f"Callback query received: '{data}' from user {user.id}")

    # Determine the game/module based on the prefix
    prefix = data.split("_", 1)[0]

    try:
        if prefix == "bj":
            # Ensure Blackjack actions are in private chat before delegating
            if update.effective_chat.type != ChatType.PRIVATE:
                await query.answer("Играть в эту игру можно только в личном чате.", show_alert=True)
                return
            # Delegate to the blackjack module's handler
            await blackjack.handle_blackjack_callback(update, context, data)
            # Note: The game-specific handler should answer the query appropriately.

        # elif prefix == "othergame": # Example for future expansion
        #     await othergame.handle_callback(update, context, data)

        else:
            logger.warning(f"Unknown callback prefix: {prefix} in data '{data}'")
            await query.answer() # Acknowledge silently

    except ApplicationHandlerStop:
        # If a handler explicitly stops propagation (rarely needed here)
        raise
    except BadRequest as e:
         # Handle common errors like query too old here, as the specific handler might have finished
         if "query is too old" in str(e).lower():
              logger.info(f"Callback query '{data}' is too old for user {user.id}.")
              # Don't answer, the user likely already got feedback or the state changed
         else:
              logger.error(f"Main Callback BadRequest for '{data}' user {user.id}: {e}")
              try: await query.answer("Произошла ошибка при обработке.", show_alert=True)
              except Exception: pass # Ignore if answering itself fails
    except Exception as e:
        logger.error(f"Main Callback general error for '{data}' user {user.id}: {e}", exc_info=True)
        try: await query.answer("Произошла внутренняя ошибка.", show_alert=True)
        except Exception: pass

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors raised by Handlers or the Dispatcher."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error! Ensure only ONE bot instance is running with this token.")
    elif isinstance(context.error, BadRequest):
        logger.warning(f"BadRequest error: {context.error}. Update: {update}")

# --- Main Function ---
def main():
    """Starts the bot."""
    logger.info("Starting bot application...")
    start_keep_alive()

    try:
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .connect_timeout(30)
            .read_timeout(30)
            .pool_timeout(30)
            .build()
        )

        # --- Register Core Handlers ---
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))

        # --- Register Game Handlers ---
        blackjack.register_handlers(application)
        # Add other game registrations here later:
        # other_game.register_handlers(application)

        # --- Register Main Callback Router ---
        # This single handler routes *all* button clicks based on prefix
        application.add_handler(CallbackQueryHandler(button_callback_handler))

        # --- Register Error Handler ---
        application.add_error_handler(error_handler)

        logger.info("All handlers registered successfully.")
        print("Bot is running... Press Ctrl+C to stop.")

        # --- Start Polling ---
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

    except ValueError as e:
         logger.critical(f"Configuration Error: {e}")
         print(f"CRITICAL ERROR: {e}")
    except Conflict as e:
        logger.critical(f"Conflict Error: {e}. Is another instance running?")
        print("CRITICAL ERROR: Conflict detected. Another instance might be running.")
    except Exception as e:
        logger.critical(f"Critical error during bot startup or runtime: {e}", exc_info=True)
        print(f"CRITICAL ERROR: {e}")
    finally:
        print("Bot stopped.")
        logger.info("Bot application has stopped.")

if __name__ == "__main__":
    # --- Database Schema Check/Setup (Optional but recommended) ---
    def setup_database():
        logger.info("Checking database schema...")
        sql_create_table = """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance NUMERIC(15, 2) DEFAULT 0.00 NOT NULL,
            last_bonus TIMESTAMP WITHOUT TIME ZONE NULL
        );
        """
        # Add indexes for performance later if needed:
        # CREATE INDEX IF NOT EXISTS idx_users_balance ON users (balance DESC);
        try:
            with get_db_conn() as conn, conn.cursor() as cur:
                cur.execute(sql_create_table)
                conn.commit() # Commit schema changes
                logger.info("Database schema check complete. 'users' table exists.")
        except Exception as e:
            logger.error(f"Database setup failed: {e}", exc_info=True)
            # Decide if you want to exit if DB setup fails
            # exit(1)

    setup_database()
    main()