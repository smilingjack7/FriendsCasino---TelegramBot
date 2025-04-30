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

# Импортируем игровые модули
import blackjack

# --- Константы и Конфигурация ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL: raise ValueError("DATABASE_URL environment variable not set")

INITIAL_BALANCE = 100.0
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
LEADERBOARD_LIMIT = 10

# --- Настройка Логгирования ---
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Веб-сервер для Keep-Alive ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home(): return "Bot is alive!"
def run_web_server():
    port = int(os.environ.get("PORT", 8080))
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

# --- Взаимодействие с БД (Общее) ---
def get_db_conn():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True
        logger.debug("Database connection established.")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"DB connection error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected DB connection error: {e}")
        raise

def get_or_create_user(user_id: int) -> dict | None:
    sql_select = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    sql_insert = "INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, NULL) ON CONFLICT (user_id) DO NOTHING;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql_select, (user_id,))
            user_data = cur.fetchone()
            if not user_data:
                logger.info(f"User {user_id} not found, creating...")
                cur.execute(sql_insert, (user_id, INITIAL_BALANCE))
                cur.execute(sql_select, (user_id,))
                user_data = cur.fetchone()
                if user_data: logger.info(f"New user {user_id} created.")
                else: logger.error(f"Failed create/find user {user_id} after insert."); return None

            if user_data and user_data.get('last_bonus'):
                 if isinstance(user_data['last_bonus'], str):
                     try: user_data['last_bonus'] = datetime.datetime.fromisoformat(user_data['last_bonus'])
                     except ValueError: user_data['last_bonus'] = None
                 elif not isinstance(user_data['last_bonus'], datetime.datetime):
                     user_data['last_bonus'] = None
            return user_data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}", exc_info=True)
        return None

# --- Функции Баланса и Бонуса (Общие) ---
async def update_balance(user_id: int, change: float) -> float | None:
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        # Используем with для автоматического закрытия соединения и курсора
        # get_db_conn() синхронная, так что await не нужен
        with get_db_conn() as conn:
             with conn.cursor() as cur:
                cur.execute(sql, (change, user_id))
                result = cur.fetchone()
                if result:
                    new_balance = result[0]
                    logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                    return float(new_balance)
                else:
                    logger.warning(f"Update balance failed for user {user_id}.")
                    # Возможно, стоит попробовать создать пользователя здесь, если его нет?
                    # get_or_create_user(user_id) # Убедиться, что пользователь существует
                    # И повторить попытку? Но это усложняет логику.
                    return None
    except Exception as e:
        logger.error(f"DB Error (update_balance) for {user_id}: {e}", exc_info=True)
        return None


async def get_balance(user_id: int) -> float | None:
    user_data = get_or_create_user(user_id) # Синхронная функция
    if user_data and 'balance' in user_data:
        return float(user_data['balance'])
    else:
        logger.warning(f"Could not retrieve balance for user {user_id}.")
        return None

async def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    ts_naive = ts_utc.replace(tzinfo=None) if ts_utc else None
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts_naive, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {ts_naive}")
    except Exception as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}", exc_info=True)

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None
    if last_bonus and not isinstance(last_bonus, datetime.datetime):
         logger.warning(f"Invalid last_bonus type for user {user_id}: {type(last_bonus)}. Returning None.")
         return None
    return last_bonus

# --- Функция Лидерборда (Общая) ---
def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            leaders = cur.fetchall()
            return leaders
    except Exception as e:
        logger.error(f"DB Error (get_leaderboard): {e}", exc_info=True)
        return []

# --- Упоминание Пользователя (HTML, Общее) ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    now = time.monotonic()
    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
            return cached['mention']

    mention = f"User {user_id}"
    try:
        user_chat = await context.bot.get_chat(user_id)
        mention = user_chat.mention_html() or f"User {user_id}"
    except BadRequest as e:
         if "chat not found" in str(e).lower() or "user not found" in str(e).lower():
              logger.warning(f"Could not get chat for user {user_id}: {e}")
         else: logger.warning(f"Failed mention {user_id} BadRequest: {e}")
    except Exception as e:
        logger.warning(f"Failed mention {user_id} (error): {e}", exc_info=True)

    async with _cache_lock:
        _user_mention_cache[user_id] = {'mention': mention, 'ts': now}
    return mention

# --- Хелпер для Данных Заданий ---
def get_job_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if context.job and hasattr(context.job, 'data'):
        return context.job.data
    logger.warning("Attempted get job data but context.job/data missing.")
    return {}

# --- Основные Команды Бота ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id) # Синхронно
    balance = await get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {html_escape(user.first_name)}! 👋\n"
        f"Ваш баланс: <b>{balance_str}</b> фишек.\n\n"
        f"Чтобы сыграть в Блекджек, используйте /blackjack.\n"
        f"Для справки по командам введите /help.",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/help user {user.id}")
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
        "\n<i>Играйте ответственно! Удачи!</i>"
    )
    await update.message.reply_text(help_text.format(BONUS_COOLDOWN_HOURS=BONUS_COOLDOWN_HOURS), parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/balance user {user.id}")
    balance = await get_balance(user.id)
    if balance is not None:
        await update.message.reply_text(f"Ваш текущий баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/bonus user {user.id} chat {chat.id} type {chat.type}")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Получить бонус можно только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return

    if not get_or_create_user(user.id): # Синхронно
        await update.message.reply_text("Ошибка: Не удалось найти или создать ваш профиль.")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_naive = get_last_bonus_time(user.id) # Синхронно, возвращает наивное
    last_bonus_utc = None
    if last_bonus_naive:
        last_bonus_utc = last_bonus_naive.replace(tzinfo=datetime.timezone.utc)

    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_utc and (now_utc < last_bonus_utc + cooldown):
        time_diff = last_bonus_utc + cooldown - now_utc
        hours, remainder = divmod(time_diff.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(
            f"⏳ Бонус уже был получен. Снова через: <b>{int(hours)} ч {int(minutes)} мин</b>." ,
            parse_mode=ParseMode.HTML
        )
        return

    new_balance = await update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        await update_last_bonus_time(user.id, now_utc) # Асинхронно
        await update.message.reply_text(
            f"🎉 Поздравляем! Бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ Ошибка при начислении бонуса. Попробуйте позже.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/leaderboard user {user.id}")

    leaders = get_leaderboard(LEADERBOARD_LIMIT) # Синхронно
    if not leaders:
        await update.message.reply_text("🏆 Таблица лидеров пока пуста.")
        return

    leaderboard_text = f"🏆 <b>Таблица Лидеров (Топ {LEADERBOARD_LIMIT})</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]

    mentions = await asyncio.gather(*(get_user_mention(context, leader['user_id']) for leader in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        name = mentions[i] if i < len(mentions) else f"User {leader['user_id']}"
        balance_str = f"{leader.get('balance', 0):.2f}"
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"

    try:
        await update.message.reply_text(
            leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров.")

# --- Главный Маршрутизатор Callback Query ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user

    if not data: await query.answer(); return
    logger.debug(f"Callback query: '{data}' user {user.id}")

    prefix = data.split("_", 1)[0]

    try:
        if prefix == "bj":
            if update.effective_chat.type != ChatType.PRIVATE:
                await query.answer("Играть можно только в личном чате.", show_alert=True)
                return
            # Делегируем обработку модулю blackjack
            await blackjack.handle_blackjack_callback(update, context, data)

        # elif prefix == "othergame":
        #     await othergame.handle_callback(update, context, data)

        else:
            logger.warning(f"Unknown callback prefix: {prefix} in data '{data}'")
            await query.answer() # Молча подтверждаем

    except ApplicationHandlerStop: raise # Редко используется
    except BadRequest as e:
         if "query is too old" in str(e).lower():
              logger.info(f"Callback query '{data}' too old user {user.id}.")
         else:
              logger.error(f"Main Callback BadRequest '{data}' user {user.id}: {e}")
              try: await query.answer("Ошибка обработки.", show_alert=True)
              except Exception: pass
    except Exception as e:
        logger.error(f"Main Callback error '{data}' user {user.id}: {e}", exc_info=True)
        try: await query.answer("Внутренняя ошибка.", show_alert=True)
        except Exception: pass

# --- Обработчик Ошибок ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error! Ensure only ONE instance running.")
    elif isinstance(context.error, BadRequest):
        logger.warning(f"BadRequest error: {context.error}. Update: {update}")

# --- Основная Функция ---
def main():
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

        # --- Регистрация Основных Обработчиков ---
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))

        # --- Регистрация Игровых Обработчиков ---
        blackjack.register_handlers(application)
        # other_game.register_handlers(application) # Для будущих игр

        # --- Регистрация Главного Маршрутизатора Callback ---
        application.add_handler(CallbackQueryHandler(button_callback_handler))

        # --- Регистрация Обработчика Ошибок ---
        application.add_error_handler(error_handler)

        logger.info("All handlers registered.")
        print("Bot is running... Press Ctrl+C to stop.")

        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except ValueError as e: logger.critical(f"Config Error: {e}"); print(f"CRITICAL: {e}")
    except Conflict as e: logger.critical(f"Conflict Error: {e}. Instance running?"); print("CRITICAL: Conflict detected.")
    except Exception as e: logger.critical(f"Critical runtime error: {e}", exc_info=True); print(f"CRITICAL: {e}")
    finally: print("Bot stopped."); logger.info("Bot application stopped.")

# --- Настройка/Проверка БД ---
def setup_database():
    logger.info("Checking database schema...")
    sql_create_table = """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        balance NUMERIC(15, 2) DEFAULT 0.00 NOT NULL,
        last_bonus TIMESTAMP WITHOUT TIME ZONE NULL
    );
    """
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql_create_table)
            # conn.commit() # Не нужно, если autocommit=True
            logger.info("Database schema check complete. 'users' table exists.")
    except Exception as e:
        logger.error(f"Database setup failed: {e}", exc_info=True)
        # exit(1) # Раскомментируйте, если без БД бот не должен работать

if __name__ == "__main__":
    setup_database()
    main()