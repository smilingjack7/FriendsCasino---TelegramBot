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
from html import escape as html_escape
import uuid # Needed for unique bet IDs potentially

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, JobQueue
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Conflict

import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# --- Constants and Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("BOT_TOKEN environment variable not set")
if not DATABASE_URL: raise ValueError("DATABASE_URL environment variable not set")

# --- General Bot Settings ---
LEADERBOARD_LIMIT = 10

# --- Blackjack Constants ---
INITIAL_BALANCE = 100.0 # For new users
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
NUM_DECKS = 8
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5 # Payout for Blackjack (usually 3:2)
MAX_SPLITS = 3         # Max times a hand can be split
DEALER_TURN_DELAY = 0.7 # Delay for dealer actions (seconds)
BJ_GAME_KEY = 'blackjack_game'

# --- Roulette Constants ---
ROULETTE_TIMER_SECONDS = 60 # Betting time in seconds
ROULETTE_MAX_BETS_PER_PLAYER = 5
ROULETTE_SPIN_ANIMATION_DURATION = 3.5 # Seconds for simple animation
ROULETTE_CLEANUP_DELAY = 60 # Seconds after result before cleaning game data

# American Roulette Layout: 0, 00, 1-36
ROULETTE_NUMBER_00 = 37 # Internal representation for 00
ROULETTE_NUMBERS = list(range(37)) + [ROULETTE_NUMBER_00] # Numbers 0-36 plus our 00 marker (37)

ROULETTE_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_BLACK = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}
# 0 and 00 (37) are green

# Dozens
ROULETTE_DOZEN_1 = set(range(1, 13)) # 1-12
ROULETTE_DOZEN_2 = set(range(13, 25)) # 13-24
ROULETTE_DOZEN_3 = set(range(25, 37)) # 25-36

# Bet types and payouts (multiplier INCLUDES original bet back, e.g., 2 means 1:1 payout)
ROULETTE_PAYOUTS = {
    "number": 36,  # Straight up (Pays 35:1)
    "red": 2,      # Pays 1:1
    "black": 2,    # Pays 1:1
    "even": 2,     # Pays 1:1 (Loses on 0, 00)
    "odd": 2,      # Pays 1:1 (Loses on 0, 00)
    "low": 2,      # 1-18 (Pays 1:1, Loses on 0, 00)
    "high": 2,     # 19-36 (Pays 1:1, Loses on 0, 00)
    "dozen1": 3,   # First Dozen (Pays 2:1)
    "dozen2": 3,   # Second Dozen (Pays 2:1)
    "dozen3": 3,   # Third Dozen (Pays 2:1)
}
ROULETTE_BET_TYPES_DESC = {
    "number": "Число",
    "red": "Красное🔴",
    "black": "Черное⚫",
    "even": "Четное",
    "odd": "Нечетное",
    "low": "Малые (1-18)",
    "high": "Большие (19-36)",
    "dozen1": "Дюжина 1 (1-12)",
    "dozen2": "Дюжина 2 (13-24)",
    "dozen3": "Дюжина 3 (25-36)",
}
# Key for chat_data
ROULETTE_GAME_KEY = 'roulette_game'


# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logging.getLogger("apscheduler").setLevel(logging.WARNING) # Reduce scheduler noise
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive ---
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
                data = cur.fetchone() or {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        if data and data.get('last_bonus') and not isinstance(data['last_bonus'], datetime.datetime):
            data['last_bonus'] = None
        return data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}")
        return None

def update_balance(user_id: int, change: float) -> float | None:
    # Prevent tiny fractional balances from causing issues
    change = round(change, 2)
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            # Ensure balance doesn't go below zero if needed by checking before update (more complex)
            # Or handle potential negative balance after update
            cur.execute(sql, (change, user_id,))
            res = cur.fetchone()
            if res:
                new_balance = round(res[0], 2)
                logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                return new_balance
            else:
                logger.warning(f"Update balance failed for user {user_id} (user not found or other issue)")
                return None
    except Exception as e:
        if isinstance(e, psycopg2.Error):
             logger.error(f"DB Error (update_balance) for {user_id}: {e.pgcode} - {e.pgerror}", exc_info=False)
        else:
             logger.error(f"DB Error (update_balance) for {user_id}: {e}", exc_info=True)
        return None

def get_balance(user_id: int) -> float | None:
    user_data = get_or_create_user(user_id)
    # Return None if balance is missing or user_data is None
    balance = user_data.get('balance') if user_data else None
    return round(balance, 2) if balance is not None else None


def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    ts_naive = ts_utc.replace(tzinfo=None)
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts_naive, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {ts_naive}")
    except Exception as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None
    if last_bonus and not isinstance(last_bonus, datetime.datetime):
         logger.warning(f"Invalid last_bonus type for user {user_id}: {type(last_bonus)}. Resetting.")
         return None
    # Optional: Make timezone-aware if DB stores naive but represents UTC
    # if last_bonus: return last_bonus.replace(tzinfo=datetime.timezone.utc)
    return last_bonus


def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            # Round balance for display consistency
            leaders = cur.fetchall()
            for leader in leaders:
                 if 'balance' in leader and leader['balance'] is not None:
                      leader['balance'] = round(leader['balance'], 2)
            return leaders
    except Exception as e:
        logger.error(f"DB Error (get_leaderboard): {e}")
        return []

# --- Blackjack Game Utilities ---
def create_deck(num=NUM_DECKS)->list:
    deck = [(r, s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card: tuple | None) -> int:
    return RANK_VALUES.get(card[0], 0) if card and card[0] in RANK_VALUES else 0

def get_hand_value(hand: list) -> int:
    value = sum(get_card_value(card) for card in hand if card)
    num_aces = sum(1 for card in hand if card and card[0] == 'A')
    while value > 21 and num_aces > 0:
        value -= 10
        num_aces -= 1
    return value

def format_hand(hand: list, hide_one: bool = False) -> str:
    if not hand: return "Пусто"
    if hide_one and len(hand) > 1:
        first_card = f"{hand[0][0]}{hand[0][1]}" if hand[0] else "??"
        return f"[{first_card}, ??]"
    return ", ".join([f"{card[0]}{card[1]}" for card in hand if card])

def draw_card(deck: list) -> tuple | None:
    if not deck:
        logger.warning("Attempted to draw from an empty deck.")
        return None
    try:
        return deck.pop(random.randrange(len(deck)))
    except (ValueError, IndexError) as e:
        logger.error(f"Error drawing card: {e}")
        return None

# --- Roulette Helper Functions ---
def display_roulette_number(number: int) -> str:
    """Returns the display string for a number (handles 00)."""
    if number == ROULETTE_NUMBER_00:
        return "00"
    return str(number)

def get_roulette_color(number: int) -> str | None:
    """Gets color based on internal number representation."""
    if number == 0 or number == ROULETTE_NUMBER_00: return "green"
    if number in ROULETTE_RED: return "red"
    if number in ROULETTE_BLACK: return "black"
    logger.warning(f"Could not determine color for invalid roulette number: {number}")
    return None

def get_roulette_color_emoji(number: int) -> str:
    """Gets emoji based on internal number representation."""
    zwsp = "\u200B" # Zero Width Space for potential alignment
    if number == 0: return f"{zwsp}0️⃣{zwsp}"
    if number == ROULETTE_NUMBER_00: return f"{zwsp}0️⃣0️⃣{zwsp}" # Or find a better representation
    color = get_roulette_color(number)
    if color == "red": return "🔴"
    if color == "black": return "⚫"
    return ""

def format_roulette_bet(bet: dict) -> str:
    """Formats a single roulette bet for display"""
    desc = ROULETTE_BET_TYPES_DESC.get(bet['type'], bet['type'].capitalize())
    value_str = f" {display_roulette_number(bet['value'])}" if bet['type'] == 'number' and bet.get('value') is not None else ""
    amount_str = f"{bet['amount']:.0f}" if bet['amount'] == int(bet['amount']) else f"{bet['amount']:.2f}"
    return f"{html_escape(desc)}{value_str} ({amount_str} F)"

# --- Helper to get User Mention (HTML with Caching) ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600 # 1 hour

async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """Gets HTML mention for a user, using a cache."""
    now = time.monotonic()
    async with _cache_lock:
        cached = _user_mention_cache.get(user_id)
        if cached and (now - cached['ts']) < _cache_ttl:
            return cached['mention']

    try:
        user_chat = await context.bot.get_chat(user_id)
        mention = user_chat.mention_html() if user_chat.can_be_mentioned else html_escape(user_chat.first_name or f"User {user_id}")
    except BadRequest as e:
         mention = f"User {user_id}"
         if "chat not found" not in str(e).lower():
             logger.warning(f"Failed to get mention for {user_id} due to BadRequest: {e}")
    except Exception as e:
        mention = f"User {user_id}"
        logger.warning(f"Failed to get mention for {user_id}: {e}")

    async with _cache_lock:
        _user_mention_cache[user_id] = {'mention': mention, 'ts': now}
    return mention


# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {html_escape(user.first_name)}! 👋\n"
        f"Ваш баланс: <b>{balance_str}</b> фишек.\n\n"
        f"Для игры в Блекджек (в личке): /blackjack\n"
        f"Для игры в Рулетку (в группе): /roulette\n"
        f"Справка по командам: /help",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/help command from user {user.id}")
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>Личный Чат:</b>\n"
        "/start - Начало работы / Баланс\n"
        "/blackjack - Начать игру в Блекджек\n"
        "/balance - Показать текущий баланс\n"
        "/bonus - Получить бонус (раз в 6 часов)\n\n"
        "<b>Групповой Чат:</b>\n"
        "/roulette - Начать игру в Рулетку\n\n"
        "<b>Везде:</b>\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение\n\n"
        "<i>Играйте ответственно!</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/balance command from user {user.id}")
    balance = get_balance(user.id)
    if balance is not None:
        await update.message.reply_text(f"Ваш текущий баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/bonus command from user {user.id}")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Получить бонус можно только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return

    if not get_or_create_user(user.id):
        await update.message.reply_text("Ошибка: Не удалось найти или создать ваш профиль.")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_db = get_last_bonus_time(user.id)
    last_bonus_utc = last_bonus_db.replace(tzinfo=datetime.timezone.utc) if last_bonus_db else None

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

    new_balance = update_balance(user.id, BONUS_AMOUNT)
    if new_balance is not None:
        update_last_bonus_time(user.id, now_utc)
        await update.message.reply_text(
            f"🎉 Поздравляем! Вы получили бонус <b>+{BONUS_AMOUNT:.2f}</b> фишек!\n"
            f"Ваш новый баланс: <b>{new_balance:.2f}</b> фишек.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ Ошибка при начислении бонуса. Пожалуйста, попробуйте позже.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/leaderboard command")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("🏆 Таблица лидеров пока пуста.")
        return

    leaderboard_text = f"🏆 <b>Таблица Лидеров (Топ {LEADERBOARD_LIMIT})</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]
    mentions = await asyncio.gather(*(get_user_mention(context, leader['user_id']) for leader in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i + 1}.</b>"
        name = mentions[i] if i < len(mentions) else f"User {leader['user_id']}"
        balance_str = f"{leader['balance']:.2f}" if leader.get('balance') is not None else "N/A"
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"

    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        await update.message.reply_text("Не удалось отобразить таблицу лидеров.")

# --- Blackjack Game Logic (Private Chat Only) ---

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

    if is_callback:
        await update.callback_query.answer()

    user_game = context.user_data.get(BJ_GAME_KEY, {})
    previous_message_id = user_game.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(chat.id, previous_message_id)
            logger.debug(f"Deleted previous BJ message {previous_message_id} for user {user.id}")
        except Exception as e:
            logger.debug(f"Failed to delete old BJ message {previous_message_id}: {e}")

    context.user_data.pop(BJ_GAME_KEY, None)

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
    if row: buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)
    text = f"Ваш баланс: <b>{balance:.2f}</b> F.\nВыберите вашу ставку:"

    try:
        if callback_message_id:
            try: await context.bot.delete_message(chat.id, callback_message_id)
            except Exception: pass # Ignore if deletion fails

        sent_message = await context.bot.send_message(
            chat_id=chat.id, text=text, reply_markup=markup, parse_mode=ParseMode.HTML
        )
        context.user_data[BJ_GAME_KEY] = {'state': 'waiting_bet', 'message_id': sent_message.message_id}
        logger.info(f"BJ bet prompt sent (msg {sent_message.message_id}) for user {user.id}")
    except Exception as e:
        logger.error(f"BJ start error sending bet prompt for user {user.id}: {e}", exc_info=True)
        # Attempt to send error reply to the original message/chat
        try: await source_message.reply_text("❌ Произошла ошибка при начале игры.")
        except Exception: logger.error(f"Failed to send error reply for BJ start user {user.id}")


async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})
    bet_prompt_message_id = q.message.message_id

    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id:
        await q.answer("Эта игра больше неактивна.", show_alert=False)
        return

    balance = get_balance(uid)
    if balance is None: await q.answer("Ошибка получения баланса.", show_alert=True); return
    if bet <= 0 or bet > balance: await q.answer(f"Недопустимая ставка/недостаточно средств ({balance:.2f} F).", show_alert=True); return

    if update_balance(uid, -bet) is None:
        await q.answer("Ошибка при списании ставки.", show_alert=True)
        return

    deck = create_deck()
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0

    try:
        for _ in range(2):
            card_p = draw_card(deck); card_d = draw_card(deck)
            if not card_p or not card_d: raise IndexError("Deck empty during deal")
            player_hand.append(card_p); dealer_hand.append(card_d)
            cards_dealt_count += 2
    except IndexError as e:
        logger.error(f"BJ dealing error for user {uid}: {e}")
        update_balance(uid, bet) # Refund bet
        try: await q.edit_message_text(f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.")
        except Exception: pass
        context.user_data.pop(BJ_GAME_KEY, None)
        return
    except Exception as e:
        logger.error(f"BJ unexpected dealing error for user {uid}: {e}", exc_info=True)
        update_balance(uid, bet) # Refund bet
        try: await q.edit_message_text(f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.")
        except Exception: pass
        context.user_data.pop(BJ_GAME_KEY, None)
        return

    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    game_state = 'player_turn'
    hand_status = 'active'
    outcome_text = None

    if player_has_blackjack:
        hand_status = 'blackjack'; game_state = 'game_over'
        if dealer_has_blackjack:
            outcome_text = "⚖️ Ничья! У обоих Блекджек."; update_balance(uid, bet)
        else:
            winnings = bet * BLACKJACK_PAYOUT; update_balance(uid, bet + winnings)
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {winnings:.2f} F!"
    elif dealer_has_blackjack:
        game_state = 'game_over'; outcome_text = "😥 У дилера Блекджек! Вы проиграли."

    game.update({
        'state': game_state, 'deck': deck, 'cards_dealt': cards_dealt_count,
        'player_hands': [{'hand': player_hand, 'bet': bet, 'status': hand_status,
                          'can_double': (game_state == 'player_turn' and len(player_hand) == 2),
                          'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': dealer_hand, 'initial_bet': bet,
        'split_count': 0, 'outcome_text': outcome_text,
        'outcome_determined': (game_state == 'game_over')
    })

    try: await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e: logger.warning(f"Could not delete bet prompt message {bet_prompt_message_id}: {e}")

    new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=False)

    if new_message_info and isinstance(new_message_info, Message):
        game['message_id'] = new_message_info.message_id
        logger.info(f"BJ initial state sent (msg {new_message_info.message_id}) for user {uid}. State: {game_state}")
    elif not new_message_info:
        logger.error(f"Failed to send initial BJ state for user {uid}")
        update_balance(uid, bet) # Refund bet
        context.user_data.pop(BJ_GAME_KEY, None)
        await context.bot.send_message(chat_id, "❌ Ошибка отображения игры. Ставка возвращена.")
        return

    await q.answer(f"Ставка принята: {bet} F")


async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, edit_existing: bool = True) -> Message | int | None:
    if game_state is None: game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state: logger.warning(f"blackjack_show_state called for user {user_id} but no game state found."); return None

    message_id_to_process = game_state.get('message_id')
    if edit_existing and not message_id_to_process: logger.error(f"BJ show_state: Attempted to edit but no message_id for user {user_id}"); return None

    balance = get_balance(user_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands_data = game_state.get('player_hands', [])
    current_hand_idx = game_state.get('current_hand_index', -1)
    state = game_state.get('state', 'unknown')
    dealer_value = get_hand_value(dealer_hand)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)
    hide_dealer_card = (state == 'player_turn' and not dealer_has_blackjack)

    text = f"<b>Блекджек</b> | Баланс: <b>{balance_str}</b> F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands_data if isinstance(h, dict))
    num_hands = len(player_hands_data)
    text += f"Общая ставка: <b>{total_bet}</b> F{' (Рук: ' + str(num_hands) + ')' if num_hands > 1 else ''}\n"
    text += "--------------------\n"

    dealer_value_display = "??" if not dealer_hand else (str(dealer_value) if not hide_dealer_card else f"{get_card_value(dealer_hand[0])}+?")
    text += f"<b>Диллер:</b> {format_hand(dealer_hand, hide_one=hide_dealer_card)} ({dealer_value_display})\n\n"

    text += "<b>Вы:</b>\n"
    active_hand_data = None
    for i, hand_data in enumerate(player_hands_data):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', '?')
        hand_bet = hand_data.get('bet', 0)
        is_current_turn = (i == current_hand_idx and hand_status == 'active' and state == 'player_turn')

        indicator = "▶️" if is_current_turn else "✅" if hand_status == 'stand' else "❌" if hand_status == 'bust' else "💰" if hand_status == 'blackjack' else "▫️"
        text += f"{indicator} Рука {i+1}: {format_hand(hand)} (<b>{hand_value}</b>) [<i>{hand_bet:.0f} F</i>]" # Use .0f for integer display

        status_label = ""
        if hand_status == 'bust': status_label = " - <b>Перебор!</b>"
        elif hand_status == 'blackjack': status_label = " - <b>Блекджек!</b>"
        elif hand_status == 'stand' and not is_current_turn: status_label = " - <i>Стоп</i>"
        text += status_label + "\n"
        if is_current_turn: active_hand_data = hand_data

    keyboard = []
    if active_hand_data and state == 'player_turn':
        player_hand = active_hand_data.get('hand', [])
        player_bet = active_hand_data.get('bet', 0)
        can_double = (active_hand_data.get('can_double', False) and len(player_hand) == 2 and balance is not None and balance >= player_bet)
        can_split = (len(player_hand) == 2 and player_hand[0] and player_hand[1] and
                     get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
                     balance is not None and balance >= player_bet and
                     game_state.get('split_count', 0) < MAX_SPLITS)
        active_hand_data['can_split'] = can_split

        keyboard.append([InlineKeyboardButton("Еще", callback_data=f"bj_hit_{current_hand_idx}"), InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{current_hand_idx}")])
        special_buttons = []
        if can_double: special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{current_hand_idx}"))
        if can_split: special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{current_hand_idx}"))
        if special_buttons: keyboard.append(special_buttons)

    elif state == 'game_over':
        text += f"\n<b>Игра окончена!</b>\n{game_state.get('outcome_text', 'Результат не определен.')}\n"
        final_balance = get_balance(user_id)
        text += f"\nИтоговый баланс: <b>{final_balance:.2f}</b> F." if final_balance is not None else ""
        keyboard.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
    elif state == 'dealer_turn':
        text += "\n<i>⏳ Ход дилера...</i>"

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    result: Message | int | None = None

    try:
        if edit_existing and message_id_to_process:
            logger.debug(f"Editing BJ state msg {message_id_to_process} for user {user_id}")
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id_to_process, text=text,
                reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            result = message_id_to_process
        else:
            logger.debug(f"Sending NEW BJ state message for user {user_id}")
            if message_id_to_process:
                try: await context.bot.delete_message(chat_id, message_id_to_process)
                except Exception: pass
            new_message = await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            game_state['message_id'] = new_message.message_id
            result = new_message
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): result = message_id_to_process
        elif "message to edit not found" in str(e).lower() or "chat not found" in str(e).lower():
            logger.error(f"CRITICAL: Message {message_id_to_process}/Chat {chat_id} not found for user {user_id}. Cleaning game state.")
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
            result = None
        elif "can't parse entities" in str(e).lower():
             logger.error(f"HTML Parsing Error user {user_id} msg {message_id_to_process}: {e}\nText: {text[:500]}...")
             result = None
        else:
            logger.warning(f"Edit/Send BJ state failed user {user_id} msg {message_id_to_process}: {e}")
            result = None
    except Exception as e:
        logger.error(f"Unexpected error in blackjack_show_state user {user_id}: {e}", exc_info=True)
        result = None

    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})

    action, hand_index_str = parts[0], parts[1]
    try: hand_index = int(hand_index_str)
    except (ValueError, TypeError): await q.answer("Ошибка: Неверный индекс.", show_alert=True); return

    action_message_id = q.message.message_id
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != action_message_id: await q.answer("Неактуально.", show_alert=False); return

    player_hands = game.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)) or hand_index != game.get('current_hand_index', -1): await q.answer("Ход другой руки.", show_alert=False); return

    current_hand_data = player_hands[hand_index]
    if not isinstance(current_hand_data, dict) or current_hand_data.get('status') != 'active': await q.answer("Эта рука неактивна.", show_alert=False); return

    hand = current_hand_data.get('hand', [])
    deck = game.get('deck', [])
    balance = get_balance(uid)
    bet = current_hand_data.get('bet', 0)
    needs_state_update = False
    move_to_next = False

    try:
        if action == 'hit':
            card = draw_card(deck)
            if card:
                hand.append(card); game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                current_hand_data['can_double'] = False; current_hand_data['can_split'] = False
                hand_value = get_hand_value(hand); await q.answer(f"Взяли: {card[0]}{card[1]}")
                needs_state_update = True
                if hand_value > 21: current_hand_data['status'] = 'bust'; move_to_next = True
                elif hand_value == 21: current_hand_data['status'] = 'stand'; move_to_next = True
            else: raise IndexError("Draw fail")

        elif action == 'stand':
            current_hand_data['status'] = 'stand'; await q.answer("Стоп.")
            needs_state_update = True; move_to_next = True

        elif action == 'double':
            can_double = (current_hand_data.get('can_double', False) and len(hand) == 2 and balance is not None and balance >= bet)
            if can_double:
                if update_balance(uid, -bet) is not None:
                    current_hand_data['bet'] += bet; current_hand_data['can_double'] = False; current_hand_data['can_split'] = False
                    card = draw_card(deck); drawn_card_str = ""
                    if card:
                        hand.append(card); game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                        hand_value = get_hand_value(hand); current_hand_data['status'] = 'bust' if hand_value > 21 else 'stand'
                        drawn_card_str = f" Карта: {card[0]}{card[1]}. Итог: {hand_value}{' (Перебор!)' if hand_value > 21 else ''}"
                    else: current_hand_data['status'] = 'stand'; drawn_card_str = " Ошибка взятия карты."
                    await q.answer(f"Удвоено!{drawn_card_str}", show_alert=("Ошибка" in drawn_card_str))
                    needs_state_update = True; move_to_next = True
                else: await q.answer("Ошибка списания средств.", show_alert=True)
            else: await q.answer("Удвоить нельзя.", show_alert=True)

        elif action == 'split':
             can_split = current_hand_data.get('can_split', False) # Assumes show_state updated this
             if can_split and balance is not None and balance >= bet:
                 if update_balance(uid, -bet) is not None:
                     game['split_count'] = game.get('split_count', 0) + 1
                     card_to_move = hand.pop()
                     new_hand_data = {'hand': [card_to_move], 'bet': bet, 'status': 'active', 'can_double': False, 'can_split': False}
                     player_hands.insert(hand_index + 1, new_hand_data)

                     cards_drawn = [draw_card(deck), draw_card(deck)]
                     if cards_drawn[0]: hand.append(cards_drawn[0]); game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                     if cards_drawn[1]: new_hand_data['hand'].append(cards_drawn[1]); game['cards_dealt'] = game.get('cards_dealt', 0) + 1

                     is_ace_split = get_card_value(hand[0] if hand else None) == 11
                     if is_ace_split:
                         current_hand_data['status'] = 'stand'; new_hand_data['status'] = 'stand'
                         current_hand_data['can_double'] = False; new_hand_data['can_double'] = False
                         await q.answer("Тузы разделены и стоят."); needs_state_update = True; move_to_next = True
                     else:
                         if get_hand_value(hand) == 21: current_hand_data['status'] = 'stand'
                         if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'
                         current_hand_data['can_double'] = (len(hand) == 2); new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)
                         limit_ok = game.get('split_count', 0) < MAX_SPLITS
                         current_balance_after_split = get_balance(uid) # Re-check balance
                         chd_can_resplit = (len(hand) == 2 and hand[0] and hand[1] and get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok and current_balance_after_split is not None and current_balance_after_split >= current_hand_data['bet'])
                         nhd_can_resplit = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and current_balance_after_split is not None and current_balance_after_split >= new_hand_data['bet'])
                         current_hand_data['can_split'] = chd_can_resplit; new_hand_data['can_split'] = nhd_can_resplit
                         await q.answer("Рука разделена!"); needs_state_update = True
                 else: await q.answer("Ошибка списания средств.", show_alert=True)
             else: await q.answer("Разделить нельзя.", show_alert=True)

    except IndexError as e:
        logger.warning(f"BJ action '{action}' user {uid} failed draw: {e}")
        current_hand_data['status'] = 'stand'; await q.answer("Не удалось взять карту! Ход завершен.", show_alert=True)
        needs_state_update = True; move_to_next = True
    except Exception as e:
        logger.error(f"BJ action '{action}' user {uid} unexpected error: {e}", exc_info=True)
        await q.answer("Произошла ошибка.", show_alert=True)

    if needs_state_update:
        update_result = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        if not update_result: logger.error(f"Failed to update BJ state msg after action '{action}' for user {uid}.")

    if move_to_next:
        await blackjack_next_action(context, chat_id, uid)


async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'player_turn': return

    player_hands = game.get('player_hands', [])
    current_hand_idx = game.get('current_hand_index', -1)
    next_active_idx = -1
    for i in range(current_hand_idx + 1, len(player_hands)):
        if isinstance(player_hands[i], dict) and player_hands[i].get('status') == 'active':
            next_active_idx = i; break

    message_id = game.get('message_id')
    if not message_id: logger.error(f"BJ next_action: No message_id user {user_id}."); context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None); return

    if next_active_idx != -1:
        game['current_hand_index'] = next_active_idx
        logger.info(f"BJ user {user_id}: Moving to next active hand index {next_active_idx}")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
    else:
        logger.info(f"BJ user {user_id}: All player hands done, moving to dealer's turn.")
        game['state'] = 'dealer_turn'
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True) # Show "Dealer's turn"
        timer_job_name = f"dealer_turn_{user_id}_{message_id}"
        # Remove existing job first if any (prevent duplicates)
        existing_jobs = context.job_queue.get_jobs_by_name(timer_job_name)
        for job in existing_jobs: job.schedule_removal()
        # Schedule new job
        context.job_queue.run_once(
            blackjack_dealer_turn_job, DEALER_TURN_DELAY,
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': message_id},
            name=timer_job_name
        )

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data.get('user_id'); chat_id = job_data.get('chat_id'); message_id = job_data.get('message_id')
    if not all([user_id, chat_id, message_id]): logger.error(f"BJ Dealer job missing data: {job_data}"); return

    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'dealer_turn' or game.get('message_id') != message_id:
        logger.info(f"BJ Dealer job user {user_id} msg {message_id}: Game state mismatch or ended. Aborting."); return

    deck = game.get('deck', []); dealer_hand = game.get('dealer_hand', []); player_hands = game.get('player_hands', [])
    dealer_value_initial = get_hand_value(dealer_hand); dealer_has_blackjack = (dealer_value_initial == 21 and len(dealer_hand) == 2)
    player_can_win = any(isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] for h in player_hands)
    dealer_needs_to_hit = player_can_win or dealer_has_blackjack

    dealer_stood = False; hit_occurred = False
    if dealer_needs_to_hit:
        while not dealer_stood:
            current_dealer_value = get_hand_value(dealer_hand)
            num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
            is_soft = num_aces > 0 and (current_dealer_value - num_aces * 11 < 11) # Simplified check

            stand_value_met = False
            if current_dealer_value > 17: stand_value_met = True
            elif current_dealer_value == 17 and not (is_soft and DEALER_HITS_SOFT_17): stand_value_met = True

            if stand_value_met:
                if not hit_occurred: logger.info(f"BJ Dealer user {user_id} stands initially on {current_dealer_value}.")
                dealer_stood = True; break

            logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}.")
            card = draw_card(deck)
            if card:
                dealer_hand.append(card); game['cards_dealt'] = game.get('cards_dealt', 0) + 1; hit_occurred = True
                await asyncio.sleep(DEALER_TURN_DELAY * 0.6) # Pause between hits
            else:
                logger.warning(f"BJ Dealer user {user_id} failed draw (deck empty?). Standing."); dealer_stood = True; break
    else:
        logger.info(f"BJ Dealer user {user_id}: No hit needed. Standing immediately."); dealer_stood = True

    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn value {final_dealer_value}. Updating display.")

    update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
    if update_success: await asyncio.sleep(DEALER_TURN_DELAY * 0.8) # Pause to see final hand

    await blackjack_determine_outcome(context, chat_id, user_id, dealer_has_blackjack)


async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game: logger.warning(f"BJ outcome user {user_id}: Game data not found."); return
    message_id = game.get('message_id');
    if not message_id: logger.error(f"BJ outcome user {user_id}: No message_id."); return

    if game.get('outcome_determined'):
        logger.info(f"BJ outcome user {user_id}: Already determined."); await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True); return

    player_hands = game.get('player_hands', []); dealer_hand = game.get('dealer_hand', [])
    dealer_final_value = get_hand_value(dealer_hand); dealer_busted = dealer_final_value > 21
    outcome_lines = []; total_winnings_to_pay = 0.0; total_initial_bet = 0.0

    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', []); bet = hand_data.get('bet', 0); status = hand_data.get('status')
        player_value = get_hand_value(hand); player_had_blackjack = (status == 'blackjack')
        total_initial_bet += bet

        payout_amount = 0.0; outcome_str = ""; prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

        if status == 'bust': outcome_str = f"{prefix}Перебор ({player_value}). Проигрыш (-{bet:.0f} F)."; payout_amount = 0
        elif player_had_blackjack:
            if d_had_bj: outcome_str = f"{prefix}Блекджек! Ничья."; payout_amount = bet
            else: win_amount = bet * BLACKJACK_PAYOUT; outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."; payout_amount = bet + win_amount
        elif d_had_bj: outcome_str = f"{prefix}Диллер БЖ. Проигрыш (-{bet:.0f} F)."; payout_amount = 0
        elif dealer_busted: outcome_str = f"{prefix}Диллер перебор ({dealer_final_value})! Выигрыш +{bet:.0f} F."; payout_amount = bet * 2
        elif player_value > dealer_final_value: outcome_str = f"{prefix}Выигрыш ({player_value} > {dealer_final_value}). +{bet:.0f} F."; payout_amount = bet * 2
        elif player_value == dealer_final_value: outcome_str = f"{prefix}Ничья ({player_value} = {dealer_final_value}). Возврат."; payout_amount = bet
        else: outcome_str = f"{prefix}Проигрыш ({player_value} < {dealer_final_value}). (-{bet:.0f} F)."; payout_amount = 0

        outcome_lines.append(outcome_str); total_winnings_to_pay += payout_amount

    net_change = round(total_winnings_to_pay - total_initial_bet, 2)
    balance_updated_ok = True
    if total_winnings_to_pay > 0:
        if update_balance(user_id, total_winnings_to_pay) is None:
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! ❌</b>")
            net_change = -total_initial_bet; balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED update balance payout {total_winnings_to_pay}")
        else: logger.info(f"BJ outcome user {user_id}: Balance updated +{total_winnings_to_pay:.2f}. Net: {net_change:+.2f}")
    else: logger.info(f"BJ outcome user {user_id}: No winnings. Net: {net_change:+.2f}")

    game['state'] = 'game_over'
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True

    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    if balance_updated_ok:
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        logger.info(f"BJ game state cleaned user {user_id}")
    else: logger.warning(f"BJ game state NOT cleaned user {user_id} due to balance error.")


# --- Roulette Game Logic (Group Chat) ---

async def roulette_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Рулетку можно запустить только в групповом чате.")
        return

    logger.info(f"/roulette command in chat {chat.id} ({chat.title}) by user {user.id}")

    # Check if a game is already running in this chat
    if context.chat_data.get(ROULETTE_GAME_KEY):
        existing_game = context.chat_data[ROULETTE_GAME_KEY]
        msg_id = existing_game.get('message_id')
        logger.info(f"Roulette game already exists in chat {chat.id}. State: {existing_game.get('state')}, Msg ID: {msg_id}")

        reply_text = "Игра в рулетку уже идет!" # Default message
        reply_args = {} # Start with empty args

        if msg_id:
            # Prepare reply parameters IF message ID exists
            from telegram import ReplyParameters # Import if not already imported globally
            # We want to reply TO the message, so allow_sending_without_reply should be False (default)
            reply_params = ReplyParameters(message_id=msg_id, chat_id=chat.id)
            reply_args['reply_parameters'] = reply_params
            reply_text = f"Игра в рулетку уже идет! Присоединяйтесь к ставкам 👇"

        try:
            # Attempt to reply (either to message or just in chat)
            await update.message.reply_text(reply_text, **reply_args)

        except BadRequest as e:
            # Handle case where the message we are trying to reply to doesn't exist
            if "replied message not found" in str(e).lower() or "message to reply not found" in str(e).lower():
                logger.warning(f"Previous roulette message {msg_id} not found in chat {chat.id}. Cleaning up stale game data.")
                await update.message.reply_text("Предыдущая игра завершена или ее сообщение удалено. Используйте /roulette еще раз, чтобы начать новую.")
                # Force cleanup of the stale game data immediately
                await cleanup_roulette_game(context, chat.id, delay=None) # No delay
            else:
                # Log other BadRequest errors but still inform the user generically
                logger.warning(f"Failed to reply to existing roulette game message {msg_id} in chat {chat.id}: {e}")
                await update.message.reply_text("Игра в рулетку уже идет!") # Generic fallback reply
        except Exception as e: # Catch other potential errors during reply
             logger.error(f"Unexpected error replying to existing roulette game in chat {chat.id}: {e}", exc_info=True)
             await update.message.reply_text("Игра в рулетку уже идет!") # Generic fallback reply


        # Important: Prevent starting a new game if one exists (or existed until cleanup)
        return
    # --- End of check for existing game ---


    # --- Start New Game (only if no game was found above) ---
    start_time = time.time()
    timer_job_name = f'roulette_timer_{chat.id}_{int(start_time)}'
    game_state = {
        'state': 'betting',
        'message_id': None, # Will be set after sending message
        'chat_id': chat.id,
        'start_time': start_time,
        'timer_job_name': timer_job_name,
        'bets': defaultdict(list), # Bets per user_id
        'current_selection': defaultdict(lambda: {'amount': None, 'type': None, 'value': None}), # Track selections
        'winning_number': None,
        'results': {},
    }
    context.chat_data[ROULETTE_GAME_KEY] = game_state

    # Schedule the end of betting
    job = context.job_queue.run_once(
        end_betting_phase,
        when=ROULETTE_TIMER_SECONDS,
        data={'chat_id': chat.id, 'start_time': start_time, 'timer_job_name': timer_job_name}, # Pass data to identify the correct game
        name=timer_job_name
    )
    if not job:
        logger.error(f"Failed to schedule roulette timer job for chat {chat.id}")
        await update.message.reply_text("❌ Ошибка запуска таймера игры.")
        context.chat_data.pop(ROULETTE_GAME_KEY, None) # Clean up failed game
        return

    logger.info(f"Roulette game started in chat {chat.id}. Timer job: {timer_job_name}")

    # Send initial game message (will be updated later)
    try:
        initial_message = await update.message.reply_text(
            text="⏳ Подготовка стола рулетки...",
            parse_mode=ParseMode.HTML
        )
        game_state['message_id'] = initial_message.message_id
        # Now update the message with the actual game UI
        await update_roulette_message(context, chat.id)
    except Exception as e:
        logger.error(f"Failed to send/update initial roulette message in chat {chat.id}: {e}", exc_info=True)
        # Attempt to remove the scheduled job if setup failed
        running_jobs = context.job_queue.get_jobs_by_name(timer_job_name)
        for j in running_jobs:
            try:
                j.schedule_removal()
            except Exception as job_e:
                 logger.warning(f"Failed to remove job {timer_job_name}: {job_e}")
        context.chat_data.pop(ROULETTE_GAME_KEY, None)
        await update.message.reply_text("❌ Ошибка при создании сообщения игры.")

async def update_roulette_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, custom_text: str | None = None):
    game_data = context.chat_data.get(ROULETTE_GAME_KEY)
    if not game_data or not game_data.get('message_id'): return

    state = game_data['state']; message_id = game_data['message_id']
    text = "🎰 <b>Американская Рулетка</b> 🎰\n\n"; keyboard = []

    if state == 'betting':
        remaining_time = int(game_data['start_time'] + ROULETTE_TIMER_SECONDS - time.time())
        text += f"⏱️ <b>Прием ставок! Осталось: {max(0, remaining_time)} сек.</b>\n"
        text += f"<code>Макс. ставок на игрока: {ROULETTE_MAX_BETS_PER_PLAYER}</code>\n\n"
        if game_data['bets']:
            text += "<u>Текущие ставки:</u>\n"
            player_ids = list(game_data['bets'].keys())
            player_mentions = await asyncio.gather(*(get_user_mention(context, uid) for uid in player_ids))
            mention_map = dict(zip(player_ids, player_mentions))
            for user_id, user_bets in game_data['bets'].items():
                mention = mention_map.get(user_id, f"User {user_id}")
                bet_texts = [format_roulette_bet(b) for b in user_bets]
                text += f"{mention}: {'; '.join(bet_texts)}\n"
            text += "\n"
        else: text += "<i>Ставок пока нет.</i>\n\n"
        text += "👇 <b>Нажмите, чтобы сделать ставку:</b>"
        keyboard = [[InlineKeyboardButton("💰 Сделать ставку", callback_data="rl_start_bet")]]

    elif state == 'spinning':
        text += f"🛑 Ставки закрыты! Вращаем рулетку...\n\n"; text += custom_text or "🌪️"

    elif state == 'result':
        wn = game_data.get('winning_number')
        if wn is None: text += "❌ Ошибка: Результат не определен."
        else:
            wn_display = display_roulette_number(wn); color_emoji = get_roulette_color_emoji(wn)
            color_name = (get_roulette_color(wn) or "N/A").capitalize()
            text += f"🎉 <b>Результат: {wn_display}{color_emoji} ({color_name})</b> 🎉\n\n"; text += "<u>Выплаты:</u>\n"
            results = game_data.get('results', {})
            if results:
                player_ids_in_results = list(results.keys())
                player_mentions = await asyncio.gather(*(get_user_mention(context, uid) for uid in player_ids_in_results))
                mention_map = dict(zip(player_ids_in_results, player_mentions))
                for user_id, info in results.items():
                    mention = mention_map.get(user_id, f"User {user_id}")
                    won_bets_str = info.get('won_bets_str', '')
                    if info['payout'] > 0: text += f"{mention}: <b>+{info['payout']:.2f} F</b> ({won_bets_str})\n"
                    elif won_bets_str and "[Ошибка выплаты" in won_bets_str: text += f"{mention}: ❌ Ошибка выплаты ({won_bets_str})\n"
            else: text += "<i>Никто не выиграл в этом раунде.</i>\n"
        keyboard = [[InlineKeyboardButton("🔄 Начать новую игру: /roulette", callback_data="rl_ignore")]]

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): pass
        elif "message to edit not found" in str(e).lower():
             logger.error(f"Roulette message {message_id} not found chat {chat_id}. Ending game."); await cleanup_roulette_game(context, chat_id)
        else: logger.warning(f"Failed update roulette msg {message_id} chat {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error updating roulette msg {message_id} chat {chat_id}: {e}", exc_info=True)


async def end_betting_phase(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; chat_id = job_data.get('chat_id'); start_time = job_data.get('start_time'); timer_job_name = job_data.get('timer_job_name')
    game_data = context.chat_data.get(ROULETTE_GAME_KEY)
    if not game_data or game_data.get('start_time') != start_time or game_data.get('timer_job_name') != timer_job_name or game_data['state'] != 'betting':
        logger.info(f"Roulette timer job {timer_job_name} ignored chat {chat_id}"); return

    logger.info(f"Roulette betting phase ended chat {chat_id} Job: {timer_job_name}"); game_data['state'] = 'spinning'
    winning_number = random.choice(ROULETTE_NUMBERS); game_data['winning_number'] = winning_number
    wn_display = display_roulette_number(winning_number); logger.info(f"Roulette chat {chat_id}: Winning number {wn_display} ({winning_number})")

    await update_roulette_message(context, chat_id); await asyncio.sleep(0.5)

    animation_chars = ["✶", "✸", "✹", "✺", "✹", "✸"]; num_steps = int(ROULETTE_SPIN_ANIMATION_DURATION / 0.5)
    for i in range(num_steps):
        frame_text = f"<b>{animation_chars[i % len(animation_chars)]}</b>"
        await update_roulette_message(context, chat_id, custom_text=frame_text); await asyncio.sleep(0.5)

    calculate_roulette_results(context, chat_id); game_data['state'] = 'result'
    await update_roulette_message(context, chat_id)
    await cleanup_roulette_game(context, chat_id, delay=ROULETTE_CLEANUP_DELAY)


def calculate_roulette_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game_data = context.chat_data.get(ROULETTE_GAME_KEY)
    if not game_data or game_data.get('winning_number') is None: logger.error(f"Cannot calculate results chat {chat_id}."); return

    wn = game_data['winning_number']; wn_color = get_roulette_color(wn)
    is_zero = (wn == 0 or wn == ROULETTE_NUMBER_00); is_even = not is_zero and wn % 2 == 0; is_odd = not is_zero and wn % 2 != 0
    is_low = not is_zero and 1 <= wn <= 18; is_high = not is_zero and 19 <= wn <= 36
    is_dozen1 = not is_zero and wn in ROULETTE_DOZEN_1; is_dozen2 = not is_zero and wn in ROULETTE_DOZEN_2; is_dozen3 = not is_zero and wn in ROULETTE_DOZEN_3

    all_bets = game_data.get('bets', {}); results = defaultdict(lambda: {'payout': 0.0, 'won_bets_str': []}); total_paid_out = 0.0

    for user_id, user_bets in all_bets.items():
        user_total_payout_for_round = 0.0; won_bets_details = []
        for bet in user_bets:
            bet_type = bet['type']; bet_value = bet.get('value'); bet_amount = bet['amount']; payout_multiplier = ROULETTE_PAYOUTS.get(bet_type, 0); won = False
            if bet_type == "number" and bet_value == wn: won = True
            elif bet_type == "red" and wn_color == "red": won = True
            elif bet_type == "black" and wn_color == "black": won = True
            elif bet_type == "even" and is_even: won = True
            elif bet_type == "odd" and is_odd: won = True
            elif bet_type == "low" and is_low: won = True
            elif bet_type == "high" and is_high: won = True
            elif bet_type == "dozen1" and is_dozen1: won = True
            elif bet_type == "dozen2" and is_dozen2: won = True
            elif bet_type == "dozen3" and is_dozen3: won = True
            if won: payout = round(bet_amount * payout_multiplier, 2); user_total_payout_for_round += payout; won_bets_details.append(format_roulette_bet(bet))

        if user_total_payout_for_round > 0:
            if update_balance(user_id, user_total_payout_for_round) is not None:
                results[user_id]['payout'] += user_total_payout_for_round; results[user_id]['won_bets_str'].extend(won_bets_details); total_paid_out += user_total_payout_for_round
                logger.info(f"Roulette chat {chat_id}: User {user_id} won {user_total_payout_for_round:.2f}")
            else:
                logger.error(f"Roulette chat {chat_id}: FAILED update balance user {user_id} payout {user_total_payout_for_round}")
                results[user_id]['payout'] += 0; results[user_id]['won_bets_str'].append(f"[Ошибка выплаты {user_total_payout_for_round:.2f} F]")

    for user_id in results: results[user_id]['won_bets_str'] = "; ".join(results[user_id]['won_bets_str'])
    game_data['results'] = dict(results); logger.info(f"Roulette chat {chat_id}: Calculation complete. Paid: {total_paid_out:.2f}")


async def cleanup_roulette_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int, delay: int | None = None):
    job_name = f'roulette_cleanup_{chat_id}_{int(time.time())}'
    if delay and delay > 0:
        context.job_queue.run_once(cleanup_roulette_job, when=delay, data={'chat_id': chat_id}, name=job_name)
        logger.info(f"Scheduled roulette cleanup job '{job_name}' chat {chat_id} delay {delay}s.")
    else: await cleanup_roulette_job(context, chat_id=chat_id)


async def cleanup_roulette_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None):
    if not chat_id and context.job: chat_id = context.job.data.get('chat_id')
    if chat_id:
        removed_game = context.chat_data.pop(ROULETTE_GAME_KEY, None)
        if removed_game:
            timer_job_name = removed_game.get('timer_job_name')
            if timer_job_name:
                 running_jobs = context.job_queue.get_jobs_by_name(timer_job_name)
                 for j in running_jobs: j.schedule_removal(); logger.info(f"Removed lingering timer job '{timer_job_name}' chat {chat_id}")
            logger.info(f"Roulette game data cleaned up chat {chat_id}")
        else: logger.info(f"Roulette cleanup: No game data found chat {chat_id}.")
    else: logger.error("Roulette cleanup job/call missing chat_id.")


def generate_roulette_betting_keyboard(user_selection: dict) -> list[list[InlineKeyboardButton]]:
    keyboard = []; amount = user_selection.get('amount'); bet_type = user_selection.get('type')
    if amount is None:
        amounts = [1, 5, 10, 25, 50, 100]; row = []
        for amt in amounts:
            row.append(InlineKeyboardButton(f"{amt} F", callback_data=f"rl_set_amt_{amt}"))
            if len(row) >= 4: keyboard.append(row); row = []
        if row: keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="rl_cancel_sel")])
    elif bet_type is None:
        amt_str = f" ({amount:.0f} F)"
        keyboard.append([InlineKeyboardButton(f"Красное🔴{amt_str}", callback_data="rl_set_type_red"), InlineKeyboardButton(f"Черное⚫{amt_str}", callback_data="rl_set_type_black")])
        keyboard.append([InlineKeyboardButton(f"Четное{amt_str}", callback_data="rl_set_type_even"), InlineKeyboardButton(f"Нечетное{amt_str}", callback_data="rl_set_type_odd")])
        keyboard.append([InlineKeyboardButton(f"Малые (1-18){amt_str}", callback_data="rl_set_type_low"), InlineKeyboardButton(f"Большие (19-36){amt_str}", callback_data="rl_set_type_high")])
        keyboard.append([InlineKeyboardButton(f"Дюж.1 (1-12){amt_str}", callback_data="rl_set_type_dozen1"), InlineKeyboardButton(f"Дюж.2 (13-24){amt_str}", callback_data="rl_set_type_dozen2"), InlineKeyboardButton(f"Дюж.3 (25-36){amt_str}", callback_data="rl_set_type_dozen3")])
        keyboard.append([InlineKeyboardButton(f"Конкр. число{amt_str}", callback_data="rl_set_type_number")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад (Сумма)", callback_data="rl_back_amt"), InlineKeyboardButton("❌ Отмена", callback_data="rl_cancel_sel")])
    elif bet_type == "number" and user_selection.get('value') is None:
         amt_str = f" ({amount:.0f} F)"
         keyboard.append([InlineKeyboardButton(f"0{amt_str}", callback_data=f"rl_set_val_0"), InlineKeyboardButton(f"00{amt_str}", callback_data=f"rl_set_val_{ROULETTE_NUMBER_00}")])
         max_per_row = 6; current_row = []
         for n in range(1, 37):
              current_row.append(InlineKeyboardButton(f"{n}{amt_str}", callback_data=f"rl_set_val_{n}"))
              if len(current_row) >= max_per_row: keyboard.append(current_row); current_row = []
         if current_row: keyboard.append(current_row)
         keyboard.append([InlineKeyboardButton("⬅️ Назад (Тип)", callback_data="rl_back_type"), InlineKeyboardButton("❌ Отмена", callback_data="rl_cancel_sel")])
    else:
        bet_desc = ROULETTE_BET_TYPES_DESC.get(bet_type, bet_type.capitalize()); value_display = display_roulette_number(user_selection['value']) if bet_type == 'number' else ""; value_desc = f" {value_display}" if bet_type == 'number' else ""; amt_str = f"{amount:.0f}"
        confirm_text = f"✅ Поставить {amt_str} F на {html_escape(bet_desc)}{value_desc}?"; keyboard.append([InlineKeyboardButton(confirm_text, callback_data="rl_place_bet")])
        back_target = "type"; back_text = "Тип";
        if bet_type == "number": back_target = "value"; back_text = "Число"
        keyboard.append([InlineKeyboardButton(f"⬅️ Назад ({back_text})", callback_data=f"rl_back_{back_target}"), InlineKeyboardButton("❌ Отмена", callback_data="rl_cancel_sel")])
    return keyboard


# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data; u = q.from_user; chat = update.effective_chat
    if not data or not u or not chat: logger.warning("Callback missing data/user/chat."); return

    logger.debug(f"CB:'{data}' u:{u.id} chat:{chat.id}")
    parts = data.split("_", 2); prefix = parts[0]; payload = parts[1:]

    try:
        # --- Blackjack Handler (PM) ---
        if prefix == "bj":
            if chat.type != ChatType.PRIVATE: await q.answer("Блекджек только в личке.", show_alert=True); return
            action = payload[0] if payload else None; arg = payload[1] if len(payload) > 1 else None
            if action == "bet" and arg: await blackjack_handle_bet(update, context, int(arg))
            elif action == "new": await blackjack_start_command(update, context)
            elif action in ["hit", "stand", "double", "split"] and arg: await blackjack_handle_action(update, context, [action, arg])
            else: await q.answer()

        # --- Roulette Handler (Group) ---
        elif prefix == "rl":
            if chat.type == ChatType.PRIVATE: await q.answer("Рулетка только в группе.", show_alert=True); return
            game_data = context.chat_data.get(ROULETTE_GAME_KEY); action = payload[0] if payload else None; arg_str = payload[1] if len(payload) > 1 else None

            if not game_data and action != "ignore": await q.answer("Игра не найдена/завершена.", show_alert=True); return
            if game_data and game_data.get('state') != 'betting' and action not in ['ignore']: await q.answer("Ставки закрыты.", show_alert=False); return

            user_selection = game_data['current_selection'][u.id]

            if action == "ignore": await q.answer()
            elif action == "start_bet":
                if len(game_data['bets'].get(u.id, [])) >= ROULETTE_MAX_BETS_PER_PLAYER: await q.answer(f"Лимит ({ROULETTE_MAX_BETS_PER_PLAYER}) ставок.", show_alert=True); return
                user_selection.clear(); user_selection.update({'amount': None, 'type': None, 'value': None}); keyboard = generate_roulette_betting_keyboard(user_selection)
                try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard)); await q.answer("Выберите сумму")
                except BadRequest as e: await q.answer() # Ignore message not modified
            elif action == "set":
                setting_type = arg_str; setting_value_str = payload[2] if len(payload) > 2 else None
                if setting_type == "amt":
                    amount_val = int(setting_value_str); assert amount_val > 0; user_selection['amount'] = amount_val; user_selection['type'] = None; user_selection['value'] = None; await q.answer(f"Сумма: {amount_val} F")
                elif setting_type == "type":
                    assert setting_value_str in ROULETTE_BET_TYPES_DESC; user_selection['type'] = setting_value_str; user_selection['value'] = None; bet_desc = ROULETTE_BET_TYPES_DESC.get(setting_value_str); next_step = "Выберите число" if setting_value_str == 'number' else "Подтвердите"; await q.answer(f"Тип: {bet_desc}. {next_step}")
                elif setting_type == "val":
                    num_val = int(setting_value_str); assert 0 <= num_val <= 36 or num_val == ROULETTE_NUMBER_00; user_selection['value'] = num_val; await q.answer(f"Число: {display_roulette_number(num_val)}. Подтвердите.")
                else: raise ValueError("Invalid set type")
                keyboard = generate_roulette_betting_keyboard(user_selection)
                try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
                except BadRequest as e: await q.answer() # Ignore message not modified
            elif action == "back":
                back_to = arg_str; q_text = ""
                if back_to == "amt": user_selection.clear(); q_text = "Выберите сумму"
                elif back_to == "type": user_selection['type'] = None; user_selection['value'] = None; q_text = "Выберите тип"
                elif back_to == "value": user_selection['value'] = None; q_text = "Выберите число"
                else: raise ValueError("Invalid back target")
                keyboard = generate_roulette_betting_keyboard(user_selection)
                try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard)); await q.answer(q_text)
                except BadRequest as e: await q.answer() # Ignore message not modified
            elif action == "cancel":
                 user_selection.clear(); await q.answer("Выбор отменен.")
                 main_keyboard = [[InlineKeyboardButton("💰 Сделать ставку", callback_data="rl_start_bet")]];
                 try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(main_keyboard))
                 except BadRequest as e: await q.answer() # Ignore message not modified
            elif action == "place":
                amount = user_selection.get('amount'); bet_type = user_selection.get('type'); value = user_selection.get('value')
                assert amount and bet_type, "Incomplete bet"; assert bet_type != 'number' or value is not None, "Number not selected"
                assert len(game_data['bets'].get(u.id, [])) < ROULETTE_MAX_BETS_PER_PLAYER, f"Bet limit {ROULETTE_MAX_BETS_PER_PLAYER}"
                balance = get_balance(u.id); assert balance is not None and balance >= amount, f"Insufficient funds ({balance} F)"
                assert update_balance(u.id, -amount) is not None, "Balance update failed"
                new_bet = {'amount': amount, 'type': bet_type, 'value': value}; game_data['bets'][u.id].append(new_bet)
                logger.info(f"RL chat {chat.id}: u {u.id} bet: {new_bet}"); user_selection.clear()
                await update_roulette_message(context, chat.id); await q.answer(f"Ставка принята: {format_roulette_bet(new_bet)}")
            else: await q.answer() # Unknown action

        else: await q.answer() # Unknown prefix

    except (ValueError, TypeError, AssertionError) as e: # Catch validation errors
         logger.warning(f"Callback Validation Error ('{data}' u {u.id} chat {chat.id}): {e}")
         await q.answer(f"Ошибка: {e}", show_alert=True)
    except BadRequest as e:
        if "message is not modified" in str(e).lower(): await q.answer()
        elif "query is too old" in str(e).lower(): await q.answer("Действие устарело.", show_alert=False)
        elif "message to edit not found" in str(e).lower(): await q.answer("Сообщение игры удалено.", show_alert=False)
        else: logger.error(f"CB BadRequest ('{data}' u {u.id} c {chat.id}): {e}"); await q.answer("Ошибка Telegram.", show_alert=True)
    except Exception as e:
        logger.error(f"CB Error ('{data}' u {u.id} c {chat.id}): {e}", exc_info=True); await q.answer("Внутренняя ошибка.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception handling update: {context.error}", exc_info=context.error)
    if isinstance(context.error, Conflict): logger.critical("Conflict error! Multiple instances?")
    elif isinstance(context.error, BadRequest): logger.warning(f"BadRequest: {context.error}. Update: {update}")


# --- Main Bot Setup ---
def main():
    logger.info("Starting bot application...")
    start_keep_alive()
    try:
        application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
        # Commands
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("balance", balance_command))
        application.add_handler(CommandHandler("bonus", bonus_command))
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))
        application.add_handler(CommandHandler("blackjack", blackjack_start_command))
        application.add_handler(CommandHandler("roulette", roulette_command))
        # Callbacks
        application.add_handler(CallbackQueryHandler(button_callback_handler))
        # Errors
        application.add_error_handler(error_handler)
        logger.info("Handlers registered."); print("Bot is running...")
        # Start polling
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except ValueError as e: logger.critical(f"Config Error: {e}"); print(f"CRITICAL ERROR: {e}")
    except Conflict as e: logger.critical(f"Conflict Error: {e}. Is another instance running?"); print("CRITICAL ERROR: Conflict.")
    except Exception as e: logger.critical(f"Runtime critical error: {e}", exc_info=True); print(f"CRITICAL ERROR: {e}")
    finally: print("Bot stopped."); logger.info("Bot stopped.")

if __name__ == "__main__":
    main()