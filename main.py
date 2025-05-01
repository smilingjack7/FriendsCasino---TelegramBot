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

# Ensure telegram modules are imported correctly
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        CallbackQueryHandler,
        MessageHandler,
        filters,
        # ConversationHandler is not used, so it's technically optional here
    )
    from telegram.constants import ParseMode, ChatType
    from telegram.error import BadRequest, Conflict, Forbidden
except ImportError as e:
    print(f"Error importing telegram library: {e}")
    print("Please ensure 'python-telegram-bot' is installed correctly.")
    exit(1)

# Ensure psycopg2 is imported correctly
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError as e:
    print(f"Error importing psycopg2: {e}")
    print("Please ensure 'psycopg2-binary' or 'psycopg2' is installed.")
    exit(1)

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
NUM_DECKS = 8  # Number of decks used
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3  # Max number of hands after splitting (original + 3 splits = 4 hands)
DEALER_TURN_DELAY = 0.7  # Seconds between dealer actions/reveals
LEADERBOARD_LIMIT = 10
BJ_GAME_KEY = 'blackjack_game'  # Key for user_data
# Penetration: Shuffle when deck reaches this percentage or lower
BJ_SHUFFLE_PERCENTAGE = 0.60  # Shuffle when 60% or less remains

# --- Roulette Constants ---
RL_BET_AMOUNTS = [10, 25, 50, 100, 250, 500]
RL_MAX_BETS_PER_ROUND = 10
RL_MAX_BETS_PER_USER = 3
RL_BET_TIMER_SECONDS = 45
RL_SPIN_ANIMATION_DURATION = 8.0
RL_GAME_KEY = 'roulette_game' # Key for chat_data
RL_USER_TEMP_BET_KEY = 'roulette_temp_bet' # Key for user_data (during bet creation)
RL_TIMER_DISPLAY_UPDATE_INTERVAL = 2.0  # How often to update the timer text (seconds)
AMERICAN_WHEEL_ORDER = [
    '0', '28', '9', '26', '30', '11', '7', '20', '32', '17', '5', '22', '34',
    '15', '3', '24', '36', '13', '1', '00', '27', '10', '25', '29', '12', '8',
    '19', '31', '18', '6', '21', '33', '16', '4', '23', '35', '14', '2'
]
RL_WHEEL_SIZE = len(AMERICAN_WHEEL_ORDER)
RL_RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
RL_BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}
RL_DOZENS = {'1st': set(range(1, 13)), '2nd': set(range(13, 25)), '3rd': set(range(25, 37))}
RL_COLUMNS = {
    'col1': {1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34},
    'col2': {2, 5, 8, 11, 14, 17, 20, 23, 26, 29, 32, 35},
    'col3': {3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33, 36}
}
RL_PAYOUTS = {'number': 35, 'color': 1, 'parity': 1, 'dozen': 2, 'column': 2}
RL_BET_VALUE_DISPLAY_NAMES = {
    'Red': '🔴 Красное', 'Black': '⚫ Черное',
    'Even': '⚖️ Четное', 'Odd': '❓ Нечетное',
    '1st': '1️⃣ Дюж. 1-12', '2nd': '2️⃣ Дюж. 13-24', '3rd': '3️⃣ Дюж. 25-36',
    'col1': '📊 Кол. 1', 'col2': '📊 Кол. 2', 'col3': '📊 Кол. 3'
}
# Using a set for faster 'in' checks
RL_AMERICAN_WHEEL_SET = set(AMERICAN_WHEEL_ORDER)


# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Reduce verbosity from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive ---
keep_alive_app = Flask(__name__) # Use __name__
@keep_alive_app.route('/')
def keep_alive_home():
    return "Bot is alive!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    # Suppress Werkzeug's startup messages unless it's an error
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    # use_reloader=False is important when running inside threads/other environments
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)

def start_keep_alive():
    # Daemon=True ensures the thread exits when the main program does
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Card Definitions (Blackjack) ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Database Interaction ---
def get_db_conn():
    """Establishes a database connection."""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True # Set autocommit for simplicity
        return conn
    except psycopg2.Error as e:
        logger.error(f"DB connection error: {e}")
        raise # Re-raise the exception to signal failure

def get_or_create_user(user_id: int) -> dict | None:
    """Fetches user data or creates a new user with initial balance. Returns dict or None on error."""
    sql_s = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    sql_i = """
        INSERT INTO users (user_id, balance, last_bonus)
        VALUES (%s, %s, NULL)
        ON CONFLICT (user_id) DO NOTHING;
    """
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql_s, (user_id,))
            data = cur.fetchone()

            if not data:
                # Attempt to create user
                cur.execute(sql_i, (user_id, INITIAL_BALANCE))
                # Fetch again after potential insert
                cur.execute(sql_s, (user_id,))
                data = cur.fetchone()
                if data:
                    logger.info(f"New user created: {user_id}")
                else:
                    # This case should be rare (e.g., conflict happened but fetch failed)
                    logger.error(f"Failed to create or fetch user after insert attempt for user_id: {user_id}")
                    return None # Indicate failure

            # --- Data Type Validation and Conversion ---
            if data:
                # Validate/Convert last_bonus to timezone-aware UTC datetime
                if data.get('last_bonus'):
                    bonus_time = data['last_bonus']
                    if isinstance(bonus_time, str):
                        try:
                            # Handle different ISO formats, ensure UTC
                            if bonus_time.endswith('Z'): bonus_time = bonus_time.replace('Z', '+00:00')
                            # Handle formats without timezone info (assume UTC if possible)
                            elif '+' not in bonus_time and '.' in bonus_time: bonus_time += '+00:00'
                            parsed_dt = datetime.datetime.fromisoformat(bonus_time)
                            # Ensure timezone-aware and UTC
                            if parsed_dt.tzinfo is None:
                                data['last_bonus'] = parsed_dt.replace(tzinfo=datetime.timezone.utc)
                            else:
                                data['last_bonus'] = parsed_dt.astimezone(datetime.timezone.utc)
                        except ValueError:
                            logger.warning(f"Could not parse last_bonus string '{data['last_bonus']}' for user {user_id}. Resetting.")
                            data['last_bonus'] = None
                    elif isinstance(bonus_time, datetime.datetime):
                        # Ensure timezone-aware and UTC
                        if bonus_time.tzinfo is None:
                            data['last_bonus'] = bonus_time.replace(tzinfo=datetime.timezone.utc)
                        else:
                            data['last_bonus'] = bonus_time.astimezone(datetime.timezone.utc)
                    else:
                        logger.warning(f"Invalid last_bonus type for user {user_id}: {type(bonus_time)}. Resetting.")
                        data['last_bonus'] = None

                # Validate/Convert balance to float
                if data.get('balance') is not None:
                    try:
                        data['balance'] = float(data['balance'])
                    except (ValueError, TypeError):
                        logger.error(f"Could not convert DB balance '{data['balance']}' to float for user {user_id}. Setting to 0.")
                        data['balance'] = 0.0
                else:
                    # Handle case where balance is NULL in DB (shouldn't happen with default)
                    logger.warning(f"User {user_id} has NULL balance in DB. Setting to 0.")
                    data['balance'] = 0.0

            return data # Return the processed dictionary

    except psycopg2.Error as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}", exc_info=True)
        return None
    except Exception as e: # Catch other potential errors
        logger.error(f"Unexpected Error (get_or_create_user) for {user_id}: {e}", exc_info=True)
        return None

def update_balance(user_id: int, change: float) -> float | None:
    """Updates user balance by 'change' amount. Returns new balance or None on error/failure."""
    # Ensure user exists (or try to create) before attempting update
    # Note: This adds a SELECT before UPDATE, could be optimized if strictly necessary
    # but safer to ensure user record exists.
    if get_or_create_user(user_id) is None:
        logger.error(f"Attempted balance update for non-existent or problematic user {user_id}")
        return None

    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id))
            res = cur.fetchone()
            if res and res[0] is not None:
                new_balance = float(res[0])
                logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                return new_balance
            else:
                # This could happen if the user_id was somehow deleted between get_or_create and update
                logger.warning(f"Update balance failed for user {user_id} (user not found or other issue after creation check)")
                return None
    except psycopg2.errors.CheckViolation as e:
        # Catch potential negative balance constraint violation
        logger.warning(f"Balance update rejected for user {user_id}: {e} (Likely negative balance attempt: {change})")
        # Optionally, fetch current balance to return it if needed, otherwise return None
        return get_balance(user_id) # Return current balance instead of None
    except (psycopg2.Error, ValueError, TypeError) as e: # Catch DB errors and potential type errors
        logger.error(f"DB/Type Error (update_balance) for {user_id}: {e}", exc_info=True)
        return None

def get_balance(user_id: int) -> float | None:
    """Gets the current balance for a user."""
    user_data = get_or_create_user(user_id)
    # Ensure balance is float, fallback to None if user_data is None or balance missing
    return user_data['balance'] if user_data and 'balance' in user_data else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime | None):
    """Updates the last bonus timestamp in the database."""
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    # Convert to naive UTC timestamp for storage, or use NULL if ts_utc is None
    db_timestamp = ts_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None) if ts_utc else None
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (db_timestamp, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {db_timestamp}")
    except psycopg2.Error as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected Error (update_last_bonus_time) for {user_id}: {e}", exc_info=True)

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Gets the last bonus time (as timezone-aware UTC datetime) for a user."""
    user_data = get_or_create_user(user_id)
    # 'last_bonus' should be timezone-aware UTC or None after processing in get_or_create_user
    return user_data.get('last_bonus') if user_data else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    """Fetches the top users by balance."""
    # Filter out users with zero or negative balance explicitly in SQL
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    leaders = []
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            leaders = cur.fetchall()
            # Ensure balance is float in the returned list
            for leader in leaders:
                if leader.get('balance') is not None:
                    try:
                        leader['balance'] = float(leader['balance'])
                    except (ValueError, TypeError):
                        logger.error(f"Could not convert leaderboard balance '{leader['balance']}' to float for user {leader.get('user_id')}")
                        leader['balance'] = 0.0 # Fallback to 0.0
                else:
                    leader['balance'] = 0.0 # Handle NULL balance case

            return leaders # Return list of dicts
    except psycopg2.Error as e:
        logger.error(f"DB Error (get_leaderboard): {e}", exc_info=True)
        return [] # Return empty list on error
    except Exception as e:
        logger.error(f"Unexpected Error (get_leaderboard): {e}", exc_info=True)
        return []

# --- Blackjack Game Utilities ---
def create_deck(num: int = NUM_DECKS) -> list[tuple[str, str]]:
    """Creates and shuffles a standard deck of cards."""
    if num <= 0: num = 1 # Ensure at least one deck
    # Use tuple for cards: (rank, suit)
    deck = [(r, s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card: tuple[str, str] | None) -> int:
    """Gets the numerical value of a card."""
    # Return 0 if card is None
    return RANK_VALUES.get(card[0], 0) if card and len(card) > 0 else 0

def get_hand_value(hand: list[tuple[str, str] | None]) -> int:
    """Calculates the value of a hand in Blackjack."""
    value = 0
    num_aces = 0
    if not hand: return 0

    for card in hand:
        if card: # Check if card is not None
            value += get_card_value(card)
            if card[0] == 'A':
                num_aces += 1

    # Adjust for Aces
    while value > 21 and num_aces > 0:
        value -= 10
        num_aces -= 1
    return value

def format_hand(hand: list[tuple[str, str] | None], hide_one: bool = False) -> str:
    """Formats a hand for display, optionally hiding the second card."""
    if not hand:
        return "Пусто"

    # Ensure cards are valid before formatting
    valid_cards = [card for card in hand if card and len(card) == 2]

    if not valid_cards:
        return "Пусто (ошибка карт)" # Indicate if hand contained invalid items

    if hide_one and len(valid_cards) > 1:
        # Show first card, hide the rest effectively
        first_card = f"{valid_cards[0][0]}{valid_cards[0][1]}"
        return f"[{first_card}, ??]"
    elif hide_one and len(valid_cards) == 1:
         # If hide_one is true but only one card, show it and indicate hidden
        return f"[{valid_cards[0][0]}{valid_cards[0][1]}, ??]"
    elif hide_one: # hide_one is true, but hand was empty after validation
         return "[??, ??]"
    else:
        # Default: format all valid cards
        return ", ".join([f"{card[0]}{card[1]}" for card in valid_cards])


def draw_card(deck: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Draws a single card from the deck (removes it). Uses simple pop after initial shuffle."""
    if not deck:
        logger.warning("Attempted to draw from an empty deck.")
        return None
    try:
        # Efficient way to draw after random.shuffle()
        return deck.pop()
    except IndexError:
        # Should be caught by the 'if not deck' but as a safeguard
        logger.error("Error drawing card: Deck unexpectedly empty (IndexError).")
        return None
    except Exception as e:
        logger.error(f"Unexpected error drawing card: {e}", exc_info=True)
        return None


# --- Roulette Game Utilities ---
def rl_get_value_display_name(bet_type: str, bet_value: str | int) -> str:
    """Gets the user-friendly display name for a bet value."""
    if bet_type == 'number':
        # Handle potential int input for numbers if needed, ensure string for display
        return f"🔢 {str(bet_value)}"
    # Use .get with a default fallback to the value itself if not in the dict
    return RL_BET_VALUE_DISPLAY_NAMES.get(str(bet_value), str(bet_value))

def rl_get_color(n_str: str) -> str | None:
    """Determines the color of a roulette number string."""
    if n_str in ['0', '00']: return 'Green'
    try:
        n = int(n_str)
        if n in RL_RED_NUMBERS: return 'Red'
        if n in RL_BLACK_NUMBERS: return 'Black'
        return None # Number out of range 1-36
    except (ValueError, TypeError):
        return None # Not a valid number string

def rl_is_even_or_odd(n_str: str) -> str | None:
    """Determines if a roulette number string is Even or Odd."""
    if n_str in ['0', '00']: return None
    try:
        return 'Even' if int(n_str) % 2 == 0 else 'Odd'
    except (ValueError, TypeError):
        return None

def rl_get_dozen(n_str: str) -> str | None:
    """Determines the dozen (1st, 2nd, 3rd) of a roulette number string."""
    if n_str in ['0', '00']: return None
    try:
        n = int(n_str)
        # Use next with a generator expression for clarity
        return next((name for name, d_set in RL_DOZENS.items() if n in d_set), None)
    except (ValueError, TypeError):
        return None

def rl_get_column(n_str: str) -> str | None:
    """Determines the column (col1, col2, col3) of a roulette number string."""
    if n_str in ['0', '00']: return None
    try:
        n = int(n_str)
        return next((name for name, c_set in RL_COLUMNS.items() if n in c_set), None)
    except (ValueError, TypeError):
        return None

# --- Helper to get User Mention (HTML) & Display Name ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock() # Use asyncio Lock for async context
_cache_ttl = 3600  # Cache for 1 hour

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> tuple[str, str]:
    """
    Fetches user's HTML mention and display name, using a timed cache.
    Returns (html_mention, display_name). Escapes display name.
    """
    now = time.monotonic()
    default_display_name = f"User_{user_id}"
    # Default mention should be safe HTML (just the default name)
    default_html_mention = html_escape(default_display_name)

    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
            # Ensure both parts are in cache before returning
            if 'html_mention' in cached and 'display_name' in cached:
                return cached['html_mention'], cached['display_name']
            else:
                 # Cache entry is incomplete, treat as miss
                 logger.debug(f"Incomplete cache entry for user {user_id}. Refetching.")

    # --- Cache miss or expired ---
    display_name = default_display_name
    html_mention = default_html_mention
    try:
        # Use context.bot which is readily available
        user_chat = await context.bot.get_chat(user_id)
        if isinstance(user_chat, User): # Check if it's a user object
             html_mention = user_chat.mention_html() # Safe HTML mention
             # Construct display name carefully
             name_parts = [user_chat.first_name, user_chat.last_name]
             full_name = " ".join(filter(None, name_parts)).strip() # Join non-empty parts
             if full_name:
                 display_name = full_name
             elif user_chat.username:
                 # Use username as fallback if full name is empty
                 display_name = f"@{user_chat.username}"
             # Escape the constructed display name for safety in HTML contexts elsewhere
             if display_name != default_display_name:
                 display_name = html_escape(display_name)
        else:
             logger.warning(f"get_chat({user_id}) returned type {type(user_chat)}, expected User.")

    except (BadRequest, Forbidden) as e:
        # Common errors when bot can't access user info
        logger.warning(f"Could not get chat for user {user_id}: {e}")
    except Exception as e:
        # Catch any other unexpected errors during get_chat
        logger.warning(f"Failed to get mention/name for {user_id}: {e}", exc_info=True)

    # Update cache
    async with _cache_lock:
        _user_mention_cache[user_id] = {
            'html_mention': html_mention,
            'display_name': display_name,
            'ts': now
        }

    return html_mention, display_name

# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    if not update.message or not update.effective_user: return # Should not happen
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")

    user_data = get_or_create_user(user.id) # Ensure user exists
    balance = user_data['balance'] if user_data else None # Use data from get_or_create
    balance_str = f"{balance:.2f}" if balance is not None else "Ошибка" # Indicate error better

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
    """Handles the /help command."""
    if not update.message or not update.effective_user: return
    user = update.effective_user
    logger.info(f"/help command from user {user.id}")
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>Общие команды:</b>\n"
        "/start - Приветствие и баланс\n"
        "/balance - Показать текущий баланс\n"
        f"/bonus - Получить бонус ({BONUS_AMOUNT:.2f} F, раз в {BONUS_COOLDOWN_HOURS} часов, только в ЛС)\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение\n\n"
        "<b>Игры:</b>\n"
        f"/blackjack - Начать игру в Блекджек ({NUM_DECKS} колод, только в ЛС)\n"
        "/roulette - Начать игру в Рулетку (ЛС и группы)\n"
        "  • В рулетке есть таймер ставок после первой ставки.\n"
        f"  • Макс. ставок на раунд: {RL_MAX_BETS_PER_ROUND} (общих), {RL_MAX_BETS_PER_USER} (на игрока).\n"
        "  • Используйте кнопки под сообщением рулетки для ставок.\n\n"
        "<i>Играйте ответственно! Удачи!</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /balance command."""
    if not update.message or not update.effective_user or not update.effective_chat: return
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/balance command from user {user.id} in chat {chat.id} (type: {chat.type})")

    # No need to call get_or_create here, get_balance handles it
    balance = get_balance(user.id)

    if balance is not None:
        await update.message.reply_text(f"Ваш текущий баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start или обратитесь к администратору.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /bonus command."""
    if not update.message or not update.effective_user or not update.effective_chat: return
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/bonus command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Получить бонус можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    # get_last_bonus_time handles user creation/fetching
    last_bonus_utc = get_last_bonus_time(user.id)

    # Check if get_last_bonus_time failed (implies DB issue)
    # We need get_or_create_user check here because get_last_bonus_time might return None
    # even if the user exists but hasn't taken a bonus yet.
    user_exists_check = get_or_create_user(user.id)
    if user_exists_check is None:
         await update.message.reply_text("Ошибка: Не удалось получить данные вашего профиля.")
         return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_utc and (now_utc < last_bonus_utc + cooldown):
        time_diff = last_bonus_utc + cooldown - now_utc
        # Calculate remaining time more robustly
        total_seconds = time_diff.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        await update.message.reply_text(
            f"⏳ Бонус уже был получен. Попробуйте снова через: <b>{hours} ч {minutes} мин</b>.",
            parse_mode=ParseMode.HTML
        )
        return

    # Attempt to update balance
    new_balance = update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        # Update bonus time only if balance update was successful
        update_last_bonus_time(user.id, now_utc)
        await update.message.reply_text(
            f"🎉 Поздравляем! Вы получили бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Ваш новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
        # Check if balance update failed due to insufficient funds (though bonus is positive)
        # or other DB issue
        current_balance = get_balance(user.id) # Check current balance again
        if current_balance is not None:
            await update.message.reply_text(f"❌ Ошибка при начислении бонуса. Ваш баланс: {current_balance:.2f} F. Пожалуйста, попробуйте позже или свяжитесь с администратором.")
        else:
            await update.message.reply_text("❌ Критическая ошибка при начислении бонуса и проверке баланса. Пожалуйста, попробуйте позже или свяжитесь с администратором.")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /leaderboard command."""
    if not update.message or not update.effective_user or not update.effective_chat: return
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/leaderboard command from user {user.id} in chat {chat.id} (type: {chat.type})")

    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("🏆 Таблица лидеров пока пуста.")
        return

    leaderboard_text = f"🏆 <b>Таблица Лидеров (Топ {min(LEADERBOARD_LIMIT, len(leaders))})</b> 🏆\n\n" # Adjust title if fewer leaders than limit
    place_emojis = ["🥇", "🥈", "🥉"]

    # Fetch display names efficiently
    user_ids = [leader['user_id'] for leader in leaders]
    display_name_map = {}
    if user_ids:
        try:
            # Use asyncio.gather for concurrent fetching
            mention_data_list = await asyncio.gather(*(get_user_mention(context, uid) for uid in user_ids))
            # Create map from user_id to display_name (second element of tuple)
            display_name_map = {uid: mention_data[1] for i, uid in enumerate(user_ids) for mention_data in [mention_data_list[i]]} # Corrected map creation
        except Exception as e:
            logger.error(f"Failed to fetch user display names for leaderboard: {e}", exc_info=True)
            # Fallback to User_ID if fetching fails
            display_name_map = {uid: f"User_{uid}" for uid in user_ids}

    # Build the leaderboard text
    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        # Use display name from map, fallback if somehow missing
        name = display_name_map.get(leader['user_id'], f"User_{leader['user_id']}")
        balance_str = f"{leader.get('balance', 0.0):.2f}" # Use .get with default
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"

    try:
        await update.message.reply_text(
            leaderboard_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True # Good practice for leaderboards
        )
    except Forbidden:
        logger.error(f"Forbidden: Cannot send leaderboard to chat {chat.id}")
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров из-за ошибки.")


# --- Blackjack Game (Private Chat Only - Persistent Deck) ---
async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /blackjack command or 'New Game' button."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat: return # Basic check

    logger.info(f"BJ /blackjack or 'New Game' callback from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        reply_func = query.message.reply_text if query else update.message.reply_text
        await reply_func("Играть в Блекджек можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        if query: await query.answer() # Answer callback even if rejecting
        return

    is_callback = query is not None
    source_message = query.message if is_callback else update.message
    callback_message_id = source_message.message_id if is_callback else None
    effective_chat_id = chat.id

    if is_callback:
        try:
            # Answer callback quickly
            await query.answer("Новая игра...")
        except BadRequest as e:
             # Ignore "Query is too old" errors silently
            if "query is too old" not in str(e).lower():
                 logger.warning(f"Failed to answer callback query in blackjack_start_command: {e}")
        except Exception as e:
            logger.warning(f"Failed to answer callback query in blackjack_start_command: {e}")


    # --- Game State Handling ---
    # Use context.user_data for private chat game state
    bj_session_data = context.user_data.get(BJ_GAME_KEY)
    current_game_message_id = bj_session_data.get('message_id') if bj_session_data else None

    # Scenario 1: User clicks "New Game" button after a round finished
    if is_callback and bj_session_data and bj_session_data.get('state') == 'waiting_bet':
        logger.info(f"BJ 'New Game' callback: Proceeding to bet selection for user {user.id} with existing deck.")
        # Edit the existing message (which showed the 'New Game' button) to show the bet prompt
        await blackjack_show_state(
            context, effective_chat_id, user.id,
            game_state=bj_session_data,
            edit_existing=True, # Try to edit the message
            force_game_over_display=False # Show bet prompt, not game over
        )
        return # Stop here, bet prompt is shown

    # Scenario 2: User types /blackjack or clicks "New Game" without a valid 'waiting_bet' session
    logger.info(f"Initializing new BJ session or resetting existing one for user {user.id}.")

    # --- Cleanup Old Message if Necessary ---
    # Delete the previous game message if its ID is known and different from the callback message ID
    if current_game_message_id and current_game_message_id != callback_message_id:
        try:
            await context.bot.delete_message(effective_chat_id, current_game_message_id)
            logger.debug(f"Deleted previous BJ message {current_game_message_id} for user {user.id}")
        except Exception as e:
            logger.debug(f"Failed to delete old BJ message {current_game_message_id}: {e}")

    # --- Initialize New Game State ---
    persistent_deck = create_deck()
    total_cards_in_deck = len(persistent_deck)
    # Ensure threshold is at least 1 card if percentage is very low
    shuffle_threshold_count = max(1, math.ceil(total_cards_in_deck * (1.0 - BJ_SHUFFLE_PERCENTAGE))) # Threshold is when this many cards are LEFT

    # Reset user_data for the game
    context.user_data[BJ_GAME_KEY] = {
        'persistent_deck': persistent_deck,
        'total_cards_in_deck': total_cards_in_deck,
        'shuffle_threshold_count': shuffle_threshold_count,
        'state': 'waiting_bet', # Start by waiting for the first bet
        'message_id': None, # Will be set when message is sent
        # Clear potential leftovers from previous games
        'player_hands': [],
        'dealer_hand': [],
        'outcome_text': None,
        'outcome_determined': False,
        'split_count': 0,
    }
    logger.info(f"User {user.id}: Deck created ({total_cards_in_deck} cards). Shuffle when <= {shuffle_threshold_count} cards remain.")

    # --- Get Balance and Check Viability ---
    balance = get_balance(user.id) # Handles user creation if needed

    if balance is None:
        reply_func = context.bot.send_message # Always send new if error occurred
        await reply_func(chat_id=effective_chat_id, text="Не удалось получить ваш баланс. Попробуйте /start.", parse_mode=ParseMode.HTML)
        context.user_data.pop(BJ_GAME_KEY, None) # Clean up game state on error
        return
    if balance <= 0:
        reply_func = context.bot.send_message
        await reply_func(chat_id=effective_chat_id, text=f"Ваш баланс (<b>{balance:.2f}</b> F) недостаточен для игры. Попробуйте /bonus.", parse_mode=ParseMode.HTML)
        context.user_data.pop(BJ_GAME_KEY, None)
        return

    # --- Show Bet Prompt ---
    # This part is now handled by calling blackjack_show_state
    newly_created_session_data = context.user_data.get(BJ_GAME_KEY)
    if newly_created_session_data:
         # If started via callback, delete the message with the "New Game" button
        if is_callback and callback_message_id:
             try:
                 await context.bot.delete_message(effective_chat_id, callback_message_id)
                 logger.debug(f"Deleted callback message {callback_message_id} before showing bet prompt.")
             except Exception as e:
                 logger.warning(f"Failed to delete callback message {callback_message_id}: {e}")

        await blackjack_show_state(
            context, effective_chat_id, user.id,
            game_state=newly_created_session_data,
            edit_existing=False, # Always send a new message for a new game start/reset
            force_game_over_display=False
        )
    else:
        logger.error(f"BJ session data unexpectedly missing after initialization for user {user.id}")
        await context.bot.send_message(effective_chat_id, "❌ Произошла ошибка при инициализации игры.")


async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    """Handles player clicking a bet amount button."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return

    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id
    bet_prompt_message_id = query.message.message_id

    bj_session_data = context.user_data.get(BJ_GAME_KEY, {})

    # --- Validate State ---
    if not bj_session_data:
        await query.answer("Игра не найдена. Начните новую /blackjack", show_alert=True)
        try: await query.edit_message_reply_markup(reply_markup=None) # Remove buttons
        except Exception: pass
        return
    if bj_session_data.get('state') != 'waiting_bet':
        await query.answer("Сейчас нельзя сделать ставку.", show_alert=False)
        return
    if bj_session_data.get('message_id') != bet_prompt_message_id:
        await query.answer("Эта кнопка ставки больше неактивна.", show_alert=False)
        try: await query.edit_message_reply_markup(reply_markup=None) # Remove buttons
        except Exception: pass
        return

    # --- Validate Bet Amount ---
    balance = get_balance(user_id)
    if balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True)
        return
    if not (0 < bet <= balance):
        await query.answer(f"Недопустимая ставка ({bet} F) или недостаточно средств ({balance:.2f} F).", show_alert=True)
        return

    # --- Prepare for Dealing ---
    persistent_deck = bj_session_data.get('persistent_deck')
    shuffle_threshold = bj_session_data.get('shuffle_threshold_count')
    total_cards = bj_session_data.get('total_cards_in_deck', NUM_DECKS * 52)

    if persistent_deck is None or shuffle_threshold is None:
        logger.error(f"Persistent deck data missing for user {user_id}. Cannot proceed.")
        await query.answer("Ошибка данных игры (колода отсутствует). Попробуйте /blackjack", show_alert=True)
        context.user_data.pop(BJ_GAME_KEY, None)
        try: await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
        except Exception: pass
        return

    # --- Check for Shuffle ---
    cards_remaining = len(persistent_deck)
    shuffle_notification = "" # Initialize as empty string
    logger.info(f"User {user_id}: Starting hand. Cards remaining: {cards_remaining}/{total_cards}. Shuffle threshold: <= {shuffle_threshold}")

    if cards_remaining <= shuffle_threshold:
        logger.warning(f"User {user_id}: Deck penetration reached ({cards_remaining} <= {shuffle_threshold}). Shuffling...")
        persistent_deck = create_deck() # Create and shuffle a new deck
        bj_session_data['persistent_deck'] = persistent_deck
        bj_session_data['total_cards_in_deck'] = len(persistent_deck) # Update total card count
        # Recalculate threshold based on new deck size (should be same if NUM_DECKS is constant)
        bj_session_data['shuffle_threshold_count'] = max(1, math.ceil(len(persistent_deck) * (1.0 - BJ_SHUFFLE_PERCENTAGE)))
        cards_remaining = len(persistent_deck)
        shuffle_notification = "♻️ Идет перетасовка колоды...\n\n"
        logger.info(f"User {user_id}: Deck reshuffled. Cards remaining: {cards_remaining}")
        # Show shuffle message briefly (edit the bet prompt message)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=bet_prompt_message_id,
                text=shuffle_notification + f"Ваш баланс: {balance:.2f} F\nРаздаем карты...",
                parse_mode=ParseMode.HTML, reply_markup=None # Remove bet buttons
            )
            await asyncio.sleep(1.5) # Pause to show message
        except BadRequest as e:
             if "message is not modified" not in str(e).lower():
                 logger.warning(f"Could not edit message {bet_prompt_message_id} to show shuffle: {e}")
        except Exception as e:
            logger.warning(f"Could not edit message {bet_prompt_message_id} to show shuffle: {e}")
        # Proceed even if edit fails

    # --- Deduct Bet ---
    new_balance = update_balance(user_id, -bet)
    if new_balance is None:
        # Handle case where deduction failed (e.g., DB error, constraint violation)
        await query.answer("Ошибка при списании ставки.", show_alert=True)
        # Don't proceed with dealing
        return

    # --- Deal Cards ---
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0
    deck_to_draw_from = bj_session_data['persistent_deck'] # Use the potentially updated deck

    try:
        for _ in range(2):
            card_p = draw_card(deck_to_draw_from)
            if not card_p: raise IndexError("Deck empty during player deal")
            player_hand.append(card_p); cards_dealt_count += 1

            card_d = draw_card(deck_to_draw_from)
            if not card_d: raise IndexError("Deck empty during dealer deal")
            dealer_hand.append(card_d); cards_dealt_count += 1
        logger.info(f"User {user_id}: Dealt initial hands. Cards remaining after deal: {len(deck_to_draw_from)}")
    except IndexError as e:
        logger.error(f"BJ dealing error for user {user_id}: {e}")
        update_balance(user_id, bet) # Refund bet on error
        try:
             # Try to edit the message to show error, fallback to send
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=bet_prompt_message_id,
                text=f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.",
                reply_markup=None
            )
        except Exception:
            await context.bot.send_message(chat_id, f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.")
        context.user_data.pop(BJ_GAME_KEY, None) # End game session
        return
    except Exception as e:
        logger.error(f"BJ unexpected dealing error for user {user_id}: {e}", exc_info=True)
        update_balance(user_id, bet) # Refund bet
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=bet_prompt_message_id,
                text=f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.",
                reply_markup=None
            )
        except Exception:
             await context.bot.send_message(chat_id, f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.")
        context.user_data.pop(BJ_GAME_KEY, None)
        return

    # --- Initial Game State Setup ---
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    # Blackjack check: 21 on first two cards
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    game_state = 'player_turn'
    hand_status = 'active'
    outcome_text = None
    winnings = 0.0 # Track winnings paid immediately (for BJ)
    outcome_determined = False

    if player_has_blackjack:
        hand_status = 'blackjack'
        game_state = 'game_over' # Game ends immediately if player has BJ
        outcome_determined = True
        if dealer_has_blackjack:
            outcome_text = "⚖️ Ничья! У обоих Блекджек."
            winnings = bet # Return original bet
            update_balance(user_id, bet) # Refund push
        else:
            bj_payout_amount = bet * BLACKJACK_PAYOUT
            winnings = bet + bj_payout_amount # Original bet + payout
            update_balance(user_id, winnings) # Pay winnings
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {bj_payout_amount:.2f} F!"
    elif dealer_has_blackjack:
        game_state = 'game_over' # Game ends immediately
        outcome_determined = True
        outcome_text = "😥 У дилера Блекджек! Вы проиграли."
        winnings = 0.0 # Player loses bet (already deducted)

    # --- Update Session Data ---
    # Check balance again for double/split possibility check later
    current_balance_after_bet = get_balance(user_id)
    can_double = (game_state == 'player_turn' and len(player_hand) == 2 and current_balance_after_bet is not None and current_balance_after_bet >= bet)
    can_split = (game_state == 'player_turn' and len(player_hand) == 2 and
                 player_hand[0] and player_hand[1] and # Ensure cards exist
                 get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
                 current_balance_after_bet is not None and current_balance_after_bet >= bet and
                 bj_session_data.get('split_count', 0) < MAX_SPLITS)


    bj_session_data.update({
        'state': game_state,
        'player_hands': [{ # Store hands as a list of dicts for splits
            'hand': player_hand,
            'bet': bet,
            'status': hand_status, # 'active', 'blackjack', 'bust', 'stand'
            'can_double': can_double,
            'can_split': can_split
        }],
        'current_hand_index': 0, # Index of the hand being played
        'dealer_hand': dealer_hand,
        'initial_bet': bet, # Store initial bet for reference if needed
        'split_count': 0, # Initialize split count
        'outcome_text': outcome_text, # Store immediate outcome text if any
        'outcome_determined': outcome_determined,
        'total_winnings_paid': winnings, # Track winnings paid out so far
        'shuffle_occurred_message': shuffle_notification # Store to display once
    })

    # --- Delete the bet prompt message ---
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e:
        logger.warning(f"Could not delete bet prompt message {bet_prompt_message_id}: {e}")
        # Continue anyway, but clear the message_id from state if deletion failed? No, show_state will handle it.

    # --- Show Initial Hand State ---
    # Always send a new message for the game state after bet confirmed
    await blackjack_show_state(
        context, chat_id, user_id,
        game_state=bj_session_data,
        edit_existing=False
    )

    # Answer the original bet button callback *after* showing the state
    if not shuffle_notification: # Don't show standard answer if shuffle message was shown
       try:
           await query.answer(f"Ставка принята: {bet} F")
       except BadRequest as e:
           if "query is too old" not in str(e).lower():
               logger.debug(f"Query answer failed (too old?): {e}")
       except Exception as e:
            logger.warning(f"Failed to answer bet callback query: {e}")

    # If game ended immediately (BJ checks), state is already 'game_over'.
    # The blackjack_show_state called above will handle displaying the final outcome
    # and the "New Game" button because 'outcome_determined' is true.
    # We need to reset state for the 'New Game' button logic.
    if bj_session_data.get('outcome_determined'):
        logger.info(f"BJ immediate outcome: State will be reset to 'waiting_bet' for user {user_id} after display.")
        # Prepare for the next round / 'New Game' button functionality
        bj_session_data['state'] = 'waiting_bet'
        # Clear sensitive data for the next round, keep deck and threshold
        bj_session_data.pop('player_hands', None)
        bj_session_data.pop('dealer_hand', None)
        bj_session_data.pop('current_hand_index', None)
        bj_session_data.pop('split_count', None)
        # Keep outcome_text for the final display triggered by show_state
        # Keep outcome_determined flag for show_state logic
        # message_id is updated by show_state


async def blackjack_show_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    game_state: dict | None = None,
    edit_existing: bool = True,
    force_game_over_display: bool = False, # Flag to show summary + New Game button
    final_outcome_text: str | None = None # Text from determine_outcome (overrides game_state['outcome_text'])
) -> Message | int | None:
    """
    Updates or sends the Blackjack game state message. Can show active game,
    bet prompt, or end-of-round summary.
    Returns the message object if a new message was sent, message_id if edited, or None on error.
    """
    if game_state is None:
        game_state = context.user_data.get(BJ_GAME_KEY)

    if not game_state:
        logger.warning(f"blackjack_show_state called for user {user_id} but no game session state found.")
        return None

    message_id_to_process = game_state.get('message_id')
    current_state = game_state.get('state', 'unknown')

    # Determine if we should attempt editing or force send new
    attempt_edit = edit_existing and message_id_to_process is not None
    if edit_existing and not message_id_to_process:
        logger.debug(f"BJ show_state: Attempted to edit but no message_id for user {user_id}. Forcing send new.")
        attempt_edit = False

    # --- Get Required Data ---
    balance = get_balance(user_id) # Refresh balance
    balance_str = f"{balance:.2f}" if balance is not None else "Ошибка"

    # Determine if we are showing the end-of-round summary
    # This happens if forced, or if state is 'waiting_bet' AND an outcome text exists
    has_outcome = final_outcome_text is not None or game_state.get('outcome_determined', False)
    show_final_summary = force_game_over_display or (current_state == 'waiting_bet' and has_outcome)

    # Check for one-time shuffle message
    shuffle_msg = game_state.pop('shuffle_occurred_message', '') # Get and remove

    text = shuffle_msg # Prepend shuffle message if it exists
    text += f"<b>Блекджек</b> | Баланс: <b>{balance_str}</b> F\n"

    # --- Calculate Deck Separator ---
    deck = game_state.get('persistent_deck', [])
    deck_len = len(deck)
    cards_per_deck = 52 # Standard
    # Approximate remaining decks visual indicator
    remaining_decks_approx = max(0, deck_len / cards_per_deck) # Use float for finer scale
    # Scale dashes, e.g., 8 decks = 8 dashes, 1 deck = 1 dash
    num_dashes = max(1, round(remaining_decks_approx * (NUM_DECKS / NUM_DECKS))) # Simpler: use NUM_DECKS or a fixed max
    num_dashes = max(1, min(NUM_DECKS, round(remaining_decks_approx))) # Ensure 1 to NUM_DECKS dashes
    separator = "─" * num_dashes # Use a nicer dash character

    keyboard_rows = [] # Initialize keyboard rows list

    # --- Build Text and Keyboard based on State ---

    if show_final_summary:
        # --- Display End-of-Round Summary ---
        text += f"{separator}\n"
        text += "<b>🏁 Раунд окончен! 🏁</b>\n"
        # Use passed outcome text first, then from game_state
        outcome_content = final_outcome_text if final_outcome_text is not None else game_state.get('outcome_text', 'Результат не определен.')
        text += outcome_content
        # Show final balance after payout/loss
        final_balance = get_balance(user_id) # Get balance again after potential updates
        text += f"\n\nИтоговый баланс: <b>{final_balance:.2f}</b> F." if final_balance is not None else ""

        keyboard_rows.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
        # Ensure state is correctly set for the next round trigger
        game_state['state'] = 'waiting_bet'
        game_state['outcome_determined'] = False # Reset flag after display
        game_state.pop('outcome_text', None) # Clean up text

    elif current_state == 'waiting_bet' and not show_final_summary:
        # --- Display Bet Prompt ---
        text += f"{separator}\n"
        text += f"Ваш баланс: <b>{balance_str}</b> F.\nВыберите вашу ставку:"

        bet_options = [1, 5, 10, 25, 50, 100, 250, 500, 1000] # Example bets
        valid_bets = [b for b in bet_options if balance is not None and b <= balance]

        if not valid_bets:
            min_bet_opt = min(bet_options) if bet_options else 1
            if balance is not None and balance > 0:
                text += f"\n<i>Недостаточно средств для минимальной ставки ({min_bet_opt} F).</i>"
            elif balance == 0:
                 text += "\n<i>Недостаточно средств для игры. Попробуйте /bonus</i>"
            else: # balance is None
                 text += "\n<i>Ошибка получения баланса.</i>"
        else:
            # Create button rows (e.g., 4 buttons per row)
            row = []
            for bet in valid_bets:
                row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
                if len(row) == 4:
                    keyboard_rows.append(row)
                    row = []
            if row: # Add remaining buttons
                keyboard_rows.append(row)

    elif current_state in ['player_turn', 'dealer_turn']:
        # --- Display Active Game State ---
        player_hands_data = game_state.get('player_hands', [])
        dealer_hand = game_state.get('dealer_hand', [])
        current_hand_idx = game_state.get('current_hand_index', -1)

        total_bet = sum(h.get('bet', 0) for h in player_hands_data if isinstance(h, dict))
        num_hands = len(player_hands_data)
        text += f"Общая ставка: <b>{total_bet}</b> F{' (Рук: ' + str(num_hands) + ')' if num_hands > 1 else ''}\n"
        text += f"{separator}\n"

        # Determine if dealer's card should be hidden
        # Hide if it's player's turn AND not all player hands are finished
        all_player_hands_finished = all(
            isinstance(h, dict) and h.get('status') in ['bust', 'stand', 'blackjack']
            for h in player_hands_data
        )
        hide_dealer_card = (current_state == 'player_turn' and not all_player_hands_finished)
        dealer_value = get_hand_value(dealer_hand)
        dealer_value_display = "??"
        if dealer_hand:
            if hide_dealer_card and len(dealer_hand) > 0 and dealer_hand[0]:
                # Show only the first card's value + ?
                 dealer_value_display = f"{get_card_value(dealer_hand[0])}+?"
            elif not hide_dealer_card:
                 dealer_value_display = str(dealer_value)
            # If hide_dealer_card is true but dealer hand has only 1 card, format_hand handles it

        text += f"<b>Диллер:</b> {format_hand(dealer_hand, hide_one=hide_dealer_card)} ({dealer_value_display})\n\n"
        text += "<b>Вы:</b>\n"
        active_hand_data_for_buttons = None

        for i, hand_data in enumerate(player_hands_data):
            if not isinstance(hand_data, dict): continue # Skip invalid entries

            hand = hand_data.get('hand', [])
            hand_value = get_hand_value(hand)
            hand_status = hand_data.get('status', '?')
            hand_bet = hand_data.get('bet', 0)

            is_current_turn = (i == current_hand_idx and hand_status == 'active' and current_state == 'player_turn')
            indicator = "▶️" if is_current_turn else \
                        ("✅" if hand_status == 'stand' else \
                         ("❌" if hand_status == 'bust' else \
                          ("💰" if hand_status == 'blackjack' else \
                           "❔"))) # Default/unknown indicator

            text += f"{indicator} Рука {i+1}: {format_hand(hand)} (<b>{hand_value}</b>) [<i>{hand_bet} F</i>]"

            status_label = ""
            if hand_status == 'bust': status_label = " - <b>Перебор!</b>"
            elif hand_status == 'blackjack': status_label = " - <b>Блекджек!</b>"
            elif hand_status == 'stand': status_label = " - <i>Стоп</i>"

            text += status_label + "\n"

            if is_current_turn:
                active_hand_data_for_buttons = hand_data # Store data for button generation

        # --- Add Action Buttons ---
        if current_state == 'player_turn' and active_hand_data_for_buttons:
            action_buttons = [
                InlineKeyboardButton("Еще", callback_data=f"bj_hit_{current_hand_idx}"),
                InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{current_hand_idx}")
            ]
            keyboard_rows.append(action_buttons)

            special_buttons = []
            player_bet = active_hand_data_for_buttons.get('bet', 0)
            # Check flags AND balance for double/split
            can_double_flag = active_hand_data_for_buttons.get('can_double', False)
            can_split_flag = active_hand_data_for_buttons.get('can_split', False)

            if can_double_flag and balance is not None and balance >= player_bet:
                special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{current_hand_idx}"))
            if can_split_flag and balance is not None and balance >= player_bet:
                 # Re-verify split conditions slightly more strictly here if needed
                 player_hand = active_hand_data_for_buttons.get('hand', [])
                 if (len(player_hand) == 2 and player_hand[0] and player_hand[1] and
                     get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
                     game_state.get('split_count', 0) < MAX_SPLITS):
                     special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{current_hand_idx}"))

            if special_buttons:
                keyboard_rows.append(special_buttons)

        elif current_state == 'dealer_turn':
            text += "\n<i>⏳ Ход дилера...</i>"

    # --- Finalize Keyboard ---
    reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    # --- Send/Edit Message with Retries ---
    result: Message | int | None = None
    max_retries = 2 # Allow a retry
    current_retry = 0
    edit_failed_and_sending_new = False

    while current_retry <= max_retries:
        try:
            if attempt_edit:
                logger.debug(f"Attempting edit (try {current_retry+1}) BJ state msg {message_id_to_process} for user {user_id}")
                # Edit returns True on success, raises error otherwise
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id_to_process, text=text,
                    reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
                logger.debug(f"Successfully edited BJ state msg {message_id_to_process}")
                result = message_id_to_process # Return ID if edit successful
                break
            else:
                # --- Send New Message ---
                logger.debug(f"Sending NEW BJ state message for user {user_id} (Attempt edit: {attempt_edit}, Edit failed: {edit_failed_and_sending_new})")
                # If we are here because edit failed, try deleting the old one first
                if edit_failed_and_sending_new and message_id_to_process:
                    try:
                        await context.bot.delete_message(chat_id, message_id_to_process)
                    except Exception: pass # Ignore deletion error

                new_message = await context.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
                # Update message_id in the *current* game state immediately
                current_session_state = context.user_data.get(BJ_GAME_KEY)
                if current_session_state:
                    current_session_state['message_id'] = new_message.message_id
                    logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}. Updated game state.")
                else:
                    # This is problematic, game state disappeared
                    logger.warning(f"Sent NEW BJ state msg {new_message.message_id}, but session state missing for user {user_id}.")
                result = new_message # Return the new message object
                break
        except BadRequest as e:
            error_str = str(e).lower()
            if "message is not modified" in error_str:
                logger.debug(f"BJ state msg {message_id_to_process} not modified.")
                result = message_id_to_process # Treat as success
                break
            elif "message to edit not found" in error_str or "chat not found" in error_str or "message can't be edited" in error_str:
                logger.warning(f"Message {message_id_to_process} or Chat {chat_id} not found/editable for user {user_id}. Forcing send new.")
                attempt_edit = False # Don't try editing again
                edit_failed_and_sending_new = True # Flag that edit failed
                # Clear message_id in state so next loop iteration sends new
                current_session_state_on_fail = context.user_data.get(BJ_GAME_KEY)
                if current_session_state_on_fail:
                    current_session_state_on_fail['message_id'] = None
                current_retry += 1 # Consume a retry attempt
                if current_retry > max_retries:
                     logger.error(f"CRITICAL: Failed to send new message after edit failed for user {user_id}. Cleaning session.")
                     context.user_data.pop(BJ_GAME_KEY, None)
                     result = None
                     break
                # Continue loop to attempt sending new
            elif "can't parse entities" in error_str:
                logger.error(f"HTML Parsing Error for user {user_id} (msg {message_id_to_process}): {e}\nText snippet: {text[:200]}...")
                result = None # Abort, cannot fix text here easily
                break
            elif "message text is empty" in error_str:
                logger.error(f"BJ show_state: Attempted to send empty message for user {user_id}. Text: '{text}'")
                result = None # Abort
                break
            else:
                # Other BadRequest errors, retry might help
                logger.warning(f"Edit/Send BJ state BadRequest for user {user_id} (msg {message_id_to_process}) (try {current_retry+1}): {e}")
                result = None
                current_retry += 1
                await asyncio.sleep(0.5 * current_retry) # Backoff slightly
        except Forbidden as e:
            logger.error(f"Forbidden error for user {user_id} in chat {chat_id}: {e}")
            context.user_data.pop(BJ_GAME_KEY, None) # Clean up state if forbidden
            result = None
            break
        except Exception as e:
            logger.error(f"Unexpected error in blackjack_show_state for user {user_id} (try {current_retry+1}): {e}", exc_info=True)
            result = None
            current_retry += 1
            await asyncio.sleep(0.5 * current_retry) # Backoff slightly

    if result is None:
        logger.error(f"Failed to update/send BJ state for user {user_id} after {max_retries+1} attempts.")
        # Optionally try sending a basic error message if state update failed critically
        # if not isinstance(result, (Message, int)): # Check if it really failed
        #    try: await context.bot.send_message(chat_id, "⚠️ Ошибка обновления отображения игры.")
        #    except Exception: pass

    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    """Handles player actions: Hit, Stand, Double, Split."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return

    user = query.from_user
    user_id = user.id
    chat_id = query.message.chat_id
    action_message_id = query.message.message_id

    # Extract action and hand index
    if len(parts) < 2:
        await query.answer("Ошибка: Неверный формат действия.", show_alert=True)
        return
    action, hand_index_str = parts[0], parts[1]
    try:
        hand_index = int(hand_index_str)
    except (ValueError, TypeError):
        await query.answer("Ошибка: Неверный индекс руки.", show_alert=True)
        return

    # --- Validate State ---
    bj_session_data = context.user_data.get(BJ_GAME_KEY, {})
    if not bj_session_data:
        await query.answer("Игра не найдена.", show_alert=True)
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return
    if bj_session_data.get('state') != 'player_turn':
        await query.answer("Сейчас не ваш ход.", show_alert=False)
        return
    if bj_session_data.get('message_id') != action_message_id:
        await query.answer("Эта игра или действие больше неактивны.", show_alert=False)
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    player_hands = bj_session_data.get('player_hands', [])
    # Check if the index is valid and corresponds to the currently active hand
    if not (0 <= hand_index < len(player_hands)) or hand_index != bj_session_data.get('current_hand_index', -1):
        await query.answer("Сейчас ход другой руки.", show_alert=False)
        return

    current_hand_data = player_hands[hand_index]
    # Check if the hand data is valid and the status is 'active'
    if not isinstance(current_hand_data, dict) or current_hand_data.get('status') != 'active':
        await query.answer("Эта рука неактивна для действий.", show_alert=False)
        return

    # --- Prepare for Action ---
    hand = current_hand_data.get('hand', [])
    deck_to_draw_from = bj_session_data.get('persistent_deck')
    if deck_to_draw_from is None:
        logger.error(f"Persistent deck missing during action '{action}' for user {user_id}")
        await query.answer("Критическая ошибка: Колода не найдена!", show_alert=True)
        context.user_data.pop(BJ_GAME_KEY, None)
        try: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    balance = get_balance(user_id)
    bet = current_hand_data.get('bet', 0)
    needs_state_update = False # Flag to update display
    move_to_next_hand_or_dealer = False # Flag to trigger next step

    # --- Execute Action ---
    try:
        if action == 'hit':
            card = draw_card(deck_to_draw_from)
            if card:
                hand.append(card)
                current_hand_data['can_double'] = False # Cannot double after hit
                current_hand_data['can_split'] = False # Cannot split after hit
                hand_value = get_hand_value(hand)
                await query.answer(f"Взяли: {card[0]}{card[1]}")
                needs_state_update = True
                if hand_value > 21:
                    current_hand_data['status'] = 'bust'
                    move_to_next_hand_or_dealer = True
                elif hand_value == 21:
                    # Auto-stand on 21 after hit
                    current_hand_data['status'] = 'stand'
                    move_to_next_hand_or_dealer = True
            else:
                # Deck empty during hit
                raise IndexError("Draw fail (deck empty or error)")

        elif action == 'stand':
            current_hand_data['status'] = 'stand'
            await query.answer("Стоп.")
            needs_state_update = True
            move_to_next_hand_or_dealer = True

        elif action == 'double':
            can_double_flag = current_hand_data.get('can_double', False)
            can_afford = balance is not None and balance >= bet
            if can_double_flag and can_afford:
                new_balance = update_balance(user_id, -bet) # Deduct additional bet
                if new_balance is not None:
                    current_hand_data['bet'] += bet
                    current_hand_data['can_double'] = False # Can only double once
                    current_hand_data['can_split'] = False
                    balance = new_balance # Update local balance copy

                    card = draw_card(deck_to_draw_from)
                    drawn_card_str = ""
                    if card:
                        hand.append(card)
                        hand_value = get_hand_value(hand)
                        current_hand_data['status'] = 'bust' if hand_value > 21 else 'stand' # Double stands after 1 card
                        drawn_card_str = f" Карта: {card[0]}{card[1]}. Итог: {hand_value}{' (Перебор!)' if hand_value > 21 else ''}"
                    else:
                        # Deck empty on double draw - treat as stand? Or error? Let's stand.
                        current_hand_data['status'] = 'stand'
                        drawn_card_str = " Ошибка взятия карты (колода пуста?)."
                        logger.warning(f"BJ double failed draw for user {user_id}")

                    await query.answer(f"Удвоено!{drawn_card_str}", show_alert=("Ошибка" in drawn_card_str))
                    needs_state_update = True
                    move_to_next_hand_or_dealer = True # Doubling ends turn for the hand
                else:
                    await query.answer("Ошибка списания средств для удвоения.", show_alert=True)
            else:
                reason = "недостаточно средств" if not can_afford else "удвоить сейчас нельзя"
                await query.answer(f"Нельзя удвоить ({reason}).", show_alert=True)

        elif action == 'split':
            can_split_flag = current_hand_data.get('can_split', False)
            # Re-verify conditions fully before executing
            can_afford = balance is not None and balance >= bet
            split_count = bj_session_data.get('split_count', 0)
            is_pair = (len(hand) == 2 and hand[0] and hand[1] and
                       get_card_value(hand[0]) == get_card_value(hand[1]))

            if can_split_flag and is_pair and can_afford and split_count < MAX_SPLITS:
                new_balance = update_balance(user_id, -bet) # Deduct bet for new hand
                if new_balance is not None:
                    bj_session_data['split_count'] = split_count + 1
                    balance = new_balance # Update local balance

                    # Perform the split
                    card_to_move = hand.pop() # Take second card for the new hand
                    new_hand_data = {
                        'hand': [card_to_move], # Start new hand with the moved card
                        'bet': bet,
                        'status': 'active',
                        'can_double': False, # Will be set after drawing
                        'can_split': False
                    }

                    # Draw one card for each hand
                    card1 = draw_card(deck_to_draw_from)
                    card2 = draw_card(deck_to_draw_from)

                    # Check if draws were successful
                    if card1 and card2:
                        hand.append(card1) # Add card to original hand
                        new_hand_data['hand'].append(card2) # Add card to new hand

                        # Insert the new hand *after* the current one
                        player_hands.insert(hand_index + 1, new_hand_data)

                        # Special rule for splitting Aces: Each Ace gets only one card and stands
                        is_ace_split = get_card_value(hand[0]) == 11 # Check original card value

                        if is_ace_split:
                            current_hand_data['status'] = 'stand'
                            new_hand_data['status'] = 'stand'
                            current_hand_data['can_double'] = False # No actions after Ace split
                            new_hand_data['can_double'] = False
                            await query.answer("Тузы разделены и стоят.")
                            # Move to next hand immediately will be handled by job
                            move_to_next_hand_or_dealer = True
                        else:
                            # Check for Blackjack on either hand after split (counts as 21, not BJ)
                            if get_hand_value(hand) == 21: current_hand_data['status'] = 'stand'
                            if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                            # Determine if hands can be doubled or re-split
                            current_balance_after_split = balance # Use updated balance
                            limit_ok_for_resplit = bj_session_data.get('split_count', 0) < MAX_SPLITS

                            # Check can_double for original hand
                            current_hand_data['can_double'] = (current_hand_data['status'] == 'active' and
                                                               len(hand) == 2 and
                                                               current_balance_after_split >= current_hand_data['bet'])
                            # Check can_split for original hand (re-split)
                            current_hand_data['can_split'] = (current_hand_data['status'] == 'active' and
                                                             len(hand) == 2 and hand[0] and hand[1] and
                                                             get_card_value(hand[0]) == get_card_value(hand[1]) and
                                                             limit_ok_for_resplit and
                                                             current_balance_after_split >= current_hand_data['bet'])

                             # Check can_double for new hand
                            new_hand_data['can_double'] = (new_hand_data['status'] == 'active' and
                                                           len(new_hand_data['hand']) == 2 and
                                                           current_balance_after_split >= new_hand_data['bet'])
                            # Check can_split for new hand (re-split)
                            new_hand_data['can_split'] = (new_hand_data['status'] == 'active' and
                                                          len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and
                                                          get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and
                                                          limit_ok_for_resplit and
                                                          current_balance_after_split >= new_hand_data['bet'])

                            await query.answer("Рука разделена!")
                            # If the *first* hand stood immediately (e.g. got 21), move to next
                            if current_hand_data['status'] == 'stand':
                                 move_to_next_hand_or_dealer = True
                        needs_state_update = True

                    else:
                        # Draw failed (deck likely empty) - Undo split
                        logger.warning(f"BJ split failed to draw both cards for user {user_id}. Deck empty? Undoing.")
                        hand.append(card_to_move) # Put card back
                        bj_session_data['split_count'] -= 1 # Decrement count
                        update_balance(user_id, bet) # Refund the split bet
                        # Re-evaluate original hand's double/split possibility if needed?
                        current_hand_data['can_split'] = True # Allow trying again maybe? Or just error out.
                        await query.answer("Ошибка разделения: не хватило карт! Ставка возвращена.", show_alert=True)
                        needs_state_update = True # Show state without split

                else:
                    await query.answer("Ошибка списания средств для разделения.", show_alert=True)
            else:
                 reason = "не пара" if not is_pair else \
                          "недостаточно средств" if not can_afford else \
                          f"макс. сплитов ({MAX_SPLITS})" if split_count >= MAX_SPLITS else \
                          "разделить сейчас нельзя"
                 await query.answer(f"Нельзя разделить ({reason}).", show_alert=True)

    except IndexError as e:
        # Deck empty during draw
        logger.warning(f"BJ action '{action}' user {user_id} failed draw: {e}")
        current_hand_data['status'] = 'stand' # Force stand if draw fails
        await query.answer("Не удалось взять карту (колода пуста?)! Ход завершен.", show_alert=True)
        needs_state_update = True
        move_to_next_hand_or_dealer = True
    except Exception as e:
        logger.error(f"BJ action '{action}' user {user_id} unexpected error: {e}", exc_info=True)
        await query.answer("Произошла непредвиденная ошибка.", show_alert=True)
        # Try to recover by standing the hand
        current_hand_data['status'] = 'stand'
        needs_state_update = True
        move_to_next_hand_or_dealer = True

    # --- Update Display ---
    if needs_state_update:
        update_result = await blackjack_show_state(context, chat_id, user_id, game_state=bj_session_data, edit_existing=True)
        if update_result is None: # Check if update failed
            logger.error(f"Failed to update state message after action '{action}' for user {user_id}. Game might be stuck.")
            try:
                 # Send a message indicating error if edit failed
                 await context.bot.send_message(chat_id, "⚠️ Ошибка обновления отображения игры после действия.")
            except Exception: pass
            # Potentially end the game here if UI is broken? Or just log.

    # --- Trigger Next Action (if needed) ---
    # Use job queue to avoid issues with nested callbacks or long-running tasks
    if move_to_next_hand_or_dealer:
        job_name = f"bj_next_action_{user_id}_{action_message_id}_{hand_index}" # Unique job name
        context.job_queue.run_once(
            blackjack_next_action_job,
            when=0.1, # Short delay
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': action_message_id},
            name=job_name
        )


async def blackjack_next_action_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue callback to trigger moving to the next hand or dealer."""
    job_data = context.job.data if context.job else {}
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id') # Pass message_id for validation

    if user_id and chat_id and message_id:
        # Pass message_id to the actual logic function
        await blackjack_next_action(context, chat_id, user_id, message_id)
    else:
        logger.error(f"Missing data in blackjack_next_action_job: {job_data}")


async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, expected_message_id: int):
    """Moves to the next player hand or starts the dealer's turn."""
    bj_session_data = context.user_data.get(BJ_GAME_KEY)

    # --- Validations ---
    if not bj_session_data:
        logger.info(f"BJ next_action (job) for user {user_id}: Game session state not found. Aborting.")
        return
    if bj_session_data.get('message_id') != expected_message_id:
         logger.warning(f"BJ next_action (job) for user {user_id}: Message ID mismatch ({bj_session_data.get('message_id')} vs {expected_message_id}). Aborting.")
         return
    if bj_session_data.get('state') != 'player_turn':
        # This might happen if multiple actions trigger jobs close together
        logger.info(f"BJ next_action (job) for user {user_id}: Game state is not 'player_turn' ({bj_session_data.get('state')}). Aborting.")
        return

    player_hands = bj_session_data.get('player_hands', [])
    current_hand_idx = bj_session_data.get('current_hand_index', -1)
    next_active_idx = -1

    # Find the index of the next hand with status 'active'
    for i in range(current_hand_idx + 1, len(player_hands)):
        hand_data = player_hands[i]
        if isinstance(hand_data, dict) and hand_data.get('status') == 'active':
            next_active_idx = i
            break

    if next_active_idx != -1:
        # --- Move to Next Player Hand ---
        bj_session_data['current_hand_index'] = next_active_idx
        logger.info(f"BJ user {user_id}: Moving to next active hand index {next_active_idx}")
        await blackjack_show_state(context, chat_id, user_id, game_state=bj_session_data, edit_existing=True)
    else:
        # --- All Player Hands Finished, Start Dealer's Turn ---
        logger.info(f"BJ user {user_id}: All player hands done, moving to dealer's turn.")
        bj_session_data['state'] = 'dealer_turn'

        # Update display to show "Dealer's Turn" message and reveal dealer hand if needed
        update_success = await blackjack_show_state(context, chat_id, user_id, game_state=bj_session_data, edit_existing=True)

        if update_success is None: # Check if showing state failed
            logger.error(f"BJ user {user_id}: Failed to update state to 'dealer_turn'. Aborting dealer job.")
            try: await context.bot.send_message(chat_id, "⚠️ Ошибка! Не удалось перейти к ходу дилера.")
            except Exception: pass
            context.user_data.pop(BJ_GAME_KEY, None) # Clean up broken game
            return

        # Schedule the dealer's turn logic via job queue
        dealer_job_name = f"bj_dealer_turn_{user_id}_{expected_message_id}"
        context.job_queue.run_once(
            blackjack_dealer_turn_job,
            DEALER_TURN_DELAY, # Initial delay before dealer acts
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': expected_message_id},
            name=dealer_job_name
        )


async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Job queue callback to execute the dealer's turn logic."""
    job_data = context.job.data if context.job else {}
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id')

    if not user_id or not chat_id or not message_id:
        logger.error(f"BJ Dealer job missing required data: {job_data}")
        return

    # --- Validations ---
    bj_session_data = context.user_data.get(BJ_GAME_KEY)
    if not bj_session_data:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state not found. Job aborted.")
        return
    if bj_session_data.get('state') != 'dealer_turn':
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game state is not 'dealer_turn' ({bj_session_data.get('state')}). Job aborted.")
        return
    if bj_session_data.get('message_id') != message_id:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Message ID mismatch (game has {bj_session_data.get('message_id')}). Job aborted.")
        return

    # --- Dealer Logic ---
    deck_to_draw_from = bj_session_data.get('persistent_deck')
    if deck_to_draw_from is None:
        logger.error(f"Persistent deck missing during dealer turn for user {user_id}")
        # Determine outcome with error message
        await blackjack_determine_outcome(context, chat_id, user_id, False, error_message="Критическая ошибка: Колода дилера не найдена!")
        # No need to pop BJ_GAME_KEY here, outcome handles state reset
        return

    dealer_hand = bj_session_data.get('dealer_hand', [])
    player_hands = bj_session_data.get('player_hands', [])

    # Check if any player hand is still potentially winnable (not bust)
    # Dealer only hits if there's a player hand that hasn't busted.
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] # Player BJ already paid
        for h in player_hands
    )

    dealer_stood = False
    hit_occurred = False

    # Reveal hidden card first (already done by show_state in next_action)

    if player_can_win:
        logger.info(f"BJ Dealer user {user_id}: At least one player hand active. Dealer starts hitting sequence.")
        while not dealer_stood:
            current_dealer_value = get_hand_value(dealer_hand)
            num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
            # Check for soft hand (Ace counted as 11 without busting)
            is_soft = num_aces > 0 and (current_dealer_value <= 21) and \
                      (get_hand_value([c for c in dealer_hand if c[0] != 'A'] + [('A', '')] * (num_aces-1) if num_aces > 0 else []) + 1 <= 11)


            should_stand = False
            if current_dealer_value > 17:
                should_stand = True
            elif current_dealer_value == 17:
                # Stand on hard 17, Hit or Stand on soft 17 based on rule
                if not (is_soft and DEALER_HITS_SOFT_17):
                    should_stand = True

            if should_stand:
                log_msg = f"stands initially on {current_dealer_value}" if not hit_occurred else f"stands on {current_dealer_value}"
                if is_soft and current_dealer_value == 17: log_msg += " (soft)"
                logger.info(f"BJ Dealer user {user_id} {log_msg}.")
                dealer_stood = True
                # No state update needed here yet, just break loop
                break # Exit the while loop

            # --- Dealer Hits ---
            logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}{' (soft)' if is_soft else ''}.")
            hit_occurred = True
            card = draw_card(deck_to_draw_from)

            if card:
                dealer_hand.append(card)
                # Update display incrementally to show dealer hits
                update_res = await blackjack_show_state(context, chat_id, user_id, game_state=bj_session_data, edit_existing=True)
                if update_res is None:
                    logger.error(f"BJ Dealer user {user_id}: Failed to show state after dealer hit. Aborting turn.")
                    await blackjack_determine_outcome(context, chat_id, user_id, False, error_message="Ошибка отображения хода дилера.")
                    return # Stop dealer turn

                await asyncio.sleep(DEALER_TURN_DELAY * 0.8) # Pause between hits
            else:
                # Deck empty during dealer hit
                logger.warning(f"BJ Dealer user {user_id} failed to draw card (deck empty?). Standing.")
                dealer_stood = True
                # Break loop, outcome will be determined with current hand
                break
    else:
        final_dealer_value_no_hit = get_hand_value(dealer_hand)
        logger.info(f"BJ Dealer user {user_id}: No active player hands. Dealer stands immediately with {final_dealer_value_no_hit}.")
        dealer_stood = True # Technically stood immediately

    # --- Dealer Turn Finished ---
    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn with value {final_dealer_value}. Cards remaining: {len(deck_to_draw_from)}. Determining outcome.")

    # Ensure final dealer hand is displayed *before* outcome calculation shown
    # (State already shows 'dealer_turn', this reveals final cards if any were drawn)
    final_show_success = await blackjack_show_state(context, chat_id, user_id, game_state=bj_session_data, edit_existing=True)
    if final_show_success is None:
        logger.error(f"BJ Dealer user {user_id}: Failed to show final dealer hand. Determining outcome anyway.")
        # Proceed to outcome determination even if final display failed

    await asyncio.sleep(DEALER_TURN_DELAY * 0.5) # Short pause before showing results

    # Determine outcome based on final hands
    dealer_had_initial_blackjack = (len(dealer_hand) == 2 and final_dealer_value == 21 and not hit_occurred) # Check if BJ was from start
    await blackjack_determine_outcome(context, chat_id, user_id, dealer_had_initial_blackjack)


async def blackjack_determine_outcome(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    dealer_had_blackjack: bool, # Was dealer's 21 from initial deal?
    error_message: str | None = None # Optional error message for forced refund
    ):
    """Calculates winnings, updates balance, displays final outcome, and resets state."""
    bj_session_data = context.user_data.get(BJ_GAME_KEY)
    if not bj_session_data:
        logger.warning(f"BJ outcome user {user_id}: Game session data not found.")
        return

    message_id = bj_session_data.get('message_id')
    # We need a message ID to show the result, but don't abort if missing, just log.
    if not message_id:
        logger.error(f"BJ outcome user {user_id}: No message_id found in session data. Result cannot be displayed by editing.")
        # We can still calculate and update balance, but UI will be broken.

    # Prevent double processing if somehow triggered twice
    if bj_session_data.get('outcome_determined', False) and not error_message:
        logger.info(f"BJ outcome user {user_id}: Outcome previously determined. Skipping calculation.")
        # Ensure state is ready for next round if needed
        bj_session_data['state'] = 'waiting_bet'
        return

    player_hands = bj_session_data.get('player_hands', [])
    dealer_hand = bj_session_data.get('dealer_hand', [])
    outcome_lines = []
    total_winnings_to_pay = 0.0
    total_bets_placed = 0.0 # Sum of all bets across all hands

    if error_message:
        # --- Handle Forced Refund due to Error ---
        outcome_lines.append(f"<b>{html_escape(error_message)}</b>")
        for i, hand_data in enumerate(player_hands):
            if isinstance(hand_data, dict):
                bet = hand_data.get('bet', 0)
                total_bets_placed += bet # Track original bet for refund calc
                total_winnings_to_pay += bet # Amount to refund is the bet itself
                prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""
                outcome_lines.append(f"{prefix}Ставка {bet:.2f} F возвращена из-за ошибки.")
        logger.error(f"BJ outcome user {user_id}: Critical error '{error_message}'. Refunding bets totalling {total_winnings_to_pay:.2f} F.")

    else:
        # --- Normal Outcome Calculation ---
        dealer_final_value = get_hand_value(dealer_hand)
        dealer_busted = dealer_final_value > 21

        for i, hand_data in enumerate(player_hands):
            if not isinstance(hand_data, dict): continue

            hand = hand_data.get('hand', [])
            bet = hand_data.get('bet', 0)
            status = hand_data.get('status')
            player_value = get_hand_value(hand)
            # Player had BJ only if status is 'blackjack' (i.e., from initial deal)
            player_had_blackjack = (status == 'blackjack')

            total_bets_placed += bet # Add this hand's bet to total
            payout_amount = 0.0 # Net amount won/lost for this hand (0 if push)
            winnings_paid_this_hand = 0.0 # Amount to return to player (bet + winnings)
            outcome_str = ""
            prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

            if status == 'bust':
                outcome_str = f"{prefix}Перебор ({player_value}). Ставка проиграна (-{bet:.2f} F)."
                payout_amount = -bet
                winnings_paid_this_hand = 0.0
            elif player_had_blackjack:
                 # BJ payout already handled in handle_bet, this just confirms outcome display
                if dealer_had_blackjack:
                    outcome_str = f"{prefix}Блекджек! Ничья с дилером (Push)."
                    payout_amount = 0.0 # Bet was already returned
                    winnings_paid_this_hand = 0.0 # Already handled
                else:
                    win_amount = bet * BLACKJACK_PAYOUT
                    outcome_str = f"{prefix}Блекджек! Выигрыш (+{win_amount:.2f} F)."
                    payout_amount = win_amount # Net win
                    winnings_paid_this_hand = 0.0 # Already handled
            elif dealer_had_blackjack:
                # Player didn't have BJ, dealer did
                outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{bet:.2f} F)."
                payout_amount = -bet
                winnings_paid_this_hand = 0.0
            elif dealer_busted:
                outcome_str = f"{prefix}У дилера перебор ({dealer_final_value})! Выигрыш (+{bet:.2f} F)."
                payout_amount = bet
                winnings_paid_this_hand = bet * 2 # Return original bet + winnings
            elif status == 'stand':
                # Compare hands
                comparison_str = ""
                if player_value > dealer_final_value:
                    comparison_str = f"({player_value} > {dealer_final_value})"
                    outcome_str = f"{prefix}Вы выиграли {html_escape(comparison_str)}. Выигрыш (+{bet:.2f} F)."
                    payout_amount = bet
                    winnings_paid_this_hand = bet * 2
                elif player_value == dealer_final_value:
                    comparison_str = f"({player_value} = {dealer_final_value})"
                    outcome_str = f"{prefix}Ничья {html_escape(comparison_str)} (Push)."
                    payout_amount = 0.0
                    winnings_paid_this_hand = bet # Return original bet
                else: # player_value < dealer_final_value
                    comparison_str = f"({player_value} < {dealer_final_value})"
                    outcome_str = f"{prefix}Вы проиграли {html_escape(comparison_str)}. Ставка проиграна (-{bet:.2f} F)."
                    payout_amount = -bet
                    winnings_paid_this_hand = 0.0
            else:
                # Should not happen if logic is correct (e.g., hand status 'active')
                outcome_str = f"{prefix}Неопределенный результат для руки (статус: {status})."
                logger.error(f"BJ outcome user {user_id}: Unexpected hand status '{status}' for hand {i}")
                winnings_paid_this_hand = bet # Refund bet in case of error state

            outcome_lines.append(outcome_str)
            total_winnings_to_pay += winnings_paid_this_hand

    # --- Update Balance ---
    # Calculate net change based on winnings to pay vs total bets placed initially
    # Note: BJ payouts were handled earlier, so total_winnings_to_pay only includes
    # winnings from regular hands, pushes, and refunds from errors.
    initial_total_bet_deducted = sum(h.get('initial_bet', h.get('bet', 0)) for h in player_hands if isinstance(h, dict)) # Get initial total bet amount

    # Amount to add back to balance = total_winnings_to_pay
    # Net change = amount_added_back - initial_total_bet_deducted
    net_change = total_winnings_to_pay - initial_total_bet_deducted

    balance_updated_ok = True
    if total_winnings_to_pay > 0:
        current_balance_before_update = get_balance(user_id) # Get balance before final payout
        new_balance = update_balance(user_id, total_winnings_to_pay)
        if new_balance is None:
            # Payout failed!
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! Обратитесь к администратору. ❌</b>")
            # Recalculate net change assuming payout failed
            net_change = -initial_total_bet_deducted
            balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED final balance update. Payout amount: {total_winnings_to_pay:.2f}. Initial Bet Sum: {initial_total_bet_deducted:.2f}. Balance before: {current_balance_before_update}")
        else:
            logger.info(f"BJ outcome user {user_id}: Final Balance updated +{total_winnings_to_pay:.2f}. Net: {net_change:+.2f}. New Bal: {new_balance:.2f}")
    else:
        logger.info(f"BJ outcome user {user_id}: No final winnings to pay. Net change: {net_change:+.2f}")

    # --- Prepare Final Display ---
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    final_outcome_display_text = "\n".join(outcome_lines) + final_summary

    # Mark outcome as processed for display logic in show_state
    bj_session_data['outcome_determined'] = True
    bj_session_data['outcome_text'] = final_outcome_display_text # Store for show_state

    # --- Reset State for Next Round ---
    # Keep deck, total_cards, threshold, message_id
    # Set state to 'waiting_bet' to enable 'New Game' button functionality
    bj_session_data['state'] = 'waiting_bet'
    # Clear hand-specific data
    bj_session_data.pop('player_hands', None)
    bj_session_data.pop('dealer_hand', None)
    bj_session_data.pop('current_hand_index', None)
    bj_session_data.pop('split_count', None)
    bj_session_data.pop('total_winnings_paid', None) # Clear intermediate winnings tracker

    # --- Show Final Result and 'New Game' button ---
    await blackjack_show_state(
        context, chat_id, user_id,
        game_state=bj_session_data,
        edit_existing=True, # Try editing the last known message
        force_game_over_display=True, # Ensure game over format is used
        final_outcome_text=final_outcome_display_text # Pass text directly
    )

    # Note: outcome_determined and outcome_text are cleared inside show_state
    # when show_final_summary is true.

    logger.info(f"BJ hand finished for user {user_id}. State reset to 'waiting_bet'. Deck persists.")

# --- End of Updated Blackjack Code ---


# --- Roulette Game (Full Code, Checked & Updated) ---
# --- Start of Full Roulette Code ---

async def rl_get_display_name_map(context: ContextTypes.DEFAULT_TYPE, user_ids: list[int]) -> dict[int, str]:
    """Helper to fetch display names for a list of user IDs."""
    display_name_map = {}
    if user_ids:
        try:
            mention_data_list = await asyncio.gather(*(get_user_mention(context, uid) for uid in user_ids))
            display_name_map = {uid: mention_data[1] for i, uid in enumerate(user_ids) for mention_data in [mention_data_list[i]]}
        except Exception as e:
            logger.error(f"Failed to fetch user display names for roulette: {e}")
            display_name_map = {uid: f"User_{uid}" for uid in user_ids}
    return display_name_map

def rl_get_main_menu_keyboard(chat_data: dict, display_name_map: dict[int, str]) -> InlineKeyboardMarkup:
    """Generates the main keyboard for the roulette game state."""
    game_state = chat_data.get(RL_GAME_KEY, {})
    state = game_state.get('state', 'idle')
    active_bets_by_user = game_state.get('active_bets', {})
    keyboard_rows = []

    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    total_bet_amount = sum(b.get('amount', 0) for bets in active_bets_by_user.values() for b in bets if isinstance(b, dict)) # Safer sum

    # --- Display Active Bets ---
    if active_bets_by_user:
        keyboard_rows.append([InlineKeyboardButton("📝 Текущие ставки:", callback_data='rl_noop')]) # Non-clickable header
        for user_id, bets in active_bets_by_user.items():
            # Use display name from the pre-fetched map
            user_display_name = display_name_map.get(user_id, f"User_{user_id}")
            # Format bets, ensure 'amount' exists
            bet_parts = [f"{b.get('value_display', '?')} ({b.get('amount', 0)}F)" for b in bets if isinstance(b, dict)]
            bet_str = ", ".join(bet_parts)

            # Truncate long bet strings for button text limits (approx 64 bytes)
            button_text = f"{user_display_name}: {bet_str}"
            max_len = 60
            if len(button_text.encode('utf-8')) > max_len: # Check byte length
                 # Simple truncation, might cut mid-character in multibyte chars
                 button_text = button_text[:max_len-3] + "..."
                 # A more robust approach would iterate byte-wise

            keyboard_rows.append([InlineKeyboardButton(button_text, callback_data='rl_noop')]) # Non-clickable bet display

        # Summary row
        keyboard_rows.append([InlineKeyboardButton(
            f"💰 Общая сумма: {total_bet_amount} F ({total_bets_count}/{RL_MAX_BETS_PER_ROUND} ставок)",
            callback_data='rl_noop'
        )])
        keyboard_rows.append([InlineKeyboardButton("───────", callback_data='rl_noop')]) # Separator


    # --- Add Action Buttons based on State ---
    if state == 'accepting_bets':
        # 'Add Bet' button if limits allow
        if total_bets_count < RL_MAX_BETS_PER_ROUND:
            keyboard_rows.append([InlineKeyboardButton("➕ Добавить ставку", callback_data='rl_start_bet')])
        else:
            # Indicate round bet limit reached
            keyboard_rows.append([InlineKeyboardButton("🚫 Лимит ставок раунда достигнут", callback_data='rl_noop')])

        # 'Spin' button - Allow ONLY if bets exist AND (for simplicity) only one player is betting
        # You could change this condition (e.g., allow initiator to spin, or anyone)
        if active_bets_by_user: # Allow spin if any bets exist
            # Condition to allow spin: only one better OR initiator can spin?
            # Simplified: Allow if only one person bet
            # if len(active_bets_by_user) == 1:
            # Allow anyone to spin if bets exist:
            keyboard_rows.append([InlineKeyboardButton("🎰 Крутить!", callback_data='rl_spin')])

    elif state == 'idle' or state == 'finished':
        # Button to start a new round
        keyboard_rows.append([InlineKeyboardButton("▶️ Начать новый раунд", callback_data='rl_new_round')])

    elif state == 'spinning':
        # Indicate wheel is spinning
        keyboard_rows.append([InlineKeyboardButton("⏳ Колесо вращается...", callback_data='rl_noop')])

    # Always add help button
    keyboard_rows.append([InlineKeyboardButton("❓ Правила Рулетки", callback_data='rl_show_help')])

    return InlineKeyboardMarkup(keyboard_rows)

def rl_get_bet_type_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for selecting the type of bet."""
    keyboard = [
        [InlineKeyboardButton("🔢 Число", callback_data='rl_type_number'),
         InlineKeyboardButton("🎨 Цвет", callback_data='rl_type_color')],
        [InlineKeyboardButton("⚖️ Чет/Нечет", callback_data='rl_type_parity'),
         InlineKeyboardButton("📦 Дюжина", callback_data='rl_type_dozen')],
        [InlineKeyboardButton("📊 Колонка", callback_data='rl_type_column')],
        [InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')] # Cancel betting process
    ]
    return InlineKeyboardMarkup(keyboard)

def rl_get_bet_value_keyboard(bet_type: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting the specific value of a bet."""
    kb_rows = []
    options = []
    if bet_type == 'color': options = [('Red', '🔴 Красное'), ('Black', '⚫ Черное')]
    elif bet_type == 'parity': options = [('Even', '⚖️ Четное'), ('Odd', '❓ Нечетное')]
    elif bet_type == 'dozen': options = [('1st', '1️⃣ Дюж. 1-12'), ('2nd', '2️⃣ Дюж. 13-24'), ('3rd', '3️⃣ Дюж. 25-36')]
    elif bet_type == 'column': options = [('col1', '📊 Кол. 1'), ('col2', '📊 Кол. 2'), ('col3', '📊 Кол. 3')]

    # Build keyboard rows from options
    row = []
    max_per_row = 2 if bet_type in ['color', 'parity'] else 1 # Adjust layout
    for value, display in options:
        row.append(InlineKeyboardButton(display, callback_data=f'rl_value_{value}'))
        if len(row) == max_per_row:
            kb_rows.append(row)
            row = []
    if row: kb_rows.append(row) # Add remaining

    # Add navigation buttons
    kb_rows.append([InlineKeyboardButton("⬅️ Назад (к типу)", callback_data='rl_back_to_bet_type')])
    kb_rows.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(kb_rows)


def rl_get_bet_amount_keyboard(balance: float | None, bet_type: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting the bet amount."""
    keyboard_rows = []
    row = []
    # Ensure balance is not None before comparison
    valid_amounts = [a for a in RL_BET_AMOUNTS if balance is not None and a <= balance]

    if not valid_amounts and balance is not None and balance > 0:
         # Indicate if balance is too low for any standard bet amount
         keyboard_rows.append([InlineKeyboardButton(f"Баланс ({balance:.2f} F) < мин. ставки ({min(RL_BET_AMOUNTS)} F)", callback_data='rl_noop')])
    else:
        for amount in valid_amounts:
            row.append(InlineKeyboardButton(str(amount), callback_data=f'rl_amount_{amount}'))
            if len(row) == 3: # 3 amounts per row
                keyboard_rows.append(row)
                row = []
        if row:
            keyboard_rows.append(row)

    # Determine correct "Back" button target
    back_cb = 'rl_back_to_bet_type' if bet_type == 'number' else 'rl_back_to_bet_value'
    back_txt = "⬅️ Назад (к типу)" if bet_type == 'number' else "⬅️ Назад (к значению)"
    keyboard_rows.append([InlineKeyboardButton(back_txt, callback_data=back_cb)])
    keyboard_rows.append([InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')])
    return InlineKeyboardMarkup(keyboard_rows)

def rl_get_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for confirming the bet placement."""
    keyboard = [
        [InlineKeyboardButton("✅ Да, поставить!", callback_data='rl_confirm_bet_yes'),
         InlineKeyboardButton("✏️ Нет, изменить", callback_data='rl_back_to_bet_type')], # Go back to type selection
        [InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Roulette Async/Job Functions ---

async def rl_remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Removes job queue jobs by name if they exist. Returns True if removed."""
    # Use context.job_queue directly
    jobs = context.job_queue.get_jobs_by_name(name)
    if not jobs:
        return False
    removed_count = 0
    for job in jobs:
        job.schedule_removal()
        removed_count += 1
    if removed_count > 0:
        logger.info(f"Removed {removed_count} Job Queue job(s): {name}")
        return True
    return False


async def rl_spin_roulette_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job queue callback function that triggers the roulette spin after the timer."""
    job_data = context.job.data if context.job else {}
    chat_id = job_data.get('chat_id')
    if not chat_id:
        logger.error(f"Roulette timer job missing chat_id: {job_data}")
        return

    logger.info(f"Roulette betting timer expired for chat_id={chat_id}")
    # Use application.chat_data for persistent chat-specific data
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # --- State Check ---
    if not game_state or game_state.get('state') != 'accepting_bets':
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but game not in 'accepting_bets' state ({game_state.get('state') if game_state else 'No Game'}). Aborting.")
        # Clean up potentially orphaned display timer job if main timer fired unexpectedly
        display_timer_name = game_state.get('timer_display_job_name') if game_state else None
        if display_timer_name:
            await rl_remove_job_if_exists(display_timer_name, context)
            if game_state: game_state.pop('timer_display_job_name', None)
        # Also remove self-reference if present
        if game_state: game_state.pop('timer_job_name', None)
        return

    # --- Check for Bets ---
    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user:
        logger.warning(f"Roulette timer job fired for chat {chat_id}, but no bets placed. Ending round.")
        game_state['state'] = 'finished' # Mark as finished
        # Clean up timers
        spin_timer_name = game_state.pop('timer_job_name', None) # Should be self
        display_timer_name = game_state.pop('timer_display_job_name', None)
        # await rl_remove_job_if_exists(spin_timer_name, context) # No need, job is finishing
        if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)

        message_id = game_state.get('message_id')
        if message_id:
            try:
                # Get display names (empty in this case, but keep structure)
                display_names = await rl_get_display_name_map(context, [])
                final_reply_markup = rl_get_main_menu_keyboard(chat_data, display_names)
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text="⏳ Время для ставок истекло. Ставок не было.\nНачните новый раунд.",
                    reply_markup=final_reply_markup
                )
            except Forbidden: logger.error(f"Forbidden: Cannot edit message in chat {chat_id}")
            except Exception as e:
                logger.warning(f"Could not edit message in chat {chat_id} after timer expired with no bets: {e}")
        return

    # --- Bets exist, proceed to spin ---
    logger.info(f"Roulette timer starting spin for chat {chat_id}")
    # Call the main spin logic function
    await rl_spin_roulette_logic(context, chat_id)


async def rl_update_timer_display_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repeating job to update the timer display in the roulette message."""
    job_data = context.job.data if context.job else {}
    chat_id = job_data.get('chat_id')
    main_timer_job_name = job_data.get('main_timer_job_name')
    this_job_name = context.job.name if context.job else None

    if not chat_id or not main_timer_job_name:
        logger.error(f"Roulette display timer job missing data: {job_data} (Job: {this_job_name})")
        if context.job: context.job.schedule_removal() # Stop invalid job
        return

    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # --- Check if Game State is Still Valid for Timer ---
    if not game_state or game_state.get('state') != 'accepting_bets':
        logger.debug(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Game state is not 'accepting_bets' ({game_state.get('state') if game_state else 'No Game'}).")
        if context.job: context.job.schedule_removal() # Stop this repeating job
        # Clean up reference in game state if it matches this job
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
            game_state.pop('timer_display_job_name', None)
        return

    # --- Check if Main Timer Still Exists ---
    main_timer_jobs = context.job_queue.get_jobs_by_name(main_timer_job_name)
    if not main_timer_jobs:
        logger.debug(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Main timer job '{main_timer_job_name}' not found.")
        if context.job: context.job.schedule_removal() # Stop this repeating job
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
            game_state.pop('timer_display_job_name', None)
        # Update the message one last time without the timer text
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # --- Main Timer Exists, Update Display ---
    update_result = await rl_show_game_state(context, chat_id, edit_existing=True)
    if update_result is None: # Check if the message update failed
        logger.warning(f"Stopping display timer job '{this_job_name}' for chat {chat_id}: Failed to update game state message.")
        if context.job: context.job.schedule_removal() # Stop job if message can't be updated
        if game_state and game_state.get('timer_display_job_name') == this_job_name:
            game_state.pop('timer_display_job_name', None)


# --- Roulette Command and Callback Handlers ---
async def roulette_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /roulette command to start or show the game."""
    if not update.message or not update.effective_chat or not update.effective_user: return
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    logger.info(f"/roulette command from user {user.id} in chat {chat_id} (type: {chat.type})")

    # Ensure user profile exists
    get_or_create_user(user.id)

    chat_data = context.application.chat_data.setdefault(chat_id, {}) # Ensure chat_data exists
    game_state = chat_data.get(RL_GAME_KEY)
    current_message_id = game_state.get('message_id') if game_state else None

    # --- Handle Existing Game States ---
    if game_state:
        state = game_state.get('state')
        if state == 'spinning':
            logger.info(f"Roulette game currently spinning in chat {chat_id}. Ignoring /roulette command.")
            try:
                # Quote the command message if possible
                await update.message.reply_text("⏳ Колесо рулетки уже вращается, подождите окончания раунда.", quote=True)
            except Exception: pass
            return
        elif state == 'accepting_bets':
            logger.info(f"Roulette game already active in chat {chat_id} (state: accepting_bets). Resending status message.")
            # Delete the triggering command message
            try: await update.message.delete()
            except Exception as e: logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")
            # Resend the game state message if the old one is gone or needs update
            await rl_show_game_state(context, chat_id, edit_existing=True) # Try editing first
            return

    # --- Start a New Round ---
    logger.info(f"Starting new roulette round in chat {chat_id}")

    # Clean up any old timers associated with this chat
    old_spin_timer_job_name = game_state.get('timer_job_name') if game_state else None
    old_display_timer_job_name = game_state.get('timer_display_job_name') if game_state else None
    if old_spin_timer_job_name:
        await rl_remove_job_if_exists(old_spin_timer_job_name, context)
        logger.info(f"Removed old spin timer '{old_spin_timer_job_name}' for chat {chat_id}")
    if old_display_timer_job_name:
        await rl_remove_job_if_exists(old_display_timer_job_name, context)
        logger.info(f"Removed old display timer '{old_display_timer_job_name}' for chat {chat_id}")

    # Clean up old message if it exists
    if current_message_id:
        try:
            await context.bot.delete_message(chat_id, current_message_id)
            logger.debug(f"Deleted previous roulette message {current_message_id} in chat {chat_id}")
        except Exception as e:
            logger.debug(f"Failed to delete previous roulette message {current_message_id} in chat {chat_id}: {e}")

    # Initialize new game state in chat_data
    new_game_state = {
        'state': 'accepting_bets',
        'active_bets': {}, # Bets stored as {user_id: [bet_dict1, bet_dict2]}
        'message_id': None, # Will be set by show_game_state
        'timer_job_name': None,
        'timer_display_job_name': None,
        'initiator_id': user.id # Track who started the round
    }
    chat_data[RL_GAME_KEY] = new_game_state

    # Delete the triggering command message
    try: await update.message.delete()
    except Exception as e: logger.warning(f"Could not delete /roulette command message in chat {chat_id}: {e}")

    # Send the initial game state message
    await rl_show_game_state(
        context, chat_id,
        message_text="🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!",
        edit_existing=False # Force sending a new message
    )

async def rl_new_round_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Start New Round' button press from the main keyboard."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat = query.message.chat
    chat_id = chat.id
    current_message_id = query.message.message_id

    logger.info(f"'rl_new_round' callback from user {user.id} in chat {chat_id}")
    await query.answer("Запуск нового раунда...") # Answer callback quickly

    # Ensure user profile exists
    get_or_create_user(user.id)

    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # Clean up timers from the previous round
    old_spin_timer_job_name = game_state.get('timer_job_name') if game_state else None
    old_display_timer_job_name = game_state.get('timer_display_job_name') if game_state else None
    if old_spin_timer_job_name: await rl_remove_job_if_exists(old_spin_timer_job_name, context)
    if old_display_timer_job_name: await rl_remove_job_if_exists(old_display_timer_job_name, context)

    # Delete the message that had the "Start New Round" button
    try:
        await context.bot.delete_message(chat_id, current_message_id)
        logger.debug(f"Deleted previous roulette message {current_message_id} via rl_new_round_callback in chat {chat_id}")
    except Exception as e:
        logger.debug(f"Failed to delete message {current_message_id} in rl_new_round_callback for chat {chat_id}: {e}")

    # Initialize new game state
    new_game_state = {
        'state': 'accepting_bets',
        'active_bets': {},
        'message_id': None,
        'timer_job_name': None,
        'timer_display_job_name': None,
        'initiator_id': user.id
    }
    chat_data[RL_GAME_KEY] = new_game_state # Update chat_data

    # Send the initial betting message
    await rl_show_game_state(
        context, chat_id,
        message_text="🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!",
        edit_existing=False # Send new message
    )

async def rl_show_game_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_text: str | None = None,
    edit_existing: bool = True
    ) -> Message | int | None:
    """Updates or sends the main roulette game message with keyboard. Returns message_id/object or None."""
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    if not game_state:
        logger.debug(f"rl_show_game_state called for chat {chat_id} but no game state found.")
        return None

    message_id = game_state.get('message_id')
    state = game_state.get('state', 'unknown')
    main_timer_job_name = game_state.get('timer_job_name')
    active_bets_by_user = game_state.get('active_bets', {})

    # Determine if editing is possible
    attempt_edit = edit_existing and message_id is not None

    # --- Fetch Display Names for Keyboard ---
    user_ids_with_bets = list(active_bets_by_user.keys())
    display_name_map = await rl_get_display_name_map(context, user_ids_with_bets)

    # --- Determine Base Text ---
    if message_text is None:
        if state == 'accepting_bets':
            base_text = "🎲 <b>Американская Рулетка!</b>\nДелайте ваши ставки!"
        elif state == 'spinning':
            base_text = "🎰 <b>Колесо вращается...</b>"
        elif state == 'finished':
            base_text = "🏁 Раунд Рулетки завершен." # Outcome shown in spin logic message
        else: # idle or unknown
            base_text = f"🎲 <b>Американская Рулетка</b> [Состояние: {state}]"
    else:
        base_text = message_text # Use provided text

    # --- Add Timer Text if Applicable ---
    timer_text = ""
    if main_timer_job_name and state == 'accepting_bets':
        jobs = context.job_queue.get_jobs_by_name(main_timer_job_name)
        if jobs and jobs[0].next_t: # Check if job exists and has next run time
            try:
                remaining = max(0, int(jobs[0].next_t.timestamp() - time.time()))
                timer_text = f"\n⏳ <i>Авто-старт через ~{remaining} сек...</i>"
            except Exception as e:
                logger.warning(f"Error calculating remaining time for job {main_timer_job_name}: {e}")
        else:
            logger.debug(f"Timer job '{main_timer_job_name}' in state, but job not found/active in chat {chat_id}.")
            # Timer job might have just finished or been removed, remove from state if still present
            if game_state.get('timer_job_name') == main_timer_job_name:
                game_state.pop('timer_job_name', None)
            if game_state.get('timer_display_job_name'): # Also stop display timer if main is gone
                 await rl_remove_job_if_exists(game_state['timer_display_job_name'], context)
                 game_state.pop('timer_display_job_name', None)


    # --- Combine Text and Get Keyboard ---
    full_text = base_text + timer_text
    reply_markup = rl_get_main_menu_keyboard(chat_data, display_name_map)

    # --- Send or Edit Message ---
    sent_message_obj: Message | None = None
    result: Message | int | None = None
    edit_failed_and_sending_new = False

    try:
        if attempt_edit:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=full_text,
                reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            logger.debug(f"Edited roulette state message {message_id} in chat {chat_id}")
            result = message_id # Return ID on successful edit
        else:
            # Try deleting old message before sending new if edit wasn't attempted/possible but ID existed
            if message_id and not attempt_edit:
                try:
                    await context.bot.delete_message(chat_id, message_id)
                    logger.debug(f"Deleted old message {message_id} before sending new in chat {chat_id}")
                except Exception: pass # Ignore delete error

            sent_message_obj = await context.bot.send_message(
                chat_id=chat_id, text=full_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            # Update game state with the new message ID *immediately*
            current_game_state = context.application.chat_data.get(chat_id, {}).get(RL_GAME_KEY)
            if current_game_state:
                current_game_state['message_id'] = sent_message_obj.message_id
                logger.info(f"Sent new roulette state message {sent_message_obj.message_id} in chat {chat_id}. Updated game state.")
            else:
                logger.warning(f"Sent new roulette message {sent_message_obj.message_id} for chat {chat_id}, but game state was missing upon update.")
            result = sent_message_obj # Return new message object

    except BadRequest as e:
        error_str = str(e).lower()
        if "message is not modified" in error_str:
            logger.debug(f"Roulette state message {message_id} not modified.")
            result = message_id # Treat as success
        elif "message to edit not found" in error_str or "chat not found" in error_str or "message can't be edited" in error_str:
            logger.warning(f"Failed to edit roulette message {message_id} in chat {chat_id} (not found/editable). Forcing send new.")
            if game_state: game_state['message_id'] = None # Clear invalid ID
            # Retry by calling self to send a new message
            if not edit_failed_and_sending_new: # Prevent infinite loop
                return await rl_show_game_state(context, chat_id, message_text=full_text, edit_existing=False)
            else:
                logger.error(f"Recursive send new failed in chat {chat_id} after edit failure.")
                return None
        elif "message text is empty" in error_str:
            logger.error(f"Attempted to send empty message to chat {chat_id}. Text: '{full_text}'")
            return None # Cannot send empty message
        else:
            # Other BadRequest, log and return None
            logger.error(f"BadRequest showing roulette state for chat {chat_id} (msg {message_id}): {e}")
            return None
    except Forbidden:
        logger.error(f"Forbidden error in chat {chat_id} (likely bot kicked/blocked). Cleaning up game state.")
        # Clean up game state and timers for this chat if forbidden
        current_chat_data = context.application.chat_data.get(chat_id, {})
        current_game_state = current_chat_data.pop(RL_GAME_KEY, None) # Remove game state
        if current_game_state:
            timer_job = current_game_state.get('timer_job_name')
            display_timer_job = current_game_state.get('timer_display_job_name')
            if timer_job: await rl_remove_job_if_exists(timer_job, context)
            if display_timer_job: await rl_remove_job_if_exists(display_timer_job, context)
        return None # Indicate failure
    except Exception as e:
        logger.error(f"Unexpected error showing roulette state for chat {chat_id} (msg {message_id}): {e}", exc_info=True)
        return None # Indicate failure

    return result


async def rl_start_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Add Bet' button press."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id
    logger.debug(f"rl_start_bet_callback from user {user.id} in chat {chat_id}")

    # Ensure user profile exists
    get_or_create_user(user.id)

    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # --- Validations ---
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

    # --- Start Bet Creation Process ---
    # Use user_data for temporary bet construction state
    context.user_data[RL_USER_TEMP_BET_KEY] = {'step': 'type'}
    await query.answer() # Acknowledge button press

    try:
        # Edit the main game message to show bet type selection
        await query.edit_message_text(
            text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
            reply_markup=rl_get_bet_type_keyboard(),
            parse_mode=ParseMode.HTML
        )
        # Store the message_id being edited for later steps
        context.user_data[RL_USER_TEMP_BET_KEY]['prompt_message_id'] = query.message.message_id

    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing message for bet type selection: {e}")
            # Attempt to send a reply if edit failed fundamentally
            try: await query.message.reply_text("Ошибка отображения меню ставок.")
            except Exception: pass
        else:
            logger.debug("Message not modified on starting bet type selection.")
    except Exception as e:
        logger.error(f"Error editing message for bet type selection: {e}")
        # Attempt to send a reply if edit failed
        try: await context.bot.send_message(chat_id, "Ошибка отображения меню ставок.")
        except Exception: pass


async def rl_choose_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of bet type (Number, Color, etc.)."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id # Get chat_id

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # --- Validation ---
    if not temp_bet or temp_bet.get('step') != 'type':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        # Restore main game state view if bet process is broken
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    await query.answer() # Acknowledge button press
    bet_type_choice = query.data.replace('rl_type_', '')
    temp_bet['type'] = bet_type_choice
    prompt_message_id = temp_bet.get('prompt_message_id')

    try:
        if bet_type_choice == 'number':
            # --- Ask for Number Input ---
            temp_bet['step'] = 'ask_number'
            # Ensure prompt_message_id is stored
            if not prompt_message_id: prompt_message_id = query.message.message_id
            temp_bet['prompt_message_id'] = prompt_message_id
            context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet # Save state

            # Edit the message to prompt for text input
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=prompt_message_id,
                text="➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n<b>Введите число (0, 00, или 1-36) в чат:</b>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]),
                parse_mode=ParseMode.HTML
            )
        else:
            # --- Show Value Selection Keyboard ---
            temp_bet['step'] = 'value'
            # Ensure prompt_message_id is stored
            if not prompt_message_id: prompt_message_id = query.message.message_id
            temp_bet['prompt_message_id'] = prompt_message_id
            context.user_data[RL_USER_TEMP_BET_KEY] = temp_bet # Save state

            # Get display name for the chosen type
            type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type_choice.capitalize(), bet_type_choice.capitalize()) # Fallback
            await context.bot.edit_message_text(
                 chat_id=chat_id, message_id=prompt_message_id,
                 text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                 reply_markup=rl_get_bet_value_keyboard(bet_type_choice),
                 parse_mode=ParseMode.HTML
             )

    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing message for bet value/number selection (chat {chat_id}): {e}")
    except Forbidden:
         logger.error(f"Forbidden: Cannot edit message for bet value/number selection in chat {chat_id}")
    except Exception as e:
        logger.error(f"Error editing message for bet value/number selection (chat {chat_id}): {e}")


async def rl_handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text messages when expecting a roulette number bet."""
    if not update.message or not update.effective_user or not update.effective_chat: return
    user = update.effective_user
    chat_id = update.effective_chat.id
    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # Only process if we are in the 'ask_number' step
    if not temp_bet or temp_bet.get('step') != 'ask_number':
        # logger.debug(f"Ignoring text input from user {user.id} in chat {chat_id}, not in 'ask_number' step.")
        return

    number_input = update.message.text.strip().lower()
    prompt_message_id = temp_bet.get('prompt_message_id') # Get the ID of the message to edit

    # Delete the user's number input message for cleanliness
    try: await update.message.delete()
    except Exception as e: logger.debug(f"Could not delete user number input message in chat {chat_id}: {e}")

    # Validate the input
    if number_input not in RL_AMERICAN_WHEEL_SET:
        if prompt_message_id:
            try:
                # Re-edit the prompt message to show error
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=prompt_message_id,
                    text=f"➕ <b>Новая ставка</b>\nТип: 🔢 Число\n\n"
                         f"<b>Неверный ввод: '{html_escape(number_input)}'.</b>\n"
                         f"Введите число (0, 00, или 1-36) в чат:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='rl_cancel_bet_step')]]),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Error editing message to show invalid number input: {e}")
        else:
            logger.error(f"Cannot show invalid number input error, prompt_message_id missing for user {user.id}.")
        return # Wait for valid input

    # --- Valid Number Received ---
    bet_type = temp_bet['type'] # Should be 'number'
    temp_bet['value'] = number_input # Store the valid number string
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, number_input)
    temp_bet['step'] = 'amount' # Move to amount selection step

    # Get balance for amount keyboard
    balance = get_balance(user.id)
    if balance is None:
        logger.error(f"Failed to get balance for user {user.id} during number bet.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Cancel bet process
        if prompt_message_id:
            try: await context.bot.delete_message(chat_id, prompt_message_id) # Clean up prompt
            except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True) # Show main state
        try: await context.bot.send_message(chat_id, "Ошибка получения вашего баланса. Ставка отменена.")
        except Exception: pass
        return

    # Edit the prompt message to ask for amount
    if prompt_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=prompt_message_id,
                text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\n"
                     f"Ваш баланс: {balance:.2f} F\nВыберите сумму ставки:",
                reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error editing message for amount selection after number input: {e}")
            # If edit fails, try to revert to main state? Or send error message?
            context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
            await rl_show_game_state(context, chat_id, edit_existing=True)
    else:
        logger.error(f"Cannot proceed with amount selection, prompt_message_id missing for user {user.id}.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Cancel bet process
        await rl_show_game_state(context, chat_id, edit_existing=True) # Restore main state


async def rl_choose_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of bet value (Red, Black, Even, Odd, Dozen, Column)."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # --- Validation ---
    if not temp_bet or temp_bet.get('step') != 'value':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    await query.answer() # Acknowledge button press
    bet_value_choice = query.data.replace('rl_value_', '')
    bet_type = temp_bet['type']
    temp_bet['value'] = bet_value_choice
    temp_bet['value_display'] = rl_get_value_display_name(bet_type, bet_value_choice)
    temp_bet['step'] = 'amount' # Move to amount selection
    prompt_message_id = temp_bet.get('prompt_message_id', query.message.message_id)
    temp_bet['prompt_message_id'] = prompt_message_id # Ensure it's stored

    # Get balance for amount keyboard
    balance = get_balance(user.id)
    if balance is None:
        logger.error(f"Failed to get balance for user {user.id} during value bet selection.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Cancel bet
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        try: await context.bot.send_message(chat_id,"Ошибка получения вашего баланса. Ставка отменена.")
        except Exception: pass
        return

    # Edit message to show amount selection
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=prompt_message_id,
            text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet['value_display']}\n\n"
                 f"Ваш баланс: {balance:.2f} F\nВыберите сумму ставки:",
            reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing message for bet amount selection (chat {chat_id}): {e}")
    except Forbidden:
         logger.error(f"Forbidden: Cannot edit message for bet amount selection in chat {chat_id}")
    except Exception as e:
        logger.error(f"Error editing message for bet amount selection (chat {chat_id}): {e}")


async def rl_choose_bet_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles selection of the bet amount."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # --- Validation ---
    if not temp_bet or temp_bet.get('step') != 'amount':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Extract amount
    try:
        bet_amount = int(query.data.replace('rl_amount_', ''))
    except ValueError:
        await query.answer("Неверное значение суммы.", show_alert=True)
        return

    # Check balance
    balance = get_balance(user.id)
    bet_type = temp_bet.get('type', 'unknown') # Get type for back button context
    prompt_message_id = temp_bet.get('prompt_message_id', query.message.message_id)
    temp_bet['prompt_message_id'] = prompt_message_id # Ensure stored

    if balance is None:
        logger.error(f"Failed to get balance for user {user.id} during amount selection.")
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Cancel bet
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        try: await context.bot.send_message(chat_id, "Ошибка получения вашего баланса. Ставка отменена.")
        except Exception: pass
        return

    if not (0 < bet_amount <= balance):
        await query.answer(f"Недостаточно средств ({balance:.2f} F) или неверная сумма.", show_alert=True)
        # Re-show amount selection with error indication (optional, can just rely on alert)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=prompt_message_id,
                text=f"➕ <b>Новая ставка</b>\nТип: {temp_bet.get('value_display', 'N/A')}\n\n"
                     f"Ваш баланс: {balance:.2f} F\n"
                     f"<b>Неверная сумма ({bet_amount} F)!</b> Выберите сумму ставки:", # Highlight error
                reply_markup=rl_get_bet_amount_keyboard(balance, bet_type),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                 logger.error(f"Error re-editing message for invalid amount: {e}")
        except Exception as e:
            logger.error(f"Error re-editing message for invalid amount: {e}")
        return # Don't proceed

    # --- Amount Valid, Proceed to Confirmation ---
    await query.answer() # Acknowledge selection
    temp_bet['amount'] = bet_amount
    temp_bet['step'] = 'confirm' # Move to confirmation step

    # Fetch user display name for confirmation message
    _, display_name = await get_user_mention(context, user.id)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=prompt_message_id,
            text=(f"➕ <b>Подтверждение ставки</b>\n"
                  f" - Игрок: {display_name}\n"
                  f" - Ставка: {temp_bet['value_display']}\n"
                  f" - Сумма: {temp_bet['amount']} F\n\n"
                  f"Подтверждаете?"),
            reply_markup=rl_get_confirmation_keyboard(),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing message for confirmation: {e}")
    except Forbidden:
        logger.error(f"Forbidden: Cannot edit message for confirmation in chat {chat_id}")
    except Exception as e:
        logger.error(f"Error editing message for confirmation: {e}")


async def rl_confirm_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Yes' confirmation button press and starts timers if first bet."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id
    prompt_message_id = query.message.message_id # This is the ID of the confirmation message

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)

    # --- Validation ---
    if not temp_bet or temp_bet.get('step') != 'confirm':
        await query.answer("Неверный шаг или ставка отменена.", show_alert=True)
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Ставки больше не принимаются.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None) # Clear temp bet
        # Delete the confirmation message and show main state
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Final checks before placing bet
    bet_amount = temp_bet.get('amount', 0)
    balance = get_balance(user.id)

    if balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    if not (0 < bet_amount <= balance):
        await query.answer(f"Недостаточно средств ({balance:.2f} F) или неверная сумма.", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Check game limits again
    active_bets_by_user = game_state.get('active_bets', {})
    total_bets_count = sum(len(bets) for bets in active_bets_by_user.values())
    user_bets_count = len(active_bets_by_user.get(user.id, []))

    if total_bets_count >= RL_MAX_BETS_PER_ROUND:
        await query.answer(f"Достигнут общий лимит ставок ({RL_MAX_BETS_PER_ROUND})!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return
    if user_bets_count >= RL_MAX_BETS_PER_USER:
        await query.answer(f"Вы достигли своего лимита ставок ({RL_MAX_BETS_PER_USER})!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # --- All checks passed, place the bet ---
    new_balance = update_balance(user.id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка списания средств со счета!", show_alert=True)
        context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, chat_id, edit_existing=True)
        return

    # Create final bet dictionary
    final_bet = {
        'type': temp_bet.get('type'),
        'value': temp_bet.get('value'),
        'amount': bet_amount,
        'value_display': temp_bet.get('value_display', 'N/A')
    }

    # Add bet to chat_data game state
    if user.id not in active_bets_by_user:
        active_bets_by_user[user.id] = []
    active_bets_by_user[user.id].append(final_bet)
    game_state['active_bets'] = active_bets_by_user # Update state

    # Clear temporary user data
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
    await query.answer("✅ Ставка принята!")
    logger.info(f"User {user.id} placed bet in chat {chat_id}: {final_bet}")

    # --- Start Timers if First Bet ---
    current_total_bets = sum(len(bets) for bets in active_bets_by_user.values())
    spin_timer_job_name = f'rl_spin_timer_{chat_id}'
    display_timer_job_name = f'rl_display_timer_{chat_id}'

    # Check if timers already exist for this chat
    spin_timer_exists = bool(context.job_queue.get_jobs_by_name(spin_timer_job_name))
    display_timer_exists = bool(context.job_queue.get_jobs_by_name(display_timer_job_name))

    if current_total_bets == 1 and not spin_timer_exists and not display_timer_exists:
        # Schedule the main spin timer
        context.job_queue.run_once(
            rl_spin_roulette_job,
            RL_BET_TIMER_SECONDS,
            chat_id=chat_id, # Pass chat_id to job context
            name=spin_timer_job_name,
            data={'chat_id': chat_id} # Pass data needed by job
        )
        game_state['timer_job_name'] = spin_timer_job_name # Store name in state

        # Schedule the repeating display timer
        context.job_queue.run_repeating(
            rl_update_timer_display_job,
            interval=RL_TIMER_DISPLAY_UPDATE_INTERVAL,
            first=0.1, # Start quickly after first bet
            chat_id=chat_id, # Pass chat_id
            name=display_timer_job_name,
            data={'chat_id': chat_id, 'main_timer_job_name': spin_timer_job_name} # Pass needed data
        )
        game_state['timer_display_job_name'] = display_timer_job_name # Store name
        logger.info(f"Started roulette timers for chat {chat_id}: Spin='{spin_timer_job_name}', Display='{display_timer_job_name}'")
    elif not spin_timer_exists and current_total_bets > 0:
         logger.warning(f"Bets exist ({current_total_bets}) but spin timer job '{spin_timer_job_name}' not found for chat {chat_id}. Timer might not have started correctly.")
         # Optionally try restarting timer here? Risky.


    # --- Update Main Game Message ---
    # Delete the confirmation message first
    try: await context.bot.delete_message(chat_id, prompt_message_id)
    except Exception as e: logger.warning(f"Could not delete confirmation message {prompt_message_id}: {e}")

    # Show the updated main game state (with the new bet listed)
    await rl_show_game_state(context, chat_id, message_text=None, edit_existing=True)


async def rl_cancel_bet_step_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Cancel button presses during bet creation."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id
    prompt_message_id = query.message.message_id # ID of message with cancel button

    # Clear temporary bet data
    context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
    await query.answer("Действие отменено.")

    # Delete the bet creation message and show the main game state again
    try: await context.bot.delete_message(chat_id, prompt_message_id)
    except Exception as e: logger.warning(f"Could not delete bet creation message {prompt_message_id} on cancel: {e}")

    await rl_show_game_state(context, chat_id, message_text="Создание ставки отменено.", edit_existing=True)


async def rl_back_to_bet_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Back button press to return to bet type selection."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user # Keep user variable consistent
    chat_id = query.message.chat_id

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)
    prompt_message_id = query.message.message_id # ID of message with back button

    if temp_bet:
        # Reset step to 'type', keep prompt_message_id if available
        original_prompt_id = temp_bet.get('prompt_message_id', prompt_message_id)
        context.user_data[RL_USER_TEMP_BET_KEY] = {
            'step': 'type',
            'prompt_message_id': original_prompt_id
        }
        await query.answer() # Acknowledge back button

        try:
            # Edit the message back to type selection
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=original_prompt_id,
                text="➕ <b>Новая ставка</b>\nВыберите тип ставки:",
                reply_markup=rl_get_bet_type_keyboard(),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error editing message for back to bet type: {e}")
        except Exception as e:
            logger.error(f"Error editing message for back to bet type: {e}")
            # If edit fails, maybe try reverting to main state?
            context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
            try: await context.bot.delete_message(chat_id, original_prompt_id)
            except Exception: pass
            await rl_show_game_state(context, chat_id, edit_existing=True)
    else:
        # Temp bet data lost, just cancel
        await query.answer("Отмена (ошибка состояния).")
        try: await context.bot.delete_message(chat_id, prompt_message_id)
        except Exception: pass
        await rl_show_game_state(context, query.message.chat_id, edit_existing=True)


async def rl_back_to_bet_value_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Back button press from amount selection to value selection (not for 'number' type)."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    user = query.from_user
    chat_id = query.message.chat_id

    temp_bet = context.user_data.get(RL_USER_TEMP_BET_KEY)
    prompt_message_id = query.message.message_id # ID of message with back button

    # Check if bet type exists and is not 'number'
    if temp_bet and 'type' in temp_bet and temp_bet.get('type') != 'number':
        bet_type = temp_bet['type']
        # Reset step to 'value', keep type and prompt_id
        original_prompt_id = temp_bet.get('prompt_message_id', prompt_message_id)
        context.user_data[RL_USER_TEMP_BET_KEY] = {
            'step': 'value',
            'type': bet_type,
            'prompt_message_id': original_prompt_id
        }
        await query.answer() # Acknowledge button

        type_display = RL_BET_VALUE_DISPLAY_NAMES.get(bet_type.capitalize(), bet_type)
        try:
            # Edit message back to value selection
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=original_prompt_id,
                text=f"➕ <b>Новая ставка</b>\nТип: {type_display}\n\nВыберите значение:",
                reply_markup=rl_get_bet_value_keyboard(bet_type),
                parse_mode=ParseMode.HTML
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing message for back to bet value: {e}")
        except Exception as e:
            logger.error(f"Error editing message for back to bet value: {e}")
            # If edit fails, revert to main state
            context.user_data.pop(RL_USER_TEMP_BET_KEY, None)
            try: await context.bot.delete_message(chat_id, original_prompt_id)
            except Exception: pass
            await rl_show_game_state(context, chat_id, edit_existing=True)
    else:
        # If type is number or temp_bet is broken, go all the way back to type selection
        await rl_back_to_bet_type_callback(update, context)


async def rl_spin_roulette_logic(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Contains the core logic for spinning the wheel and determining results."""
    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)
    bot = context.bot # Use context.bot

    if not game_state:
        logger.error(f"Spin logic called for chat {chat_id} but no game state found.")
        return

    # --- State and Bet Validation ---
    current_state = game_state.get('state')
    if current_state == 'spinning':
        logger.warning(f"Spin logic called again for chat {chat_id} while already spinning. Ignoring.")
        return
    if current_state != 'accepting_bets':
        logger.warning(f"Spin logic called for chat {chat_id} but state is '{current_state}'. Aborting spin.")
        return

    active_bets_by_user = dict(game_state.get('active_bets', {})) # Create a copy
    if not active_bets_by_user:
        logger.warning(f"Spin logic called for chat {chat_id} but no bets found.")
        game_state['state'] = 'finished' # Mark as finished
        # Clean up timers (display timer should have been removed by now)
        spin_timer_name = game_state.pop('timer_job_name', None)
        display_timer_name = game_state.pop('timer_display_job_name', None)
        if spin_timer_name: await rl_remove_job_if_exists(spin_timer_name, context)
        if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)

        msg_id = game_state.get('message_id')
        if msg_id:
            try:
                display_names = await rl_get_display_name_map(context, [])
                final_reply_markup = rl_get_main_menu_keyboard(chat_data, display_names)
                await bot.edit_message_text(
                    chat_id, msg_id,
                    "Ставок не было, раунд завершен.\nНажмите 'Начать новый раунд' ниже.",
                    reply_markup=final_reply_markup)
            except Exception as e: logger.warning(f"Failed to edit no-bets message: {e}")
        return

    # --- Prepare for Spin ---
    message_id = game_state.get('message_id')
    if not message_id:
        logger.error(f"Cannot spin roulette in chat {chat_id}, message_id is missing.")
        try: await bot.send_message(chat_id, "❌ Ошибка: Не найдено сообщение для отображения спина!")
        except Exception: pass
        # Clean up broken game state
        timer_job = game_state.pop('timer_job_name', None)
        display_timer_job = game_state.pop('timer_display_job_name', None)
        chat_data.pop(RL_GAME_KEY, None)
        if timer_job: await rl_remove_job_if_exists(timer_job, context)
        if display_timer_job: await rl_remove_job_if_exists(display_timer_job, context)
        return

    # Update state and remove timers
    game_state['state'] = 'spinning'
    spin_timer_name = game_state.pop('timer_job_name', None)
    display_timer_name = game_state.pop('timer_display_job_name', None)
    if spin_timer_name: await rl_remove_job_if_exists(spin_timer_name, context)
    if display_timer_name: await rl_remove_job_if_exists(display_timer_name, context)
    logger.debug(f"Removed timers '{spin_timer_name}', '{display_timer_name}' before animation start in chat {chat_id}")

    # --- Determine Winning Number ---
    winning_number_str = random.choice(AMERICAN_WHEEL_ORDER)
    try:
        target_index = AMERICAN_WHEEL_ORDER.index(winning_number_str)
    except ValueError:
        logger.error(f"Winning number '{winning_number_str}' not found in wheel order! Defaulting to '0'.")
        winning_number_str = '0'
        target_index = 0
    logger.info(f"Roulette spin result for chat {chat_id}: {winning_number_str} (index {target_index})")

    # --- Animation Setup ---
    spin_duration = RL_SPIN_ANIMATION_DURATION
    min_full_rotations = 2
    max_full_rotations = 4
    updates_per_second = 4 # How many times to update the message per second
    update_interval = 1.0 / updates_per_second
    spinner_emojis = ['[∙∙∙ ]', '[∙∙ ]', '[∙ ]', '[ ]'] # Alternative spinner

    # Calculate animation steps
    start_index = random.randint(0, RL_WHEEL_SIZE - 1)
    current_index = start_index
    num_full_rotations = random.randint(min_full_rotations, max_full_rotations)
    steps_for_rotations = num_full_rotations * RL_WHEEL_SIZE
    # Steps needed to get from start_index to target_index
    steps_to_target = (target_index - start_index + RL_WHEEL_SIZE) % RL_WHEEL_SIZE
    total_steps = steps_for_rotations + steps_to_target
    if total_steps == 0: total_steps = RL_WHEEL_SIZE # Ensure at least one rotation if start==target

    logger.info(f"Roulette Animation chat {chat_id}: Start={start_index}, Target={target_index}, Rot={num_full_rotations}, Steps={total_steps}")

    # Initial message update
    try:
        await bot.edit_message_text(
            "🎰 <b>Колесо вращается...</b>", chat_id=chat_id, message_id=message_id,
            reply_markup=None, parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(0.5) # Small pause before animation starts
    except Exception as e:
        logger.warning(f"Failed to edit message {message_id} for spin start in chat {chat_id}: {e}")
        # Continue without initial message if edit fails

    # --- Animation Loop ---
    loop_start_time = time.monotonic()
    steps_taken = 0
    next_update_time = loop_start_time
    last_displayed_number = ""
    animation_successful = True

    def ease_out_cubic(t): # t goes from 0 to 1
        t -= 1
        return t * t * t + 1

    while steps_taken < total_steps:
        # Calculate progress and time budget for this step using easing
        progress = min(1.0, (steps_taken + 1) / total_steps)
        eased_progress = ease_out_cubic(progress)
        target_step_end_time = loop_start_time + spin_duration * eased_progress
        current_mono_time = time.monotonic()

        # Update message only at intervals
        if current_mono_time >= next_update_time:
            display_number = AMERICAN_WHEEL_ORDER[current_index]
            color_char = rl_get_color(display_number)
            display_color_emoji = "🟢" if color_char == 'Green' else ("🔴" if color_char == 'Red' else "⚫")
            spinner = spinner_emojis[steps_taken % len(spinner_emojis)]
            frame_text = f"🎰 {spinner} {display_color_emoji} {display_number}"

            # Avoid editing if text hasn't changed (reduces API calls)
            if frame_text != last_displayed_number: # Use frame_text for comparison now
                try:
                    await bot.edit_message_text(
                        text=frame_text, chat_id=chat_id, message_id=message_id
                    )
                    last_displayed_number = frame_text # Store the full text sent
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        pass # Ignore this specific error
                    else:
                        logger.warning(f"BadRequest editing animation chat {chat_id} (step {steps_taken}): {e}")
                        # Continue animation? Or break? Let's try continuing.
                except Forbidden:
                    logger.error(f"Forbidden error during animation in chat {chat_id}. Aborting.")
                    animation_successful = False
                    break
                except Exception as e:
                    logger.warning(f"Error editing animation chat {chat_id} (step {steps_taken}): {e}")
                    # Optionally break animation on other errors
                    # animation_successful = False
                    # break
            # Schedule next update
            next_update_time = current_mono_time + update_interval

        # Calculate sleep duration to match target time
        current_mono_time = time.monotonic()
        sleep_duration = max(0.005, target_step_end_time - current_mono_time)
        await asyncio.sleep(sleep_duration)

        # Move to next step
        current_index = (current_index + 1) % RL_WHEEL_SIZE
        steps_taken += 1

    # --- Show Final Result ---
    if animation_successful:
        try:
            final_color_char = rl_get_color(winning_number_str)
            final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
            await bot.edit_message_text(
                text=f"<b>➡️ {final_color_emoji} {winning_number_str} ⬅️</b>",
                chat_id=chat_id, message_id=message_id,
                parse_mode=ParseMode.HTML
            )
            await asyncio.sleep(1.5) # Pause on the winning number
        except Exception as e:
            logger.warning(f"Failed to show final animation number for chat {chat_id}: {e}")

    # --- Determine Winners and Losers ---
    winning_color = rl_get_color(winning_number_str)
    winning_parity = rl_is_even_or_odd(winning_number_str)
    winning_dozen = rl_get_dozen(winning_number_str)
    winning_column = rl_get_column(winning_number_str)

    results_by_user = defaultdict(lambda: {'wins': 0.0, 'returned': 0.0, 'log': [], 'bets': []})
    total_net_change = 0.0

    for user_id, bets in active_bets_by_user.items():
        user_results = results_by_user[user_id]
        user_results['bets'] = bets # Store original bets for reference
        total_bet_this_user = 0.0

        for bet in bets:
            if not isinstance(bet, dict): continue # Skip invalid bet entries

            win = False
            payout_mult = 0
            bet_type = bet.get('type')
            bet_value = bet.get('value') # Raw value (e.g., 'Red', '1st', 5)
            bet_amount = bet.get('amount', 0)
            value_disp = bet.get('value_display', 'N/A') # Display name

            if not all([bet_type, bet_value is not None, bet_amount > 0]):
                logger.warning(f"Skipping malformed bet for user {user_id} in chat {chat_id}: {bet}")
                continue

            total_bet_this_user += bet_amount

            # Check win conditions
            if bet_type == 'number' and str(bet_value) == winning_number_str:
                payout_mult = RL_PAYOUTS['number']
                win = True
            elif bet_type == 'color' and bet_value == winning_color:
                payout_mult = RL_PAYOUTS['color']
                win = True
            elif bet_type == 'parity' and bet_value == winning_parity:
                payout_mult = RL_PAYOUTS['parity']
                win = True
            elif bet_type == 'dozen' and bet_value == winning_dozen:
                payout_mult = RL_PAYOUTS['dozen']
                win = True
            elif bet_type == 'column' and bet_value == winning_column:
                payout_mult = RL_PAYOUTS['column']
                win = True

            # Calculate results
            if win:
                winnings = bet_amount * payout_mult
                returned = bet_amount + winnings # Amount to give back (original + win)
                user_results['wins'] += winnings
                user_results['returned'] += returned
                user_results['log'].append(f"✅ {value_disp} ({bet_amount:.2f}F) -> +{winnings:.2f}F")
            else:
                user_results['log'].append(f"❌ {value_disp} ({bet_amount:.2f}F)")
                # No winnings, no return amount added for this bet

    # --- Format Results and Update Balances ---
    result_lines = []
    player_ids = list(results_by_user.keys())
    # Fetch mentions efficiently
    html_mention_map = {}
    if player_ids:
         try:
             mention_data_list = await asyncio.gather(*(get_user_mention(context, uid) for uid in player_ids))
             html_mention_map = {uid: mention_data[0] for i, uid in enumerate(player_ids) for mention_data in [mention_data_list[i]]}
         except Exception as e:
             logger.error(f"Failed to fetch HTML mentions for roulette results in chat {chat_id}: {e}")
             html_mention_map = {uid: f"User_{uid}" for uid in player_ids} # Fallback

    processed_users = set()
    for user_id, results in results_by_user.items():
        processed_users.add(user_id)
        user_mention_html = html_mention_map.get(user_id, f"User_{user_id}")
        amount_to_pay = results.get('returned', 0.0)
        # Calculate total bet amount for this user from stored bets
        total_bet_amount_user = sum(b.get('amount', 0) for b in results.get('bets', []) if isinstance(b, dict))
        net_change_user = amount_to_pay - total_bet_amount_user

        result_lines.append(f"\n--- {user_mention_html} ---")
        result_lines.extend(results.get('log', []))

        # Update balance
        balance_update_status = ""
        new_bal = None
        if amount_to_pay > 0:
            new_bal = update_balance(user_id, amount_to_pay)
            if new_bal is None:
                balance_update_status = " ⚠️<b>Ошибка начисления!</b>"
                logger.error(f"Roulette payout FAILED for user {user_id} in chat {chat_id}. Amount: {amount_to_pay:.2f}")
                # Adjust net change if payout failed
                net_change_user = -total_bet_amount_user
            else:
                 balance_update_status = f" -> Баланс: {new_bal:.2f}F"
        else:
            # If no payout, just show current balance
            current_bal = get_balance(user_id)
            if current_bal is not None:
                balance_update_status = f" -> Баланс: {current_bal:.2f}F"
            else:
                 balance_update_status = " (ошибка баланса)"


        result_lines.append(f"<i>Итог: {net_change_user:+.2f} F{balance_update_status}</i>")
        total_net_change += net_change_user # Accumulate total net change

    # Check for users who bet but weren't processed (shouldn't happen)
    original_user_ids = list(active_bets_by_user.keys())
    for user_id in original_user_ids:
        if user_id not in processed_users:
            logger.warning(f"User {user_id} had bets but was not in results_by_user for chat {chat_id}")
            result_lines.append(f"\n--- User_{user_id} (Ошибка обработки) ---")

    # --- Final Message ---
    final_color_char = rl_get_color(winning_number_str)
    final_color_emoji = "🟢" if final_color_char == 'Green' else ("🔴" if final_color_char == 'Red' else "⚫")
    timestamp = datetime.datetime.now().strftime("%H:%M:%S") # Add seconds
    result_header = f"🎉 Выпало: <b>{final_color_emoji} {winning_number_str}</b> 🎉\n"
    result_summary = f"\n\n<b>Общий итог раунда: {total_net_change:+.2f} F</b>    [{timestamp}]"
    full_result_text = result_header + "\n".join(result_lines) + result_summary

    # Update game state to finished
    current_game_state = context.application.chat_data.get(chat_id, {}).get(RL_GAME_KEY)
    if current_game_state:
        current_game_state['state'] = 'finished'
        current_game_state['active_bets'] = {} # Clear bets
        current_game_state['timer_job_name'] = None
        current_game_state['timer_display_job_name'] = None
    else:
        logger.warning(f"Game state for chat {chat_id} disappeared before final state reset in spin logic.")

    # Get final keyboard (should show "Start New Round")
    # Need display names again for keyboard generation (empty map is fine here)
    final_chat_data = context.application.chat_data.get(chat_id, {})
    final_reply_markup = rl_get_main_menu_keyboard(final_chat_data, {})

    try:
        await bot.edit_message_text(
            text=full_result_text, chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML, reply_markup=final_reply_markup
        )
    except Forbidden:
        logger.error(f"Forbidden: Cannot send final result to chat {chat_id}")
        # Clean up game data if message failed due to permissions
        context.application.chat_data.pop(chat_id, None)
    except Exception as e:
        logger.error(f"Failed to edit final roulette result for chat {chat_id}: {e}")
        # If edit fails, try sending as a new message (less ideal)
        try:
            await bot.send_message(
                chat_id=chat_id, text=full_result_text,
                parse_mode=ParseMode.HTML, reply_markup=final_reply_markup
            )
        except Exception as send_e:
            logger.error(f"Failed to send final roulette result as new message for chat {chat_id}: {send_e}")

    logger.info(f"Roulette round finished in chat {chat_id}. Winning number: {winning_number_str}")


async def rl_spin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the manual 'Spin!' button press."""
    query = update.callback_query
    if not query or not query.message or not query.from_user: return
    chat_id = query.message.chat_id
    user = query.from_user
    logger.info(f"Manual spin triggered by user {user.id} in chat {chat_id}")

    chat_data = context.application.chat_data.get(chat_id, {})
    game_state = chat_data.get(RL_GAME_KEY)

    # --- Validation ---
    if not game_state or game_state.get('state') != 'accepting_bets':
        await query.answer("Сейчас нельзя запустить вращение.", show_alert=True)
        return

    active_bets_by_user = game_state.get('active_bets', {})
    if not active_bets_by_user:
        await query.answer("Нет ставок для запуска вращения.", show_alert=True)
        return

    # Optional: Check if the user pressing the button is allowed to spin
    # (e.g., the initiator, or anyone if bets exist)
    # initiator_id = game_state.get('initiator_id')
    # if user.id != initiator_id:
    #     await query.answer("Только инициатор раунда может запустить вращение досрочно.", show_alert=True)
    #     return

    await query.answer("Запускаем вращение...")
    # Call the main spin logic
    await rl_spin_roulette_logic(context, chat_id)


async def rl_show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the roulette rules explanation."""
    query = update.callback_query
    if not query or not update.effective_chat: return

    # Use f-string for constants, makes updates easier
    help_text = (
        f"<b>🎲 Правила Американской рулетки:</b>\n\n"
        f"• Делайте ставки с помощью кнопок.\n"
        f"• Макс. ставок в раунде: {RL_MAX_BETS_PER_ROUND} (всего), {RL_MAX_BETS_PER_USER} (на игрока).\n"
        f"• После первой ставки раунд запустится через {RL_BET_TIMER_SECONDS} сек (если не нажать 'Крутить!' раньше).\n"
        # f"• Можно нажать 'Крутить!' раньше (только если ставит 1 игрок).\n" # Removed constraint comment if spin is always allowed
        f"• Можно нажать 'Крутить!' раньше, если есть ставки.\n"
        f"• Ставки на 0 или 00 выигрывают только при ставке на 'Число'.\n\n"
        f"<b>Типы ставок (Выплата 1 к X):</b>\n"
        f"- Число (Number): 1 к {RL_PAYOUTS['number']}\n"
        f"- Цвет (Color - Red/Black): 1 к {RL_PAYOUTS['color']}\n"
        f"- Чет/Нечет (Parity - Even/Odd): 1 к {RL_PAYOUTS['parity']}\n"
        f"- Дюжина (Dozen - 1st/2nd/3rd): 1 к {RL_PAYOUTS['dozen']}\n"
        f"- Колонка (Column - col1/col2/col3): 1 к {RL_PAYOUTS['column']}\n\n"
        f"<i>Ставки на Цвет, Чет/Нечет, Дюжины, Колонки <b>проигрывают</b> при выпадении 0 или 00.</i>"
    )
    await query.answer() # Answer the callback first
    try:
        # Send help as a new message in the chat
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=help_text,
            parse_mode=ParseMode.HTML
        )
    except Forbidden:
        logger.error(f"Forbidden: Cannot send roulette help to chat {update.effective_chat.id}")
    except Exception as e:
        logger.error(f"Failed to send roulette help: {e}")

async def rl_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for non-clickable buttons (like headers or display rows)."""
    if update.callback_query:
        await update.callback_query.answer() # Just acknowledge

# --- End of Full Roulette Code ---


# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """General handler for all Inline Keyboard Button presses."""
    query = update.callback_query
    if not query or not query.data or not query.from_user or not update.effective_chat:
        if query: await query.answer() # Answer even if data is missing
        return

    data = query.data
    user = query.from_user
    chat = update.effective_chat
    logger.debug(f"Callback query received: '{data}' from user {user.id} in chat {chat.id} ({chat.type})")

    # Basic routing based on prefix
    parts = data.split("_")
    prefix = parts[0] if parts else None

    try:
        # --- Blackjack Callbacks (Private Chat Only) ---
        if prefix == "bj":
            if chat.type != ChatType.PRIVATE:
                await query.answer("Играть в Блекджек можно только в личном чате.", show_alert=True)
                return

            action = parts[1] if len(parts) > 1 else None
            arg = parts[2] if len(parts) > 2 else None # Optional argument

            if action == "bet" and arg:
                try: bet_amount = int(arg)
                except ValueError: await query.answer("Ошибка: неверная сумма ставки.", show_alert=True); return
                await blackjack_handle_bet(update, context, bet_amount)
            elif action == "new" and arg == "game":
                 # blackjack_start_command now handles the logic for reusing session
                 await blackjack_start_command(update, context)
            elif action in ["hit", "stand", "double", "split"] and arg is not None:
                # Pass action and hand index (arg)
                await blackjack_handle_action(update, context, [action, arg])
            else:
                logger.warning(f"Unknown or incomplete BJ callback: {data}")
                await query.answer() # Acknowledge silently

        # --- Roulette Callbacks ---
        elif prefix == "rl":
            # Route based on full callback data string for clarity
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
            elif data == "rl_new_round": await rl_new_round_callback(update, context)
            elif data == "rl_noop": await rl_noop_callback(update, context)
            else:
                logger.warning(f"Unknown or incomplete RL callback: {data}")
                await query.answer() # Acknowledge silently

        else:
            logger.warning(f"Unknown callback prefix: {prefix} in data: {data}")
            await query.answer() # Acknowledge silently

    except ValueError as e:
        # Catch specific errors like int conversion failures
        logger.error(f"Callback ValueError for '{data}' user {user.id}: {e}", exc_info=True)
        try: await query.answer("Ошибка: Неверный формат данных.", show_alert=True)
        except Exception: pass # Ignore error if answering fails
    except BadRequest as e:
        error_str = str(e).lower()
        # Don't alert user for common, expected errors
        if "query is too old" in error_str:
            logger.debug(f"Ignoring too old callback query for user {user.id}")
            # No answer needed
        elif "message is not modified" in error_str:
            logger.debug(f"Callback resulted in 'Message is not modified' for user {user.id}")
            # Answer silently
            try: await query.answer()
            except Exception: pass
        elif "message to edit not found" in error_str:
            logger.warning(f"Callback failed: 'Message to edit not found' for user {user.id}. Data: {data}")
            try: await query.answer("Сообщение игры было удалено или изменено.", show_alert=False) # Inform user gently
            except Exception: pass
        else:
            # Other BadRequests might be more severe
            logger.warning(f"Callback BadRequest for '{data}' user {user.id}: {e}")
            try: await query.answer("Произошла ошибка при обработке.", show_alert=True)
            except Exception: pass
    except Forbidden as e:
        logger.error(f"Callback Forbidden error for user {user.id} in chat {chat.id}: {e}")
        try: await query.answer("Ошибка: Бот не имеет достаточных прав в этом чате.", show_alert=True)
        except Exception: pass
    except Conflict as e:
         logger.error(f"Conflict error during callback processing: {e}. Is another instance running?")
         try: await query.answer("Ошибка сервера. Повторите попытку позже.", show_alert=True)
         except Exception: pass
    except Exception as e:
        # Catch any other unexpected errors
        logger.error(f"Callback general error for '{data}' user {user.id}: {e}", exc_info=True)
        try: await query.answer("Произошла внутренняя ошибка сервера.", show_alert=True)
        except Exception: pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors raised by Handlers."""
    # Log the error and traceback
    logger.error(f"Exception while handling an update:", exc_info=context.error)

    # --- Specific Error Handling ---
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error detected! Ensure only ONE instance of the bot is running with this token.")
        # Optionally, notify admin or shut down gracefully?
    elif isinstance(context.error, Forbidden):
        logger.error(f"Forbidden error: {context.error}. Bot might be blocked, kicked, or lack permissions. Update: {update}")
        # Clean up chat_data or user_data if related to a specific chat/user?
        # Example: If error occurs during game in chat X, remove game state for chat X.
        if update and isinstance(update, Update) and update.effective_chat:
             chat_id = update.effective_chat.id
             logger.warning(f"Removing potentially stale game data for chat {chat_id} due to Forbidden error.")
             context.application.chat_data.pop(chat_id, None)
             # Consider removing job queue tasks for this chat_id too

    elif isinstance(context.error, BadRequest):
        error_str = str(context.error).lower()
        # Ignore common, less critical BadRequest errors in main log
        if "message is not modified" not in error_str and \
           "query is too old" not in error_str and \
           "message to delete not found" not in error_str and \
           "message identifier is not specified" not in error_str and \
           "message can't be edited" not in error_str and \
           "message to edit not found" not in error_str:
            logger.warning(f"BadRequest error: {context.error}. Update: {update}")
    # Add more specific error type handling if needed

    # Note: Avoid sending messages to the user in the main error handler
    # as the cause might be that the bot *cannot* send messages (e.g., Forbidden).


# --- Main Bot Setup ---
def main():
    """Starts the bot."""
    logger.info("Starting bot application...")
    start_keep_alive() # Start the keep-alive web server thread

    try:
        # Build the application
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True) # Enable concurrent handling
            .connect_timeout(30)      # Connection timeout
            .read_timeout(30)         # Read timeout
            .pool_timeout(30)         # Pool timeout (for get_updates long polling)
            .build()
        )

        # --- Register Handlers ---
        # Commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))
        application.add_handler(CommandHandler("blackjack", blackjack_start_command))
        application.add_handler(CommandHandler("roulette", roulette_start_command))

        # Callback Queries (Buttons) - Group 0
        application.add_handler(CallbackQueryHandler(button_callback_handler), group=0)

        # Message Handler for Roulette Number Input - Group 1
        # Handles non-command text messages, useful for the roulette number input
        # Ensure it doesn't clash with other potential text inputs if added later.
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE | filters.ChatType.GROUPS, rl_handle_number_input),
            group=1
        )

        # Error Handler (ensure it's added)
        application.add_error_handler(error_handler)

        logger.info("Handlers registered successfully.")
        print("Bot is running... Press Ctrl+C to stop.")

        # Start polling
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, # Process all update types
            drop_pending_updates=True # Ignore updates received while offline
        )

    except ValueError as e:
        # Catch potential config errors early
        logger.critical(f"Configuration Error: {e}")
        print(f"CRITICAL CONFIGURATION ERROR: {e}")
    except Conflict as e:
        # Catch conflict error (multiple instances)
        logger.critical(f"Conflict Error: {e}. Is another instance running with the same token?")
        print("CRITICAL ERROR: Conflict detected. Ensure only one instance is running.")
    except Exception as e:
        # Catch any other unexpected errors during startup
        logger.critical(f"Unexpected critical error during bot startup: {e}", exc_info=True)
        print(f"CRITICAL STARTUP ERROR: {e}")
    finally:
        print("Bot stopped.")
        logger.info("Bot application has stopped.")

if __name__ == "__main__":
    main()
