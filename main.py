# -*- coding: utf-8 -*-
import logging
import os
import random
import datetime
import time
import asyncio
import math # <<< ADDED FROM ROULETTE
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
    MessageHandler, # <<< ADDED FROM ROULETTE
    filters, # <<< ADDED FROM ROULETTE
    ConversationHandler # We will NOT use ConversationHandler, but keep the concept
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Conflict, Forbidden # Added Forbidden

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL: raise ValueError("DATABASE_URL environment variable not set")

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
RL_BET_AMOUNTS = [10, 25, 50, 100, 250, 500] # Renamed prefix
RL_MAX_BETS_PER_ROUND = 10 # Max total unique bets in a round (e.g. User1 bets Red, User2 bets Even = 2 bets)
RL_MAX_BETS_PER_USER = 3 # Max bets one user can place in a round
RL_BET_TIMER_SECONDS = 45 # Time to place bets after first bet
RL_SPIN_ANIMATION_DURATION = 8.0 # Duration of spin animation
RL_GAME_KEY = 'roulette_game' # Key for chat_data
RL_USER_TEMP_BET_KEY = 'roulette_temp_bet' # Key for user_data during bet placement
# American Wheel Layout & Definitions
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
RL_AMERICAN_WHEEL_SET = set(AMERICAN_WHEEL_ORDER) # For fast checking 'ask_number_input'


# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive (No Changes) ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home(): return "Bot is alive!"
def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)
def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Card Definitions (Blackjack - No Changes) ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Database Interaction (No Changes Required Here) ---
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
                data = cur.fetchone() or {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        if data and data.get('last_bonus') and not isinstance(data['last_bonus'], datetime.datetime):
            if isinstance(data['last_bonus'], str):
                try:
                    data['last_bonus'] = datetime.datetime.fromisoformat(data['last_bonus'])
                except ValueError:
                     data['last_bonus'] = None
            elif not isinstance(data['last_bonus'], datetime.datetime):
                 data['last_bonus'] = None
        return data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}")
        return None

def update_balance(user_id: int, change: float) -> float | None:
    # Ensure user exists before updating balance
    if not get_or_create_user(user_id):
        logger.error(f"Attempted balance update for non-existent user {user_id}")
        return None
    # Proceed with update
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
                logger.warning(f"Update balance failed for user {user_id} (user not found or other issue after creation check)")
                return None
    except psycopg2.errors.CheckViolation as e: # Catch potential negative balance violation
         logger.warning(f"Balance update rejected for user {user_id}: {e}")
         return None # Indicate failure due to constraint
    except Exception as e:
        logger.error(f"DB Error (update_balance) for {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
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
    if last_bonus and isinstance(last_bonus, str): # Attempt parse if stored as string
         try:
             last_bonus = datetime.datetime.fromisoformat(last_bonus)
         except ValueError:
             logger.warning(f"Could not parse last_bonus string '{last_bonus}' for user {user_id}. Resetting.")
             last_bonus = None
    elif last_bonus and not isinstance(last_bonus, datetime.datetime):
         logger.warning(f"Invalid last_bonus type for user {user_id}: {type(last_bonus)}. Resetting.")
         last_bonus = None
    # Make timezone aware (assuming stored as naive UTC)
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

# --- Blackjack Game Utilities (No Changes) ---
def create_deck(num=NUM_DECKS)->list:
    deck = [(r, s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card: tuple | None) -> int:
    return RANK_VALUES.get(card[0], 0) if card else 0

def get_hand_value(hand: list) -> int:
    value = sum(get_card_value(card) for card in hand if card)
    num_aces = sum(1 for card in hand if card and card[0] == 'A')
    # Adjust for Aces
    while value > 21 and num_aces > 0:
        value -= 10
        num_aces -= 1
    return value

def format_hand(hand: list, hide_one: bool = False) -> str:
    if not hand: return "Пусто"
    if hide_one and len(hand) > 1:
        # Show first card, hide second
        first_card = f"{hand[0][0]}{hand[0][1]}" if hand[0] else "??"
        return f"[{first_card}, ??]"
    # Show all cards
    return ", ".join([f"{card[0]}{card[1]}" for card in hand if card])

def draw_card(deck: list) -> tuple | None:
    """ Draws a single card from the deck, removing it. Returns None if deck is empty. """
    if not deck:
        logger.warning("Attempted to draw from an empty deck.")
        return None
    try:
        return deck.pop(random.randrange(len(deck)))
    except (ValueError, IndexError) as e: # Handle potential race condition or empty deck
        logger.error(f"Error drawing card: {e}")
        return None

# --- Roulette Game Utilities (Imported & Adapted) ---
def rl_get_value_display_name(bet_type, bet_value): # Renamed prefix
    if bet_type == 'number': return f"🔢 {bet_value}" # Simpler display
    return RL_BET_VALUE_DISPLAY_NAMES.get(bet_value, bet_value)

def rl_get_color(n_str): # Renamed prefix
    if n_str in ['0', '00']: return 'Green'
    try:
        n = int(n_str)
        return 'Red' if n in RL_RED_NUMBERS else ('Black' if n in RL_BLACK_NUMBERS else None)
    except ValueError:
        return None

def rl_is_even_or_odd(n_str): # Renamed prefix
    if n_str in ['0', '00']: return None
    try:
        return 'Even' if int(n_str) % 2 == 0 else 'Odd'
    except ValueError:
        return None

def rl_get_dozen(n_str): # Renamed prefix
    if n_str in ['0', '00']: return None
    try:
        n = int(n_str)
        return next((name for name, d_set in RL_DOZENS.items() if n in d_set), None)
    except ValueError:
        return None

def rl_get_column(n_str): # Renamed prefix
    if n_str in ['0', '00']: return None
    try:
        n = int(n_str)
        return next((name for name, c_set in RL_COLUMNS.items() if n in c_set), None)
    except ValueError:
        return None

# --- Helper to get User Mention (HTML) (No Changes) ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    now = time.monotonic()
    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
            return cached['mention']
    try:
        # Use get_chat for better compatibility with user privacy settings
        user_chat = await context.bot.get_chat(user_id)
        mention = user_chat.mention_html() # Prefer mention_html
    except (BadRequest, Forbidden) as e: # Added Forbidden
         if "chat not found" in str(e).lower() or "user not found" in str(e).lower() or isinstance(e, Forbidden):
              mention = f"User_{user_id}" # More generic fallback
              logger.warning(f"Could not get chat for user {user_id} (likely deleted, invalid, or blocked): {e}")
         else:
              mention = f"User {user_id}" # Fallback on other errors
              logger.warning(f"Failed to get mention for {user_id} due to BadRequest: {e}")
    except Exception as e:
        mention = f"User {user_id}" # General fallback
        logger.warning(f"Failed to get mention for {user_id}: {e}")

    # Update cache outside lock after fetching
    async with _cache_lock:
        _user_mention_cache[user_id] = {'mention': mention, 'ts': now}
    return mention


# --- Core Bot Commands (Start, Help, Balance, Bonus, Leaderboard - Minor Changes) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id) # Ensure user exists in DB
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
        "/bonus - Получить бонус (раз в 6 часов, только в ЛС)\n"
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

    # Ensure user exists for balance check
    get_or_create_user(user.id)
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

    if not get_or_create_user(user.id): # Ensure user exists
        await update.message.reply_text("Ошибка: Не удалось найти или создать ваш профиль.")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_utc = get_last_bonus_time(user.id) # Already returns timezone-aware or None

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

    # Grant bonus
    new_balance = update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        update_last_bonus_time(user.id, now_utc) # Pass timezone-aware timestamp
        await update.message.reply_text(
            f"🎉 Поздравляем! Вы получили бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Ваш новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
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

    # Fetch mentions concurrently
    mentions = await asyncio.gather(*(get_user_mention(context, leader['user_id']) for leader in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        # Use fetched mention, fallback gracefully if needed
        name = mentions[i] if i < len(mentions) else f"User_{leader['user_id']}"
        balance_str = f"{leader['balance']:.2f}"
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"

    try:
        await update.message.reply_text(
            leaderboard_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True # Disable previews for user links
        )
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров.")

# --- Blackjack Game (Private Chat Only - WITH CORRECTED INDENTATION) ---

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"BJ /blackjack command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Играть в Блекджек можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    is_callback = update.callback_query is not None
    source_message = update.callback_query.message if is_callback else update.message
    callback_message_id = source_message.message_id if is_callback else None
    effective_chat_id = chat.id

    if is_callback:
        try:
            await update.callback_query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query in blackjack_start_command: {e}")

    user_game = context.user_data.get(BJ_GAME_KEY, {})
    previous_message_id = user_game.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(effective_chat_id, previous_message_id)
            logger.debug(f"Deleted previous BJ message {previous_message_id} for user {user.id}")
        except Exception as e:
            logger.debug(f"Failed to delete old BJ message {previous_message_id}: {e}")

    context.user_data.pop(BJ_GAME_KEY, None)

    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
    if balance is None:
         await source_message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.", parse_mode=ParseMode.HTML)
         return
    if balance <= 0:
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) недостаточен для игры. Попробуйте /bonus.", parse_mode=ParseMode.HTML)
        return

    bet_options = [1, 5, 10, 25, 50, 100, 250, 500, 1000]
    valid_bets = [b for b in bet_options if b <= balance]

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) меньше минимальной ставки (<b>{min_bet}</b> F).", parse_mode=ParseMode.HTML)
        return

    buttons = []
    row = []
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row: # Add remaining buttons
        buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)
    text = f"Ваш баланс: <b>{balance:.2f}</b> F.\nВыберите вашу ставку:"

    try:
        # Delete the callback message if this was triggered by a button ("New Game")
        if callback_message_id:
            try:
                await context.bot.delete_message(effective_chat_id, callback_message_id)
            except Exception as e:
                 logger.warning(f"Failed to delete callback message {callback_message_id} in blackjack_start_command: {e}")

        # Send the bet prompt message
        sent_message = await context.bot.send_message(
            chat_id=effective_chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )

        # Store initial game state (waiting for bet) and message ID
        context.user_data[BJ_GAME_KEY] = {
            'state': 'waiting_bet',
            'message_id': sent_message.message_id
        }
        logger.info(f"BJ bet prompt sent (msg {sent_message.message_id}) for user {user.id}")

    except Exception as e: # CORRECTED INDENTATION HERE
        logger.error(f"BJ start error sending bet prompt for user {user.id}: {e}", exc_info=True)
        try:
            await context.bot.send_message(effective_chat_id, "❌ Произошла ошибка при начале игры.")
        except Exception:
            pass # Ignore errors sending the error message


async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})
    bet_prompt_message_id = q.message.message_id

    # --- Validations ---
    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id:
        await q.answer("Эта игра больше неактивна.", show_alert=False)
        return

    balance = get_balance(uid)
    if balance is None:
        await q.answer("Ошибка получения баланса.", show_alert=True)
        return
    if bet <= 0 or bet > balance:
        await q.answer(f"Недопустимая ставка ({bet} F) или недостаточно средств ({balance:.2f} F).", show_alert=True)
        return

    # --- Deduct Bet ---
    if update_balance(uid, -bet) is None:
        await q.answer("Ошибка при списании ставки.", show_alert=True)
        return

    # --- Deal Initial Hands ---
    deck = create_deck()
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0

    try:
        for _ in range(2):
            # Player card
            card_p = draw_card(deck)
            if not card_p: raise IndexError("Deck empty during player deal")
            player_hand.append(card_p)
            cards_dealt_count += 1
            # Dealer card
            card_d = draw_card(deck)
            if not card_d: raise IndexError("Deck empty during dealer deal")
            dealer_hand.append(card_d)
            cards_dealt_count += 1
    except IndexError as e: # CORRECTED INDENTATION HERE
        logger.error(f"BJ dealing error for user {uid}: {e}")
        update_balance(uid, bet) # Refund bet on error
        try:
            await q.edit_message_text(f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.")
        except Exception:
            pass # Ignore if editing fails
        context.user_data.pop(BJ_GAME_KEY, None) # Clear game state
        return
    except Exception as e: # CORRECTED INDENTATION HERE
        logger.error(f"BJ unexpected dealing error for user {uid}: {e}", exc_info=True)
        update_balance(uid, bet) # Refund bet
        try:
             await q.edit_message_text(f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.")
        except Exception:
             pass
        context.user_data.pop(BJ_GAME_KEY, None)
        return

    # --- Check for Initial Blackjacks ---
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand) # Initial dealer value (before hiding)
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    game_state = 'player_turn'
    hand_status = 'active'
    outcome_text = None # For initial BJ results
    winnings = 0.0

    if player_has_blackjack:
        hand_status = 'blackjack'
        game_state = 'game_over' # Game ends immediately
        if dealer_has_blackjack:
            outcome_text = "⚖️ Ничья! У обоих Блекджек."
            update_balance(uid, bet) # Refund bet (push)
            winnings = bet # Payout is just the bet back
        else:
            bj_payout_amount = bet * BLACKJACK_PAYOUT
            update_balance(uid, bet + bj_payout_amount) # Refund bet + BJ payout
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {bj_payout_amount:.2f} F!"
            winnings = bet + bj_payout_amount
    elif dealer_has_blackjack:
        game_state = 'game_over' # Game ends immediately
        outcome_text = "😥 У дилера Блекджек! Вы проиграли."
        winnings = 0.0 # Bet was already deducted, no refund needed

    # --- Update Game State in user_data ---
    game.update({
        'state': game_state,
        'deck': deck,
        'cards_dealt': cards_dealt_count,
        'player_hands': [{ # Start with one hand
            'hand': player_hand,
            'bet': bet,
            'status': hand_status, # 'active', 'blackjack'
             # Allow double only if it's player's turn (no initial BJs) and hand has 2 cards
            'can_double': (game_state == 'player_turn' and len(player_hand) == 2),
            'can_split': False # Evaluated later in show_state
        }],
        'current_hand_index': 0,
        'dealer_hand': dealer_hand,
        'initial_bet': bet, # Store for reference if needed
        'split_count': 0,
        'outcome_text': outcome_text, # Store outcome if game ended due to BJ
        'outcome_determined': (game_state == 'game_over'), # Mark if outcome already set
        'total_winnings_paid': winnings if game_state == 'game_over' else 0.0 # Store initial BJ winnings
    })

    # --- Delete Bet Prompt and Show Game State ---
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e:
        logger.warning(f"Could not delete bet prompt message {bet_prompt_message_id}: {e}")

    # Send the initial game state message (or final if BJ occurred)
    new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=False)

    if new_message_info and isinstance(new_message_info, Message):
        # IMPORTANT: Update the message_id in game state if a new message was sent
        game['message_id'] = new_message_info.message_id
        logger.info(f"BJ initial state sent (msg {new_message_info.message_id}) for user {uid}. State: {game_state}")
        if game['outcome_determined']: # Clean up if game ended due to initial BJ
             context.user_data.pop(BJ_GAME_KEY, None)
             logger.info(f"BJ game state cleaned for user {uid} after initial Blackjack outcome.")

    elif not new_message_info: # CORRECTED INDENTATION HERE
        # Handle case where sending the game state failed
        logger.error(f"Failed to send initial BJ state for user {uid}")
        # Refund bet? Clean up game?
        update_balance(uid, bet) # Refund bet as game didn't start properly
        context.user_data.pop(BJ_GAME_KEY, None)
        await context.bot.send_message(chat_id, "❌ Ошибка отображения игры. Ставка возвращена.")
        return # Stop further execution

    await q.answer(f"Ставка принята: {bet} F")


async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, edit_existing: bool = True) -> Message | int | None:
    """ Edits or sends a message showing the current Blackjack game state. """
    is_new_send = False # Flag to track if we send a new message
    if game_state is None:
        game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state:
        logger.warning(f"blackjack_show_state called for user {user_id} but no game state found.")
        return None

    message_id_to_process = game_state.get('message_id')
    if edit_existing and not message_id_to_process:
        logger.error(f"BJ show_state: Attempted to edit but no message_id for user {user_id}. Will try sending new.")
        edit_existing = False # Force sending a new message

    # --- Fetch Data ---
    balance = get_balance(user_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands_data = game_state.get('player_hands', [])
    current_hand_idx = game_state.get('current_hand_index', -1)
    state = game_state.get('state', 'unknown')
    dealer_value = get_hand_value(dealer_hand)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2 and game_state.get('cards_dealt', 0) <= 4) # Check initial deal BJ

    # Hide dealer's second card only during player's turn AND if dealer doesn't have BJ revealed yet
    # Exception: Reveal card if player busted on all hands (no need to hide anymore)
    all_player_hands_finished = all(
        hdata.get('status') in ['bust', 'stand', 'blackjack'] for hdata in player_hands_data if isinstance(hdata, dict)
    )
    hide_dealer_card = (state == 'player_turn' and not dealer_has_blackjack and not all_player_hands_finished)


    # --- Build Text ---
    text = f"<b>Блекджек</b> | Баланс: <b>{balance_str}</b> F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands_data if isinstance(h, dict))
    num_hands = len(player_hands_data)
    text += f"Общая ставка: <b>{total_bet}</b> F{' (Рук: ' + str(num_hands) + ')' if num_hands > 1 else ''}\n"
    text += "--------------------\n" # Separator

    # Dealer's Hand
    dealer_value_display = "??" if not dealer_hand else (str(dealer_value) if not hide_dealer_card else f"{get_card_value(dealer_hand[0])}+?")
    text += f"<b>Диллер:</b> {format_hand(dealer_hand, hide_one=hide_dealer_card)} ({dealer_value_display})\n\n"

    # Player's Hand(s)
    text += "<b>Вы:</b>\n"
    active_hand_data = None # To determine buttons
    for i, hand_data in enumerate(player_hands_data):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', '?')
        hand_bet = hand_data.get('bet', 0)
        is_current_turn = (i == current_hand_idx and hand_status == 'active' and state == 'player_turn')

        indicator = "▶️" if is_current_turn else \
                    "✅" if hand_status == 'stand' else \
                    "❌" if hand_status == 'bust' else \
                    "💰" if hand_status == 'blackjack' else \
                    "▫️" # Default/waiting

        text += f"{indicator} Рука {i+1}: {format_hand(hand)} (<b>{hand_value}</b>) [<i>{hand_bet} F</i>]"

        # Add status text for non-active hands or final states
        status_label = ""
        if hand_status == 'bust': status_label = " - <b>Перебор!</b>"
        elif hand_status == 'blackjack': status_label = " - <b>Блекджек!</b>"
        elif hand_status == 'stand' and not is_current_turn: status_label = " - <i>Стоп</i>"
        text += status_label + "\n"

        if is_current_turn:
            active_hand_data = hand_data # Store data for button logic

    # --- Build Keyboard ---
    keyboard = []
    if active_hand_data and state == 'player_turn':
        player_hand = active_hand_data.get('hand', [])
        player_bet = active_hand_data.get('bet', 0)
        # Check Double possibility
        can_double = (active_hand_data.get('can_double', False) and
                      len(player_hand) == 2 and
                      balance is not None and balance >= player_bet)
        # Check Split possibility
        can_split = (len(player_hand) == 2 and
                     player_hand[0] and player_hand[1] and
                     get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
                     balance is not None and balance >= player_bet and
                     game_state.get('split_count', 0) < MAX_SPLITS)
        active_hand_data['can_split'] = can_split # Update possibility in state for handle_action

        action_buttons = [
            InlineKeyboardButton("Еще", callback_data=f"bj_hit_{current_hand_idx}"),
            InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{current_hand_idx}")
        ]
        keyboard.append(action_buttons)

        special_buttons = []
        if can_double:
            special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{current_hand_idx}"))
        if can_split:
            special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{current_hand_idx}"))
        if special_buttons:
            keyboard.append(special_buttons)

    elif state == 'game_over':
        text += f"\n<b>Игра окончена!</b>\n{game_state.get('outcome_text', 'Результат не определен.')}\n"
        final_balance = get_balance(user_id)
        text += f"\nИтоговый баланс: <b>{final_balance:.2f}</b> F." if final_balance is not None else ""
        keyboard.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
    elif state == 'dealer_turn':
        text += "\n<i>⏳ Ход дилера...</i>"
        # No buttons during dealer's turn

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Send or Edit Message ---
    result: Message | int | None = None
    max_retries = 1 # Max attempts to edit before sending new
    current_retry = 0

    while current_retry <= max_retries:
        try:
            if edit_existing and message_id_to_process:
                # Edit existing message
                logger.debug(f"Attempting edit (try {current_retry+1}) BJ state msg {message_id_to_process} for user {user_id}")
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id_to_process,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                logger.debug(f"Successfully edited BJ state msg {message_id_to_process}")
                result = message_id_to_process # Return message ID on successful edit
                break # Success, exit loop

            else:
                # Send new message
                is_new_send = True # Mark that we are sending new
                logger.debug(f"Sending NEW BJ state message for user {user_id}")
                if message_id_to_process: # Attempt to delete old message if sending new (CORRECTED INDENTATION)
                    try:
                        await context.bot.delete_message(chat_id, message_id_to_process)
                    except Exception:
                        pass # Ignore deletion errors

                new_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                # IMPORTANT: Update message ID in game state
                game_state['message_id'] = new_message.message_id
                logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}. Updated game state.")
                result = new_message # Return the new Message object
                break # Success, exit loop

        except BadRequest as e: # CORRECTED INDENTATION AND LOGIC
            error_str = str(e).lower()
            if "message is not modified" in error_str:
                result = message_id_to_process # No change needed, treat as success
                logger.debug(f"BJ state msg {message_id_to_process} not modified.")
                break # Success, exit loop
            elif "message to edit not found" in error_str or "chat not found" in error_str or "message can't be edited" in error_str:
                 logger.error(f"Message {message_id_to_process} or Chat {chat_id} not found/editable for user {user_id}. Forcing send new.")
                 edit_existing = False # Force send new on next retry (if any)
                 message_id_to_process = None # Clear invalid ID
                 game_state['message_id'] = None # Clear from game state too
                 current_retry += 1
                 if current_retry > max_retries: # If already tried sending new, clean up
                    logger.error(f"CRITICAL: Failed to send new message after edit failed for user {user_id}. Cleaning game state.")
                    context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
                    result = None
                 # No break here, loop will retry with edit_existing=False
            elif "can't parse entities" in error_str:
                logger.error(f"HTML Parsing Error for user {user_id} (msg {message_id_to_process}): {e}\nText snippet: {text[:200]}...") # Log error and part of the text
                result = None # Indicate failure, don't retry parse error
                break # Exit loop
            else:
                logger.warning(f"Edit/Send BJ state failed for user {user_id} (msg {message_id_to_process}) (try {current_retry+1}): {e}")
                result = None # Indicate other failure
                current_retry += 1 # Retry generic BadRequest maybe?
                await asyncio.sleep(0.5) # Small delay before retry

        except Forbidden as e: # CORRECTED INDENTATION
            logger.error(f"Forbidden error for user {user_id} in chat {chat_id} (likely blocked): {e}")
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up broken game
            result = None
            break # Exit loop, clean state

        except Exception as e: # CORRECTED INDENTATION
            logger.error(f"Unexpected error in blackjack_show_state for user {user_id} (try {current_retry+1}): {e}", exc_info=True)
            result = None # Indicate failure
            current_retry += 1 # Retry unexpected errors
            await asyncio.sleep(0.5)

    # If result is None after retries, log final failure
    if result is None and not is_new_send: # Check is_new_send to avoid logging failure if send_message failed initially
        logger.error(f"Failed to update BJ state for user {user_id} after retries.")
    elif result is None and is_new_send:
         logger.error(f"Failed to send initial BJ state for user {user_id} after edit failure.")

    # Return Message object if new, message_id if edited, None if failed
    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})

    # Robust check for parts length
    if len(parts) < 2:
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

    action_message_id = q.message.message_id

    # --- Validations ---
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != action_message_id:
        await q.answer("Эта игра или действие больше неактивны.", show_alert=False)
        # Attempt to delete buttons if message still exists
        if game.get('message_id') == action_message_id: # CORRECTED INDENTATION
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=action_message_id, reply_markup=None)
            except Exception:
                pass # Ignore if fails
        return

    player_hands = game.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)) or hand_index != game.get('current_hand_index', -1):
        await q.answer("Сейчас ход другой руки.", show_alert=False)
        return

    current_hand_data = player_hands[hand_index]
    if not isinstance(current_hand_data, dict) or current_hand_data.get('status') != 'active':
        await q.answer("Эта рука неактивна для действий.", show_alert=False)
        return

    # --- Get Required Data ---
    hand = current_hand_data.get('hand', [])
    deck = game.get('deck', [])
    balance = get_balance(uid) # Get current balance for checks
    bet = current_hand_data.get('bet', 0)

    # --- Execute Action ---
    needs_state_update = False
    move_to_next = False # Flag to check if turn for this hand ended

    try:
        if action == 'hit':
            card = draw_card(deck)
            if card:
                hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                current_hand_data['can_double'] = False # Cannot double/split after hit
                current_hand_data['can_split'] = False
                hand_value = get_hand_value(hand)
                await q.answer(f"Взяли: {card[0]}{card[1]}")
                needs_state_update = True # Always update after hit

                if hand_value > 21:
                    current_hand_data['status'] = 'bust'
                    move_to_next = True
                elif hand_value == 21:
                    current_hand_data['status'] = 'stand' # Auto-stand on 21
                    move_to_next = True
                # Else: hand_value < 21, turn continues, move_to_next remains False
            else:
                raise IndexError("Draw fail (deck empty or error)")

        elif action == 'stand':
            current_hand_data['status'] = 'stand'
            await q.answer("Стоп.")
            needs_state_update = True
            move_to_next = True

        elif action == 'double':
            can_double = (current_hand_data.get('can_double', False) and
                          len(hand) == 2 and
                          balance is not None and balance >= bet)
            if can_double:
                new_balance = update_balance(uid, -bet) # Deduct first
                if new_balance is not None:
                    current_hand_data['bet'] += bet # Double the bet amount
                    current_hand_data['can_double'] = False
                    current_hand_data['can_split'] = False
                    balance = new_balance # Update local balance variable

                    card = draw_card(deck)
                    drawn_card_str = ""
                    if card:
                        hand.append(card)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                        hand_value = get_hand_value(hand)
                        current_hand_data['status'] = 'bust' if hand_value > 21 else 'stand'
                        drawn_card_str = f" Карта: {card[0]}{card[1]}. Итог: {hand_value}{' (Перебор!)' if hand_value > 21 else ''}"
                    else: # Failed to draw card
                        current_hand_data['status'] = 'stand' # Stand with original 2 cards
                        drawn_card_str = " Ошибка взятия карты."
                        logger.warning(f"BJ double failed draw for user {uid}")

                    await q.answer(f"Удвоено!{drawn_card_str}", show_alert=("Ошибка" in drawn_card_str))
                    needs_state_update = True
                    move_to_next = True
                else:
                    await q.answer("Ошибка списания средств для удвоения.", show_alert=True)
            else:
                await q.answer("Удвоить сейчас нельзя.", show_alert=True)

        elif action == 'split':
            # Re-check split possibility right before execution
            can_split = (len(hand) == 2 and
                         hand[0] and hand[1] and
                         get_card_value(hand[0]) == get_card_value(hand[1]) and
                         balance is not None and balance >= bet and
                         game.get('split_count', 0) < MAX_SPLITS)
            # Update 'can_split' in hand_data based on this fresh check
            current_hand_data['can_split'] = can_split

            if can_split:
                new_balance = update_balance(uid, -bet) # Deduct bet for new hand
                if new_balance is not None:
                    game['split_count'] = game.get('split_count', 0) + 1
                    balance = new_balance # Update local balance variable
                    card_to_move = hand.pop() # Take second card for the new hand

                    # Prepare new hand data
                    new_hand_data = {
                        'hand': [card_to_move],
                        'bet': bet,
                        'status': 'active',
                        'can_double': False, # Will be updated after draw
                        'can_split': False  # Will be updated after draw
                    }

                    # Deal one card to each split hand
                    cards_drawn = [draw_card(deck), draw_card(deck)]
                    drawn_count = 0
                    if cards_drawn[0]:
                         hand.append(cards_drawn[0])
                         drawn_count += 1
                    if cards_drawn[1]:
                         new_hand_data['hand'].append(cards_drawn[1])
                         drawn_count += 1

                    if drawn_count < 2: # Check if deck ran out during split deal
                         logger.warning(f"BJ split failed to draw both cards for user {uid}. Deck empty?")
                         # Decide how to handle: Maybe revert split? Force stand?
                         # For now, let's force stand on both if draw failed
                         current_hand_data['status'] = 'stand'
                         new_hand_data['status'] = 'stand'
                         # Put card back? Refund bet? Needs careful thought.
                         # Simplest: just stand both, bet is already taken.
                         update_balance(uid, bet) # Refund the second bet as split failed
                         game['split_count'] -= 1
                         hand.append(card_to_move) # Put card back
                         await q.answer("Ошибка разделения: не хватило карт! Ставка возвращена.", show_alert=True)
                         needs_state_update = True
                         move_to_next = True # End turn for original hand
                         # Don't insert the new hand data
                         # Restore split capability if needed (though turn ends now)
                         # This check is complex and likely not needed as turn ends
                         # current_hand_data['can_split'] = (len(hand) == 2 and ...)
                         current_hand_data['can_double'] = (len(hand) == 2)

                    else:
                        # Insert the fully formed new hand AFTER the current one
                        player_hands.insert(hand_index + 1, new_hand_data)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + drawn_count

                        # Handle Ace split rule (stand immediately)
                        is_ace_split = get_card_value(hand[0] if hand else None) == 11
                        if is_ace_split:
                            current_hand_data['status'] = 'stand'
                            new_hand_data['status'] = 'stand'
                            current_hand_data['can_double'] = False
                            new_hand_data['can_double'] = False
                            await q.answer("Тузы разделены и стоят.")
                            needs_state_update = True
                            # Do NOT set move_to_next = True here.
                            # The current hand (index 0) stands, but the logic in
                            # blackjack_next_action will correctly move to the newly
                            # inserted hand (index 1) if it's active (which it isn't here).
                            # We need to manually trigger next action check after update.
                        else:
                            # Check for 21 on deal for non-Ace splits
                            if get_hand_value(hand) == 21: current_hand_data['status'] = 'stand'
                            if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                            # Set initial double/split possibilities for new hands after draw
                            current_hand_data['can_double'] = (len(hand) == 2 and current_hand_data['status'] == 'active')
                            new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2 and new_hand_data['status'] == 'active')

                            # Re-check split possibility (recursive split) - needs balance check too
                            limit_ok = game.get('split_count', 0) < MAX_SPLITS
                            current_balance_after_split = balance # Use updated balance

                            chd_can_resplit = (current_hand_data['status'] == 'active' and len(hand) == 2 and hand[0] and hand[1] and
                                               get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok and
                                               current_balance_after_split >= current_hand_data['bet'])
                            nhd_can_resplit = (new_hand_data['status'] == 'active' and len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and
                                               get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and
                                               current_balance_after_split >= new_hand_data['bet'])
                            current_hand_data['can_split'] = chd_can_resplit
                            new_hand_data['can_split'] = nhd_can_resplit

                            await q.answer("Рука разделена!")
                            needs_state_update = True
                            # move_to_next remains False, turn continues on the first split hand (hand_index)
                            # unless it auto-stood on 21
                            if current_hand_data['status'] == 'stand':
                                move_to_next = True

                else:
                    await q.answer("Ошибка списания средств для разделения.", show_alert=True)
            else:
                await q.answer("Разделить сейчас нельзя.", show_alert=True)

    except IndexError as e: # Catch draw failure (CORRECTED INDENTATION)
        logger.warning(f"BJ action '{action}' user {uid} failed draw: {e}")
        current_hand_data['status'] = 'stand' # Force stand if draw fails mid-action
        await q.answer("Не удалось взять карту! Ход завершен.", show_alert=True)
        needs_state_update = True
        move_to_next = True
    except Exception as e: # CORRECTED INDENTATION
        logger.error(f"BJ action '{action}' user {uid} unexpected error: {e}", exc_info=True)
        await q.answer("Произошла непредвиденная ошибка.", show_alert=True)
        # Force stand and move next on unexpected errors
        current_hand_data['status'] = 'stand'
        needs_state_update = True
        move_to_next = True


    # --- Update State Message ---
    if needs_state_update:
        update_result = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        if not update_result:
             logger.error(f"Failed to update state message after action '{action}' for user {uid}. Game might be stuck.")
             # Attempt to send a message to the user about the error (CORRECTED INDENTATION)
             try:
                 await context.bot.send_message(chat_id, "⚠️ Ошибка обновления отображения игры. Состояние может быть некорректным.")
             except Exception:
                 pass
             # If state update fails, maybe don't proceed to next action? Or risk it?
             # Let's try proceeding, but the display might be wrong.

    # --- Move to Next Action if Hand Ended ---
    # Special handling for Ace split: trigger next action check even if move_to_next wasn't set by the stand itself
    is_ace_split_action = (action == 'split' and get_card_value(hand[0] if hand else None) == 11)

    if move_to_next or is_ace_split_action:
        # Use run_once to detach from the callback handler, preventing potential timeouts
        # and allowing the callback answer to return quickly.
        context.job_queue.run_once(
             blackjack_next_action_job,
             when=0.1, # Short delay, almost immediate
             data={'chat_id': chat_id, 'user_id': uid},
             name=f"next_action_{uid}_{action_message_id}"
         )


async def blackjack_next_action_job(context: ContextTypes.DEFAULT_TYPE):
    """ Job wrapper for blackjack_next_action to detach from callback handler. """
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    if user_id and chat_id:
        await blackjack_next_action(context, chat_id, user_id)
    else:
        logger.error(f"Missing data in blackjack_next_action_job: {job_data}")


async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """ Determines the next step after a player's hand finishes its turn (stand/bust/double/ace split). """
    # Use application.user_data directly as context.user_data might be scoped differently in jobs
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game:
        logger.info(f"BJ next_action (job) for user {user_id}: Game state not found. Aborting.")
        return
    if game.get('state') != 'player_turn':
        logger.info(f"BJ next_action (job) for user {user_id}: Game state is not 'player_turn' ({game.get('state')}). Aborting.")
        return

    player_hands = game.get('player_hands', [])
    current_hand_idx = game.get('current_hand_index', -1)

    # Find the index of the *next* hand that is still 'active'
    next_active_idx = -1
    for i in range(current_hand_idx + 1, len(player_hands)):
        # Add robust check for hand data structure
        hand_data = player_hands[i]
        if isinstance(hand_data, dict) and hand_data.get('status') == 'active':
            next_active_idx = i
            break

    message_id = game.get('message_id')
    if not message_id:
        logger.error(f"BJ next_action: No message_id for user {user_id}. Cannot proceed.")
        # Clean up?
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        return

    if next_active_idx != -1:
        # Found another active player hand, switch to it
        game['current_hand_index'] = next_active_idx
        logger.info(f"BJ user {user_id}: Moving to next active hand index {next_active_idx}")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
    else:
        # No more active player hands, move to dealer's turn
        logger.info(f"BJ user {user_id}: All player hands done, moving to dealer's turn.")
        game['state'] = 'dealer_turn'
        # Update message to show "Dealer's turn..." and remove player buttons
        update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

        if not update_success: # CORRECTED INDENTATION
            logger.error(f"BJ user {user_id}: Failed to update state to 'dealer_turn'. Aborting dealer job.")
            # Maybe try sending a message? Clean up?
            try:
                await context.bot.send_message(chat_id, "⚠️ Ошибка! Не удалось перейти к ходу дилера.")
            except Exception:
                pass
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up broken game
            return

        # Schedule the dealer logic job
        context.job_queue.run_once(
            blackjack_dealer_turn_job,
            DEALER_TURN_DELAY, # Use the defined delay
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': message_id}, # Pass message_id too
            name=f"dealer_turn_{user_id}_{message_id}" # Unique job name
        )

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """ Handles the dealer's turn logic (drawing cards). """
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id') # Get message_id from job data

    if not user_id or not chat_id or not message_id:
        logger.error(f"BJ Dealer job missing required data: {job_data}")
        return

    # Use application.user_data for jobs
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    # --- Validations ---
    if not game:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state not found. Job aborted.")
        return
    if game.get('state') != 'dealer_turn':
         logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state is not 'dealer_turn' ({game.get('state')}). Job aborted.")
         return
    if game.get('message_id') != message_id:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Message ID mismatch (game has {game.get('message_id')}). Job aborted.")
        return

    # --- Get Data ---
    deck = game.get('deck', [])
    dealer_hand = game.get('dealer_hand', [])
    player_hands = game.get('player_hands', [])
    dealer_value_initial = get_hand_value(dealer_hand) # Value before hitting
    dealer_had_initial_blackjack = (dealer_value_initial == 21 and len(dealer_hand) == 2 and game.get('cards_dealt', 0) <= 4) # Was it an initial BJ?

    # Determine if dealer *must* play (i.e., if any player hand could potentially win)
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] # Player BJ handled separately
        for h in player_hands
    )

    # Dealer only hits if a player *could* win (didn't bust/get BJ) AND dealer doesn't already have 21+
    dealer_needs_to_hit = player_can_win and get_hand_value(dealer_hand) < 21

    dealer_stood = False
    hit_occurred = False # Track if dealer actually took a card

    if dealer_needs_to_hit:
        # --- Dealer Drawing Loop ---
        logger.info(f"BJ Dealer user {user_id} starts hitting sequence.")
        while not dealer_stood:
            current_dealer_value = get_hand_value(dealer_hand)
            num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
            # Correct Soft 17 check: has an Ace counted as 11
            is_soft = num_aces > 0 and (current_dealer_value - (num_aces * 10)) <= 11

            # Dealer Stand Conditions
            stand_value_met = False
            if current_dealer_value > 17:
                stand_value_met = True
            elif current_dealer_value == 17:
                if not (is_soft and DEALER_HITS_SOFT_17): # Stand on hard 17, or soft 17 if rule is False
                    stand_value_met = True

            if stand_value_met or current_dealer_value >= 21: # Also stand/stop on 21 or bust
                if not hit_occurred: # Log stand only if no hits occurred before this check
                     logger.info(f"BJ Dealer user {user_id} stands initially on {current_dealer_value}{' (soft)' if is_soft and current_dealer_value==17 else ''}.")
                else:
                    logger.info(f"BJ Dealer user {user_id} stands on {current_dealer_value}.")
                dealer_stood = True
                break # Exit the while loop

            # Dealer Hits
            logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}{' (soft)' if is_soft else ''}.")
            card = draw_card(deck)
            if card:
                dealer_hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                hit_occurred = True # Mark that a hit happened
                # ** NO state update here to avoid flicker **
                await asyncio.sleep(DEALER_TURN_DELAY * 0.6) # Small delay between hits
            else:
                logger.warning(f"BJ Dealer user {user_id} failed to draw card (deck empty?). Standing.")
                dealer_stood = True # Stop hitting if card cannot be drawn
                break # Exit the while loop
    else:
        # Dealer doesn't need to hit
        final_dealer_value_no_hit = get_hand_value(dealer_hand)
        logger.info(f"BJ Dealer user {user_id}: No player can win or dealer already has >= 21 ({final_dealer_value_no_hit}). Dealer stands immediately.")
        dealer_stood = True


    # --- AFTER the loop (or skipped) ---
    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn with value {final_dealer_value}. Updating display before outcome.")

    # *** KEY CHANGE: Update display to show final dealer hand ***
    # The state is still technically 'dealer_turn' until determine_outcome changes it,
    # but show_state will reveal the card because it's not 'player_turn'.
    update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    # Check if update failed; if so, maybe abort outcome?
    if not update_success: # CORRECTED INDENTATION
        logger.error(f"BJ Dealer user {user_id}: Failed to show final dealer hand. Aborting outcome calculation.")
        # Maybe try to send an error message? Clean up?
        try:
            await context.bot.send_message(chat_id, "⚠️ Ошибка отображения хода дилера. Игра завершена некорректно.")
        except Exception:
            pass
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up broken game
        return


    # *** KEY CHANGE: Optional delay so user can see the revealed hand/result of hits ***
    await asyncio.sleep(DEALER_TURN_DELAY * 0.8) # Adjust delay as needed

    # --- Determine Outcome ---
    # Pass the initial BJ status, as that's what matters for player BJ payout/push
    # We need the initial dealer BJ status for correct payout rules
    await blackjack_determine_outcome(context, chat_id, user_id, dealer_had_initial_blackjack)

async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    """ Calculates results for each hand, updates balance, shows final state, and cleans up. """
    # Use application.user_data for jobs/tasks detached from original context
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game:
        logger.warning(f"BJ outcome user {user_id}: Game data not found.")
        return

    message_id = game.get('message_id')
    if not message_id:
        logger.error(f"BJ outcome user {user_id}: No message_id found.")
        # Attempt cleanup even without message_id?
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        return

    # Prevent double processing (using a flag set just before cleanup)
    if game.get('outcome_determined'):
        logger.info(f"BJ outcome user {user_id}: Outcome already determined. Skipping redundant calculation.")
        # Ensure final state is shown again if needed (e.g., if user triggers somehow)
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
        return

    # --- Get Final Data ---
    player_hands = game.get('player_hands', [])
    dealer_hand = game.get('dealer_hand', [])
    dealer_final_value = get_hand_value(dealer_hand)
    dealer_busted = dealer_final_value > 21

    outcome_lines = []
    total_winnings_to_pay = 0 # Tracks only the amount to PAY the user (bets + profits)
    total_initial_bet_sum = 0 # Sum of all initial bets placed across hands

    # --- Calculate Outcome per Hand ---
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue

        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status')
        player_value = get_hand_value(hand)
        player_had_blackjack = (status == 'blackjack') # Handled during deal if player got BJ

        total_initial_bet_sum += bet # Track total bet amount *before* outcome

        payout_amount = 0 # Amount to return to player for THIS hand (0=loss, bet=push, bet*2=win, bet*(1+BJ_PAYOUT)=BJ win)
        outcome_str = ""
        prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

        if status == 'bust':
            outcome_str = f"{prefix}Перебор ({player_value}). Ставка проиграна (-{bet} F)."
            payout_amount = 0
        elif player_had_blackjack: # Player had initial BJ
            if d_had_bj: # Dealer also had initial BJ
                outcome_str = f"{prefix}Блекджек! Ничья с дилером."
                payout_amount = bet # Push (bet returned)
            else:
                win_amount = bet * BLACKJACK_PAYOUT
                outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."
                payout_amount = bet + win_amount # Bet returned + BJ payout
        elif d_had_bj: # Dealer had initial BJ, player didn't
            outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{bet} F)."
            payout_amount = 0
        elif dealer_busted: # Dealer busted, player didn't have BJ and didn't bust
            outcome_str = f"{prefix}У дилера перебор ({dealer_final_value})! Выигрыш +{bet:.2f} F."
            payout_amount = bet * 2 # Bet returned + winnings
        elif player_value > dealer_final_value: # Player wins
            outcome_str = f"{prefix}Вы выиграли ({player_value} {html_escape('>')}) {dealer_final_value}. Выигрыш +{bet:.2f} F."
            payout_amount = bet * 2
        elif player_value == dealer_final_value: # Push
            outcome_str = f"{prefix}Ничья ({player_value} = {dealer_final_value}). Ставка возвращена."
            payout_amount = bet # Push
        else: # player_value < dealer_final_value, Player loses
            outcome_str = f"{prefix}Вы проиграли ({player_value} {html_escape('<')} {dealer_final_value}). Ставка проиграна (-{bet} F)."
            payout_amount = 0

        outcome_lines.append(outcome_str)
        total_winnings_to_pay += payout_amount # Accumulate total amount to be paid back

    # --- Calculate Net Change and Update Balance ---
    # Net change = (Total Payout) - (Sum of Bets Placed)
    net_change = total_winnings_to_pay - total_initial_bet_sum

    balance_updated_ok = True
    if total_winnings_to_pay > 0:
        current_balance_before_update = get_balance(user_id) # For logging
        if update_balance(user_id, total_winnings_to_pay) is None:
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! ❌</b>")
            # If payout fails, the net change IS the loss of the initial bets
            net_change = -total_initial_bet_sum # Correct net change if payout failed
            balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED to update balance with payout {total_winnings_to_pay}. Initial bet sum was {total_initial_bet_sum}. Balance before attempt: {current_balance_before_update}")
        else:
            logger.info(f"BJ outcome user {user_id}: Balance updated by adding {total_winnings_to_pay:.2f}. Net change for round: {net_change:+.2f}")
    else:
        # No winnings to pay, balance already reflects deducted bets
        logger.info(f"BJ outcome user {user_id}: No winnings to pay. Net change: {net_change:+.2f}")


    # --- Finalize Game State ---
    game['state'] = 'game_over'
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True # Mark as determined *before* final show_state

    # --- Show Final State ---
    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    # --- Clean Up Game Data ---
    # Only clean up if balance update was successful OR there were no winnings to pay
    if balance_updated_ok:
        # Use pop with default to avoid KeyError if already cleaned elsewhere
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        logger.info(f"BJ game state cleaned for user {user_id}")
    else:
        logger.warning(f"BJ game state NOT cleaned for user {user_id} due to balance update error. Game state kept for potential review.")


# --- Roulette Game (NEW - Adapted for Group/Private & Chat Data - CHECKED INDENTATION) ---
# --- All rl_ functions below have been checked for indentation issues ---

# --- Roulette Keyboards (Adapted with rl_ prefix) ---
def rl_get_main_menu_keyboard(chat_data: dict, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    game_state = chat_data.get(RL_GAME_KEY, {})
    state = game_state.get('state', 'idle') #'idle', 'accepting_bets', 'spinning', 'finished'
    active_bets_by_user = game_state.get('active_bets', {}) # {user_id: [bet_dict, ...]}

    keyboard = []
    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    total_bet_amount = sum(b['amount'] for bets in active_bets_by_user.values() for b in bets)

    # Display Active Bets (Grouped by User)
    if active_bets_by_user:
         bet_lines = []
         user_ids = list(active_bets_by_user.keys())
         # --- Fetch mentions asynchronously within the main loop ---
         # We need to run the async function get_user_mention properly.
         # Since this function itself isn't async, we use asyncio.run_coroutine_threadsafe
         # if running in a separate thread, or ensure get_main_menu_keyboard
         # is called from an async context. For simplicity here, we assume
         # it's called where we can await. If called synchronously, this part needs adjustment.
         # A common pattern is to pre-fetch mentions if possible or display IDs as fallback.
         # Let's fetch them here assuming an async context for rl_show_game_state.
         # NOTE: This fetch will block if called synchronously. Best practice is to call
         # get_main_menu_keyboard from within an async function.
         # Simplified sync call for now (might block):
         try:
             mentions = asyncio.run(asyncio.gather(*(get_user_mention(context, uid) for uid in user_ids)))
             mention_map = dict(zip(user_ids, mentions))
         except RuntimeError: # Cannot run nested event loops
             logger.warning("Could not fetch user mentions synchronously for roulette keyboard. Displaying IDs.")
             mention_map = {uid: f"User_{uid}" for uid in user_ids}


         keyboard.append([InlineKeyboardButton("📝 Текущие ставки:", callback_data='rl_noop')]) # Header
         for user_id, bets in active_bets_by_user.items():
             user_mention = mention_map.get(user_id, f"User_{user_id}")
             bet_str = ", ".join([f"{b['value_display']} ({b['amount']}F)" for b in bets])
             # Ensure button text isn't excessively long
             button_text = f"{user_mention}: {bet_str}"
             if len(button_text) > 60: # Arbitrary limit for button text
                 button_text = button_text[:57] + "..."
             keyboard.append([InlineKeyboardButton(button_text, callback_data='rl_noop')])
         keyboard.append([InlineKeyboardButton(f"💰 Общая сумма: {total_bet_amount} F ({total_bets_count}/{RL_MAX_BETS_PER_ROUND} ставок)", callback_data='rl_noop')])
         keyboard.append([InlineKeyboardButton("---", callback_data='rl_noop')])


    # Action Buttons based on state
    if state == 'accepting_bets':
        if total_bets_count < RL_MAX_BETS_PER_ROUND:
            keyboard.append([InlineKeyboardButton("➕ Добавить ставку", callback_data='rl_start_bet')])
        else:
            keyboard.append([InlineKeyboardButton("🚫 Лимит ставок раунда достигнут", callback_data='rl_noop')])
        # Spin button always available if bets exist
        if active_bets_by_user:
            keyboard.append([InlineKeyboardButton("🎰 Крутить!", callback_data='rl_spin')])
    elif state == 'idle':
         keyboard.append([InlineKeyboardButton("▶️ Начать раунд (сделать ставку)", callback_data='rl_start_bet')])
    elif state == 'spinning':
        keyboard.append([InlineKeyboardButton("⏳ Колесо вращается...", callback_data='rl_noop')])
    elif state == 'finished':
        # After results are shown, this state might be used briefly before resetting to idle
        # Or we could immediately reset to idle in spin_logic
         keyboard.append([InlineKeyboardButton("🔄 Начать новый раунд (/roulette)", callback_data='rl_noop')]) # Info only

    # Always show help
    keyboard.append([InlineKeyboardButton("❓ Правила Рулетки", callback_data='rl_show_help')])

    return InlineKeyboardMarkup(keyboard)

def rl_get_bet_type_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [ InlineKeyboardButton("🔢 Число", callback_data='rl_type_number'), InlineKeyboardButton("🎨 Цвет", callback_data='rl_type_color') ],
        [ InlineKeyboardButton("⚖️ Чет/Нечет", callback_data='rl_type_parity'), InlineKeyboardButton("📦 Дюжина", callback_data='rl_type_dozen') ],
        [ InlineKeyboardButton("📊 Колонка", callback_data='rl_type_column') ],
        [ InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step') ],
    ]
    return InlineKeyboardMarkup(keyboard)

def rl_get_bet_value_keyboard(bet_type: str) -> InlineKeyboardMarkup:
    kb_rows = []
    if bet_type == 'color': kb_rows = [[ InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Red'], callback_data='rl_value_Red'), InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Black'], callback_data='rl_value_Black') ]]
    elif bet_type == 'parity': kb_rows = [[ InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Even'], callback_data='rl_value_Even'), InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['Odd'], callback_data='rl_value_Odd') ]]
    elif bet_type == 'dozen': kb_rows = [ [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['1st'], callback_data='rl_value_1st')], [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['2nd'], callback_data='rl_value_2nd')], [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['3rd'], callback_data='rl_value_3rd')] ]
    elif bet_type == 'column': kb_rows = [ [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col1'], callback_data='rl_value_col1')], [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col2'], callback_data='rl_value_col2')], [InlineKeyboardButton(RL_BET_VALUE_DISPLAY_NAMES['col3'], callback_data='rl_value_col3')] ]
    kb_rows.append([InlineKeyboardButton("⬅️ Назад (к типу)", callback_data='rl_back_to_bet_type')])
    kb_rows.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(kb_rows)

def rl_get_bet_amount_keyboard(balance: float, bet_type: str) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    # Filter amounts based on balance
    valid_amounts = [a for a in RL_BET_AMOUNTS if a <= balance]
    for amount in valid_amounts:
        row.append(InlineKeyboardButton(str(amount), callback_data=f'rl_amount_{amount}'))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Add back button depending on previous step
    back_cb = 'rl_back_to_bet_type' if bet_type == 'number' else 'rl_back_to_bet_value'
    back_txt = "⬅️ Назад (к типу)" if bet_type == 'number' else "⬅️ Назад (к значению)"
    keyboard.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(keyboard)

def rl_get_confirmation_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [ InlineKeyboardButton("✅ Да, поставить!", callback_data='rl_confirm_bet_yes'), InlineKeyboardButton("✏️ Нет, изменить", callback_data='rl_back_to_bet_type') ], # Back always goes to type for simplicity now
         [InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Roulette Job Queue Functions ---
async def rl_spin_roulette_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Function called by the timer to automatically start the spin."""
    job_context = context.job.data
    chat_id = job_context.get('chat_id')
    # We don't need user_id here specifically, game state is in chat_data

    if not chat_id:
        logger.error(f"Roulette timer job missing chat_id: {job_context}")
        return

    logger.info(f"Roulette betting timer expired for chat_id={chat_id}")

    # Get current chat data
    # NOTE: Use application.chat_data for jobs
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # Validate state
    if not game_state or game_state.get('state') != 'accepting_bets':
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but game not in 'accepting_bets' state ({game_state.get('state') if game_state else 'No Game'}). Aborting.")
        # Clean up the job reference just in case
        if game_state:
            game_state.pop('timer_job_name', None)
        return

    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user:
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but no bets placed. Ending round.")
        game_state['state'] = 'idle' # Reset state
        game_state.pop('timer_job_name', None)
        # Optionally update the message
        message_id = game_state.get('message_id')
        if message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⏳ Время для ставок истекло. Ставок не было.\nНачните новый раунд /roulette",
                    reply_markup=None # Remove buttons
                )
            except Exception as e:
                logger.warning(f"Could not edit message in chat {chat_id} after timer expired with no bets: {e}")
        return

    # --- Start the spin logic ---
    logger.info(f"Roulette timer starting spin for chat {chat_id}")
    # Clear timer job reference *before* starting spin
    game_state.pop('timer_job_name', None)
    await rl_spin_roulette_logic(context, chat_id)


async def rl_remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Removes a job by name if it exists."""
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

# --- Roulette Main Command & State Update ---
async def roulette_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a new roulette round or shows the current one."""
    chat = update.effective_chat
    user = update.effective_user # User initiating the command
    chat_id = chat.id
    logger.info(f"/roulette command from user {user.id} in chat {chat_id} (type: {chat.type})")

    # Ensure initiator exists in DB for potential betting
    get_or_create_user(user.id)

    # --- Get or Initialize Game State in chat_data ---
    chat_data = context.chat_data # Use chat_data for shared state
    game_state = chat_data.get(RL_GAME_KEY)

    # --- If game is active, just show status ---
    # Check for 'spinning' state specifically to avoid interrupting animation with a new message
    if game_state and game_state.get('state') == 'spinning':
         logger.info(f"Roulette game currently spinning in chat {chat_id}. Ignoring /roulette command.")
         try:
            await update.message.reply_text("⏳ Колесо рулетки уже вращается, подождите окончания раунда.", quote=True)
         except Exception: pass
         return
    elif game_state and game_state.get('state') == 'accepting_bets':
        logger.info(f"Roulette game already active in chat {chat_id} (state: accepting_bets). Resending status message.")
        # Delete the command message
        try:
            await update.message.delete()
        except Exception as e:
             logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")
        # Resend the current game state instead of sending a new message over it
        await rl_show_game_state(context, chat_id, edit_existing=False) # Send new message to ensure visibility
        return

    # --- Start a New Round (if state is idle, finished, or None) ---
    logger.info(f"Starting new roulette round in chat {chat_id}")

    # Clean up any previous timer job for this chat
    old_timer_job_name = f'rl_spin_timer_{chat_id}'
    await rl_remove_job_if_exists(old_timer_job_name, context)

    # Delete previous game message if it exists and we are starting fresh
    if game_state and game_state.get('message_id'):
        try:
            await context.bot.delete_message(chat_id, game_state['message_id'])
            logger.debug(f"Deleted previous roulette message {game_state['message_id']} in chat {chat_id}")
        except Exception as e:
            logger.debug(f"Failed to delete previous roulette message {game_state.get('message_id')} in chat {chat_id}: {e}")


    # Initialize new game state
    new_game_state = {
        'state': 'accepting_bets',   # Initial state
        'active_bets': {},           # {user_id: [bet_dict, ...]}
        'message_id': None,          # ID of the main game message
        'timer_job_name': None,      # Name of the timer job
        'initiator_id': user.id      # Optional: track who started
    }
    chat_data[RL_GAME_KEY] = new_game_state

    # Delete the command message
    try:
        await update.message.delete()
    except Exception as e:
         logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")

    # Send the initial game message
    await rl_show_game_state(context, chat_id, message_text="🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!", edit_existing=False)


async def rl_show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_text: str | None = None, edit_existing: bool = True):
    """Sends or edits the main roulette game message."""
    chat_data = context.application.chat_data.get(chat_id, {}) # Use application.chat_data in jobs/callbacks
    game_state = chat_data.get(RL_GAME_KEY)

    if not game_state:
        logger.warning(f"rl_show_game_state called for chat {chat_id} but no game state found.")
        # Maybe send an error message?
        try:
            await context.bot.send_message(chat_id, "Ошибка: Не удалось найти данные игры в рулетку.")
        except Exception:
            pass
        return None # Indicate failure

    message_id = game_state.get('message_id')
    state = game_state.get('state', 'unknown')
    timer_job_name = game_state.get('timer_job_name')

    # --- Build Text ---
    # Determine base text based on state if not provided
    if message_text is None:
        if state == 'accepting_bets':
            base_text = "🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!"
        elif state == 'spinning':
             base_text = "🎰 <b>Колесо вращается...</b>"
        elif state == 'idle' or state == 'finished':
             base_text = "🏁 Раунд Рулетки завершен.\nИспользуйте /roulette для начала нового раунда."
        else:
             base_text = f"🎲 <b>Американская Рулетка</b> [Состояние: {state}]"

    timer_text = ""
    if timer_job_name and context.job_queue.get_jobs_by_name(timer_job_name):
         # Calculate remaining time (approximate)
         jobs = context.job_queue.get_jobs_by_name(timer_job_name)
         if jobs:
              next_t = jobs[0].next_t
              if next_t:
                   remaining = max(0, int(next_t.timestamp() - time.time()))
                   timer_text = f"\n⏳ <i>Авто-старт через ~{remaining} сек...</i>"

    full_text = base_text + timer_text

    # --- Build Keyboard ---
    # Need to handle the async call carefully if rl_show_game_state can be called synchronously
    # For now, assuming it's called from an async context (handlers, jobs)
    reply_markup = rl_get_main_menu_keyboard(chat_data, context) # Pass context for get_user_mention

    # --- Send or Edit ---
    sent_message = None
    new_message_sent = False
    try:
        if edit_existing and message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=full_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            logger.debug(f"Edited roulette state message {message_id} in chat {chat_id}")
        else:
            # If trying to edit failed or edit_existing is false, send new
            # Delete old message first if it exists and we are sending new
            if message_id and edit_existing: # Only delete if edit failed
                 try:
                     await context.bot.delete_message(chat_id, message_id)
                 except Exception: pass # Ignore delete errors

            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=full_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            game_state['message_id'] = sent_message.message_id # Update message ID
            logger.info(f"Sent new roulette state message {sent_message.message_id} in chat {chat_id}")
            new_message_sent = True

    except BadRequest as e:
        error_str = str(e).lower()
        if "message is not modified" in error_str:
            logger.debug(f"Roulette state message {message_id} not modified.")
        elif "message to edit not found" in error_str or "chat not found" in error_str or "message can't be edited" in error_str:
            logger.warning(f"Failed to edit roulette message {message_id} in chat {chat_id} (not found/editable). Forcing send new.")
            game_state['message_id'] = None # Clear invalid ID
            # Retry sending new, return the result of the recursive call
            return await rl_show_game_state(context, chat_id, message_text=full_text, edit_existing=False)
        else:
            logger.error(f"BadRequest showing roulette state for chat {chat_id} (msg {message_id}): {e}")
            return None # Indicate failure
    except Forbidden as e:
         logger.error(f"Forbidden error in chat {chat_id} (likely bot kicked/blocked): {e}")
         # Clean up game state for this chat if bot is blocked
         chat_data.pop(RL_GAME_KEY, None)
         # Clean up timer job too
         timer_job = game_state.get('timer_job_name')
         if timer_job: await rl_remove_job_if_exists(timer_job, context)
         return None # Indicate failure
    except Exception as e:
        logger.error(f"Unexpected error showing roulette state for chat {chat_id} (msg {message_id}): {e}", exc_info=True)
        return None # Indicate failure

    return sent_message if new_message_sent else message_id # Return Message or existing ID

# --- Roulette Betting Logic Callbacks (Checked Indentation) ---

async def rl_start_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'Add Bet' button press."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.debug(f"rl_start_bet_callback from user {user.id} in chat {chat_id}")

    # Ensure user exists in DB
    get_or_create_user(user.id)

    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    # Validations
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

    # Initialize temporary bet storage for this user
    context.user_data[RL_USER_TEMP_BET_KEY] = {'step': 'type'} # Track current step

    await query.answer()
    try:
        await query.edit_message_text(
            text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
            reply_markup=rl_get_bet_type_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
         if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message for bet type selection: {e}")
             await query.message.reply_text("Ошибка отображения меню ставок.") # Fallback
         else:
             logger.debug("Message not modified on starting bet type selection.")
    except Exception as e:
        logger.error(f"Error editing message for bet type selection: {e}")
        # Send a new message as fallback if edit failed critically
        await context.bot.send_message(chat_id, "Ошибка отображения меню ставок.")


async def rl_choose_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    if not temp_bet or temp_bet.get('step') != 'type':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        # Try to restore main game view if possible
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    await query.answer()
    bet_type_choice = query.data.replace('rl_type_', '') # Adjusted prefix removal
    temp_bet['type'] = bet_type_choice
    temp_bet['step'] = 'value' # Default next step

    try:
        if bet_type_choice == 'number':
            temp_bet['step'] = 'ask_number' # Special step for number input
            context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet # Save state before editing
            await query.edit_message_text(
                text="➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n"
                     "<b>Введите число (0, 00, или 1-36) в чат:</b>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]), # Only cancel button
                parse_mode=ParseMode.HTML
            )
        else:
            # Use helper for type display name
            type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type_choice.capitalize()) # Heuristic for display name
            if not type_display: # Fallback for dozen/column if needed
                 if bet_type_choice in ['dozen', 'column']: type_display = bet_type_choice.capitalize()
                 else: type_display = bet_type_choice

            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                reply_markup=rl_get_bet_value_keyboard(bet_type_choice),
                parse_mode=ParseMode.HTML
            )
    except BadRequest as e:
         if "message is not modified" not in str(e).lower(): logger.error(f"Error editing message for bet value selection: {e}")
    except Exception as e:
        logger.error(f"Error editing message for bet value selection: {e}")


async def rl_handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages when user is expected to input a number."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Check if this user in this chat is supposed to be entering a number
    if not temp_bet or temp_bet.get('step') != 'ask_number':
        # This message is not for the roulette number input, ignore here
        # Let other handlers process it if needed
        # logger.debug(f"Ignoring message from user {user.id} in chat {chat_id} as not in 'ask_number' state.")
        return # IMPORTANT: Don't block other handlers

    # We are expecting a number input from this user
    number_input = update.message.text.strip().lower()

    # Delete the user's message containing the number
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete user number input message in chat {chat_id}: {e}")


    if number_input not in RL_AMERICAN_WHEEL_SET: # Use the precomputed set
        # Respond by editing the bot's message, not replying to user's (which we deleted)
        game_message_id = context.chat_data.get(RL_GAME_KEY, {}).get('message_id')
        if game_message_id:
             try:
                await context.bot.edit_message_text(
                     chat_id=chat_id,
                     message_id=game_message_id, # Edit the main game message temporarily
                     text="➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n"
                          f"<b>Неверный ввод: '{html_escape(number_input)}'.</b>\n"
                          "Введите число (0, 00, или 1-36) в чат:",
                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]),
                     parse_mode=ParseMode.HTML
                 )
             except Exception as e:
                 logger.error(f"Error editing message to show invalid number input: {e}")
                 # Fallback if edit fails
                 # await context.bot.send_message(chat_id, f"Неверный ввод: '{html_escape(number_input)}'. Введите 0, 00, или 1-36.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]))

        return # Stay in 'ask_number' state

    # --- Valid Number Received ---
    bet_type = temp_bet['type']
    temp_bet['value'] = number_input
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, number_input)
    temp_bet['step'] = 'amount'

    # Ensure user exists and get balance
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    if balance is None:
        await context.bot.send_message(chat_id, "Ошибка получения вашего баланса.")
        # Cancel the bet process
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Edit the bot's message again to ask for amount
    game_message_id = context.chat_data.get(RL_GAME_KEY, {}).get('message_id')
    if game_message_id:
        try:
             await context.bot.edit_message_text(
                 chat_id=chat_id,
                 message_id=game_message_id,
                 text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\n"
                      f"Ваш баланс: {balance:.2f} F\nВыберите сумму ставки:",
                 reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                 parse_mode=ParseMode.HTML
             )
        except Exception as e:
            logger.error(f"Error editing message for amount selection after number input: {e}")
            # await context.bot.send_message(chat_id, f"Выбрано: {temp_bet['value_display']}. Ошибка отображения кнопок суммы.")
            # Potentially cancel bet here or let user use cancel button
    else:
         # Should not happen if game started correctly
         logger.error(f"Cannot find game message ID in chat {chat_id} during number input handling.")


async def rl_choose_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    if not temp_bet or temp_bet.get('step') != 'value':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    await query.answer()
    bet_value_choice = query.data.replace('rl_value_', '') # Adjusted prefix removal
    bet_type = temp_bet['type']
    temp_bet['value'] = bet_value_choice
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, bet_value_choice)
    temp_bet['step'] = 'amount'

    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
    if balance is None:
        await query.edit_message_text("Ошибка получения вашего баланса. Ставка отменена.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    try:
        await query.edit_message_text(
            text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\n" # Shows chosen value now
                 f"Ваш баланс: {balance:.2f} F\nВыберите сумму ставки:",
            reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
         if "message is not modified" not in str(e).lower(): logger.error(f"Error editing message for bet amount selection: {e}")
    except Exception as e:
        logger.error(f"Error editing message for bet amount selection: {e}")


async def rl_choose_bet_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    if not temp_bet or temp_bet.get('step') != 'amount':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    try:
        bet_amount = int(query.data.replace('rl_amount_', '')) # Adjusted prefix removal
    except ValueError:
        await query.answer("Неверное значение суммы.", show_alert=True)
        return

    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
    bet_type = temp_bet.get('type', 'unknown') # Get bet_type for potential back navigation

    if balance is None:
        await query.edit_message_text("Ошибка получения вашего баланса. Ставка отменена.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)
        return

    if bet_amount <= 0 or bet_amount > balance:
        await query.answer(f"Недостаточно средств ({balance:.2f} F) или неверная сумма.", show_alert=True)
        # Show amount keyboard again
        try:
            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet.get('value_display', 'N/A')}\n\n"
                     f"Ваш баланс: {balance:.2f} F\n<b>Неверная сумма!</b> Выберите сумму ставки:",
                reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): logger.error(f"Error re-editing message for invalid amount: {e}")
        except Exception as e:
            logger.error(f"Error re-editing message for invalid amount: {e}")
        return # Stay in amount step

    await query.answer()
    temp_bet['amount'] = bet_amount
    temp_bet['step'] = 'confirm'

    try:
        await query.edit_message_text(
            text=f"➕ <b>Подтверждение ставки</b>\n"
                 f" - Игрок: {user.mention_html()}\n"
                 f" - Ставка: {temp_bet['value_display']}\n"
                 f" - Сумма: {temp_bet['amount']} F\n\n"
                 f"Подтверждаете?",
            reply_markup=rl_get_confirmation_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
         if "message is not modified" not in str(e).lower(): logger.error(f"Error editing message for confirmation: {e}")
    except Exception as e:
        logger.error(f"Error editing message for confirmation: {e}")


async def rl_confirm_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles confirmation 'Yes' button."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    if not temp_bet or temp_bet.get('step') != 'confirm':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # --- All checks before confirming ---
    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)
    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Ставки больше не принимаются.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Clean up temp bet
        await rl_show_game_state(context, chat_id, edit_existing=True) # Show final state
        return

    bet_amount = temp_bet.get('amount', 0)
    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)

    if balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return
    if bet_amount <= 0 or bet_amount > balance:
        await query.answer(f"Недостаточно средств ({balance:.2f} F).", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True) # Show state, user needs to restart bet
        return

    # Check limits again just before confirming
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

    # --- Deduct balance and add bet ---
    new_balance = update_balance(user.id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка списания средств со счета!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Add bet to chat_data
    # Make a copy to avoid modifying the temp_bet dict elsewhere
    final_bet = {
        'type': temp_bet['type'],
        'value': temp_bet['value'],
        'amount': temp_bet['amount'],
        'value_display': temp_bet['value_display']
    }
    if user.id not in active_bets_by_user:
        active_bets_by_user[user.id] = []
    active_bets_by_user[user.id].append(final_bet)
    game_state['active_bets'] = active_bets_by_user # Ensure update back to chat_data

    # Clean up user's temporary bet data
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)

    await query.answer("✅ Ставка принята!")
    logger.info(f"User {user.id} placed bet in chat {chat_id}: {final_bet}")

    # --- Start Timer if First Bet ---
    current_total_bets = sum(len(bets) for bets in active_bets_by_user.values())
    timer_job_name = f'rl_spin_timer_{chat_id}'
    existing_jobs = context.job_queue.get_jobs_by_name(timer_job_name)

    if current_total_bets == 1 and not existing_jobs:
        context.job_queue.run_once(
            rl_spin_roulette_job,
            RL_BET_TIMER_SECONDS,
            chat_id=chat_id,
            name=timer_job_name,
            data={'chat_id': chat_id} # Pass chat_id to job
        )
        game_state['timer_job_name'] = timer_job_name # Store job name
        logger.info(f"Started roulette timer '{timer_job_name}' for chat {chat_id}")

    # --- Update the main game message ---
    user_mention = user.mention_html() # Get mention here
    await rl_show_game_state(context, chat_id, message_text=f"✅ Ставка от {user_mention}: {final_bet['value_display']} ({final_bet['amount']} F) принята!", edit_existing=True)


async def rl_cancel_bet_step_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'Cancel' or 'Back' buttons during bet creation."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id

    # Clear temporary bet data for the user
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)

    await query.answer("Действие отменено.")

    # Restore the main game state view
    await rl_show_game_state(context, chat_id, message_text="Создание ставки отменено.", edit_existing=True)


async def rl_back_to_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # If temp_bet exists, reset it to the 'type' step
    if temp_bet:
        context.user_data[RL_USER_TEMP_BET_KEY] = {'step': 'type'}
        await query.answer()
        try:
            await query.edit_message_text(
                text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
                reply_markup=rl_get_bet_type_keyboard(),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): logger.error(f"Error editing message for back to bet type: {e}")
        except Exception as e:
             logger.error(f"Error editing message for back to bet type: {e}")
    else:
        # If no temp data, just go back to main menu
        await query.answer("Отмена.")
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)


async def rl_back_to_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    if temp_bet and 'type' in temp_bet and temp_bet.get('type') != 'number':
        bet_type = temp_bet['type']
        # Reset step and clear value/amount
        temp_bet_reset = {'step': 'value', 'type': bet_type}
        context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet_reset
        await query.answer()

        type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type.capitalize(), bet_type)
        try:
            await query.edit_message_text(
                text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                reply_markup=rl_get_bet_value_keyboard(bet_type),
                 parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower(): logger.error(f"Error editing message for back to bet value: {e}")
        except Exception as e:
             logger.error(f"Error editing message for back to bet value: {e}")

    else:
        # If type is number or no temp data, go back to type selection
        await rl_back_to_bet_type_callback(update, context)


# --- Roulette Spin Logic (Checked Indentation) ---
async def rl_spin_roulette_logic(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Main logic for spinning the wheel, calculating results, and updating state."""
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)
    bot = context.bot

    # --- Validations ---
    if not game_state:
        logger.error(f"Spin logic called for chat {chat_id} but no game state found.")
        return
    # Allow spin only if accepting bets or if somehow called again while spinning (idempotency check later)
    if game_state.get('state') not in ['accepting_bets', 'spinning']:
        logger.warning(f"Spin logic called for chat {chat_id} but state is '{game_state.get('state')}'. Aborting spin.")
        return

    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user and game_state.get('state') == 'accepting_bets':
        logger.warning(f"Spin logic called for chat {chat_id} but no bets found.")
        game_state['state'] = 'idle' # Reset state
        # Update message?
        msg_id = game_state.get('message_id')
        if msg_id:
             try:
                 await bot.edit_message_text(chat_id, msg_id, "Ставок не было, раунд завершен.\nИспользуйте /roulette для старта.", reply_markup=None)
             except Exception: pass
        return

    message_id = game_state.get('message_id')
    if not message_id:
        logger.error(f"Cannot spin roulette in chat {chat_id}, message_id is missing.")
        try:
            await bot.send_message(chat_id, "❌ Ошибка: Не найдено сообщение для отображения спина!")
        except Exception: pass
        chat_data.pop(RL_GAME_KEY, None) # Clean broken state
        return

    # --- Prevent Double Spin ---
    # If already spinning, log and exit.
    if game_state.get('state') == 'spinning':
        logger.warning(f"Spin logic called again for chat {chat_id} while already spinning. Ignoring.")
        return

    # --- Set State to Spinning & Remove Timer ---
    game_state['state'] = 'spinning'
    timer_job_name = game_state.pop('timer_job_name', None) # Remove and get name
    if timer_job_name:
        await rl_remove_job_if_exists(timer_job_name, context)

    # --- 1. Predetermine Result ---
    winning_number_str = random.choice(AMERICAN_WHEEL_ORDER)
    target_index = AMERICAN_WHEEL_ORDER.index(winning_number_str)
    logger.info(f"Roulette spin result for chat {chat_id}: {winning_number_str} (index {target_index})")

    # --- 2. Animation ---
    spin_duration = RL_SPIN_ANIMATION_DURATION
    min_full_rotations = 2; max_full_rotations = 4
    frames_per_second = 2.5 # Adjust FPS for smoother/less frequent edits
    update_interval = 1.0 / frames_per_second
    spinner_emojis = ['◜','◝','◞','◟']

    start_index = random.randint(0, RL_WHEEL_SIZE - 1); current_index = start_index
    num_full_rotations = random.randint(min_full_rotations, max_full_rotations)
    steps_for_rotations = num_full_rotations * RL_WHEEL_SIZE
    steps_to_target = (target_index - start_index + RL_WHEEL_SIZE) % RL_WHEEL_SIZE
    total_steps = steps_for_rotations + steps_to_target
    if total_steps == 0:
        total_steps = RL_WHEEL_SIZE # Ensure at least one rotation

    logger.info(f"Roulette Animation chat {chat_id}: Start={start_index}, Target={target_index}, Rot={num_full_rotations}, Steps={total_steps}")

    # Edit initial message to show spinner
    try:
        await bot.edit_message_text("🎰 <b>Колесо вращается...</b>", chat_id=chat_id, message_id=message_id, reply_markup=None, parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5) # Small pause before animation starts
    except Exception as e:
        logger.warning(f"Failed to edit message {message_id} for spin start in chat {chat_id}: {e}")
        # Continue anyway, animation might just start from previous state

    loop_start_time = time.monotonic(); steps_taken = 0; next_update_time_budget = loop_start_time
    last_displayed_number = ""; animation_successful = True

    def ease_out_cubic(t):
        t -= 1
        return t * t * t + 1

    while steps_taken < total_steps:
        progress = (steps_taken + 1) / total_steps
        eased_progress = ease_out_cubic(progress)
        target_step_end_time = loop_start_time + spin_duration * eased_progress
        current_mono_time = time.monotonic()

        if current_mono_time >= next_update_time_budget:
            display_number = AMERICAN_WHEEL_ORDER[current_index]
            color_char = rl_get_color(display_number)
            display_color_emoji = "🟢" if color_char == 'Green' else ("🔴" if color_char == 'Red' else "⚫")
            spinner = spinner_emojis[steps_taken % len(spinner_emojis)]
            frame_text = f"🎰 {spinner} {display_color_emoji} {display_number}"

            if display_number != last_displayed_number:
                try:
                    await bot.edit_message_text(
                        text=frame_text, chat_id=chat_id, message_id=message_id
                    )
                    last_displayed_number = display_number
                    next_update_time_budget = current_mono_time + update_interval
                except BadRequest as e:
                    if "Message is not modified" in str(e): pass # Ignore
                    else: logger.warning(f"BadRequest editing animation chat {chat_id} (step {steps_taken}): {e}"); animation_successful = False; break
                except Forbidden: logger.error(f"Forbidden error during animation in chat {chat_id}. Aborting."); animation_successful = False; break
                except Exception as e:
                    logger.warning(f"Error editing animation chat {chat_id} (step {steps_taken}): {e}"); animation_successful = False; break
            else:
                 next_update_time_budget = current_mono_time + update_interval

        current_mono_time = time.monotonic()
        sleep_duration = max(0.005, target_step_end_time - current_mono_time)
        await asyncio.sleep(sleep_duration)

        current_index = (current_index + 1) % RL_WHEEL_SIZE
        steps_taken += 1
        # if steps_taken % RL_WHEEL_SIZE == 0: logger.debug(f"Anim chat {chat_id}: Step {steps_taken}/{total_steps}, Time: {time.monotonic() - loop_start_time:.2f}s")

    # --- Show Final Number ---
    if animation_successful:
      try:
          final_color_char = rl_get_color(winning_number_str)
          final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
          await bot.edit_message_text(
              text=f"<b>➡️ {final_color_emoji} {winning_number_str} ⬅️</b>",
              chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML
          )
          await asyncio.sleep(1.5) # Pause on final number
      except Exception as e:
          logger.warning(f"Failed to show final animation number for chat {chat_id}: {e}")

    # --- 4. Calculate Winnings (Per User) ---
    winning_color = rl_get_color(winning_number_str)
    winning_parity = rl_is_even_or_odd(winning_number_str)
    winning_dozen = rl_get_dozen(winning_number_str)
    winning_column = rl_get_column(winning_number_str)

    results_by_user = defaultdict(lambda: {'wins': 0, 'returned': 0, 'log': [], 'bets': []})
    total_net_change = 0

    # Use .items() to iterate safely
    for user_id, bets in list(active_bets_by_user.items()):
        user_results = results_by_user[user_id]
        user_results['bets'] = bets # Store original bets for reference
        for bet in bets:
            win = False
            payout_mult = 0
            bet_type = bet.get('type')
            bet_value = bet.get('value')
            bet_amount = bet.get('amount', 0)
            value_disp = bet.get('value_display', 'N/A')

            # Skip if bet data is incomplete
            if not all([bet_type, bet_value, bet_amount > 0]):
                logger.warning(f"Skipping malformed bet for user {user_id} in chat {chat_id}: {bet}")
                continue

            if bet_type == 'number' and str(bet_value) == winning_number_str: payout_mult, win = RL_PAYOUTS['number'], True
            elif bet_type == 'color' and bet_value == winning_color: payout_mult, win = RL_PAYOUTS['color'], True
            elif bet_type == 'parity' and bet_value == winning_parity: payout_mult, win = RL_PAYOUTS['parity'], True
            elif bet_type == 'dozen' and bet_value == winning_dozen: payout_mult, win = RL_PAYOUTS['dozen'], True
            elif bet_type == 'column' and bet_value == winning_column: payout_mult, win = RL_PAYOUTS['column'], True

            if win:
                winnings = bet_amount * payout_mult
                returned = bet_amount + winnings
                user_results['wins'] += winnings
                user_results['returned'] += returned
                user_results['log'].append(f"✅ {value_disp} ({bet_amount}F) -> +{winnings:.2f}F")
            else:
                user_results['log'].append(f"❌ {value_disp} ({bet_amount}F)")


    # --- 5. Update Balances & Build Result Text ---
    result_lines = []
    # Fetch mentions asynchronously
    player_ids = list(results_by_user.keys())
    try:
        player_mentions = await asyncio.gather(*(get_user_mention(context, uid) for uid in player_ids))
        mention_map = dict(zip(player_ids, player_mentions))
    except Exception as e:
         logger.error(f"Failed to fetch mentions for roulette results in chat {chat_id}: {e}")
         mention_map = {uid: f"User_{uid}" for uid in player_ids}


    processed_users = set()

    for user_id, results in results_by_user.items():
        processed_users.add(user_id)
        user_mention = mention_map.get(user_id, f"User_{user_id}")
        amount_to_pay = results['returned']
        total_bet_amount = sum(b['amount'] for b in results['bets'])
        net_change = amount_to_pay - total_bet_amount

        result_lines.append(f"\n--- {user_mention} ---")
        result_lines.extend(results['log'])

        balance_update_status = ""
        if amount_to_pay > 0:
            new_bal = update_balance(user_id, amount_to_pay)
            if new_bal is None:
                 balance_update_status = " ⚠️<b>Ошибка начисления!</b>"
                 logger.error(f"Roulette payout FAILED for user {user_id} in chat {chat_id}. Amount: {amount_to_pay}")
                 # If payout failed, net change is loss of original bets
                 net_change = -total_bet_amount
            else:
                 balance_update_status = f" -> Баланс: {new_bal:.2f}F"
        elif total_bet_amount > 0: # Log balance if they lost
             current_bal = get_balance(user_id)
             if current_bal is not None:
                 balance_update_status = f" -> Баланс: {current_bal:.2f}F"


        result_lines.append(f"<i>Итог: {net_change:+.2f} F{balance_update_status}</i>")
        total_net_change += net_change # Accumulate total change for the round

    # Check for users who bet but somehow weren't processed (shouldn't happen)
    # Make sure to iterate over the original list of keys
    for user_id in list(active_bets_by_user.keys()):
         if user_id not in processed_users:
             logger.warning(f"User {user_id} had bets but was not in results_by_user for chat {chat_id}")
             result_lines.append(f"\n--- User_{user_id} (Ошибка обработки) ---")

    # --- 6. Final Message ---
    final_color_char = rl_get_color(winning_number_str)
    final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
    result_header = f"🎉 Выпало: <b>{final_color_emoji} {winning_number_str}</b> 🎉\n"
    result_summary = f"\n<b>Общий итог раунда: {total_net_change:+.2f} F</b>"

    full_result_text = result_header + "\n".join(result_lines) + result_summary

    # --- 7. Reset State & Show Results ---
    # Reset game state *before* showing final keyboard
    chat_data[RL_GAME_KEY] = {
        'state': 'idle', # Or 'finished' then reset via another action
        'active_bets': {},
        'message_id': message_id, # Keep message ID for potential reuse/edit
        'timer_job_name': None
    }
    final_reply_markup = rl_get_main_menu_keyboard(chat_data, context) # Get keyboard for idle state

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
         chat_data.pop(RL_GAME_KEY, None) # Clean up state
    except Exception as e:
        logger.error(f"Failed to edit final roulette result for chat {chat_id}: {e}")
        # Try sending as new message
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
    """Handles 'Spin' button press."""
    query = update.callback_query
    chat_id = query.message.chat_id
    user = query.from_user
    logger.info(f"Manual spin triggered by user {user.id} in chat {chat_id}")

    chat_data = context.chat_data
    game_state = chat_data.get(RL_GAME_KEY)

    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Сейчас нельзя запустить вращение.", show_alert=True)
        return

    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user:
        await query.answer("Нет ставок для запуска вращения.", show_alert=True)
        return

    await query.answer("Запускаем вращение...")

    # Remove timer and start spin logic (already handled within spin_logic)
    # timer_job_name = game_state.get('timer_job_name')
    # if timer_job_name:
    #     await rl_remove_job_if_exists(timer_job_name, context)
    #     game_state['timer_job_name'] = None # Clear from state

    await rl_spin_roulette_logic(context, chat_id)

async def rl_show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    help_text = (
        f"<b>🎲 Правила Американской рулетки:</b>\n\n"
        f"• Делайте ставки с помощью кнопок.\n"
        f"• Макс. ставок в раунде: {RL_MAX_BETS_PER_ROUND} (всего), {RL_MAX_BETS_PER_USER} (на игрока).\n"
        f"• После первой ставки раунд запустится через {RL_BET_TIMER_SECONDS} сек.\n"
        f"• Можно нажать 'Крутить!' раньше.\n"
        f"• Ставки на 0 или 00 выигрывают только при ставке на 'Число'.\n\n"
        f"<b>Типы ставок (Выплата 1 к X):</b>\n"
        f"- Число ({RL_PAYOUTS['number']}), Цвет ({RL_PAYOUTS['color']}), Чет/Нечет ({RL_PAYOUTS['parity']})\n"
        f"- Дюжина ({RL_PAYOUTS['dozen']}), Колонка ({RL_PAYOUTS['column']})\n"
        f"<i>Ставки на цвет, чет/нечет, дюжины, колонки проигрывают при 0 или 00.</i>"
    )
    await query.answer()
    # Send as a new message in the chat
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text, parse_mode=ParseMode.HTML)
    except Forbidden:
         logger.error(f"Forbidden: Cannot send roulette help to chat {update.effective_chat.id}")
    except Exception as e:
        logger.error(f"Failed to send roulette help: {e}")

async def rl_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles buttons that should just be acknowledged."""
    await update.callback_query.answer()

# --- General Handlers (Checked Indentation) ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ALL button presses and routes them."""
    q = update.callback_query
    data = q.data
    u = q.from_user
    chat = update.effective_chat # Get chat info

    if not data:
        await q.answer()
        return

    logger.debug(f"Callback query received: '{data}' from user {u.id} in chat {chat.id} ({chat.type})")
    parts = data.split("_") # Simpler split

    prefix = parts[0] if parts else None

    # --- Route Callbacks ---
    try:
        # --- Blackjack Callbacks (Private Chat Only) ---
        if prefix == "bj":
            if chat.type != ChatType.PRIVATE:
                await q.answer("Играть в Блекджек можно только в личном чате.", show_alert=True)
                return

            action = parts[1] if len(parts) > 1 else None
            arg = parts[2] if len(parts) > 2 else None

            if action == "bet" and arg:
                await q.answer(f"Ставка (BJ): {arg} F")
                await blackjack_handle_bet(update, context, int(arg))
            elif action == "new" and arg == "game": # Make specific bj_new_game
                await q.answer("Новая игра (BJ)...")
                await blackjack_start_command(update, context)
            elif action in ["hit", "stand", "double", "split"] and arg is not None:
                await blackjack_handle_action(update, context, [action, arg]) # Pass as list [action, arg]
            else:
                logger.warning(f"Unknown or incomplete BJ callback: {data}")
                await q.answer()

        # --- Roulette Callbacks (Group & Private) ---
        elif prefix == "rl":
            # Route based on full callback data string for simplicity
            if data == "rl_start_bet": await rl_start_bet_callback(update, context)
            elif data.startswith("rl_type_"): await rl_choose_bet_type_callback(update, context)
            elif data.startswith("rl_value_"): await rl_choose_bet_value_callback(update, context)
            elif data.startswith("rl_amount_"): await rl_choose_bet_amount_callback(update, context)
            elif data == "rl_confirm_bet_yes": await rl_confirm_bet_callback(update, context)
            elif data == "rl_cancel_bet_step": await rl_cancel_bet_step_callback(update, context)
            elif data == "rl_back_to_bet_type": await rl_back_to_bet_type_callback(update, context)
            elif data == "rl_back_to_bet_value": await rl_back_to_bet_value_callback(update, context)
            elif data == "rl_spin": await rl_spin_callback(update, context)
            elif data == "rl_show_help": await rl_show_help_callback(update, context)
            # elif data == "rl_new_round": await roulette_start_command(update, context) # Let user use /roulette
            elif data == "rl_noop": await rl_noop_callback(update, context)
            else:
                logger.warning(f"Unknown or incomplete RL callback: {data}")
                await q.answer() # Acknowledge silently

        # --- Other Prefixes (if any in future) ---
        else:
            logger.warning(f"Unknown callback prefix: {prefix} in data: {data}")
            await q.answer()

    except ValueError as e: # CORRECTED INDENTATION
         logger.error(f"Callback ValueError (likely int conversion) for '{data}' user {u.id}: {e}")
         try:
             await q.answer("Ошибка: Неверный формат данных.", show_alert=True)
         except Exception: pass
    except BadRequest as e: # CORRECTED INDENTATION
        error_str = str(e).lower()
        logger.warning(f"Callback BadRequest for '{data}' user {u.id}: {e}") # Log non-silently first
        if "query is too old" in error_str: pass # Ignore old queries silently
        elif "message is not modified" in error_str: pass # Ignore modification errors silently
        elif "message to edit not found" in error_str: # Message might have been deleted
            try: await q.answer("Сообщение игры было удалено.", show_alert=False)
            except Exception: pass
        else: # Show generic error for other BadRequests
            try: await q.answer("Произошла ошибка при обработке.", show_alert=True)
            except Exception: pass
    except Forbidden as e: # CORRECTED INDENTATION
         logger.error(f"Callback Forbidden error for user {u.id} in chat {chat.id} (likely blocked bot): {e}")
         try: await q.answer("Ошибка: Бот не имеет прав в этом чате.", show_alert=True)
         except Exception: pass
    except Exception as e: # CORRECTED INDENTATION
        logger.error(f"Callback general error for '{data}' user {u.id}: {e}", exc_info=True)
        try:
            await q.answer("Произошла внутренняя ошибка.", show_alert=True)
        except Exception: pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ Logs errors raised by Handlers or the Dispatcher. """
    logger.error(f"Exception while handling an update:", exc_info=context.error)

    # Log specific common errors for better debugging
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error detected! Ensure only ONE instance of the bot is running.")
    elif isinstance(context.error, Forbidden):
        logger.error(f"Forbidden error: {context.error}. Bot might be blocked or lack permissions. Update: {update}")
    elif isinstance(context.error, BadRequest):
         # Ignore "Message is not modified" and "Query is too old" as they are common and expected
         error_str = str(context.error).lower()
         if "message is not modified" not in error_str and "query is too old" not in error_str:
              logger.warning(f"BadRequest error: {context.error}. Update: {update}")
    # Add more specific error types if needed (e.g., NetworkError, TimedOut)

    # Avoid sending messages to users on errors unless absolutely necessary and safe


# --- Main Bot Setup ---
def main():
    """ Starts the bot. """
    logger.info("Starting bot application...")
    start_keep_alive() # Start the Flask keep-alive thread

    try:
        # PTB Application Builder
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True) # Handle multiple updates in parallel
            .connect_timeout(30) # Increase connect timeout
            .read_timeout(30) # Increase read timeout
            .pool_timeout(30) # Increase pool timeout
             # Persistence can be added here if needed later, but chat/user_data is in-memory by default
            .build()
        )

        # --- Register Handlers ---
        # General Commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))

        # Game Commands
        application.add_handler(CommandHandler("blackjack", blackjack_start_command))
        application.add_handler(CommandHandler("roulette", roulette_start_command))

        # Callback Query Handler (Handles ALL button presses via routing)
        # Should run before message handlers if callbacks might interact with message state
        application.add_handler(CallbackQueryHandler(button_callback_handler), group=0)

        # Message Handler (for Roulette number input)
        # Needs to be *after* command handlers but potentially *before* other general message handlers
        # group=1 seems appropriate here.
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rl_handle_number_input), group=1)

        # Errors
        application.add_error_handler(error_handler)

        logger.info("Handlers registered successfully.")
        print("Bot is running... Press Ctrl+C to stop.") # Console feedback

        # --- Start Polling ---
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, # Process all update types
            drop_pending_updates=True, # Ignore updates missed while bot was down
            # Consider adding timeout parameter if needed
            # timeout=30 # seconds
        )

    except ValueError as e:
         # Likely missing Token or DB URL
         logger.critical(f"Configuration Error: {e}")
         print(f"CRITICAL ERROR: {e}")
    except Conflict as e:
        logger.critical(f"Conflict Error: {e}. Is another instance of the bot running?")
        print("CRITICAL ERROR: Conflict detected. Another instance might be running.")
    except Exception as e:
        logger.critical(f"An unexpected critical error occurred during bot startup or runtime: {e}", exc_info=True)
        print(f"CRITICAL ERROR: {e}")
    finally:
        print("Bot stopped.")
        logger.info("Bot application has stopped.")

if __name__ == "__main__":
    main()