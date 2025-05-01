# -*- coding: utf-8 -*-
import logging
import os
import random
import datetime
import time
import asyncio
import math
from collections import defaultdict
from threading import Thread
from flask import Flask
from html import escape as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler # Not used directly, but concepts remain
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Conflict, Forbidden

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

# --- Blackjack Constants ---
INITIAL_BALANCE = 100.0
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
NUM_DECKS = 8
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.7
LEADERBOARD_LIMIT = 10
BJ_GAME_KEY = 'blackjack_game' # Key for user_data

# --- Roulette Constants (Imported & Adapted) ---
RL_BET_AMOUNTS = [10, 25, 50, 100, 250, 500]
RL_MAX_BETS_PER_ROUND = 10
RL_MAX_BETS_PER_USER = 3
RL_BET_TIMER_SECONDS = 45
RL_SPIN_ANIMATION_DURATION = 8.0
RL_GAME_KEY = 'roulette_game'
RL_USER_TEMP_BET_KEY = 'roulette_temp_bet'
RL_TIMER_DISPLAY_UPDATE_INTERVAL = 2.0 # How often to update the timer text (seconds)
AMERICAN_WHEEL_ORDER = [
    '0', '28', '9', '26', '30', '11', '7', '20', '32', '17', '5', '22', '34',
    '15', '3', '24', '36', '13', '1', '00', '27', '10', '25', '29', '12', '8',
    '19', '31', '18', '6', '21', '33', '16', '4', '23', '35', '14', '2'
]
RL_WHEEL_SIZE = len(AMERICAN_WHEEL_ORDER)
RL_RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
RL_BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}
RL_DOZENS = { '1st': set(range(1, 13)), '2nd': set(range(13, 25)), '3rd': set(range(25, 37)) }
RL_COLUMNS = { 'col1': {1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34}, 'col2': {2, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35}, 'col3': {3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36} }
RL_PAYOUTS = { 'number': 35, 'color': 1, 'parity': 1, 'dozen': 2, 'column': 2 }
RL_BET_VALUE_DISPLAY_NAMES = { 'Red': '🔴 Красное', 'Black': '⚫ Черное', 'Even': '⚖️ Четное', 'Odd': '❓ Нечетное', '1st': '1️⃣ Дюж. 1-12', '2nd': '2️⃣ Дюж. 13-24', '3rd': '3️⃣ Дюж. 25-36', 'col1': '📊 Кол. 1', 'col2': '📊 Кол. 2', 'col3': '📊 Кол. 3' }
RL_AMERICAN_WHEEL_SET = set(AMERICAN_WHEEL_ORDER)


# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home():
    return "Bot is alive!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)

def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Card Definitions (Blackjack) ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Database Interaction ---
def get_db_conn():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        raise

def get_or_create_user(user_id: int) -> dict | None:
    sql_s = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    sql_i = "INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, NULL) ON CONFLICT (user_id) DO NOTHING;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql_s, (user_id,))
            data = cur.fetchone()
            if not data:
                cur.execute(sql_i, (user_id, INITIAL_BALANCE))
                logger.info(f"New user created: {user_id}")
                cur.execute(sql_s, (user_id,))
                # Ensure data is fetched after potential creation or handle potential None
                data = cur.fetchone() or {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}

        # Post-fetch type check and conversion for last_bonus
        if data and data.get('last_bonus'):
            if isinstance(data['last_bonus'], str):
                try:
                    data['last_bonus'] = datetime.datetime.fromisoformat(data['last_bonus'])
                except ValueError:
                     logger.warning(f"Could not parse last_bonus string '{data['last_bonus']}' for user {user_id}. Resetting.")
                     data['last_bonus'] = None
            elif not isinstance(data['last_bonus'], datetime.datetime):
                 logger.warning(f"Invalid last_bonus type for user {user_id}: {type(data['last_bonus'])}. Resetting.")
                 data['last_bonus'] = None
        return data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}")
        return None

def update_balance(user_id: int, change: float) -> float | None:
    # Ensure user exists first
    if not get_or_create_user(user_id):
        logger.error(f"Attempted balance update for non-existent user {user_id}")
        return None

    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id,))
            res = cur.fetchone()
            if res:
                new_balance = res[0]
                logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                return new_balance
            else:
                # This case might happen if the user was deleted between get_or_create and update
                logger.warning(f"Update balance failed for user {user_id} (user not found or other issue after creation check)")
                return None
    except psycopg2.errors.CheckViolation as e:
         logger.warning(f"Balance update rejected for user {user_id}: {e} (Likely negative balance attempt)")
         # Return current balance if needed or None to indicate failure
         # For simplicity, returning None as the *update* failed
         return None
    except Exception as e:
        logger.error(f"DB Error (update_balance) for {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    # Convert timezone-aware datetime to naive UTC for psycopg2 compatibility if needed
    ts_naive = ts_utc.replace(tzinfo=None) if ts_utc else None
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts_naive, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {ts_naive}")
    except Exception as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None

    # Robust type checking and parsing
    if last_bonus and isinstance(last_bonus, str):
         try:
             last_bonus = datetime.datetime.fromisoformat(last_bonus)
         except ValueError:
             logger.warning(f"Could not parse last_bonus string '{last_bonus}' for user {user_id}. Resetting.")
             last_bonus = None
    elif last_bonus and not isinstance(last_bonus, datetime.datetime):
        logger.warning(f"Invalid last_bonus type for user {user_id}: {type(last_bonus)}. Resetting.")
        last_bonus = None

    # Ensure timezone awareness (UTC) if it's naive
    if last_bonus and last_bonus.tzinfo is None:
        last_bonus = last_bonus.replace(tzinfo=datetime.timezone.utc)

    return last_bonus

def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"DB Error (get_leaderboard): {e}")
        return []

# --- Blackjack Game Utilities ---
def create_deck(num=NUM_DECKS) -> list:
    deck = [(r, s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card: tuple | None) -> int:
    return RANK_VALUES.get(card[0], 0) if card else 0

def get_hand_value(hand: list) -> int:
    value = sum(get_card_value(card) for card in hand if card)
    num_aces = sum(1 for card in hand if card and card[0] == 'A')
    while value > 21 and num_aces > 0:
        value -= 10
        num_aces -= 1
    return value

def format_hand(hand: list, hide_one: bool = False) -> str:
    if not hand:
        return "Пусто"
    if hide_one and len(hand) > 1:
        first_card = f"{hand[0][0]}{hand[0][1]}" if hand[0] else "??"
        return f"[{first_card}, ??]"
    return ", ".join([f"{card[0]}{card[1]}" for card in hand if card])

def draw_card(deck: list) -> tuple | None:
    if not deck:
        logger.warning("Attempted to draw from an empty deck.")
        return None
    try:
        # Use random.randrange for potentially better performance on large lists than shuffle+pop
        return deck.pop(random.randrange(len(deck)))
    except (ValueError, IndexError) as e: # Catch if deck becomes empty between check and pop or randrange fails
        logger.error(f"Error drawing card: {e}")
        return None

# --- Roulette Game Utilities ---
def rl_get_value_display_name(bet_type, bet_value):
    if bet_type == 'number':
        return f"🔢 {bet_value}"
    return RL_BET_VALUE_DISPLAY_NAMES.get(bet_value, bet_value)

def rl_get_color(n_str):
    if n_str in ['0', '00']:
        return 'Green'
    try:
        n = int(n_str)
        return 'Red' if n in RL_RED_NUMBERS else ('Black' if n in RL_BLACK_NUMBERS else None)
    except ValueError:
        return None

def rl_is_even_or_odd(n_str):
    if n_str in ['0', '00']:
        return None
    try:
        return 'Even' if int(n_str) % 2 == 0 else 'Odd'
    except ValueError:
        return None

def rl_get_dozen(n_str):
    if n_str in ['0', '00']:
        return None
    try:
        n = int(n_str)
        return next((name for name, d_set in RL_DOZENS.items() if n in d_set), None)
    except ValueError:
        return None

def rl_get_column(n_str):
    if n_str in ['0', '00']:
        return None
    try:
        n = int(n_str)
        return next((name for name, c_set in RL_COLUMNS.items() if n in c_set), None)
    except ValueError:
        return None

# --- Helper to get User Mention (HTML) & Display Name ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600 # Cache for 1 hour

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[str, str]:
    now = time.monotonic()
    default_display_name = f"User_{user_id}"
    default_html_mention = default_display_name # Fallback if get_chat fails

    # Check cache first
    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
             # Ensure both keys exist before returning
            if 'html_mention' in cached and 'display_name' in cached:
                return cached['html_mention'], cached['display_name']

    # If not in cache or expired, fetch from Telegram
    display_name = default_display_name
    html_mention = default_html_mention
    try:
        user_chat = await context.bot.get_chat(user_id)
        html_mention = user_chat.mention_html() # Preferred way to get mention
        # Determine best display name
        if user_chat.first_name:
            display_name = user_chat.first_name
        elif user_chat.username:
            display_name = f"@{user_chat.username}"
        # Escape the display name only if it's not the default placeholder
        if display_name != default_display_name:
            display_name = html_escape(display_name)

    except (BadRequest, Forbidden) as e:
        # User not found, blocked bot, etc. Use defaults.
        logger.warning(f"Could not get chat for user {user_id}: {e}")
    except Exception as e:
        # Catch other potential errors during get_chat or attribute access
        logger.warning(f"Failed to get mention/name for {user_id}: {e}")

    # Update cache
    async with _cache_lock:
        _user_mention_cache[user_id] = {'html_mention': html_mention, 'display_name': display_name, 'ts': now}

    return html_mention, display_name

# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {html_escape(user.first_name)}! 👋\n"
        f"Ваш баланс: <b>{balance_str}</b> фишек.\n\n"
        "<b>Игры:</b>\n"
        "• /blackjack - Блекджек (только в ЛС)\n"
        "• /roulette - Американская Рулетка (ЛС и группы)\n\n"
        f"Для справки по командам введите /help.",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/help command from user {user.id}")
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>Общие команды:</b>\n"
        "/start - Приветствие и баланс\n"
        "/balance - Показать текущий баланс\n"
        f"/bonus - Получить бонус ({BONUS_AMOUNT} F, раз в {BONUS_COOLDOWN_HOURS} часов, только в ЛС)\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение\n\n"
        "<b>Игры:</b>\n"
        "/blackjack - Начать игру в Блекджек (только в ЛС)\n"
        "/roulette - Начать игру в Рулетку (ЛС и группы)\n"
        "  • В рулетке есть таймер ставок после первой ставки.\n"
        f"  • Макс. ставок на раунд: {RL_MAX_BETS_PER_ROUND} (общих), {RL_MAX_BETS_PER_USER} (на игрока).\n"
        "  • Используйте кнопки под сообщением рулетки для ставок.\n\n"
        "<i>Играйте ответственно! Удачи!</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/balance command from user {user.id} in chat {chat.id} (type: {chat.type})")
    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
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

    if not get_or_create_user(user.id): # Ensure user exists and handle potential creation failure
        await update.message.reply_text("Ошибка: Не удалось найти или создать ваш профиль.")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_utc = get_last_bonus_time(user.id)
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_utc and (now_utc < last_bonus_utc + cooldown):
        time_diff = last_bonus_utc + cooldown - now_utc
        # Use divmod for cleaner time calculation
        hours, remainder = divmod(time_diff.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(
            f"⏳ Бонус уже был получен. Попробуйте снова через: <b>{int(hours)} ч {int(minutes)} мин</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    # Attempt to update balance
    new_balance = update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        update_last_bonus_time(user.id, now_utc) # Update timestamp only on successful balance update
        await update.message.reply_text(
            f"🎉 Поздравляем! Вы получили бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Ваш новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
        # Handle potential balance update failure (e.g., DB error, check constraint)
        await update.message.reply_text("❌ Ошибка при начислении бонуса. Пожалуйста, попробуйте позже.")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/leaderboard command from user {user.id} in chat {chat.id} (type: {chat.type})")

    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("🏆 Таблица лидеров пока пуста.")
        return

    leaderboard_text = f"🏆 <b>Таблица Лидеров (Топ {LEADERBOARD_LIMIT})</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]

    # Fetch display names efficiently
    user_ids = [leader['user_id'] for leader in leaders]
    display_name_map = {}
    if user_ids:
        try:
            # Gather display names concurrently
            mention_data = await asyncio.gather(*(get_user_mention(context, uid) for uid in user_ids))
            # Use the display name (second element of the tuple)
            display_name_map = {uid: mention_data[i][1] for i, uid in enumerate(user_ids)}
        except Exception as e:
            logger.error(f"Failed to fetch user display names for leaderboard: {e}")
            # Fallback if fetching fails
            display_name_map = {uid: f"User_{uid}" for uid in user_ids}

    # Build the leaderboard string
    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        # Use fetched or fallback name
        name = display_name_map.get(leader['user_id'], f"User_{leader['user_id']}")
        balance_str = f"{leader['balance']:.2f}" # Format balance
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n" # Use 'F' for chips/currency

    try:
        await update.message.reply_text(
            leaderboard_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True # Avoid potential preview issues with names
        )
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров.")

# --- Blackjack Game (Private Chat Only - Full Code, Checked) ---
# --- Start of Full Blackjack Code ---
async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"BJ /blackjack command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Играть в Блекджек можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    # Determine source (command or callback)
    is_callback = update.callback_query is not None
    source_message = update.callback_query.message if is_callback else update.message
    callback_message_id = source_message.message_id if is_callback else None
    effective_chat_id = chat.id # Use chat.id consistently

    # Answer callback if applicable
    if is_callback:
        try:
            await update.callback_query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query in blackjack_start_command: {e}")

    # --- Game State Cleanup ---
    user_game = context.user_data.get(BJ_GAME_KEY, {})
    previous_message_id = user_game.get('message_id')

    # Delete previous game message ONLY if it exists and is DIFFERENT from the callback source
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(effective_chat_id, previous_message_id)
            logger.debug(f"Deleted previous BJ message {previous_message_id} for user {user.id}")
        except Exception as e:
            # Log failure but continue, might be already deleted or permissions issue
            logger.debug(f"Failed to delete old BJ message {previous_message_id}: {e}")

    # Always clear the game state in user_data when starting fresh
    context.user_data.pop(BJ_GAME_KEY, None)

    # --- User and Balance Check ---
    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)

    if balance is None:
        await source_message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.", parse_mode=ParseMode.HTML)
        return
    if balance <= 0:
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) недостаточен для игры. Попробуйте /bonus.", parse_mode=ParseMode.HTML)
        return

    # --- Bet Selection ---
    bet_options = [1, 5, 10, 25, 50, 100, 250, 500, 1000] # Define available bets
    valid_bets = [b for b in bet_options if b <= balance] # Filter bets user can afford

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) меньше минимальной ставки (<b>{min_bet}</b> F).", parse_mode=ParseMode.HTML)
        return

    # Build keyboard rows (max 4 buttons per row)
    buttons = []
    row = []
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row: # Add the last row if it's not empty
        buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)
    text = f"Ваш баланс: <b>{balance:.2f}</b> F.\nВыберите вашу ставку:"

    # --- Send Bet Prompt ---
    try:
        # Delete the 'New Game' button message if this was triggered by callback
        if callback_message_id:
            try:
                await context.bot.delete_message(effective_chat_id, callback_message_id)
            except Exception as e:
                 logger.warning(f"Failed to delete callback message {callback_message_id} in blackjack_start_command: {e}")

        # Send the new bet prompt
        sent_message = await context.bot.send_message(
            chat_id=effective_chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )
        # Store the new game state including the message ID
        context.user_data[BJ_GAME_KEY] = {'state': 'waiting_bet', 'message_id': sent_message.message_id}
        logger.info(f"BJ bet prompt sent (msg {sent_message.message_id}) for user {user.id}")

    except Exception as e:
        logger.error(f"BJ start error sending bet prompt for user {user.id}: {e}", exc_info=True)
        # Inform user about the error
        try:
            await context.bot.send_message(effective_chat_id, "❌ Произошла ошибка при начале игры.")
        except Exception:
            pass # Avoid error loops if sending fails too


async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})
    bet_prompt_message_id = q.message.message_id # ID of the message with bet buttons

    # --- Input Validation ---
    # Check if the game state is correct and the callback corresponds to the expected message
    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id:
        await q.answer("Эта игра больше неактивна.", show_alert=False)
        # Optionally try to remove buttons from the old message if it still exists
        # try: await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=bet_prompt_message_id, reply_markup=None)
        # except Exception: pass
        return

    balance = get_balance(uid)
    if balance is None:
        await q.answer("Ошибка получения баланса.", show_alert=True)
        return
    if not (0 < bet <= balance): # Ensure bet is positive and affordable
        await q.answer(f"Недопустимая ставка ({bet} F) или недостаточно средств ({balance:.2f} F).", show_alert=True)
        return

    # --- Process Bet ---
    # Deduct bet from balance BEFORE dealing
    if update_balance(uid, -bet) is None:
        await q.answer("Ошибка при списании ставки.", show_alert=True)
        # Don't proceed if balance update fails
        return

    # --- Deal Initial Hands ---
    deck = create_deck()
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0
    try:
        for _ in range(2):
            card_p = draw_card(deck)
            if not card_p: raise IndexError("Deck empty during player deal")
            player_hand.append(card_p)
            cards_dealt_count += 1

            card_d = draw_card(deck)
            if not card_d: raise IndexError("Deck empty during dealer deal")
            dealer_hand.append(card_d)
            cards_dealt_count += 1
    except IndexError as e:
        logger.error(f"BJ dealing error for user {uid}: {e}")
        # Refund bet if dealing fails
        update_balance(uid, bet) # Attempt refund
        try:
            await q.edit_message_text(f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.")
        except Exception: pass # Ignore if editing fails
        context.user_data.pop(BJ_GAME_KEY, None) # Clean game state
        return
    except Exception as e:
        logger.error(f"BJ unexpected dealing error for user {uid}: {e}", exc_info=True)
        update_balance(uid, bet) # Attempt refund
        try:
            await q.edit_message_text(f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.")
        except Exception: pass
        context.user_data.pop(BJ_GAME_KEY, None)
        return


    # --- Check for Initial Blackjacks ---
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    game_state = 'player_turn' # Default state
    hand_status = 'active'
    outcome_text = None
    winnings = 0.0 # Track winnings paid in this step (for BJ case)

    if player_has_blackjack:
        hand_status = 'blackjack'
        game_state = 'game_over' # Game ends immediately
        if dealer_has_blackjack:
            # Push (Blackjack vs Blackjack)
            outcome_text = "⚖️ Ничья! У обоих Блекджек."
            update_balance(uid, bet) # Return original bet
            winnings = bet # Record the returned bet as 'winnings paid' here
        else:
            # Player Blackjack wins
            bj_payout_amount = bet * BLACKJACK_PAYOUT
            update_balance(uid, bet + bj_payout_amount) # Return original bet + payout
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {bj_payout_amount:.2f} F!"
            winnings = bet + bj_payout_amount
    elif dealer_has_blackjack:
        # Dealer Blackjack wins
        game_state = 'game_over'
        outcome_text = "😥 У дилера Блекджек! Вы проиграли."
        winnings = 0.0 # Player loses the bet (already deducted)

    # --- Update Game State in user_data ---
    game.update({
        'state': game_state,
        'deck': deck,
        'cards_dealt': cards_dealt_count,
        'player_hands': [{ # Store hands as a list for splitting
            'hand': player_hand,
            'bet': bet,
            'status': hand_status, # 'active', 'blackjack', 'bust', 'stand'
            'can_double': (game_state == 'player_turn' and len(player_hand) == 2), # Can double only on first turn
            'can_split': False # Will be checked later if applicable
        }],
        'current_hand_index': 0, # Index of the hand being played
        'dealer_hand': dealer_hand,
        'initial_bet': bet, # Keep initial bet for reference
        'split_count': 0, # Track number of splits
        'outcome_text': outcome_text, # Store immediate outcome if any
        'outcome_determined': (game_state == 'game_over'), # Flag if game ended here
        'total_winnings_paid': winnings if game_state == 'game_over' else 0.0 # Track money paid out *so far*
    })

    # --- Update Telegram Message ---
    # Delete the bet prompt message first
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e:
        logger.warning(f"Could not delete bet prompt message {bet_prompt_message_id}: {e}")

    # Show the initial game state (new message)
    new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=False)

    if new_message_info and isinstance(new_message_info, Message):
        # Update message_id in game state if a new message was sent
        game['message_id'] = new_message_info.message_id
        logger.info(f"BJ initial state sent (msg {new_message_info.message_id}) for user {uid}. State: {game_state}")
        # If game ended immediately (BJ), clean up state now
        if game['outcome_determined']:
            context.user_data.pop(BJ_GAME_KEY, None)
            logger.info(f"BJ game state cleaned for user {uid} after initial Blackjack outcome.")
    elif not new_message_info:
        # Handle critical error where game state couldn't be shown
        logger.error(f"Failed to send initial BJ state for user {uid}")
        update_balance(uid, bet) # Attempt to refund bet
        context.user_data.pop(BJ_GAME_KEY, None) # Clean game state
        await context.bot.send_message(chat_id, "❌ Ошибка отображения игры. Ставка возвращена.")
        return # Stop further execution

    # Acknowledge the button press (briefly shows checkmark)
    await q.answer(f"Ставка принята: {bet} F")


async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, edit_existing: bool = True) -> Message | int | None:
    """
    Updates or sends the Blackjack game state message.

    Args:
        context: The bot context.
        chat_id: Chat ID.
        user_id: User ID.
        game_state: The game state dictionary (optional, fetched if None).
        edit_existing: Whether to edit the existing message or send a new one.

    Returns:
        The Message object if a new message was sent, the message_id if edited,
        or None if an error occurred.
    """
    is_new_send = False # Flag to track if we send a new message

    # Fetch game state if not provided
    if game_state is None:
        game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    if not game_state:
        logger.warning(f"blackjack_show_state called for user {user_id} but no game state found.")
        return None

    message_id_to_process = game_state.get('message_id')

    # If we intend to edit, but have no message ID, force sending a new one
    if edit_existing and not message_id_to_process:
        logger.error(f"BJ show_state: Attempted to edit but no message_id for user {user_id}. Will try sending new.")
        edit_existing = False # Switch to sending a new message

    # --- Prepare Message Content ---
    balance = get_balance(user_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"

    dealer_hand = game_state.get('dealer_hand', [])
    player_hands_data = game_state.get('player_hands', [])
    current_hand_idx = game_state.get('current_hand_index', -1)
    state = game_state.get('state', 'unknown')
    dealer_value = get_hand_value(dealer_hand)
    # Check if dealer had BJ initially (relevant for hiding)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2 and game_state.get('cards_dealt', 0) <= 4)

    # Determine if dealer's card should be hidden
    all_player_hands_finished = all(
        isinstance(hdata, dict) and hdata.get('status') in ['bust', 'stand', 'blackjack']
        for hdata in player_hands_data
    )
    hide_dealer_card = (state == 'player_turn' and not dealer_has_blackjack and not all_player_hands_finished)

    # --- Build Text ---
    text = f"<b>Блекджек</b> | Баланс: <b>{balance_str}</b> F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands_data if isinstance(h, dict))
    num_hands = len(player_hands_data)
    text += f"Общая ставка: <b>{total_bet}</b> F{' (Рук: ' + str(num_hands) + ')' if num_hands > 1 else ''}\n"
    text += "--------------------\n"

    # Dealer Hand
    dealer_value_display = "??"
    if dealer_hand:
        if not hide_dealer_card:
            dealer_value_display = str(dealer_value)
        else:
            # Show value of first card only if hiding
            dealer_value_display = f"{get_card_value(dealer_hand[0])}+?"
    text += f"<b>Диллер:</b> {format_hand(dealer_hand, hide_one=hide_dealer_card)} ({dealer_value_display})\n\n"

    # Player Hand(s)
    text += "<b>Вы:</b>\n"
    active_hand_data = None # Store data of the hand whose turn it is
    for i, hand_data in enumerate(player_hands_data):
        if not isinstance(hand_data, dict): continue # Skip invalid entries

        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', '?')
        hand_bet = hand_data.get('bet', 0)
        is_current_turn = (i == current_hand_idx and hand_status == 'active' and state == 'player_turn')

        # Indicator emoji
        indicator = "▶️" if is_current_turn else \
                    "✅" if hand_status == 'stand' else \
                    "❌" if hand_status == 'bust' else \
                    "💰" if hand_status == 'blackjack' else \
                    "▫️" # Default/inactive

        text += f"{indicator} Рука {i+1}: {format_hand(hand)} (<b>{hand_value}</b>) [<i>{hand_bet} F</i>]"

        # Status label
        status_label = ""
        if hand_status == 'bust':
            status_label = " - <b>Перебор!</b>"
        elif hand_status == 'blackjack':
             status_label = " - <b>Блекджек!</b>"
        elif hand_status == 'stand' and not is_current_turn: # Show 'Stand' only after moving past it
             status_label = " - <i>Стоп</i>"

        text += status_label + "\n"

        if is_current_turn:
            active_hand_data = hand_data # Found the hand whose turn it is

    # --- Build Keyboard ---
    keyboard = []
    if active_hand_data and state == 'player_turn':
        player_hand = active_hand_data.get('hand', [])
        player_bet = active_hand_data.get('bet', 0)

        # Check conditions for special actions
        can_double = (
            active_hand_data.get('can_double', False) and
            len(player_hand) == 2 and
            balance is not None and balance >= player_bet
        )
        can_split = (
            len(player_hand) == 2 and
            player_hand[0] and player_hand[1] and # Ensure cards exist
            get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
            balance is not None and balance >= player_bet and
            game_state.get('split_count', 0) < MAX_SPLITS
        )
        # Update game state with current split possibility (might change dynamically)
        active_hand_data['can_split'] = can_split

        # Basic actions
        action_buttons = [
            InlineKeyboardButton("Еще", callback_data=f"bj_hit_{current_hand_idx}"),
            InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{current_hand_idx}")
        ]
        keyboard.append(action_buttons)

        # Special actions (if available)
        special_buttons = []
        if can_double:
            special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{current_hand_idx}"))
        if can_split:
             special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{current_hand_idx}"))
        if special_buttons:
            keyboard.append(special_buttons)

    elif state == 'game_over':
        # Add outcome text if game is over
        text += f"\n<b>Игра окончена!</b>\n{game_state.get('outcome_text', 'Результат не определен.')}\n"
        final_balance = get_balance(user_id) # Show final balance
        text += f"\nИтоговый баланс: <b>{final_balance:.2f}</b> F." if final_balance is not None else ""
        # 'New Game' button
        keyboard.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
    elif state == 'dealer_turn':
        text += "\n<i>⏳ Ход дилера...</i>"
        # No buttons during dealer's turn

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Send/Edit Message with Retries ---
    result: Message | int | None = None # Store message or ID
    max_retries = 1 # Retry once on specific errors
    current_retry = 0

    while current_retry <= max_retries:
        try:
            if edit_existing and message_id_to_process:
                # --- Try Editing ---
                logger.debug(f"Attempting edit (try {current_retry+1}) BJ state msg {message_id_to_process} for user {user_id}")
                # Use await directly on edit_message_text which returns True on success or raises error
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id_to_process,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                logger.debug(f"Successfully edited BJ state msg {message_id_to_process}")
                result = message_id_to_process # Return ID on successful edit
                break # Exit retry loop

            else:
                # --- Try Sending New ---
                is_new_send = True
                logger.debug(f"Sending NEW BJ state message for user {user_id}")
                # Delete old message if we were trying to edit but failed and are now sending
                if message_id_to_process:
                    try:
                        await context.bot.delete_message(chat_id, message_id_to_process)
                    except Exception: pass # Ignore delete error

                new_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                # IMPORTANT: Update game state with the new message ID
                # Make sure game_state still exists before updating
                current_game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
                if current_game_state:
                    current_game_state['message_id'] = new_message.message_id
                    logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}. Updated game state.")
                else:
                    logger.warning(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}, but game state disappeared before update.")

                result = new_message # Return the new Message object
                break # Exit retry loop

        except BadRequest as e:
            error_str = str(e).lower()
            if "message is not modified" in error_str:
                result = message_id_to_process # Not an error, consider it success
                logger.debug(f"BJ state msg {message_id_to_process} not modified.")
                break
            elif "message to edit not found" in error_str or \
                 "chat not found" in error_str or \
                 "message can't be edited" in error_str:
                 # Can't edit, force sending a new message on next iteration
                 logger.error(f"Message {message_id_to_process} or Chat {chat_id} not found/editable for user {user_id}. Forcing send new.")
                 edit_existing = False
                 message_id_to_process = None # Clear the invalid ID
                 # Also clear from game state if it exists
                 current_game_state_on_edit_fail = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
                 if current_game_state_on_edit_fail:
                     current_game_state_on_edit_fail['message_id'] = None
                 current_retry += 1
                 if current_retry > max_retries: # If retries exhausted after trying to send new
                     logger.error(f"CRITICAL: Failed to send new message after edit failed for user {user_id}. Cleaning game state.")
                     context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up
                     result = None # Indicate failure
                     break # Exit loop after final failure
                 # Continue loop to try sending new
            elif "can't parse entities" in error_str:
                # HTML error, cannot recover easily
                logger.error(f"HTML Parsing Error for user {user_id} (msg {message_id_to_process}): {e}\nText snippet: {text[:200]}...")
                result = None
                break
            else:
                # Other BadRequest, potentially temporary, retry once
                logger.warning(f"Edit/Send BJ state failed for user {user_id} (msg {message_id_to_process}) (try {current_retry+1}): {e}")
                result = None # Assume failure for now
                current_retry += 1
                await asyncio.sleep(0.5) # Short delay before retry

        except Forbidden as e:
            # Bot blocked or kicked, cannot recover
            logger.error(f"Forbidden error for user {user_id} in chat {chat_id} (likely blocked): {e}")
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up game state
            result = None
            break

        except Exception as e:
            # Unexpected errors, retry once
            logger.error(f"Unexpected error in blackjack_show_state for user {user_id} (try {current_retry+1}): {e}", exc_info=True)
            result = None
            current_retry += 1
            await asyncio.sleep(0.5)

    # Log final failure state
    if result is None and not is_new_send:
        logger.error(f"Failed to update BJ state for user {user_id} after retries.")
    elif result is None and is_new_send:
         logger.error(f"Failed to send initial BJ state for user {user_id} after edit failure.")


    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})

    # --- Basic Validation ---
    if len(parts) < 2: # Expecting ['action', 'hand_index']
        logger.warning(f"Invalid action parts received: {parts} for user {uid}")
        await q.answer("Ошибка: Неверный формат действия.", show_alert=True)
        return

    action, hand_index_str = parts[0], parts[1]
    try:
        hand_index = int(hand_index_str)
    except (ValueError, TypeError):
        logger.warning(f"Invalid hand index received: {hand_index_str} for user {uid}")
        await q.answer("Ошибка: Неверный индекс руки.", show_alert=True)
        return

    action_message_id = q.message.message_id # Message where the button was pressed

    # --- Game State Validation ---
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != action_message_id:
        await q.answer("Эта игра или действие больше неактивны.", show_alert=False)
        # Try to remove buttons from the stale message if it's the one interacted with
        # Check if game state still exists before trying to edit
        current_game_state = context.user_data.get(BJ_GAME_KEY, {})
        if current_game_state.get('message_id') == action_message_id:
             try:
                 await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=action_message_id, reply_markup=None)
             except Exception: pass # Ignore if removal fails
        return

    player_hands = game.get('player_hands', [])
    # Check if the hand index is valid and if it's the current hand's turn
    if not (0 <= hand_index < len(player_hands)) or hand_index != game.get('current_hand_index', -1):
        await q.answer("Сейчас ход другой руки.", show_alert=False)
        return

    current_hand_data = player_hands[hand_index]
    # Check if the hand is actually active
    if not isinstance(current_hand_data, dict) or current_hand_data.get('status') != 'active':
        await q.answer("Эта рука неактивна для действий.", show_alert=False)
        return

    # --- Prepare Action Variables ---
    hand = current_hand_data.get('hand', [])
    deck = game.get('deck', [])
    balance = get_balance(uid) # Get current balance for checks
    bet = current_hand_data.get('bet', 0)
    needs_state_update = False # Flag to update message at the end
    move_to_next = False # Flag to trigger moving to next hand/dealer

    # --- Execute Action ---
    try:
        if action == 'hit':
            card = draw_card(deck)
            if card:
                hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                # Cannot double or split after hitting
                current_hand_data['can_double'] = False
                current_hand_data['can_split'] = False
                hand_value = get_hand_value(hand)
                await q.answer(f"Взяли: {card[0]}{card[1]}") # Show drawn card
                needs_state_update = True
                if hand_value > 21:
                    current_hand_data['status'] = 'bust'
                    move_to_next = True
                elif hand_value == 21: # Stand automatically on 21
                    current_hand_data['status'] = 'stand'
                    move_to_next = True
            else:
                # Deck empty or error drawing
                raise IndexError("Draw fail (deck empty or error)")

        elif action == 'stand':
            current_hand_data['status'] = 'stand'
            await q.answer("Стоп.")
            needs_state_update = True
            move_to_next = True

        elif action == 'double':
            # Re-check conditions just before execution
            can_double = (
                current_hand_data.get('can_double', False) and
                len(hand) == 2 and
                balance is not None and balance >= bet
            )
            if can_double:
                # Deduct the additional bet
                new_balance = update_balance(uid, -bet)
                if new_balance is not None:
                    current_hand_data['bet'] += bet
                    current_hand_data['can_double'] = False # Can't double again
                    current_hand_data['can_split'] = False # Can't split after doubling
                    balance = new_balance # Update local balance variable

                    # Draw exactly one card
                    card = draw_card(deck)
                    drawn_card_str = ""
                    if card:
                        hand.append(card)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                        hand_value = get_hand_value(hand)
                        # Hand automatically stands or busts after double
                        current_hand_data['status'] = 'bust' if hand_value > 21 else 'stand'
                        drawn_card_str = f" Карта: {card[0]}{card[1]}. Итог: {hand_value}{' (Перебор!)' if hand_value > 21 else ''}"
                    else:
                        # Error drawing card, hand stands with original cards
                        current_hand_data['status'] = 'stand'
                        drawn_card_str = " Ошибка взятия карты."
                        logger.warning(f"BJ double failed draw for user {uid}")

                    await q.answer(f"Удвоено!{drawn_card_str}", show_alert=("Ошибка" in drawn_card_str))
                    needs_state_update = True
                    move_to_next = True
                else:
                    await q.answer("Ошибка списания средств для удвоения.", show_alert=True)
                    # Don't proceed if balance update failed
            else:
                await q.answer("Удвоить сейчас нельзя.", show_alert=True)

        elif action == 'split':
             # Re-check conditions just before execution
            can_split = (
                 len(hand) == 2 and
                 hand[0] and hand[1] and get_card_value(hand[0]) == get_card_value(hand[1]) and
                 balance is not None and balance >= bet and
                 game.get('split_count', 0) < MAX_SPLITS
             )
            # Update the flag in case it changed (though unlikely between show_state and action)
            current_hand_data['can_split'] = can_split

            if can_split:
                # Deduct bet for the new hand
                new_balance = update_balance(uid, -bet)
                if new_balance is not None:
                    game['split_count'] = game.get('split_count', 0) + 1
                    balance = new_balance # Update local balance

                    # Create the new hand
                    card_to_move = hand.pop()
                    new_hand_data = {
                        'hand': [card_to_move],
                        'bet': bet,
                        'status': 'active',
                        'can_double': False, # Will be updated after draw
                        'can_split': False  # Will be updated after draw
                    }

                    # Draw one card for each new hand
                    cards_drawn = [draw_card(deck), draw_card(deck)]
                    drawn_count = 0

                    # Add cards to hands, handling potential draw failures
                    if cards_drawn[0]:
                        hand.append(cards_drawn[0])
                        drawn_count += 1
                    if cards_drawn[1]:
                        new_hand_data['hand'].append(cards_drawn[1])
                        drawn_count += 1

                    # Check if drawing failed
                    if drawn_count < 2:
                        logger.warning(f"BJ split failed to draw both cards for user {uid}. Deck empty?")
                        # Revert the split attempt
                        current_hand_data['status'] = 'stand' # Force stand on original hand
                        update_balance(uid, bet) # Refund the bet for the failed split
                        game['split_count'] -= 1
                        hand.append(card_to_move) # Put card back
                        await q.answer("Ошибка разделения: не хватило карт! Ставка возвращена.", show_alert=True)
                        needs_state_update = True
                        move_to_next = True # Move on from this hand
                        # Re-allow double if applicable (unlikely but possible)
                        current_hand_data['can_double'] = (len(hand) == 2)
                    else:
                         # Split successful, insert new hand and update game state
                        player_hands.insert(hand_index + 1, new_hand_data)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + drawn_count

                        # --- Handle Split Rules ---
                        is_ace_split = get_card_value(hand[0] if hand else None) == 11

                        if is_ace_split:
                            # Aces get only one card each and stand automatically
                            current_hand_data['status'] = 'stand'
                            new_hand_data['status'] = 'stand'
                            current_hand_data['can_double'] = False # No double/hit on split Aces
                            new_hand_data['can_double'] = False
                            await q.answer("Тузы разделены и стоят.")
                            needs_state_update = True
                            # `move_to_next` will be triggered by the job for Ace splits
                        else:
                            # Non-Ace split: Check for immediate 21s, set flags
                            if get_hand_value(hand) == 21:
                                current_hand_data['status'] = 'stand'
                            if get_hand_value(new_hand_data['hand']) == 21:
                                new_hand_data['status'] = 'stand'

                            # Allow double down on new hands if applicable (and status is active)
                            current_hand_data['can_double'] = (len(hand) == 2 and current_hand_data['status'] == 'active')
                            new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2 and new_hand_data['status'] == 'active')

                            # Check if re-splitting is possible on the NEW hands
                            limit_ok = game.get('split_count', 0) < MAX_SPLITS
                            current_balance_after_split = balance # Use updated balance
                            chd_can_resplit = (
                                current_hand_data['status'] == 'active' and len(hand) == 2 and hand[0] and hand[1] and
                                get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok and
                                current_balance_after_split >= current_hand_data['bet']
                            )
                            nhd_can_resplit = (
                                new_hand_data['status'] == 'active' and len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and
                                get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and
                                current_balance_after_split >= new_hand_data['bet']
                            )
                            current_hand_data['can_split'] = chd_can_resplit
                            new_hand_data['can_split'] = nhd_can_resplit

                            await q.answer("Рука разделена!")
                            needs_state_update = True
                            # Don't set move_to_next here unless the *first* hand stood on 21
                            if current_hand_data['status'] == 'stand':
                                move_to_next = True

                else:
                     # Failed to deduct balance for split
                    await q.answer("Ошибка списания средств для разделения.", show_alert=True)
            else:
                 # Conditions for split not met
                await q.answer("Разделить сейчас нельзя.", show_alert=True)

    except IndexError as e:
        # Catch draw failures specifically
        logger.warning(f"BJ action '{action}' user {uid} failed draw: {e}")
        current_hand_data['status'] = 'stand' # Force stand if draw fails
        await q.answer("Не удалось взять карту! Ход завершен.", show_alert=True)
        needs_state_update = True
        move_to_next = True
    except Exception as e:
        # Catch unexpected errors during action logic
        logger.error(f"BJ action '{action}' user {uid} unexpected error: {e}", exc_info=True)
        await q.answer("Произошла непредвиденная ошибка.", show_alert=True)
        current_hand_data['status'] = 'stand' # Force stand on error
        needs_state_update = True
        move_to_next = True

    # --- Update Display and Move Turn ---
    if needs_state_update:
        # Update the message with the new game state
        update_result = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        if not update_result:
             # Log if the message couldn't be updated, game might be stuck visually
             logger.error(f"Failed to update state message after action '{action}' for user {uid}. Game might be stuck.")
             try:
                 # Try to inform the user about the display issue
                 await context.bot.send_message(chat_id, "⚠️ Ошибка обновления отображения игры. Состояние может быть некорректным.")
             except Exception: pass

    # Special handling for Ace split: always moves to the next hand/dealer immediately after the split action completes
    is_ace_split_action = (action == 'split' and len(hand) == 2 and hand[0] and get_card_value(hand[0]) == 11) # Check card exists

    # If the hand is finished (bust, stand, double, failed draw) or it was an Ace split, schedule the next action
    if move_to_next or is_ace_split_action:
        # Use job queue to avoid potential race conditions and allow message update to finish
        context.job_queue.run_once(
            blackjack_next_action_job,
            when=0.1, # Small delay
            data={'chat_id': chat_id, 'user_id': uid},
            name=f"next_action_{uid}_{action_message_id}" # Unique job name
        )


async def blackjack_next_action_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue callback to trigger moving to the next hand or dealer."""
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    if user_id and chat_id:
        await blackjack_next_action(context, chat_id, user_id)
    else:
        logger.error(f"Missing data in blackjack_next_action_job: {job_data}")


async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Moves to the next player hand or starts the dealer's turn."""
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    if not game:
        logger.info(f"BJ next_action (job) for user {user_id}: Game state not found. Aborting.")
        return
    if game.get('state') != 'player_turn':
         # Avoid acting if state changed (e.g., game ended elsewhere)
        logger.info(f"BJ next_action (job) for user {user_id}: Game state is not 'player_turn' ({game.get('state')}). Aborting.")
        return

    player_hands = game.get('player_hands', [])
    current_hand_idx = game.get('current_hand_index', -1)
    next_active_idx = -1

    # Find the index of the next hand with status 'active'
    for i in range(current_hand_idx + 1, len(player_hands)):
        hand_data = player_hands[i]
        if isinstance(hand_data, dict) and hand_data.get('status') == 'active':
            next_active_idx = i
            break

    message_id = game.get('message_id') # Get message ID for updates/jobs
    if not message_id:
        # Critical error: cannot proceed without message ID
        logger.error(f"BJ next_action: No message_id for user {user_id}. Cannot proceed.")
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up
        return

    if next_active_idx != -1:
        # --- Move to Next Player Hand ---
        game['current_hand_index'] = next_active_idx
        logger.info(f"BJ user {user_id}: Moving to next active hand index {next_active_idx}")
        # Update the display to show the new active hand and buttons
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
    else:
        # --- All Player Hands Finished - Move to Dealer ---
        logger.info(f"BJ user {user_id}: All player hands done, moving to dealer's turn.")
        game['state'] = 'dealer_turn'

        # Update the display to show "Dealer's turn..." (and reveal dealer card if needed)
        update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

        if not update_success:
            # If we can't even update the state to show dealer's turn, something is wrong
            logger.error(f"BJ user {user_id}: Failed to update state to 'dealer_turn'. Aborting dealer job.")
            try: await context.bot.send_message(chat_id, "⚠️ Ошибка! Не удалось перейти к ходу дилера.")
            except Exception: pass
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up
            return

        # Schedule the dealer's turn logic via the job queue
        context.job_queue.run_once(
            blackjack_dealer_turn_job,
            DEALER_TURN_DELAY, # Wait before dealer starts acting
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': message_id},
            name=f"dealer_turn_{user_id}_{message_id}" # Unique job name
        )


async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue callback to execute the dealer's turn."""
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id') # Message ID at the time the job was scheduled

    if not user_id or not chat_id or not message_id:
        logger.error(f"BJ Dealer job missing required data: {job_data}")
        return

    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    # --- Pre-computation Checks ---
    if not game:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state not found. Job aborted.")
        return
    if game.get('state') != 'dealer_turn':
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state is not 'dealer_turn' ({game.get('state')}). Job aborted.")
        return
    # Check if the message ID hasn't changed (e.g., due to error/resend)
    if game.get('message_id') != message_id:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Message ID mismatch (game has {game.get('message_id')}). Job aborted.")
        return

    # --- Dealer Logic ---
    deck = game.get('deck', [])
    dealer_hand = game.get('dealer_hand', [])
    player_hands = game.get('player_hands', [])

    # Get initial dealer value (before hitting) to check for original BJ
    dealer_value_initial = get_hand_value(dealer_hand)
    dealer_had_initial_blackjack = (dealer_value_initial == 21 and len(dealer_hand) == 2 and game.get('cards_dealt', 0) <= 4)

    # Determine if the dealer needs to hit at all
    # Dealer only hits if at least one player hand is not bust or blackjack
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack']
        for h in player_hands
    )
    # Dealer also doesn't hit if they already have 21 or more
    dealer_needs_to_hit = player_can_win and get_hand_value(dealer_hand) < 21

    dealer_stood = False
    hit_occurred = False # Track if dealer actually drew cards

    if dealer_needs_to_hit:
        logger.info(f"BJ Dealer user {user_id} starts hitting sequence.")
        # --- Dealer Hit Loop ---
        while not dealer_stood:
            current_dealer_value = get_hand_value(dealer_hand)
            num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
            is_soft = num_aces > 0 and (current_dealer_value - (num_aces * 10)) <= 11 # Check if hand value relies on Ace as 11

            # --- Dealer Stand Rules ---
            stand_value_met = False
            if current_dealer_value > 17:
                stand_value_met = True
            elif current_dealer_value == 17:
                # Stand on hard 17, or soft 17 if rule dictates
                if not (is_soft and DEALER_HITS_SOFT_17):
                    stand_value_met = True

            # Stand if value met or >= 21
            if stand_value_met or current_dealer_value >= 21:
                if not hit_occurred: # Log initial stand value if no hits were made
                     logger.info(f"BJ Dealer user {user_id} stands initially on {current_dealer_value}{' (soft)' if is_soft and current_dealer_value==17 else ''}.")
                else: # Log final stand value after hitting
                     logger.info(f"BJ Dealer user {user_id} stands on {current_dealer_value}.")
                dealer_stood = True
                break # Exit hit loop

            # --- Dealer Hit ---
            logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}{' (soft)' if is_soft else ''}.")
            card = draw_card(deck)
            if card:
                dealer_hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                hit_occurred = True
                # Optional: Add a small delay between dealer hits for visual effect
                await asyncio.sleep(DEALER_TURN_DELAY * 0.6)
                # Update display *during* hits (optional, can be intensive)
                # await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
            else:
                # Deck empty, dealer must stand
                logger.warning(f"BJ Dealer user {user_id} failed to draw card (deck empty?). Standing.")
                dealer_stood = True
                break # Exit hit loop
        # --- End Dealer Hit Loop ---
    else:
        # Dealer doesn't need to hit (all players bust/BJ, or dealer starts >= 21)
        final_dealer_value_no_hit = get_hand_value(dealer_hand)
        logger.info(f"BJ Dealer user {user_id}: No player can win or dealer already has >= 21 ({final_dealer_value_no_hit}). Dealer stands immediately.")
        dealer_stood = True # Mark as stood for logic flow

    # --- Finalize Dealer Turn ---
    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn with value {final_dealer_value}. Updating display before outcome.")

    # Show the final dealer hand *before* calculating outcome
    update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    if not update_success:
        # Critical: Failed to update display before outcome
        logger.error(f"BJ Dealer user {user_id}: Failed to show final dealer hand. Aborting outcome calculation.")
        try: await context.bot.send_message(chat_id, "⚠️ Ошибка отображения хода дилера. Игра завершена некорректно.")
        except Exception: pass
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up
        return

    # Wait a moment after showing final hand before showing results
    await asyncio.sleep(DEALER_TURN_DELAY * 0.8)

    # Proceed to determine and display the outcome
    await blackjack_determine_outcome(context, chat_id, user_id, dealer_had_initial_blackjack)


async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    """Calculates winnings, updates balance, and displays the final outcome."""
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    if not game:
        logger.warning(f"BJ outcome user {user_id}: Game data not found.")
        return

    message_id = game.get('message_id') # Get message ID for final update
    if not message_id:
        logger.error(f"BJ outcome user {user_id}: No message_id found.")
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up if possible
        return

    # Prevent double execution if somehow called twice
    if game.get('outcome_determined'):
        logger.info(f"BJ outcome user {user_id}: Outcome already determined. Skipping redundant calculation.")
        # Just ensure the final state is shown correctly
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
        return


    player_hands = game.get('player_hands', [])
    dealer_hand = game.get('dealer_hand', [])
    dealer_final_value = get_hand_value(dealer_hand)
    dealer_busted = dealer_final_value > 21

    outcome_lines = []
    total_winnings_to_pay = 0 # Total amount to credit back (original bet + winnings)
    total_initial_bet_sum = 0 # Sum of all initial bets placed in the round

    # --- Calculate Outcome for Each Player Hand ---
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue # Skip invalid data

        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status') # 'bust', 'stand', 'blackjack'
        player_value = get_hand_value(hand)
        player_had_blackjack = (status == 'blackjack') # Was it an initial BJ?

        total_initial_bet_sum += bet # Track total amount wagered
        payout_amount = 0 # Amount to pay back for THIS hand (bet + winnings)
        outcome_str = ""
        prefix = f"Рука {i+1}: " if len(player_hands) > 1 else "" # Add prefix for multiple hands

        # --- Determine Win/Loss/Push ---
        if status == 'bust':
            outcome_str = f"{prefix}Перебор ({player_value}). Ставка проиграна (-{bet} F)."
            payout_amount = 0 # Player loses bet
        elif player_had_blackjack:
            if d_had_bj: # Push (Player BJ vs Dealer BJ)
                outcome_str = f"{prefix}Блекджек! Ничья с дилером."
                payout_amount = bet # Return original bet
            else: # Player BJ wins
                win_amount = bet * BLACKJACK_PAYOUT
                outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."
                payout_amount = bet + win_amount # Return bet + payout
        elif d_had_bj: # Dealer had BJ, player didn't
            outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{bet} F)."
            payout_amount = 0
        elif dealer_busted:
            outcome_str = f"{prefix}У дилера перебор ({dealer_final_value})! Выигрыш +{bet:.2f} F."
            payout_amount = bet * 2 # Return bet + win (1:1 payout)
        elif status == 'stand': # Compare hands only if player stood
            if player_value > dealer_final_value:
                outcome_str = f"{prefix}Вы выиграли ({player_value} {html_escape('>')}) {dealer_final_value}). Выигрыш +{bet:.2f} F."
                payout_amount = bet * 2 # Return bet + win (1:1 payout)
            elif player_value == dealer_final_value:
                outcome_str = f"{prefix}Ничья ({player_value} = {dealer_final_value}). Ставка возвращена."
                payout_amount = bet # Return original bet
            else: # player_value < dealer_final_value
                outcome_str = f"{prefix}Вы проиграли ({player_value} {html_escape('<')} {dealer_final_value}). Ставка проиграна (-{bet} F)."
                payout_amount = 0
        # else: Should not happen if status is stand/bust/blackjack

        outcome_lines.append(outcome_str)
        total_winnings_to_pay += payout_amount

    # --- Update Balance ---
    net_change = total_winnings_to_pay - total_initial_bet_sum
    balance_updated_ok = True

    if total_winnings_to_pay > 0:
        # Get balance *before* update for logging purposes in case of failure
        current_balance_before_update = get_balance(user_id)
        if update_balance(user_id, total_winnings_to_pay) is None:
             # Balance update failed! Critical error.
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! ❌</b>")
            net_change = -total_initial_bet_sum # Net change becomes total loss
            balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED to update balance with payout {total_winnings_to_pay}. Initial bet sum was {total_initial_bet_sum}. Balance before attempt: {current_balance_before_update}")
        else:
            # Balance updated successfully
            logger.info(f"BJ outcome user {user_id}: Balance updated by adding {total_winnings_to_pay:.2f}. Net change for round: {net_change:+.2f}")
    else:
        # No winnings to pay out, only losses (already deducted)
         logger.info(f"BJ outcome user {user_id}: No winnings to pay. Net change: {net_change:+.2f}")


    # --- Finalize Game State ---
    game['state'] = 'game_over'
    # Format net change with sign (+/-)
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True # Mark outcome as calculated

    # --- Show Final Result ---
    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    # --- Clean Up ---
    # Clean game state only if balance update was successful
    if balance_updated_ok:
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        logger.info(f"BJ game state cleaned for user {user_id}")
    else:
        # Keep game state for potential debugging if balance failed
         logger.warning(f"BJ game state NOT cleaned for user {user_id} due to balance update error. Game state kept for potential review.")

# --- End of Full Blackjack Code ---


# --- Roulette Game (Full Code, Checked & Updated) ---
# --- Start of Full Roulette Code ---
def rl_get_main_menu_keyboard(chat_data: dict, display_name_map: dict) -> InlineKeyboardMarkup:
    """Generates the main keyboard for the roulette game state."""
    game_state = chat_data.get(RL_GAME_KEY, {})
    state = game_state.get('state', 'idle')
    active_bets_by_user = game_state.get('active_bets', {})
    keyboard = []

    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    total_bet_amount = sum(b['amount'] for bets in active_bets_by_user.values() for b in bets)

    # Display active bets if any
    if active_bets_by_user:
         keyboard.append([InlineKeyboardButton("📝 Текущие ставки:", callback_data='rl_noop')]) # Non-clickable header
         for user_id, bets in active_bets_by_user.items():
             user_display_name = display_name_map.get(user_id, f"User_{user_id}") # Get display name
             bet_str = ", ".join([f"{b['value_display']} ({b['amount']}F)" for b in bets])
             # Truncate long bet strings
             button_text = f"{user_display_name}: {bet_str}"
             if len(button_text) > 60: # Max button text length approx 64 bytes
                 button_text = button_text[:57] + "..."
             keyboard.append([InlineKeyboardButton(button_text, callback_data='rl_noop')]) # Non-clickable bet display
         # Summary row
         keyboard.append([InlineKeyboardButton(
             f"💰 Общая сумма: {total_bet_amount} F ({total_bets_count}/{RL_MAX_BETS_PER_ROUND} ставок)",
             callback_data='rl_noop'
         )])
         keyboard.append([InlineKeyboardButton("---", callback_data='rl_noop')]) # Separator


    # Add action buttons based on state
    if state == 'accepting_bets':
        # 'Add Bet' button if limits allow
        if total_bets_count < RL_MAX_BETS_PER_ROUND:
            keyboard.append([InlineKeyboardButton("➕ Добавить ставку", callback_data='rl_start_bet')])
        else:
            # Indicate round bet limit reached
             keyboard.append([InlineKeyboardButton("🚫 Лимит ставок раунда достигнут", callback_data='rl_noop')])

        # 'Spin' button - Allow ONLY if bets exist AND only one player is betting (simplification)
        # You could relax this condition if needed
        if active_bets_by_user and len(active_bets_by_user) == 1:
             keyboard.append([InlineKeyboardButton("🎰 Крутить!", callback_data='rl_spin')])

    elif state == 'idle' or state == 'finished':
         # Button now triggers a specific callback instead of noop
         keyboard.append([InlineKeyboardButton("▶️ Начать новый раунд", callback_data='rl_new_round')]) # <<< MODIFIED

    elif state == 'spinning':
        # Indicate wheel is spinning
        keyboard.append([InlineKeyboardButton("⏳ Колесо вращается...", callback_data='rl_noop')])

    # Always add help button
    keyboard.append([InlineKeyboardButton("❓ Правила Рулетки", callback_data='rl_show_help')])

    return InlineKeyboardMarkup(keyboard)

def rl_get_bet_type_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting the type of bet."""
    keyboard = [
        [InlineKeyboardButton("🔢 Число", callback_data='rl_type_number'), InlineKeyboardButton("🎨 Цвет", callback_data='rl_type_color')],
        [InlineKeyboardButton("⚖️ Чет/Нечет", callback_data='rl_type_parity'), InlineKeyboardButton("📦 Дюжина", callback_data='rl_type_dozen')],
        [InlineKeyboardButton("📊 Колонка", callback_data='rl_type_column')],
        [InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')] # Cancel betting process
    ]
    return InlineKeyboardMarkup(keyboard)

def rl_get_bet_value_keyboard(bet_type: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting the specific value of a bet (e.g., Red/Black, Dozen 1/2/3)."""
    kb_rows = []
    if bet_type == 'color':
        kb_rows = [[
            InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Red'], callback_data='rl_value_Red'),
            InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Black'], callback_data='rl_value_Black')
        ]]
    elif bet_type == 'parity':
        kb_rows = [[
            InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Even'], callback_data='rl_value_Even'),
            InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Odd'], callback_data='rl_value_Odd')
        ]]
    elif bet_type == 'dozen':
        kb_rows = [
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['1st'], callback_data='rl_value_1st')],
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['2nd'], callback_data='rl_value_2nd')],
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['3rd'], callback_data='rl_value_3rd')]
        ]
    elif bet_type == 'column':
        kb_rows = [
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col1'], callback_data='rl_value_col1')],
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col2'], callback_data='rl_value_col2')],
            [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col3'], callback_data='rl_value_col3')]
        ]
    # Add navigation buttons
    kb_rows.append([InlineKeyboardButton("⬅️ Назад (к типу)", callback_data='rl_back_to_bet_type')])
    kb_rows.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(kb_rows)

def rl_get_bet_amount_keyboard(balance: float, bet_type: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting the bet amount."""
    keyboard = []
    row = []
    # Filter amounts user can afford
    valid_amounts = [a for a in RL_BET_AMOUNTS if a <= balance]

    for amount in valid_amounts:
        row.append(InlineKeyboardButton(str(amount), callback_data=f'rl_amount_{amount}'))
        if len(row) == 3: # Max 3 amount buttons per row
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Determine correct "Back" button destination
    if bet_type == 'number': # Came from text input, go back to type selection
         back_cb = 'rl_back_to_bet_type'
         back_txt = "⬅️ Назад (к типу)"
    else: # Came from value selection
        back_cb = 'rl_back_to_bet_value'
        back_txt = "⬅️ Назад (к значению)"

    keyboard.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(keyboard)

def rl_get_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for confirming the bet placement."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, поставить!", callback_data='rl_confirm_bet_yes'),
            InlineKeyboardButton("✏️ Нет, изменить", callback_data='rl_back_to_bet_type') # Go back to start of bet process
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Roulette Async/Job Functions ---

async def rl_spin_roulette_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job queue callback function that triggers the roulette spin after the timer."""
    job_context = context.job.data
    chat_id = job_context.get('chat_id')

    if not chat_id:
        logger.error(f"Roulette timer job missing chat_id: {job_context}")
        return

    logger.info(f"Roulette betting timer expired for chat_id={chat_id}")

    # Use application.chat_data for shared chat state
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # Verify game state before proceeding
    if not game_state or game_state.get('state') != 'accepting_bets':
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but game not in 'accepting_bets' state ({game_state.get('state') if game_state else 'No Game'}). Aborting.")
        # Ensure display timer is also potentially cleaned up if state is wrong
        display_timer_name = game_state.get('timer_display_job_name') if game_state else None
        if display_timer_name:
             await rl_remove_job_if_exists(display_timer_name, context)
             if game_state: game_state.pop('timer_display_job_name', None) # Check game_state again
        if game_state: game_state.pop('timer_job_name', None) # Clean up main timer name if state exists
        return

    active_bets_by_user = game_state.get('active_bets', {})

    # If timer expires and no bets were placed, end the round
    if not active_bets_by_user:
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but no bets placed. Ending round.")
        game_state['state'] = 'idle' # Reset state
        # Remove both timer names and jobs
        spin_timer_name = game_state.pop('timer_job_name', None) # Remove main timer name
        display_timer_name = game_state.pop('timer_display_job_name', None) # Remove display timer name
        if spin_timer_name: await rl_remove_job_if_exists(spin_timer_name, context)
        if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)

        message_id = game_state.get('message_id')
        if message_id:
            try:
                # Generate final keyboard before editing message
                final_reply_markup = rl_get_main_menu_keyboard(chat_data, {})
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⏳ Время для ставок истекло. Ставок не было.\nНачните новый раунд.",
                    reply_markup=final_reply_markup
                )
            except Exception as e:
                 logger.warning(f"Could not edit message in chat {chat_id} after timer expired with no bets: {e}")
        return # Stop execution

    # Bets exist, proceed to spin
    logger.info(f"Roulette timer starting spin for chat {chat_id}")
    # Timer removal is now handled inside rl_spin_roulette_logic
    await rl_spin_roulette_logic(context, chat_id)


async def rl_update_timer_display_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repeating job to update the timer display in the roulette message."""
    job_data = context.job.data
    chat_id = job_data.get('chat_id')
    main_timer_job_name = job_data.get('main_timer_job_name')

    if not chat_id or not main_timer_job_name:
        logger.error(f"Roulette display timer job missing data: {job_data}")
        if context.job: context.job.schedule_removal() # Remove self if broken
        return

    # Use application.chat_data
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)
    this_job_name = context.job.name if context.job else None

    # --- Check conditions to continue running ---
    # 1. Game must exist and be in 'accepting_bets' state
    if not game_state or game_state.get('state') != 'accepting_bets':
        logger.debug(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Game state is not 'accepting_bets' ({game_state.get('state') if game_state else 'No Game'}).")
        if context.job: context.job.schedule_removal() # Remove self
        # Ensure the name is cleared from game_state if it matches this job
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
             game_state.pop('timer_display_job_name', None)
        return

    # 2. The main timer job must still exist
    main_timer_jobs = context.job_queue.get_jobs_by_name(main_timer_job_name)
    if not main_timer_jobs:
        logger.debug(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Main timer job '{main_timer_job_name}' not found.")
        if context.job: context.job.schedule_removal() # Remove self
        # Also ensure the display timer name is cleared from game_state if it matches this job
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
            game_state.pop('timer_display_job_name', None)
        # Attempt one final update without the timer text before stopping
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # --- Update the message ---
    # We simply call rl_show_game_state, which now knows how to fetch
    # the remaining time from the main_timer_job_name stored in game_state.
    update_result = await rl_show_game_state(context, chat_id, edit_existing=True)

    # If updating the message fails (e.g., deleted message, permissions), stop this job
    if update_result is None:
        logger.warning(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Failed to update game state message.")
        if context.job: context.job.schedule_removal() # Remove self
         # Also ensure the display timer name is cleared from game_state
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
            game_state.pop('timer_display_job_name', None)


async def rl_remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Removes job queue jobs by name if they exist."""
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    removed = False
    for job in current_jobs:
        job.schedule_removal()
        removed = True
    if removed:
        logger.info(f"Removed Job Queue job: {name}")
    return removed

# --- Roulette Command and Callback Handlers ---

async def roulette_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /roulette command to start or show the game."""
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    logger.info(f"/roulette command from user {user.id} in chat {chat_id} (type: {chat.type})")

    get_or_create_user(user.id) # Ensure user profile exists

    # Use application.chat_data for shared game state in this chat
    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    # Handle existing game states
    if game_state and game_state.get('state') == 'spinning':
         logger.info(f"Roulette game currently spinning in chat {chat_id}. Ignoring /roulette command.")
         try:
             await update.message.reply_text("⏳ Колесо рулетки уже вращается, подождите окончания раунда.", quote=True)
         except Exception: pass
         return
    elif game_state and game_state.get('state') == 'accepting_bets':
        logger.info(f"Roulette game already active in chat {chat_id} (state: accepting_bets). Resending status message.")
        try:
            await update.message.delete() # Delete the command message
        except Exception as e:
             logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")
        # Show the current game state again (send as new message)
        # If the state is accepting bets, the message might have been lost, so resend
        await rl_show_game_state(context, chat_id, edit_existing=False) # Force send new
        return

    # --- Start a New Round ---
    logger.info(f"Starting new roulette round in chat {chat_id}")

    # Cancel any existing spin timer AND display timer for this chat
    old_spin_timer_job_name = game_state.get('timer_job_name') if game_state else None
    old_display_timer_job_name = game_state.get('timer_display_job_name') if game_state else None

    if old_spin_timer_job_name:
        await rl_remove_job_if_exists(old_spin_timer_job_name, context)
        logger.info(f"Removed old spin timer '{old_spin_timer_job_name}' for chat {chat_id}")
    if old_display_timer_job_name:
        await rl_remove_job_if_exists(old_display_timer_job_name, context)
        logger.info(f"Removed old display timer '{old_display_timer_job_name}' for chat {chat_id}")

    # Delete previous game message if it exists
    if game_state and game_state.get('message_id'):
        try:
            await context.bot.delete_message(chat_id, game_state['message_id'])
            logger.debug(f"Deleted previous roulette message {game_state['message_id']} in chat {chat_id}")
        except Exception as e:
            logger.debug(f"Failed to delete previous roulette message {game_state.get('message_id')} in chat {chat_id}: {e}")

    # Initialize new game state in chat_data
    new_game_state = {
        'state': 'accepting_bets',
        'active_bets': {},
        'message_id': None,
        'timer_job_name': None, # For the main spin trigger
        'timer_display_job_name': None, # For the repeating display update
        'initiator_id': user.id
    }
    chat_data[RL_GAME_KEY] = new_game_state

    # Delete the triggering /roulette command message
    try:
        await update.message.delete()
    except Exception as e:
         logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")

    # Show the initial "Accepting Bets" state (send as new message)
    await rl_show_game_state(
        context,
        chat_id,
        message_text="🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!",
        edit_existing=False # Send as a new message
    )


async def rl_new_round_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Start New Round' button press from the main keyboard."""
    query = update.callback_query
    user = query.from_user
    chat = query.message.chat
    chat_id = chat.id
    logger.info(f"'rl_new_round' callback from user {user.id} in chat {chat_id}")

    await query.answer("Запуск нового раунда...")

    get_or_create_user(user.id) # Ensure user profile exists

    # Use application.chat_data for shared game state in this chat
    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)
    current_message_id = query.message.message_id

    # --- Clean up previous round state ---
    # Cancel any existing timers (spin timer AND display timer)
    old_spin_timer_job_name = game_state.get('timer_job_name') if game_state else None
    old_display_timer_job_name = game_state.get('timer_display_job_name') if game_state else None

    if old_spin_timer_job_name:
        await rl_remove_job_if_exists(old_spin_timer_job_name, context)
    if old_display_timer_job_name:
        await rl_remove_job_if_exists(old_display_timer_job_name, context)

    # Delete the message containing the "Start New Round" button
    try:
        await context.bot.delete_message(chat_id, current_message_id)
        logger.debug(f"Deleted previous roulette message {current_message_id} via rl_new_round_callback in chat {chat_id}")
    except Exception as e:
        logger.debug(f"Failed to delete message {current_message_id} in rl_new_round_callback for chat {chat_id}: {e}")

    # Initialize new game state in chat_data
    new_game_state = {
        'state': 'accepting_bets',
        'active_bets': {}, # user_id -> list of bet dicts
        'message_id': None, # Will be set by rl_show_game_state
        'timer_job_name': None, # Will be set when first bet is placed
        'timer_display_job_name': None, # Will be set along with timer_job_name
        'initiator_id': user.id # Store who started the round (optional)
    }
    chat_data[RL_GAME_KEY] = new_game_state

    # Show the initial "Accepting Bets" state (send as new message)
    await rl_show_game_state(
        context,
        chat_id,
        message_text="🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!",
        edit_existing=False # Send as a new message
    )


async def rl_show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_text: str | None = None, edit_existing: bool = True):
    """Updates or sends the main roulette game message with keyboard."""
    # Use application.chat_data for shared state
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    if not game_state:
        # Avoid logging warning spam if called by display timer job for a game that ended
        # Check if called by timer job (could add context in job data) - for now, just lower log level
        logger.debug(f"rl_show_game_state called for chat {chat_id} but no game state found.")
        return None # Indicate failure

    message_id = game_state.get('message_id')
    state = game_state.get('state', 'unknown')
    main_timer_job_name = game_state.get('timer_job_name') # Get the name of the ONE-SHOT timer
    active_bets_by_user = game_state.get('active_bets', {})

    # --- Get Display Names for Bettors ---
    display_name_map = {}
    user_ids = list(active_bets_by_user.keys())
    if user_ids:
        try:
            mention_data = await asyncio.gather(*(get_user_mention(context, uid) for uid in user_ids))
            display_name_map = {uid: mention_data[i][1] for i, uid in enumerate(user_ids)} # Use display name
        except Exception as e:
            logger.error(f"Failed to fetch user display names for roulette state in chat {chat_id}: {e}")
            display_name_map = {uid: f"User_{uid}" for uid in user_ids} # Fallback


    # --- Determine Message Text ---
    if message_text is None:
        # Default text based on game state
        if state == 'accepting_bets':
            base_text = "🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!"
        elif state == 'spinning':
            base_text = "🎰 <b>Колесо вращается...</b>"
        elif state == 'idle' or state == 'finished':
             base_text = "🏁 Раунд Рулетки завершен.\nИспользуйте 'Начать новый раунд' ниже."
        else:
            base_text = f"🎲 <b>Американская Рулетка</b> [Состояние: {state}]" # Fallback
    else:
        # Use provided text (e.g., for cancellation message)
        base_text = message_text

    # Add timer info if active
    timer_text = ""
    # Check if the main timer name exists in game_state
    if main_timer_job_name:
         jobs = context.job_queue.get_jobs_by_name(main_timer_job_name)
         if jobs and jobs[0].next_t: # Check if job exists and has a next run time
             remaining = max(0, int(jobs[0].next_t.timestamp() - time.time()))
             timer_text = f"\n⏳ <i>Авто-старт через ~{remaining} сек...</i>"
         else:
             # Main timer job doesn't exist or finished, but name might linger in state
             logger.debug(f"Timer job name '{main_timer_job_name}' in state, but job not found/active in chat {chat_id}.")
    # *** END MODIFIED SECTION ***

    full_text = base_text + timer_text

    # --- Generate Keyboard ---
    reply_markup = rl_get_main_menu_keyboard(chat_data, display_name_map) # Pass potentially updated chat_data

    # --- Send or Edit Message ---
    sent_message = None # To store new Message object if sent
    new_message_sent = False
    edit_failed_and_sending_new = False
    try:
        if edit_existing and message_id:
            # Attempt to edit the existing message
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=full_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            logger.debug(f"Edited roulette state message {message_id} in chat {chat_id}")
        else:
            # Send a new message
            # Delete old one first if we intended to edit but couldn't find it
            if message_id and edit_existing:
                 try:
                     await context.bot.delete_message(chat_id, message_id)
                     logger.debug(f"Deleted old message {message_id} before sending new in chat {chat_id}")
                 except Exception: pass
                 edit_failed_and_sending_new = True # Flag that edit failed

            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=full_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            # IMPORTANT: Update message_id in game state ONLY if game still exists
            # Re-fetch game_state as it might have changed (e.g., ended)
            current_game_state = context.application.chat_data.get(chat_id, {}).get(RL_GAME_KEY)
            if current_game_state:
                 current_game_state['message_id'] = sent_message.message_id
                 logger.info(f"Sent new roulette state message {sent_message.message_id} in chat {chat_id}. Updated game state.")
            else:
                 logger.warning(f"Sent new roulette message {sent_message.message_id} for chat {chat_id}, but game state was missing upon update.")

            new_message_sent = True

    except BadRequest as e:
        error_str = str(e).lower()
        if "message is not modified" in error_str:
            logger.debug(f"Roulette state message {message_id} not modified.")
        elif "message to edit not found" in error_str or "chat not found" in error_str or "message can't be edited" in error_str:
             logger.warning(f"Failed to edit roulette message {message_id} in chat {chat_id} (not found/editable). Forcing send new.")
             if game_state: game_state['message_id'] = None # Clear invalid ID from potentially stale state
             # Recursive call ONLY if we weren't already trying to send new after an edit failure
             if not edit_failed_and_sending_new:
                  return await rl_show_game_state(context, chat_id, message_text=full_text, edit_existing=False)
             else:
                  logger.error(f"Recursive send new failed in chat {chat_id} after edit failure.")
                  return None # Avoid infinite recursion
        elif "message text is empty" in error_str:
             logger.error(f"Attempted to send empty message to chat {chat_id}. Text: '{full_text}'")
             return None # Indicate failure
        else:
            logger.error(f"BadRequest showing roulette state for chat {chat_id} (msg {message_id}): {e}")
            return None # Indicate failure
    except Forbidden as e:
        logger.error(f"Forbidden error in chat {chat_id} (likely bot kicked/blocked): {e}")
        # Clean up game state and timers for this chat
        chat_data.pop(RL_GAME_KEY, None) # Use chat_data which is definitely defined here
        timer_job = game_state.get('timer_job_name') if game_state else None
        display_timer_job = game_state.get('timer_display_job_name') if game_state else None
        if timer_job: await rl_remove_job_if_exists(timer_job, context)
        if display_timer_job: await rl_remove_job_if_exists(display_timer_job, context)
        return None
    except Exception as e:
        logger.error(f"Unexpected error showing roulette state for chat {chat_id} (msg {message_id}): {e}", exc_info=True)
        return None

    # Return the Message object if new, or the ID if edited successfully
    return sent_message if new_message_sent else message_id


async def rl_start_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Add Bet' button press."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.debug(f"rl_start_bet_callback from user {user.id} in chat {chat_id}")

    get_or_create_user(user.id) # Ensure user exists

    # Use application.chat_data and user_data
    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    # --- Check Game State and Limits ---
    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Сейчас нельзя делать ставки.", show_alert=True)
        return

    active_bets_by_user = game_state.get('active_bets', {})
    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    user_bets_count = len(active_bets_by_user.get(user.id, []))

    if total_bets_count >= RL_MAX_BETS_PER_ROUND:
        await query.answer(f"Достигнут лимит ставок на раунд ({RL_MAX_BETS_PER_ROUND})!", show_alert=True)
        return
    if user_bets_count >= RL_MAX_BETS_PER_USER:
        await query.answer(f"Вы достигли лимита ставок на раунд ({RL_MAX_BETS_PER_USER})!", show_alert=True)
        return

    # --- Start Bet Process ---
    # Store temporary bet info in user_data (specific to this user)
    context.user_data[RL_USER_TEMP_BET_KEY] = {'step': 'type'}
    await query.answer() # Acknowledge button press

    # Edit the message to show the bet type selection
    try:
        await query.edit_message_text(
            text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
            reply_markup=rl_get_bet_type_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message for bet type selection: {e}")
             # Inform user if edit fails (and wasn't just 'not modified')
             await query.message.reply_text("Ошибка отображения меню ставок.")
        else:
             logger.debug("Message not modified on starting bet type selection.") # Common case
    except Exception as e:
        logger.error(f"Error editing message for bet type selection: {e}")
        # Fallback: send new message if edit fails unexpectedly
        await context.bot.send_message(chat_id, "Ошибка отображения меню ставок.")


async def rl_choose_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of bet type (Number, Color, etc.)."""
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Check if user is in the correct step
    if not temp_bet or temp_bet.get('step') != 'type':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        # Restore main game view if the message was hijacked for betting
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    await query.answer() # Acknowledge selection
    bet_type_choice = query.data.replace('rl_type_', '')
    temp_bet['type'] = bet_type_choice
    temp_bet['step'] = 'value' # Default next step

    try:
        if bet_type_choice == 'number':
            # Special handling for 'number' - requires text input
            temp_bet['step'] = 'ask_number'
            context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet # Save state change
            # Edit message to prompt for number input
            await query.edit_message_text(
                text="➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n<b>Введите число (0, 00, или 1-36) в чат:</b>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]), # Only Cancel button
                parse_mode=ParseMode.HTML
            )
        else:
            # For other types, show the value selection keyboard
            type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type_choice.capitalize()) # Get display name (e.g., "🔴 Красное")
            if not type_display: type_display = bet_type_choice.capitalize() # Fallback

            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                reply_markup=rl_get_bet_value_keyboard(bet_type_choice),
                parse_mode=ParseMode.HTML
            )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message for bet value/number selection: {e}")
    except Exception as e:
        logger.error(f"Error editing message for bet value/number selection: {e}")


async def rl_handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages when expecting a roulette number bet."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Only process if the user is in the 'ask_number' step
    if not temp_bet or temp_bet.get('step') != 'ask_number':
        return # Ignore other text messages

    number_input = update.message.text.strip().lower()

    # Delete the user's number message for cleaner chat
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete user number input message in chat {chat_id}: {e}")


    # Validate the input number
    if number_input not in RL_AMERICAN_WHEEL_SET:
        # Invalid number, re-prompt the user
        game_message_id = context.chat_data.get(RL_GAME_KEY, {}).get('message_id') # Get the main game message ID
        if game_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=game_message_id,
                    text=f"➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n<b>Неверный ввод: '{html_escape(number_input)}'.</b>\nВведите число (0, 00, или 1-36) в чат:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                 logger.error(f"Error editing message to show invalid number input: {e}")
        return # Stop processing this invalid input

    # --- Valid Number Received ---
    bet_type = temp_bet['type']
    temp_bet['value'] = number_input # Store the validated number string
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, number_input)
    temp_bet['step'] = 'amount' # Move to amount selection

    # Get balance for amount selection
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    if balance is None:
        await context.bot.send_message(chat_id, "Ошибка получения вашего баланса.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Cancel bet
        await rl_show_game_state(context, chat_id, edit_existing=True) # Restore main view
        return

    # Edit the main game message to show amount selection
    game_message_id = context.chat_data.get(RL_GAME_KEY, {}).get('message_id')
    if game_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game_message_id,
                text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\nВаш баланс: {balance:.2f} F\nВыберите сумму ставки:",
                reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error editing message for amount selection after number input: {e}")
    else:
         # Should not happen if betting started correctly
         logger.error(f"Cannot find game message ID in chat {chat_id} during number input handling.")


async def rl_choose_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of bet value (Red, Black, Even, Odd, Dozen, Column)."""
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Check step
    if not temp_bet or temp_bet.get('step') != 'value':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    await query.answer() # Acknowledge selection
    bet_value_choice = query.data.replace('rl_value_', '')
    bet_type = temp_bet['type']
    temp_bet['value'] = bet_value_choice
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, bet_value_choice)
    temp_bet['step'] = 'amount' # Move to next step

    # Get balance
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    if balance is None:
        # Handle error, cancel bet
        await query.edit_message_text("Ошибка получения вашего баланса. Ставка отменена.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    # Edit message to show amount selection
    try:
        await query.edit_message_text(
            text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\nВаш баланс: {balance:.2f} F\nВыберите сумму ставки:",
            reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message for bet amount selection: {e}")
    except Exception as e:
        logger.error(f"Error editing message for bet amount selection: {e}")


async def rl_choose_bet_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of the bet amount."""
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Check step
    if not temp_bet or temp_bet.get('step') != 'amount':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    # Parse amount from callback data
    try:
        bet_amount = int(query.data.replace('rl_amount_', ''))
    except ValueError:
        await query.answer("Неверное значение суммы.", show_alert=True)
        return

    # Validate amount against balance
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    bet_type = temp_bet.get('type', 'unknown') # Get type for back button context

    if balance is None:
        await query.edit_message_text("Ошибка получения вашего баланса. Ставка отменена.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    if not (0 < bet_amount <= balance):
        # Invalid amount or insufficient funds
        await query.answer(f"Недостаточно средств ({balance:.2f} F) или неверная сумма.", show_alert=True)
        # Re-show amount selection with error indication (optional, but helpful)
        try:
            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet.get('value_display', 'N/A')}\n\nВаш баланс: {balance:.2f} F\n<b>Неверная сумма!</b> Выберите сумму ставки:",
                reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                 logger.error(f"Error re-editing message for invalid amount: {e}")
        except Exception as e:
            logger.error(f"Error re-editing message for invalid amount: {e}")
        return # Stop processing

    # --- Valid Amount Selected ---
    await query.answer() # Acknowledge selection
    temp_bet['amount'] = bet_amount
    temp_bet['step'] = 'confirm' # Move to confirmation step

    # Edit message to show confirmation prompt
    try:
        # Get user's display name for the confirmation message
        _, display_name = await get_user_mention(context, user.id)
        await query.edit_message_text(
            text=(
                f"➕ <b>Подтверждение ставки</b>\n"
                f" - Игрок: {display_name}\n"
                f" - Ставка: {temp_bet['value_display']}\n"
                f" - Сумма: {temp_bet['amount']} F\n\n"
                f"Подтверждаете?"
            ),
            reply_markup=rl_get_confirmation_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message for confirmation: {e}")
    except Exception as e:
        logger.error(f"Error editing message for confirmation: {e}")


async def rl_confirm_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Yes' confirmation button press and starts timers if first bet."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Check step
    if not temp_bet or temp_bet.get('step') != 'confirm':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, chat_id, edit_existing=True) # Restore main view
        return

    # --- Final Checks Before Placing Bet ---
    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    # Check if betting is still allowed
    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Ставки больше не принимаются.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Clear temp bet
        await rl_show_game_state(context, chat_id, edit_existing=True) # Show final state
        return

    bet_amount = temp_bet.get('amount', 0)
    get_or_create_user(user.id)
    balance = get_balance(user.id) # Re-check balance just in case

    if balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    if not (0 < bet_amount <= balance):
        await query.answer(f"Недостаточно средств ({balance:.2f} F).", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Check round and user bet limits again
    active_bets_by_user = game_state.get('active_bets', {})
    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    user_bets_count = len(active_bets_by_user.get(user.id, []))

    if total_bets_count >= RL_MAX_BETS_PER_ROUND:
        await query.answer(f"Достигнут общий лимит ставок ({RL_MAX_BETS_PER_ROUND})!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return
    if user_bets_count >= RL_MAX_BETS_PER_USER:
        await query.answer(f"Вы достигли своего лимита ставок ({RL_MAX_BETS_PER_USER})!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # --- Place the Bet ---
    # 1. Deduct balance
    new_balance = update_balance(user.id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка списания средств со счета!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # 2. Add bet to game_state
    final_bet = {
        'type': temp_bet['type'],
        'value': temp_bet['value'],
        'amount': temp_bet['amount'],
        'value_display': temp_bet['value_display']
    }
    if user.id not in active_bets_by_user:
        active_bets_by_user[user.id] = []
    active_bets_by_user[user.id].append(final_bet)
    game_state['active_bets'] = active_bets_by_user # Update game state

    # 3. Clean up temporary user data
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)

    await query.answer("✅ Ставка принята!")
    logger.info(f"User {user.id} placed bet in chat {chat_id}: {final_bet}")

    # --- Start Timers if First Bet ---
    current_total_bets = sum(len(bets) for bets in active_bets_by_user.values())
    spin_timer_job_name = f'rl_spin_timer_{chat_id}'
    display_timer_job_name = f'rl_display_timer_{chat_id}'
    spin_timer_exists = bool(context.job_queue.get_jobs_by_name(spin_timer_job_name))
    display_timer_exists = bool(context.job_queue.get_jobs_by_name(display_timer_job_name))

    # Start timers only if it's the very first bet of the round and timers aren't already running
    if current_total_bets == 1 and not spin_timer_exists and not display_timer_exists:
        # Schedule the main one-shot timer to trigger the spin
        context.job_queue.run_once(
            rl_spin_roulette_job,
            RL_BET_TIMER_SECONDS,
            chat_id=chat_id,
            name=spin_timer_job_name,
            data={'chat_id': chat_id}
        )
        game_state['timer_job_name'] = spin_timer_job_name # Store main timer name

        # Schedule the repeating timer to update the display
        context.job_queue.run_repeating(
            rl_update_timer_display_job,
            interval=RL_TIMER_DISPLAY_UPDATE_INTERVAL,
            first=0.1, # Start updating quickly
            chat_id=chat_id,
            name=display_timer_job_name,
            data={'chat_id': chat_id, 'main_timer_job_name': spin_timer_job_name}
        )
        game_state['timer_display_job_name'] = display_timer_job_name # Store display timer name

        logger.info(f"Started roulette timers for chat {chat_id}: Spin='{spin_timer_job_name}', Display='{display_timer_job_name}'")
    # *** END MODIFIED SECTION ***

    # --- Update Game Display ---
    # Show the main game state again, now including the new bet and possibly timer info
    # The timer text will now be fetched correctly by rl_show_game_state
    await rl_show_game_state(context, chat_id, message_text=None, edit_existing=True)


async def rl_cancel_bet_step_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Cancel button presses during bet creation."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id

    # Remove temporary bet data
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
    await query.answer("Действие отменено.")

    # Restore the main game view
    await rl_show_game_state(context, chat_id, message_text="Создание ставки отменено.", edit_existing=True)


async def rl_back_to_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Back button press to return to bet type selection."""
    query = update.callback_query
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Only proceed if temporary bet data exists
    if temp_bet:
        # Reset the temporary bet state to 'type' selection
        context.user_data[RL_USER_TEMP_BET_KEY] = {'step': 'type'}
        await query.answer() # Acknowledge button press

        # Edit the message back to the type selection screen
        try:
            await query.edit_message_text(
                text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
                reply_markup=rl_get_bet_type_keyboard(),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing message for back to bet type: {e}")
        except Exception as e:
            logger.error(f"Error editing message for back to bet type: {e}")
    else:
        # If no temp data, just cancel
        await query.answer("Отмена.")
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)


async def rl_back_to_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Back button press from amount selection to value selection (not for 'number' type)."""
    query = update.callback_query
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Only proceed if temp data exists and type is NOT 'number'
    if temp_bet and 'type' in temp_bet and temp_bet.get('type') != 'number':
        bet_type = temp_bet['type']
        # Reset state to 'value' selection for the current type
        temp_bet_reset = {'step': 'value', 'type': bet_type}
        context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet_reset
        await query.answer()

        # Get display name for the type
        type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type.capitalize(), bet_type)

        # Edit message back to value selection screen
        try:
            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                reply_markup=rl_get_bet_value_keyboard(bet_type),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing message for back to bet value: {e}")
        except Exception as e:
            logger.error(f"Error editing message for back to bet value: {e}")
    else:
        # If type is 'number' or temp data is missing, go back to type selection instead
        await rl_back_to_bet_type_callback(update, context)


async def rl_spin_roulette_logic(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Contains the core logic for spinning the wheel and determining results."""
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)
    bot = context.bot

    if not game_state:
        logger.error(f"Spin logic called for chat {chat_id} but no game state found.")
        return

    # Prevent starting spin if not in correct state or already spinning
    # Allow spin if state is 'accepting_bets' (triggered by timer/button)
    if game_state.get('state') != 'accepting_bets':
        if game_state.get('state') == 'spinning':
            logger.warning(f"Spin logic called again for chat {chat_id} while already spinning. Ignoring.")
        else:
             logger.warning(f"Spin logic called for chat {chat_id} but state is '{game_state.get('state')}'. Aborting spin.")
        return

    # Make a copy of bets to process, as game_state might change
    active_bets_by_user = dict(game_state.get('active_bets', {})) # Use dict() for a shallow copy

    # Check if there are actually any bets
    if not active_bets_by_user:
        logger.warning(f"Spin logic called for chat {chat_id} but no bets found.")
        game_state['state'] = 'idle' # Reset state
        # Clean up any lingering timers if somehow spin was triggered with no bets
        spin_timer_name = game_state.pop('timer_job_name', None)
        display_timer_name = game_state.pop('timer_display_job_name', None)
        if spin_timer_name: await rl_remove_job_if_exists(spin_timer_name, context)
        if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)

        msg_id = game_state.get('message_id')
        if msg_id:
            try:
                final_reply_markup = rl_get_main_menu_keyboard(chat_data, {})
                await bot.edit_message_text(
                    chat_id, msg_id,
                    "Ставок не было, раунд завершен.\nИспользуйте 'Начать новый раунд' ниже.",
                    reply_markup=final_reply_markup)
            except Exception: pass
        return

    message_id = game_state.get('message_id')
    if not message_id:
        logger.error(f"Cannot spin roulette in chat {chat_id}, message_id is missing.")
        try: await bot.send_message(chat_id, "❌ Ошибка: Не найдено сообщение для отображения спина!")
        except Exception: pass
        # Clean up broken game state and timers
        spin_timer_name = game_state.pop('timer_job_name', None) if game_state else None
        display_timer_name = game_state.pop('timer_display_job_name', None) if game_state else None
        chat_data.pop(RL_GAME_KEY, None)
        if spin_timer_name: await rl_remove_job_if_exists(spin_timer_name, context)
        if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)
        return

    # --- Set State to Spinning & Clean Timers ---
    game_state['state'] = 'spinning'
    spin_timer_name = game_state.pop('timer_job_name', None) # Remove main timer name
    display_timer_name = game_state.pop('timer_display_job_name', None) # Remove display timer name

    if spin_timer_name:
        await rl_remove_job_if_exists(spin_timer_name, context) # Ensure main timer job is cancelled
        logger.debug(f"Removed spin timer '{spin_timer_name}' before animation start in chat {chat_id}")
    if display_timer_name:
        await rl_remove_job_if_exists(display_timer_name, context) # Ensure display timer job is cancelled
        logger.debug(f"Removed display timer '{display_timer_name}' before animation start in chat {chat_id}")
    # *** END MODIFIED SECTION ***

    # --- Determine Winning Number ---
    winning_number_str = random.choice(AMERICAN_WHEEL_ORDER)
    target_index = AMERICAN_WHEEL_ORDER.index(winning_number_str)
    logger.info(f"Roulette spin result for chat {chat_id}: {winning_number_str} (index {target_index})")

    # --- Animation Parameters ---
    spin_duration = RL_SPIN_ANIMATION_DURATION
    min_full_rotations = 2
    max_full_rotations = 4
    frames_per_second = 2.5 # How many times to update the message per second (approx)
    update_interval = 1.0 / frames_per_second
    spinner_emojis = ['◜','◝','◞','◟'] # Simple spinner

    # Calculate animation steps
    start_index = random.randint(0, RL_WHEEL_SIZE - 1)
    current_index = start_index
    num_full_rotations = random.randint(min_full_rotations, max_full_rotations)
    steps_for_rotations = num_full_rotations * RL_WHEEL_SIZE
    # Steps needed to get from start to target index (circularly)
    steps_to_target = (target_index - start_index + RL_WHEEL_SIZE) % RL_WHEEL_SIZE
    total_steps = steps_for_rotations + steps_to_target
    # Ensure at least one rotation if start == target
    if total_steps == 0: total_steps = RL_WHEEL_SIZE
    logger.info(f"Roulette Animation chat {chat_id}: Start={start_index}, Target={target_index}, Rot={num_full_rotations}, Steps={total_steps}")


    # --- Run Animation ---
    try:
        # Initial "spinning" message update (remove keyboard)
        await bot.edit_message_text(
            "🎰 <b>Колесо вращается...</b>",
            chat_id=chat_id, message_id=message_id, reply_markup=None, parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(0.5) # Short pause before animation starts
    except Exception as e:
        logger.warning(f"Failed to edit message {message_id} for spin start in chat {chat_id}: {e}")
        # Continue anyway, animation might fail but result should still be calculated

    loop_start_time = time.monotonic()
    steps_taken = 0
    next_update_time_budget = loop_start_time # Time when the next message edit is allowed
    last_displayed_number = "" # To avoid "message not modified" errors
    animation_successful = True

    # Easing function (cubic ease-out) for smoother slowdown
    def ease_out_cubic(t):
        t -= 1
        return t * t * t + 1

    while steps_taken < total_steps:
        # Calculate progress and eased progress
        progress = (steps_taken + 1) / total_steps # Progress from 0 to 1
        eased_progress = ease_out_cubic(progress)
        # Time when this step *should* ideally end based on eased progress
        target_step_end_time = loop_start_time + spin_duration * eased_progress

        current_mono_time = time.monotonic()

        # Check if it's time to update the message visually
        if current_mono_time >= next_update_time_budget:
            display_number = AMERICAN_WHEEL_ORDER[current_index]
            color_char = rl_get_color(display_number)
            display_color_emoji = "🟢" if color_char == 'Green' else ("🔴" if color_char == 'Red' else "⚫")
            spinner = spinner_emojis[steps_taken % len(spinner_emojis)] # Cycle through spinner
            frame_text = f"🎰 {spinner} {display_color_emoji} {display_number}"

            # Only edit if the number changed to avoid spamming Telegram API
            if display_number != last_displayed_number:
                try:
                    await bot.edit_message_text(
                        text=frame_text,
                        chat_id=chat_id,
                        message_id=message_id
                    )
                    last_displayed_number = display_number
                    # Budget time for the next update
                    next_update_time_budget = current_mono_time + update_interval
                except BadRequest as e:
                    if "Message is not modified" in str(e).lower(): pass # Ignore benign error
                    else:
                        logger.warning(f"BadRequest editing animation chat {chat_id} (step {steps_taken}): {e}")
                        animation_successful = False; break # Stop animation on error
                except Forbidden:
                    logger.error(f"Forbidden error during animation in chat {chat_id}. Aborting.")
                    animation_successful = False; break
                except Exception as e:
                    logger.warning(f"Error editing animation chat {chat_id} (step {steps_taken}): {e}")
                    animation_successful = False; break
            else:
                # If number didn't change, still update the budget time
                next_update_time_budget = current_mono_time + update_interval

        # Calculate sleep duration to match the target end time for this step
        current_mono_time = time.monotonic() # Re-check time after potential edit
        sleep_duration = max(0.005, target_step_end_time - current_mono_time) # Sleep at least a tiny bit
        await asyncio.sleep(sleep_duration)

        # Move to next step/index
        current_index = (current_index + 1) % RL_WHEEL_SIZE
        steps_taken += 1

    # --- Show Final Result (Briefly) ---
    if animation_successful:
      try:
          final_color_char = rl_get_color(winning_number_str)
          final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
          await bot.edit_message_text(
              text=f"<b>➡️ {final_color_emoji} {winning_number_str} ⬅️</b>", # Highlight winning number
              chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML
          )
          await asyncio.sleep(1.5) # Pause on the winning number
      except Exception as e:
          logger.warning(f"Failed to show final animation number for chat {chat_id}: {e}")
          # Proceed to outcome calculation anyway

    # --- Calculate Winnings ---
    winning_color = rl_get_color(winning_number_str)
    winning_parity = rl_is_even_or_odd(winning_number_str)
    winning_dozen = rl_get_dozen(winning_number_str)
    winning_column = rl_get_column(winning_number_str)

    # Store results per user {user_id: {'wins': amount, 'returned': amount, 'log': [str], 'bets': [bet_dict]}}
    results_by_user = defaultdict(lambda: {'wins': 0, 'returned': 0, 'log': [], 'bets': []})
    total_net_change = 0 # Track overall change for logging

    for user_id, bets in active_bets_by_user.items():
        user_results = results_by_user[user_id]
        user_results['bets'] = bets # Store original bets for reference/debugging

        for bet in bets:
            win = False
            payout_mult = 0
            bet_type = bet.get('type')
            bet_value = bet.get('value')
            bet_amount = bet.get('amount', 0)
            value_disp = bet.get('value_display', 'N/A') # Use display name for logs

            # Skip malformed bets
            if not all([bet_type, bet_value, bet_amount > 0]):
                logger.warning(f"Skipping malformed bet for user {user_id} in chat {chat_id}: {bet}")
                continue

            # Check winning conditions based on bet type
            if bet_type == 'number' and str(bet_value) == winning_number_str:
                payout_mult, win = RL_PAYOUTS['number'], True
            elif bet_type == 'color' and bet_value == winning_color:
                payout_mult, win = RL_PAYOUTS['color'], True
            elif bet_type == 'parity' and bet_value == winning_parity:
                payout_mult, win = RL_PAYOUTS['parity'], True
            elif bet_type == 'dozen' and bet_value == winning_dozen:
                payout_mult, win = RL_PAYOUTS['dozen'], True
            elif bet_type == 'column' and bet_value == winning_column:
                payout_mult, win = RL_PAYOUTS['column'], True

            # Calculate winnings and log result
            if win:
                winnings = bet_amount * payout_mult
                returned = bet_amount + winnings # Amount to give back (original bet + win)
                user_results['wins'] += winnings
                user_results['returned'] += returned
                user_results['log'].append(f"✅ {value_disp} ({bet_amount}F) -> +{winnings:.2f}F")
            else:
                # Loss
                user_results['log'].append(f"❌ {value_disp} ({bet_amount}F)")

    # --- Format Results and Update Balances ---
    result_lines = []
    player_ids = list(results_by_user.keys())
    html_mention_map = {}

    # Fetch HTML mentions for results message
    if player_ids:
         try:
             mention_data = await asyncio.gather(*(get_user_mention(context, uid) for uid in player_ids))
             html_mention_map = {uid: mention_data[i][0] for i, uid in enumerate(player_ids)} # Use HTML mention
         except Exception as e:
             logger.error(f"Failed to fetch HTML mentions for roulette results in chat {chat_id}: {e}")
             html_mention_map = {uid: f"User_{uid}" for uid in player_ids} # Fallback

    processed_users = set() # Keep track of users processed to catch potential errors

    for user_id, results in results_by_user.items():
        processed_users.add(user_id)
        user_mention_html = html_mention_map.get(user_id, f"User_{user_id}") # Get mention or fallback
        amount_to_pay = results['returned']
        total_bet_amount = sum(b['amount'] for b in results['bets'])
        net_change = amount_to_pay - total_bet_amount

        # Add user header and individual bet results
        result_lines.append(f"\n--- {user_mention_html} ---")
        result_lines.extend(results['log'])

        # Update balance if needed
        balance_update_status = ""
        if amount_to_pay > 0:
            new_bal = update_balance(user_id, amount_to_pay)
            if new_bal is None:
                # Balance update failed!
                balance_update_status = " ⚠️<b>Ошибка начисления!</b>"
                logger.error(f"Roulette payout FAILED for user {user_id} in chat {chat_id}. Amount: {amount_to_pay}")
                net_change = -total_bet_amount # Ensure net change reflects loss if payout failed
            else:
                balance_update_status = f" -> Баланс: {new_bal:.2f}F" # Show new balance
        elif total_bet_amount > 0: # If user lost but had bets, show current balance
            current_bal = get_balance(user_id)
            if current_bal is not None: balance_update_status = f" -> Баланс: {current_bal:.2f}F"


        result_lines.append(f"<i>Итог: {net_change:+.2f} F{balance_update_status}</i>") # Show net change and balance status
        total_net_change += net_change

    # Sanity check: Ensure all users who had bets were processed
    original_user_ids = list(active_bets_by_user.keys())
    for user_id in original_user_ids:
         if user_id not in processed_users:
             logger.warning(f"User {user_id} had bets but was not in results_by_user for chat {chat_id}")
             result_lines.append(f"\n--- User_{user_id} (Ошибка обработки) ---")


    # --- Construct Final Message ---
    final_color_char = rl_get_color(winning_number_str)
    final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
    timestamp = datetime.datetime.now().strftime("%H:%M") # Add timestamp to result
    result_header = f"🎉 Выпало: <b>{final_color_emoji} {winning_number_str}</b> 🎉\n"
    result_summary = f"\n\n<b>Общий итог раунда: {total_net_change:+.2f} F</b>    {timestamp}" # Added timestamp
    full_result_text = result_header + "\n".join(result_lines) + result_summary

    # --- Reset Game State ---
    # Important: access chat_data again in case it was modified elsewhere concurrently
    current_chat_data = context.application.chat_data.get(chat_id, {})
    current_game_state = current_chat_data.get(RL_GAME_KEY)
    if current_game_state:
        current_game_state['state'] = 'finished' # Set state to finished/idle
        current_game_state['active_bets'] = {} # Clear bets
        current_game_state['timer_job_name'] = None # Ensure cleared
        current_game_state['timer_display_job_name'] = None # Ensure cleared
    else:
        # Should not happen ideally
        logger.warning(f"Game state for chat {chat_id} disappeared before state reset in spin logic.")

    # Get the final keyboard (should show "Start New Round")
    final_chat_data = context.application.chat_data.get(chat_id, {}) # Get potentially updated chat_data
    final_reply_markup = rl_get_main_menu_keyboard(final_chat_data, {}) # Generate keyboard based on final state


    # --- Send Final Result Message ---
    try:
        await bot.edit_message_text(
            text=full_result_text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=ParseMode.HTML,
            reply_markup=final_reply_markup
        )
    except Forbidden:
         logger.error(f"Forbidden: Cannot send final result to chat {chat_id}")
         chat_data.pop(RL_GAME_KEY, None) # Clean up game state if blocked
    except Exception as e:
        logger.error(f"Failed to edit final roulette result for chat {chat_id}: {e}")
        # Fallback: Try sending as a new message if edit failed
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=full_result_text,
                parse_mode=ParseMode.HTML,
                reply_markup=final_reply_markup
            )
        except Exception as send_e:
            logger.error(f"Failed to send final roulette result as new message for chat {chat_id}: {send_e}")

    logger.info(f"Roulette round finished in chat {chat_id}. Winning number: {winning_number_str}")


async def rl_spin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the manual 'Spin!' button press."""
    query = update.callback_query
    chat_id = query.message.chat_id
    user = query.from_user
    logger.info(f"Manual spin triggered by user {user.id} in chat {chat_id}")

    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    # --- Validate Conditions for Manual Spin ---
    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Сейчас нельзя запустить вращение.", show_alert=True)
        return

    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user:
        await query.answer("Нет ставок для запуска вращения.", show_alert=True)
        return

    # Check if only one player is betting (as per current keyboard logic)
    if len(active_bets_by_user) != 1:
        await query.answer("Кнопка 'Крутить!' доступна только если ставит один игрок.", show_alert=True)
        return

    # --- Initiate Spin ---
    await query.answer("Запускаем вращение...")
    # Call the main spin logic function
    await rl_spin_roulette_logic(context, chat_id)


async def rl_show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the roulette rules explanation."""
    query = update.callback_query
    help_text = (
        f"<b>🎲 Правила Американской рулетки:</b>\n\n"
        f"• Делайте ставки с помощью кнопок.\n"
        f"• Макс. ставок в раунде: {RL_MAX_BETS_PER_ROUND} (всего), {RL_MAX_BETS_PER_USER} (на игрока).\n"
        f"• После первой ставки раунд запустится через {RL_BET_TIMER_SECONDS} сек (если не нажать 'Крутить!' раньше).\n"
        f"• Можно нажать 'Крутить!' раньше (только если ставит 1 игрок).\n"
        f"• Ставки на 0 или 00 выигрывают только при ставке на 'Число'.\n\n"
        f"<b>Типы ставок (Выплата 1 к X):</b>\n"
        f"- Число (Number): 1 к {RL_PAYOUTS['number']}\n"
        f"- Цвет (Color - Red/Black): 1 к {RL_PAYOUTS['color']}\n"
        f"- Чет/Нечет (Parity - Even/Odd): 1 к {RL_PAYOUTS['parity']}\n"
        f"- Дюжина (Dozen - 1st/2nd/3rd): 1 к {RL_PAYOUTS['dozen']}\n"
        f"- Колонка (Column - col1/col2/col3): 1 к {RL_PAYOUTS['column']}\n\n"
        f"<i>Ставки на Цвет, Чет/Нечет, Дюжины, Колонки <b>проигрывают</b> при выпадении 0 или 00.</i>"
    )
    await query.answer() # Acknowledge button press

    try:
        # Send rules as a new message in the chat
        await context.bot.send_message(
            chat_id=update.effective_chat.id, # Send in the chat where the button was pressed
            text=help_text,
            parse_mode=ParseMode.HTML
        )
    except Forbidden:
        logger.error(f"Forbidden: Cannot send roulette help to chat {update.effective_chat.id}")
    except Exception as e:
        logger.error(f"Failed to send roulette help: {e}")


async def rl_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for non-clickable buttons (like headers or display rows)."""
    # Simply acknowledge the press without doing anything.
    await update.callback_query.answer()

# --- End of Full Roulette Code ---

# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General handler for all Inline Keyboard Button presses."""
    q = update.callback_query
    data = q.data # The callback_data string
    u = q.from_user
    chat = update.effective_chat # Get chat object

    if not data:
        await q.answer() # Answer empty callbacks
        return

    logger.debug(f"Callback query received: '{data}' from user {u.id} in chat {chat.id} ({chat.type})")
    parts = data.split("_") # Split data like "bj_bet_10" or "rl_start_bet"
    prefix = parts[0] if parts else None

    try:
        # --- Blackjack Callbacks ---
        if prefix == "bj":
            # Enforce private chat for Blackjack actions
            if chat.type != ChatType.PRIVATE:
                await q.answer("Играть в Блекджек можно только в личном чате.", show_alert=True)
                return

            action = parts[1] if len(parts) > 1 else None
            arg = parts[2] if len(parts) > 2 else None # e.g., bet amount or hand index

            if action == "bet" and arg:
                # Handle bet selection
                # await q.answer(f"Ставка (BJ): {arg} F") # Removed answer here, handled in handle_bet
                await blackjack_handle_bet(update, context, int(arg))
            elif action == "new" and arg == "game":
                # Handle 'New Game' button
                await q.answer("Новая игра (BJ)...")
                await blackjack_start_command(update, context) # Reuse start command logic
            elif action in ["hit", "stand", "double", "split"] and arg is not None:
                # Handle in-game actions (pass action and index)
                 # No q.answer() here, it's handled within blackjack_handle_action
                await blackjack_handle_action(update, context, [action, arg])
            else:
                logger.warning(f"Unknown or incomplete BJ callback: {data}")
                await q.answer() # Answer silently

        # --- Roulette Callbacks ---
        elif prefix == "rl":
            # Route based on full callback data for Roulette
            if data == "rl_start_bet":
                await rl_start_bet_callback(update, context)
            elif data.startswith("rl_type_"):
                await rl_choose_bet_type_callback(update, context)
            elif data.startswith("rl_value_"):
                await rl_choose_bet_value_callback(update, context)
            elif data.startswith("rl_amount_"):
                await rl_choose_bet_amount_callback(update, context)
            elif data == "rl_confirm_bet_yes":
                await rl_confirm_bet_callback(update, context)
            elif data == "rl_cancel_bet_step":
                await rl_cancel_bet_step_callback(update, context)
            elif data == "rl_back_to_bet_type":
                await rl_back_to_bet_type_callback(update, context)
            elif data == "rl_back_to_bet_value":
                await rl_back_to_bet_value_callback(update, context)
            elif data == "rl_spin":
                await rl_spin_callback(update, context)
            elif data == "rl_show_help":
                await rl_show_help_callback(update, context)
            elif data == "rl_new_round": # <<< ADDED
                await rl_new_round_callback(update, context)
            elif data == "rl_noop": # Handle non-clickable buttons
                await rl_noop_callback(update, context)
            else:
                logger.warning(f"Unknown or incomplete RL callback: {data}")
                await q.answer() # Answer silently

        # --- Other Callbacks (if any) ---
        else:
            logger.warning(f"Unknown callback prefix: {prefix} in data: {data}")
            await q.answer() # Answer silently

    # --- Error Handling for Callbacks ---
    except ValueError as e:
        logger.error(f"Callback ValueError (likely int conversion) for '{data}' user {u.id}: {e}")
        try: await q.answer("Ошибка: Неверный формат данных.", show_alert=True)
        except Exception: pass
    except BadRequest as e:
        error_str = str(e).lower()
        if "query is too old" in error_str:
             logger.debug(f"Ignoring too old callback query for user {u.id}")
             pass # User clicked an old button, nothing we can do
        elif "message is not modified" in error_str:
            logger.debug(f"Callback resulted in 'Message is not modified' for user {u.id}")
            pass # Edit resulted in no change, not really an error
        elif "message to edit not found" in error_str:
            logger.warning(f"Callback failed: 'Message to edit not found' for user {u.id}. Data: {data}")
            try: await q.answer("Сообщение игры было удалено или изменено.", show_alert=False)
            except Exception: pass
        else:
            logger.warning(f"Callback BadRequest for '{data}' user {u.id}: {e}")
            try: await q.answer("Произошла ошибка при обработке.", show_alert=True)
            except Exception: pass
    except Forbidden as e:
        logger.error(f"Callback Forbidden error for user {u.id} in chat {chat.id}: {e}")
        try: await q.answer("Ошибка: Бот не имеет прав в этом чате.", show_alert=True)
        except Exception: pass
    except Exception as e:
        logger.error(f"Callback general error for '{data}' user {u.id}: {e}", exc_info=True)
        try: await q.answer("Произошла внутренняя ошибка.", show_alert=True)
        except Exception: pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors raised by Handlers."""
    logger.error(f"Exception while handling an update:", exc_info=context.error)

    # Specific error handling (optional but helpful)
    if isinstance(context.error, Conflict):
        # Usually means multiple bot instances running with the same token
        logger.critical("Conflict error detected! Ensure only ONE instance of the bot is running.")
    elif isinstance(context.error, Forbidden):
        # Bot blocked, kicked, or lacks permissions in a group
        logger.error(f"Forbidden error: {context.error}. Bot might be blocked or lack permissions. Update: {update}")
    elif isinstance(context.error, BadRequest):
         # Log other BadRequests, but be mindful of common ones
         error_str = str(context.error).lower()
         if "message is not modified" not in error_str and "query is too old" not in error_str:
              logger.warning(f"BadRequest error: {context.error}. Update: {update}")
    # Add more specific error type handling if needed

# --- Main Bot Setup ---
def main():
    logger.info("Starting bot application...")
    start_keep_alive() # Start the Flask keep-alive server in a background thread

    try:
        # --- Application Setup ---
        # Use recommended builder pattern
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True) # Handle multiple updates concurrently
            .connect_timeout(30)      # Adjust timeouts if needed
            .read_timeout(30)
            .pool_timeout(30)
            .build()
        )

        # --- Register Handlers ---
        # Core Commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))

        # Game Commands
        application.add_handler(CommandHandler("blackjack", blackjack_start_command))
        application.add_handler(CommandHandler("roulette", roulette_start_command))

        # Callback Query Handler (for all buttons) - Group 0 for higher priority
        application.add_handler(CallbackQueryHandler(button_callback_handler), group=0)

        # Message Handler for Roulette Number Input - Group 1 for lower priority than commands/callbacks
        # Handles non-command text messages ONLY if not handled by other handlers in group 0
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rl_handle_number_input), group=1)


        # Error Handler
        application.add_error_handler(error_handler)

        logger.info("Handlers registered successfully.")
        print("Bot is running... Press Ctrl+C to stop.")

        # --- Run the Bot ---
        # Start polling
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, # Process all update types
            drop_pending_updates=True # Ignore updates received while offline
            )

    except ValueError as e: # Catch configuration errors early
        logger.critical(f"Configuration Error: {e}")
        print(f"CRITICAL ERROR: {e}")
    except Conflict as e: # Catch multiple instance errors
        logger.critical(f"Conflict Error: {e}. Is another instance of the bot running?")
        print("CRITICAL ERROR: Conflict detected.")
    except Exception as e: # Catch any other critical startup errors
        logger.critical(f"An unexpected critical error occurred during startup: {e}", exc_info=True)
        print(f"CRITICAL ERROR: {e}")
    finally:
        print("Bot stopped.")
        logger.info("Bot application has stopped.")

if __name__ == "__main__":
    main()