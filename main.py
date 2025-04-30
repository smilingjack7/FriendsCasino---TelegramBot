# -*- coding: utf-8 -*-
import logging
import os
import random
import datetime
import time
import asyncio
from collections import defaultdict
from threading import Thread
from flask import Flask
from html import escape as html_escape # Для экранирования в MarkdownV1/HTML

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL: raise ValueError("DATABASE_URL environment variable not set")

INITIAL_BALANCE = 100.0
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
NUM_DECKS = 8 # Количество колод в "башмаке"
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5 # Множитель выплаты за Блекджек (1.5 к 1)
MAX_SPLITS = 3 # Максимальное количество сплитов на одну начальную руку
DEALER_TURN_DELAY = 0.3 # Задержка перед ходом дилера (секунды)
LEADERBOARD_LIMIT = 10 # Количество позиций в таблице лидеров

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
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
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)

def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11}

# --- Database Interaction ---

def get_db_conn():
    """Establishes a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise

def get_or_create_user(user_id: int) -> dict | None:
    """Retrieves user data or creates a new user with initial balance."""
    select_sql = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    insert_sql = "INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, NULL) ON CONFLICT (user_id) DO NOTHING;"
    user_data = None
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(select_sql, (user_id,))
            user_data = cursor.fetchone()
            if user_data is None:
                cursor.execute(insert_sql, (user_id, INITIAL_BALANCE))
                logger.info(f"New user created: {user_id} with balance {INITIAL_BALANCE}")
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()
                if not user_data: # Fallback if fetch after insert fails
                    logger.warning(f"Failed to fetch user {user_id} immediately after creation.")
                    return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        # Ensure last_bonus is datetime or None
        if user_data and user_data.get('last_bonus') and not isinstance(user_data['last_bonus'], datetime.datetime):
             user_data['last_bonus'] = None # Treat invalid date as None
        return user_data
    except Exception as e:
        logger.error(f"DB error (get_or_create_user) for {user_id}: {e}", exc_info=True)
        return None

def update_balance(user_id: int, amount_change: float) -> float | None:
    """Updates user balance atomically and returns the new balance."""
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cursor:
            cursor.execute(sql_update, (amount_change, user_id))
            result = cursor.fetchone()
            if result:
                new_balance = result[0]
                logger.info(f"User {user_id} balance changed by {amount_change:+.2f}. New balance: {new_balance:.2f}")
                return new_balance
            else:
                logger.warning(f"Failed to update balance for non-existent user? ID: {user_id}")
                return None
    except Exception as e:
        logger.error(f"DB error (update_balance) for {user_id}: {e}", exc_info=True)
        return None

def get_balance(user_id: int) -> float | None:
    """Gets the current balance for a user."""
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, bonus_time_utc: datetime.datetime):
     """Updates the last bonus timestamp (expects offset-naive UTC)."""
     sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
     # Ensure time is offset-naive before saving
     bonus_time_to_save = bonus_time_utc.replace(tzinfo=None)
     try:
         with get_db_conn() as conn, conn.cursor() as cursor:
             cursor.execute(sql, (bonus_time_to_save, user_id))
         logger.info(f"Last bonus time for {user_id} updated to {bonus_time_to_save}")
     except Exception as e:
         logger.error(f"DB error (update_last_bonus_time) for {user_id}: {e}", exc_info=True)

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Gets the last bonus time as an offset-naive UTC datetime."""
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None
    return last_bonus # Should already be offset-naive datetime or None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    """Gets the top players by balance."""
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, (limit,))
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"DB error (get_leaderboard): {e}", exc_info=True)
        return []

# --- Game Utilities ---

def create_deck(num_decks=NUM_DECKS) -> list:
    """Creates and shuffles a multi-deck shoe."""
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    logger.info(f"Created and shuffled a new {num_decks}-deck shoe ({len(deck)} cards).")
    return deck

def get_card_value(card: tuple | None) -> int:
    """Gets the Blackjack value of a card (Ace=11 initially)."""
    if not card: return 0
    return RANK_VALUES.get(card[0], 0)

def get_hand_value(hand: list) -> int:
    """Calculates the Blackjack value of a hand, adjusting for Aces."""
    value = sum(get_card_value(card) for card in hand if card)
    ace_count = sum(1 for card in hand if card and card[0] == 'A')
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
    return value

def format_hand(hand: list, hide_one: bool = False) -> str:
    """Formats a hand for display, optionally hiding the second card."""
    if not hand: return "Пусто"
    if hide_one and len(hand) > 1:
        first_card = hand[0]
        return f"[{first_card[0]}{first_card[1]}, ??]" if first_card else "[??, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in hand if c])

# --- Obfuscated Card Drawing ---
# WARNING: This function's internal logic is intentionally obscured.
# It performs a standard random draw and removal from the source list.
def _fetch_and_update_source(data_container: list, param1: int, param2: int) -> tuple:
    """
    Retrieves a random element from the data container and removes it.
    The parameters param1 and param2 are currently unused placeholders.
    Returns (element, status_code) or (None, status_code).
    """
    _status_ok = 0 # Represents a successful operation status
    _status_err = -1 # Represents an error status
    if not data_container:
        logger.warning("Data container is empty during fetch operation.")
        return None, _status_err # Return error status if container is empty
    try:
        container_size = len(data_container)
        # Select index randomly
        selected_index = random.randrange(container_size)
        # Retrieve and remove element by index
        retrieved_element = data_container.pop(selected_index)
        # Return the element and OK status
        return retrieved_element, _status_ok
    except IndexError:
        # This might happen in rare race conditions or if container becomes empty
        logger.warning("Fetch operation failed: Index out of bounds.")
        return None, _status_err
    except Exception as e:
        logger.error(f"Unexpected error during fetch operation: {e}", exc_info=True)
        return None, _status_err

# --- Core Bot Commands ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    logger.info(f"/start command from {user.full_name} (ID: {user.id})")
    get_or_create_user(user.id) # Ensure user exists
    balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Добро пожаловать, {user.first_name}! 👋\n"
        f"Ваш баланс: {balance_str} фишек.\n\n"
        f"Начните игру: /blackjack\n"
        f"Помощь: /help"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /help command using MarkdownV2."""
    user_id = update.effective_user.id
    logger.info(f"/help command from {user_id}")
    # --- Escape reserved characters for MarkdownV2 ---
    help_text = (
        "ℹ️ *Список доступных команд:*\n\n"
        "/start \\- Приветствие и баланс\n"
        "/blackjack \\- Начать игру в Блекджек\n"
        "/balance \\- Показать текущий баланс\n"
        f"/bonus \\- Ежедневный бонус \\({BONUS_AMOUNT} F, раз в {BONUS_COOLDOWN_HOURS} ч\\)\n" # Escaped ( and )
        "/leaderboard \\- Таблица лидеров\n"
        "/help \\- Это сообщение помощи"
    )
    try:
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Error sending /help with MarkdownV2: {e}")
        # Fallback to plain text
        plain_text = help_text.replace("\\", "").replace("*", "") # Remove escapes and formatting
        try: await update.message.reply_text(plain_text)
        except Exception as fe: logger.error(f"Failed to send plain text /help fallback: {fe}")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /balance command."""
    user_id = update.effective_user.id
    logger.info(f"/balance command from {user_id}")
    balance = get_balance(user_id)
    if balance is not None:
        await update.message.reply_text(f"💰 Ваш баланс: {balance:.2f} фишек.")
    else:
        await update.message.reply_text("Не удалось получить баланс. Попробуйте /start.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /bonus command."""
    user_id = update.effective_user.id
    logger.info(f"/bonus command from {user_id}")
    if not get_or_create_user(user_id):
        await update.message.reply_text("Ошибка получения данных. Попробуйте /start."); return

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus_time = get_last_bonus_time(user_id)
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time and (now_utc < last_bonus_time + cooldown):
        time_left = last_bonus_time + cooldown - now_utc
        hours, rem = divmod(time_left.total_seconds(), 3600)
        minutes, _ = divmod(rem, 60)
        await update.message.reply_text(f"⏳ Бонус уже получен. Попробуйте через {int(hours)} ч {int(minutes)} мин.")
        return

    new_balance = update_balance(user_id, BONUS_AMOUNT)
    if new_balance is not None:
        update_last_bonus_time(user_id, now_utc)
        await update.message.reply_text(f"🎉 Бонус {BONUS_AMOUNT} фишек начислен!\nНовый баланс: {new_balance:.2f} фишек.")
    else:
        await update.message.reply_text("Ошибка начисления бонуса. Попробуйте позже.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /leaderboard command using Markdown V1."""
    user_id = update.effective_user.id
    logger.info(f"/leaderboard command from {user_id}")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("Таблица лидеров пуста."); return

    leaderboard_text = "🏆 *Таблица Лидеров*\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]

    # Cache user info to reduce API calls
    user_cache = context.bot_data.setdefault('user_cache', {})
    cache_expiry = datetime.timedelta(hours=1)
    now = datetime.datetime.now(datetime.timezone.utc)

    async def get_display_name(leader_id):
        cached = user_cache.get(leader_id)
        if cached and (now - cached['timestamp']) < cache_expiry:
            return cached['name']

        try:
            user_chat = await context.bot.get_chat(leader_id)
            # Use HTML escaping for names in Markdown V1
            name = html_escape(user_chat.first_name or user_chat.full_name or f"User_{leader_id}")
            display_name = name # Default to name
            # Prefer @username if available (no link in V1)
            if user_chat.username:
                 display_name = f"@{html_escape(user_chat.username)}"

            user_cache[leader_id] = {'name': display_name, 'timestamp': now}
            return display_name
        except Exception: # Catch broad exceptions for get_chat
            logger.warning(f"Failed to get info for user {leader_id} in leaderboard")
            return f"User ID: {leader_id}" # Fallback

    display_names = await asyncio.gather(*(get_display_name(l['user_id']) for l in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"{i+1}."
        name = display_names[i]
        balance_str = f"{leader['balance']:.2f}"
        leaderboard_text += f"{place} {name} - `{balance_str}` фишек\n" # Use ` for balance

    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        plain_text = leaderboard_text.replace("*", "").replace("`", "") # Fallback
        try: await update.message.reply_text(plain_text)
        except: pass # Ignore fallback error

# --- Blackjack Game ---
# (Group Blackjack specific functions and handlers here)

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates a new Blackjack game or prompts for a bet."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"Blackjack game initiated by {user.full_name} ({user.id}) in chat {chat_id}")

    reply_func = None
    delete_prev_msg_id = None

    if update.callback_query:
        reply_func = update.callback_query.message.reply_text
        delete_prev_msg_id = update.callback_query.message.message_id
        try: await update.callback_query.answer()
        except BadRequest: pass # Ignore old query errors
    elif update.message:
        reply_func = update.message.reply_text
    else: return # Should not happen

    balance = get_balance(user.id)
    if balance is None:
        await reply_func("Не удалось проверить баланс. /start"); return

    # Game state management
    games = context.bot_data.setdefault('games', {})
    if chat_id in games:
        game = games[chat_id]
        old_msg_id = game.get('message_id')
        # Allow starting only if previous game is finished/waiting
        if game.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Текущая игра не завершена."); return
        # Clean up previous game message if needed
        if old_msg_id and old_msg_id != delete_prev_msg_id:
            try: await context.bot.delete_message(chat_id, old_msg_id)
            except Exception: pass # Ignore deletion errors
        del games[chat_id] # Remove old game state

    if balance <= 0:
        await reply_func(f"Баланс ({balance:.2f}) 0 или меньше. /bonus"); return

    bet_options = [1, 5, 10, 25, 50, 100, 250, 500]
    valid_bets = [b for b in bet_options if b <= balance]
    if not valid_bets:
        await reply_func(f"Баланс ({balance:.2f}) < мин. ставки ({min(bet_options)}). /bonus"); return

    # Create bet buttons
    buttons = []
    row = []
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) >= 4: buttons.append(row); row = []
    if row: buttons.append(row)
    markup = InlineKeyboardMarkup(buttons)

    # Send bet prompt
    try:
        text = f"Баланс: {balance:.2f}. Ваша ставка:"
        if delete_prev_msg_id: # If called from "New Game" button
            try: await context.bot.delete_message(chat_id, delete_prev_msg_id)
            except Exception: pass
            sent_message = await context.bot.send_message(chat_id, text, reply_markup=markup)
        else: # If called from /blackjack command
            sent_message = await reply_func(text, reply_markup=markup)

        games[chat_id] = {'player_id': user.id, 'state': 'waiting_bet', 'message_id': sent_message.message_id}
        logger.info(f"Blackjack game {chat_id} waiting for bet from {user.id}")
    except Exception as e:
        logger.error(f"Failed to send bet prompt for {chat_id}: {e}", exc_info=True)
        try: await reply_func("Ошибка начала игры.")
        except: pass

async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    """Handles the bet selection and deals initial cards."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    games = context.bot_data.get('games', {})

    if chat_id not in games: await query.answer("Игра не найдена.", show_alert=True); return
    game = games[chat_id]
    if game.get('player_id') != user.id: await query.answer("Не ваша игра.", show_alert=True); return
    if game.get('state') != 'waiting_bet': await query.answer("Ставка уже сделана.", show_alert=False); return

    balance = get_balance(user.id)
    if balance is None: await query.answer("Ошибка баланса.", show_alert=True); return
    if bet_amount <= 0 or bet_amount > balance: await query.answer("Неверная ставка.", show_alert=True); return

    if update_balance(user.id, -bet_amount) is None: await query.answer("Ошибка списания ставки.", show_alert=True); return

    deck = create_deck(NUM_DECKS)
    player_hand, dealer_hand = [], []
    dealt_count = 0
    try: # Initial deal
        for _ in range(2):
            card_p, status_p = _fetch_and_update_source(deck, dealt_count, NUM_DECKS)
            card_d, status_d = _fetch_and_update_source(deck, dealt_count + 1, NUM_DECKS)
            if status_p < 0 or status_d < 0: raise ValueError("Failed to draw card")
            player_hand.append(card_p); dealt_count += 1
            dealer_hand.append(card_d); dealt_count += 1
    except Exception as e:
        logger.error(f"Initial deal error {chat_id}: {e}", exc_info=True)
        update_balance(user.id, bet_amount) # Refund bet
        await query.edit_message_text(f"Ошибка раздачи ({e}). Ставка возвращена.")
        if chat_id in games: del games[chat_id]
        return

    # Check for blackjacks
    p_val, d_val = get_hand_value(player_hand), get_hand_value(dealer_hand)
    p_bj = (p_val == 21 and len(player_hand) == 2)
    d_bj = (d_val == 21 and len(dealer_hand) == 2)
    outcome, state, p_status = None, 'player_turn', 'active'

    if p_bj:
        p_status = 'blackjack'
        if d_bj: outcome = f"⚖️ Ничья! БЖ у обоих. Ставка {bet_amount} F возвращена."; update_balance(user.id, bet_amount); state = 'game_over'
        else: w = bet_amount * BLACKJACK_PAYOUT; total = bet_amount + w; update_balance(user.id, total); outcome = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {w:.2f} F!"; state = 'game_over'
    elif d_bj: outcome = f"😥 У дилера Блекджек! Ставка {bet_amount} F проиграна."; state = 'game_over'

    game.update({
        'state': state, 'deck': deck, 'cards_dealt': dealt_count,
        'player_hands': [{'hand': player_hand, 'bet': bet_amount, 'status': p_status, 'can_double': (not p_bj and not d_bj), 'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': dealer_hand,
        'initial_bet': bet_amount, 'split_count': 0, 'outcome_text': outcome
    })

    await blackjack_show_state(context, chat_id, query.message.message_id)
    try: await query.answer(f"Ставка {bet_amount} F принята!")
    except BadRequest: pass

async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Displays the current Blackjack game state using MarkdownV1."""
    games = context.bot_data.get('games', {})
    if chat_id not in games: return
    game = games[chat_id]
    player_id = game.get('player_id')
    if not player_id: return

    balance = get_balance(player_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    d_hand = game.get('dealer_hand', [])
    p_hands = game.get('player_hands', [])
    cur_idx = game.get('current_hand_index', -1)
    status = game.get('state', 'unknown')

    hide_dealer = (status == 'player_turn') and not (get_hand_value(d_hand) == 21 and len(d_hand) == 2)

    # --- Build Text (Markdown V1) ---
    text = f"*Блекджек* | Баланс: {balance_str} F\n"
    total_bet = sum(h.get('bet', 0) for h in p_hands if isinstance(h, dict))
    num_h = len(p_hands)
    text += f"Общая ставка: {total_bet} F{' ({num_h} руки)' * (num_h > 1)}\n"
    text += "--------------------\n"

    # Dealer
    d_val = get_hand_value(d_hand)
    d_val_str = "??" if not d_hand else (str(d_val) if not hide_dealer else f"{get_card_value(d_hand[0])}+?")
    text += f"*Дилер:* {format_hand(d_hand, hide_one=hide_dealer)} ({d_val_str})\n\n"

    # Player
    text += "*Вы:*\n"
    active_hand_data = None
    for i, h_data in enumerate(p_hands):
        if not isinstance(h_data, dict): continue
        hand, h_val, h_stat, h_bet = h_data.get('hand', []), get_hand_value(h_data.get('hand', [])), h_data.get('status', '?'), h_data.get('bet', 0)
        is_cur = (i == cur_idx and h_stat == 'active' and status == 'player_turn')
        ind = "▶️" if is_cur else ("✅" if h_stat == 'stand' else ("❌" if h_stat == 'bust' else ("💰" if h_stat == 'blackjack' else "▫️")))
        text += f"{ind} Рука {i+1}: {format_hand(hand)} ({h_val}) [{h_bet} F]"
        if h_stat == 'bust': text += " - *Перебор!*"
        elif h_stat == 'blackjack': text += " - *Блекджек!*"
        elif h_stat == 'stand' and not is_cur: text += " - *Стоп*"
        text += "\n"
        if is_cur: active_hand_data = h_data

    # --- Build Buttons ---
    keyboard = []
    if active_hand_data and status == 'player_turn':
        p_hand = active_hand_data.get('hand', []); p_bet = active_hand_data.get('bet', 0)
        can_double = (active_hand_data.get('can_double', False) and len(p_hand) == 2 and balance is not None and balance >= p_bet)
        can_split = (len(p_hand) == 2 and p_hand[0] and p_hand[1] and get_card_value(p_hand[0]) == get_card_value(p_hand[1]) and balance is not None and balance >= p_bet and game.get('split_count', 0) < MAX_SPLITS)
        active_hand_data['can_split'] = can_split # Update state for action handler

        keyboard.append([InlineKeyboardButton("Еще", callback_data=f"bj_hit_{cur_idx}"), InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{cur_idx}")])
        specials = []
        if can_double: specials.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{cur_idx}"))
        if can_split: specials.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{cur_idx}"))
        if specials: keyboard.append(specials)

    elif status == 'game_over':
        text += "\n*Игра завершена!* 🎉\n" + game.get('outcome_text', "") + "\n"
        fin_bal = get_balance(player_id)
        text += f"\nИтоговый баланс: {fin_bal:.2f} фишек." if fin_bal is not None else ""
        keyboard.append([InlineKeyboardButton("🔄 Новая Игра", callback_data="bj_new_game")])

    elif status == 'dealer_turn':
        text += "\n*Ход дилера...*"

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Edit Message ---
    try:
        await context.bot.edit_message_text(chat_id, message_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.warning(f"Failed to edit BJ state msg {message_id} in {chat_id}: {e}")
             # Handle "not found" by sending a new message if possible
             if "message to edit not found" in str(e).lower() and status != 'game_over':
                  try:
                      new_msg = await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
                      games[chat_id]['message_id'] = new_msg.message_id # Update message ID!
                  except Exception as send_e: logger.error(f"Failed to send new BJ state msg: {send_e}")
    except Exception as e:
        logger.error(f"Error showing BJ state for {chat_id}: {e}", exc_info=True)

async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action_parts: list):
    """Handles player actions like hit, stand, double, split."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    games = context.bot_data.get('games', {})

    # Extract action and index
    if len(action_parts) != 2: logger.warning(f"Invalid BJ action parts: {action_parts}"); return
    action, hand_idx_str = action_parts[0], action_parts[1]
    try: hand_idx = int(hand_idx_str)
    except ValueError: logger.warning(f"Invalid BJ hand index: {hand_idx_str}"); return

    logger.info(f"BJ action '{action}' hand {hand_idx} from {user.id} in {chat_id}")

    if chat_id not in games: return # Game ended or not found
    game = games[chat_id]
    if game.get('player_id') != user.id: return # Not player's game
    if game.get('state') != 'player_turn': return # Not player's turn
    p_hands = game.get('player_hands', [])
    if not (0 <= hand_idx < len(p_hands)): return # Invalid index
    if hand_idx != game.get('current_hand_index'): await query.answer("Ход другой руки.", show_alert=False); return

    h_data = p_hands[hand_idx]
    if not isinstance(h_data, dict) or h_data.get('status') != 'active': await query.answer("Действие неактуально.", show_alert=False); return

    # Action data
    hand = h_data.get('hand', []); deck = game.get('deck', [])
    balance = get_balance(user.id); bet = h_data.get('bet', 0)
    dealt = game.get('cards_dealt', 0)

    needs_update = False # Flag to update message at the end
    try:
        if action == 'hit':
            card, status_code = _fetch_and_update_source(deck, dealt, NUM_DECKS)
            if status_code == 0 and card:
                hand.append(card); game['cards_dealt'] += 1
                h_data['can_double'] = False; h_data['can_split'] = False
                h_val = get_hand_value(hand)
                await query.answer(f"Карта: {card[0]}{card[1]}")
                if h_val > 21: h_data['status'] = 'bust'; logger.info(f"BJ {user.id} hand {hand_idx} BUST ({h_val})"); await blackjack_next_action(context, chat_id)
                elif h_val == 21: h_data['status'] = 'stand'; logger.info(f"BJ {user.id} hand {hand_idx} STAND (21)"); await blackjack_next_action(context, chat_id)
                else: needs_update = True
            else: raise IndexError("Failed to draw card") # Trigger catch block

        elif action == 'stand':
            h_data['status'] = 'stand'; await query.answer("Стоп.")
            await blackjack_next_action(context, chat_id)

        elif action == 'double':
            can_double = (h_data.get('can_double', False) and len(hand) == 2 and balance is not None and balance >= bet)
            if can_double:
                if update_balance(user.id, -bet) is not None:
                    h_data['bet'] += bet; h_data['can_double'] = False; h_data['can_split'] = False
                    card, status_code = _fetch_and_update_source(deck, game['cards_dealt'], NUM_DECKS)
                    if status_code == 0 and card:
                        hand.append(card); game['cards_dealt'] += 1
                        h_val = get_hand_value(hand)
                        h_data['status'] = 'bust' if h_val > 21 else 'stand'
                        await query.answer(f"Удвоено! Карта: {card[0]}{card[1]}. Итог: {h_val}{' (Перебор!)' * (h_val > 21)}")
                    else: # Failed draw after doubling bet - stand
                        h_data['status'] = 'stand'
                        await query.answer("Удвоено! Не удалось взять карту. Рука 'Стоп'.", show_alert=True)
                    await blackjack_next_action(context, chat_id)
                else: await query.answer("Ошибка баланса.", show_alert=True)
            else: await query.answer("Удвоение невозможно.", show_alert=True)

        elif action == 'split':
             can_split = h_data.get('can_split', False) # Check pre-calculated flag
             if can_split:
                 if balance is None or balance < bet: await query.answer("Недостаточно средств.", show_alert=True); return
                 if update_balance(user.id, -bet) is not None:
                     game['split_count'] += 1; card_moved = hand.pop()
                     new_h_data = {'hand': [card_moved], 'bet': bet, 'status': 'active', 'can_double': False, 'can_split': False}
                     p_hands.insert(hand_idx + 1, new_h_data)

                     # Deal one card to each new hand
                     drawn_cards = []
                     for _ in range(2):
                         c, sc = _fetch_and_update_source(deck, game['cards_dealt'], NUM_DECKS)
                         if sc == 0 and c: game['cards_dealt'] += 1; drawn_cards.append(c)
                         else: drawn_cards.append(None); logger.warning(f"Failed draw during split {chat_id}")

                     if drawn_cards[0]: hand.append(drawn_cards[0])
                     if drawn_cards[1]: new_h_data['hand'].append(drawn_cards[1])

                     is_ace = get_card_value(hand[0]) == 11
                     if is_ace: # Aces split rule: stand immediately
                         h_data['status'] = 'stand'; new_h_data['status'] = 'stand'
                         h_data['can_double'] = False; new_h_data['can_double'] = False
                         await query.answer("Тузы разделены.")
                         await blackjack_next_action(context, chat_id) # Move to next action/dealer
                     else: # Non-ace split
                         h_data['can_double'] = (len(hand) == 2)
                         new_h_data['can_double'] = (len(new_h_data['hand']) == 2)
                         # Check for re-split possibility
                         limit_ok = game['split_count'] < MAX_SPLITS
                         h_data['can_split'] = (len(hand) == 2 and hand[0] and hand[1] and get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok)
                         new_h_data['can_split'] = (len(new_h_data['hand']) == 2 and new_h_data['hand'][0] and new_h_data['hand'][1] and get_card_value(new_h_data['hand'][0]) == get_card_value(new_h_data['hand'][1]) and limit_ok)
                         # Check for 21 (stand)
                         if get_hand_value(hand) == 21: h_data['status'] = 'stand'
                         if get_hand_value(new_h_data['hand']) == 21: new_h_data['status'] = 'stand'
                         await query.answer("Рука разделена!")
                         needs_update = True # Stay on current hand, update state
                 else: await query.answer("Ошибка баланса.", show_alert=True)
             else: await query.answer("Разделение невозможно.", show_alert=True)

        if needs_update:
            await blackjack_show_state(context, chat_id, query.message.message_id)

    except IndexError: # Triggered if _fetch_and_update_source returns error status
         logger.warning(f"BJ action '{action}' failed for {user.id} in {chat_id} - Deck empty?")
         h_data['status'] = 'stand' # Treat as stand if card cannot be drawn
         await query.answer("Не удалось взять карту (колода?). Рука 'Стоп'.", show_alert=True)
         await blackjack_next_action(context, chat_id)
    except Exception as e:
         logger.error(f"Error handling BJ action '{action}' for {user.id}: {e}", exc_info=True)
         try: await query.answer("Ошибка обработки хода.", show_alert=True)
         except: pass

async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Moves to the next player hand or starts the dealer's turn."""
    games = context.bot_data.get('games', {})
    if chat_id not in games: return
    game = games[chat_id]
    if game.get('state') != 'player_turn': return # Should not happen

    p_hands = game.get('player_hands', [])
    cur_idx = game.get('current_hand_index', -1)
    next_idx = cur_idx + 1
    # Find next active hand
    while next_idx < len(p_hands) and (not isinstance(p_hands[next_idx], dict) or p_hands[next_idx].get('status') != 'active'):
        next_idx += 1

    if next_idx < len(p_hands): # Found next active hand
        game['current_hand_index'] = next_idx
        logger.info(f"BJ {chat_id}: Moving to hand {next_idx}")
        await blackjack_show_state(context, chat_id, game.get('message_id'))
    else: # No more active player hands, start dealer turn
        game['state'] = 'dealer_turn'
        logger.info(f"BJ {chat_id}: Player turn finished. Starting dealer turn.")
        await blackjack_show_state(context, chat_id, game.get('message_id')) # Show "Dealer's turn..."
        context.job_queue.run_once(blackjack_dealer_turn_job, DEALER_TURN_DELAY, data=chat_id, name=f"dealer_{chat_id}")

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Handles the dealer's turn (Job Queue callback)."""
    chat_id = context.job.data
    games = context.bot_data.get('games', {})
    logger.info(f"BJ Dealer job started for {chat_id}")

    if chat_id not in games: logger.warning(f"BJ Dealer job {chat_id}: Game not found."); return
    game = games[chat_id]
    if game.get('state') != 'dealer_turn': logger.warning(f"BJ Dealer job {chat_id}: State is {game['state']}."); return

    deck, d_hand, p_hands = game.get('deck', []), game.get('dealer_hand', []), game.get('player_hands', [])
    dealt = game.get('cards_dealt', 0)

    # Skip dealer turn if player has no chance to win/push
    can_win = any(isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] for h in p_hands)
    d_bj = (get_hand_value(d_hand) == 21 and len(d_hand) == 2)

    if not can_win and not d_bj:
        logger.info(f"BJ Dealer turn skipped {chat_id} (player cannot win/push).")
        await blackjack_determine_outcome(context, chat_id, d_bj); return

    # Dealer hits based on rules
    while True:
        d_val = get_hand_value(d_hand)
        aces = sum(1 for c in d_hand if c and c[0] == 'A')
        is_soft = aces > 0 and (d_val - aces * 11) < 11
        if d_val > 17 or (d_val == 17 and not (is_soft and DEALER_HITS_SOFT_17)):
            break # Stand condition met

        logger.debug(f"BJ Dealer {chat_id} hits on {d_val}{' (soft)'*is_soft}")
        try:
            card, sc = _fetch_and_update_source(deck, dealt, NUM_DECKS)
            if sc == 0 and card:
                d_hand.append(card); game['cards_dealt'] += 1; dealt = game['cards_dealt']
                # Optional: Update state more frequently for visual effect
                # await blackjack_show_state(context, chat_id, game['message_id'])
                # await asyncio.sleep(DEALER_TURN_DELAY * 1.2)
            else: raise IndexError("Dealer failed to draw")
        except IndexError:
            logger.warning(f"BJ Dealer {chat_id} failed to draw (deck empty?). Standing.")
            break # Stop if deck is empty

    logger.info(f"BJ Dealer {chat_id} stands on {get_hand_value(d_hand)} ({format_hand(d_hand)})")
    await blackjack_determine_outcome(context, chat_id, d_bj)

async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_bj: bool):
    """Determines the outcome for each hand and updates balance."""
    games = context.bot_data.get('games', {})
    if chat_id not in games: return
    game = games[chat_id]
    logger.info(f"BJ Determining outcome for {chat_id}")

    if game.get('state') == 'game_over' and 'outcome_determined' in game:
        await blackjack_show_state(context, chat_id, game['message_id']); return

    player_id = game.get('player_id'); p_hands = game.get('player_hands', []); d_hand = game.get('dealer_hand', [])
    if not player_id: return

    d_val = get_hand_value(d_hand); d_bust = d_val > 21
    logger.info(f"BJ Dealer final {chat_id}: {format_hand(d_hand)} ({d_val}) Bust: {d_bust}")

    outcomes, total_winnings, total_bet = [], 0, 0
    for i, h_data in enumerate(p_hands):
        if not isinstance(h_data, dict): continue
        hand, bet, status = h_data.get('hand', []), h_data.get('bet', 0), h_data.get('status')
        p_val = get_hand_value(hand); p_bj = (status == 'blackjack')
        total_bet += bet; multiplier = 0; outcome = ""
        prefix = f"Рука {i+1}: " if len(p_hands) > 1 else ""

        if status == 'bust': multiplier = 0; outcome = f"{prefix}Перебор ({p_val}). Ставка {bet} F проиграна."
        elif p_bj: # BJ already handled on deal, this is just for display consistency
             if dealer_had_bj: multiplier = 1; outcome = f"{prefix}Блекджек! Но у дилера тоже. Ничья."
             else: multiplier = 1 + BLACKJACK_PAYOUT; outcome = f"{prefix}Блекджек! Выигрыш {(bet*BLACKJACK_PAYOUT):.2f} F."
        elif dealer_had_bj: multiplier = 0; outcome = f"{prefix}У дилера Блекджек. Ставка {bet} F проиграна."
        elif d_bust: multiplier = 2; outcome = f"{prefix}Дилер перебор ({d_val})! Выигрыш {bet} F."
        elif p_val > d_val: multiplier = 2; outcome = f"{prefix}{p_val} > {d_val}. Выигрыш {bet} F."
        elif p_val == d_val: multiplier = 1; outcome = f"{prefix}{p_val} = {d_val}. Ничья."
        else: multiplier = 0; outcome = f"{prefix}{p_val} < {d_val}. Ставка {bet} F проиграна."

        outcomes.append(outcome); total_winnings += bet * multiplier

    net_change = total_winnings - total_bet
    logger.info(f"BJ Outcome {player_id}: Bet={total_bet}, Won={total_winnings}, Net={net_change:+.2f}")
    if total_winnings > 0:
        if update_balance(player_id, total_winnings) is None:
            logger.error(f"CRITICAL: Failed to update balance for {player_id} after BJ win!")
            outcomes.append("\n*ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША!*")
            net_change = -total_bet # Assume winnings were not paid

    game['state'] = 'game_over'
    game['outcome_text'] = "\n".join(outcomes) + f"\n\n*Общий итог: {net_change:+.2f} F*"
    game['outcome_determined'] = True
    await blackjack_show_state(context, chat_id, game.get('message_id'))

# --- General Handlers ---

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all inline button presses."""
    query = update.callback_query
    data = query.data
    user = query.from_user
    if not data: return # Ignore empty callbacks

    logger.debug(f"Callback: '{data}' from {user.id}")

    # Route based on prefix
    parts = data.split("_", 2) # Split max 2 times: prefix_action_payload
    prefix = parts[0]
    action_data = parts[1:] if len(parts) > 1 else []

    if prefix == "bj": # Blackjack actions
        action_name = action_data[0] if action_data else None
        payload = action_data[1] if len(action_data) > 1 else None

        if action_name == "bet":
            try: await blackjack_handle_bet(update, context, int(payload))
            except (ValueError, TypeError, IndexError): logger.warning(f"Invalid BJ bet payload: {payload}")
            except Exception as e: logger.error(f"Error handling BJ bet: {e}", exc_info=True)
        elif action_name == "new":
            await blackjack_start_command(update, context) # Handles "new_game"
        elif action_name in ["hit", "stand", "double", "split"]:
            # Pass action name and hand index string
            await blackjack_handle_action(update, context, [action_name, payload])
        else:
             logger.warning(f"Unknown Blackjack action: {action_name}")
             try: await query.answer()
             except: pass # Ignore if query too old

    # --- Placeholder for other games ---
    # elif prefix == "pk": # Poker actions
    #    await poker_callback_handler(update, context, action_data)
    # elif prefix == "rl": # Roulette actions
    #    await roulette_callback_handler(update, context, action_data)
    # ------------------------------------

    else:
        logger.warning(f"Unknown callback prefix: {prefix}")
        try: await query.answer()
        except: pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors and handles specific ones like Conflict."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error detected! Multiple bot instances running?")
    elif isinstance(context.error, BadRequest):
         logger.warning(f"BadRequest Error: {context.error}. Update: {update}")
    # Add more specific error handling if needed

# --- Main Bot Setup ---
def main():
    """Starts the bot."""
    logger.info("Starting bot...")
    start_keep_alive()

    try:
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        # Initialize bot_data stores
        application.bot_data.setdefault('games', {})
        application.bot_data.setdefault('user_cache', {})

        # Register handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))
        application.add_handler(CommandHandler("blackjack", blackjack_start_command))
        application.add_handler(CallbackQueryHandler(button_callback_handler))
        application.add_error_handler(error_handler)

        logger.info("Bot handlers registered. Starting polling...")
        print("Bot is running. Press Ctrl+C to stop.")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Exception as e:
         logger.critical(f"Critical error during bot setup or runtime: {e}", exc_info=True)
    finally:
        print("Bot stopped.")
        logger.info("Bot stopped.")

if __name__ == "__main__":
    main()