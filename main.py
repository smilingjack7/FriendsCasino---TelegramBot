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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message # Import Message
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
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

INITIAL_BALANCE = 100.0
BONUS_AMOUNT = 10.0
BONUS_COOLDOWN_HOURS = 6
NUM_DECKS = 8
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.5 # Slightly increased delay for better visibility
LEADERBOARD_LIMIT = 10

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Keep INFO for bot actions
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home(): return "Bot is alive!"
def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    logging.getLogger('werkzeug').setLevel(logging.WARNING) # Reduce Flask logging noise
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)
def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Database Interaction (Simplified) ---
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
        # Ensure last_bonus is datetime or None
        if data and data.get('last_bonus') and not isinstance(data['last_bonus'], datetime.datetime):
            data['last_bonus'] = None
        return data
    except Exception as e:
        logger.error(f"DB Error (get_or_create_user) for {user_id}: {e}")
        return None

def update_balance(user_id: int, change: float) -> float | None:
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id,))
            res = cur.fetchone()
            if res:
                new_balance = res[0]
                logger.info(f"Balance updated for {user_id}: {change:+.2f}. New balance: {new_balance:.2f}")
                return new_balance
            else: # Should not happen if user exists, but good practice
                logger.warning(f"Update balance failed for user {user_id} (user not found or other issue)")
                return None
    except Exception as e:
        logger.error(f"DB Error (update_balance) for {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    # Ensure timezone info is removed before saving to DB if DB doesn't handle it
    ts_naive = ts_utc.replace(tzinfo=None)
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts_naive, user_id))
            logger.info(f"Bonus timestamp updated for {user_id} to {ts_naive}")
    except Exception as e:
        logger.error(f"DB Error (update_last_bonus_time) for {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    user_data = get_or_create_user(user_id)
    # Ensure the returned value is datetime or None
    last_bonus = user_data.get('last_bonus') if user_data else None
    if last_bonus and not isinstance(last_bonus, datetime.datetime):
         logger.warning(f"Invalid last_bonus type for user {user_id}: {type(last_bonus)}. Resetting.")
         return None # Or attempt to parse if possible
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

# --- Game Utilities ---
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

# --- Obfuscated Card Drawing (Simplified Name) ---
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

# --- Helper to get User Mention (HTML) ---
_user_mention_cache = {}
_cache_lock = asyncio.Lock()
_cache_ttl = 3600 # 1 hour

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
    except BadRequest as e:
         if "chat not found" in str(e).lower():
              mention = f"User {user_id}"
              logger.warning(f"Could not get chat for user {user_id} (likely deleted or invalid): {e}")
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

# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start command from user {user.id} ({user.username or 'no_username'})")
    get_or_create_user(user.id) # Ensure user exists in DB
    balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {html_escape(user.first_name)}! 👋\n"
        f"Ваш баланс: <b>{balance_str}</b> фишек.\n\n"
        f"Чтобы сыграть в Блекджек, используйте /blackjack (только в личных сообщениях).\n"
        f"Для справки по командам введите /help.",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/help command from user {user.id}")
    help_text = (
        "<b>ℹ️ Справка по командам:</b>\n\n"
        "<b>Команды для Личного Чата:</b>\n"
        "/start - Начать и проверить баланс\n"
        "/blackjack - Начать новую игру в Блекджек\n"
        "/balance - Показать текущий баланс\n"
        "/bonus - Получить ежедневный бонус (раз в 6 часов)\n\n"
        "<b>Команды для Групп и Личного Чата:</b>\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение\n\n"
        "<i>Играйте ответственно! Удачи!</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/balance command from user {user.id} in chat {chat.id} (type: {chat.type})")
    # Allow balance check anywhere, but maybe restrict sensitive actions
    # if chat.type != ChatType.PRIVATE:
    #     await update.message.reply_text("Проверить баланс можно только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
    #     return

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
    last_bonus_utc = get_last_bonus_time(user.id)
    # If last_bonus_utc exists, make it timezone-aware for comparison
    if last_bonus_utc:
        last_bonus_utc = last_bonus_utc.replace(tzinfo=datetime.timezone.utc)

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
        name = mentions[i] if i < len(mentions) else f"User {leader['user_id']}"
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

# --- Blackjack Game (Private Chat Only) ---
BJ_GAME_KEY = 'blackjack_game' # Key for user_data

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"BJ /blackjack command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Играть в Блекджек можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    # Determine message source (command or button callback)
    is_callback = update.callback_query is not None
    source_message = update.callback_query.message if is_callback else update.message
    callback_message_id = source_message.message_id if is_callback else None

    # Answer callback query if applicable
    if is_callback:
        await update.callback_query.answer()

    # Clean up previous game message if exists
    user_game = context.user_data.get(BJ_GAME_KEY, {})
    previous_message_id = user_game.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(chat.id, previous_message_id)
            logger.debug(f"Deleted previous BJ message {previous_message_id} for user {user.id}")
        except Exception as e:
            logger.debug(f"Failed to delete old BJ message {previous_message_id}: {e}")

    # Clear previous game state from user_data
    context.user_data.pop(BJ_GAME_KEY, None)

    # Check balance
    balance = get_balance(user.id)
    if balance is None:
         await source_message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.", parse_mode=ParseMode.HTML)
         return
    if balance <= 0:
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) недостаточен для игры. Попробуйте /bonus.", parse_mode=ParseMode.HTML)
        return

    # Define bet options and filter valid ones based on balance
    bet_options = [1, 5, 10, 25, 50, 100, 250, 500, 1000] # Example options
    valid_bets = [b for b in bet_options if b <= balance]

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) меньше минимальной ставки (<b>{min_bet}</b> F).", parse_mode=ParseMode.HTML)
        return

    # Create bet buttons (max 4 per row)
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
            await context.bot.delete_message(chat.id, callback_message_id)

        # Send the bet prompt message
        sent_message = await context.bot.send_message(
            chat_id=chat.id,
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

    except Exception as e:
        logger.error(f"BJ start error sending bet prompt for user {user.id}: {e}", exc_info=True)
        await context.bot.send_message(chat.id, "❌ Произошла ошибка при начале игры.")


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
    except IndexError as e:
        logger.error(f"BJ dealing error for user {uid}: {e}")
        update_balance(uid, bet) # Refund bet on error
        try:
            await q.edit_message_text(f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.")
        except Exception: pass # Ignore if editing fails
        context.user_data.pop(BJ_GAME_KEY, None) # Clear game state
        return
    except Exception as e: # Catch other potential errors during deal
        logger.error(f"BJ unexpected dealing error for user {uid}: {e}", exc_info=True)
        update_balance(uid, bet) # Refund bet
        try:
             await q.edit_message_text(f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.")
        except Exception: pass
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

    if player_has_blackjack:
        hand_status = 'blackjack'
        game_state = 'game_over' # Game ends immediately
        if dealer_has_blackjack:
            outcome_text = "⚖️ Ничья! У обоих Блекджек."
            update_balance(uid, bet) # Refund bet (push)
        else:
            winnings = bet * BLACKJACK_PAYOUT
            update_balance(uid, bet + winnings) # Refund bet + BJ payout
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {winnings:.2f} F!"
    elif dealer_has_blackjack:
        game_state = 'game_over' # Game ends immediately
        outcome_text = "😥 У дилера Блекджек! Вы проиграли."
        # Bet was already deducted, no refund needed

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
        'outcome_determined': (game_state == 'game_over') # Mark if outcome already set
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
    elif not new_message_info:
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
    if game_state is None:
        game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state:
        logger.warning(f"blackjack_show_state called for user {user_id} but no game state found.")
        return None

    message_id_to_process = game_state.get('message_id')
    if edit_existing and not message_id_to_process:
        logger.error(f"BJ show_state: Attempted to edit but no message_id for user {user_id}")
        return None # Cannot edit without an ID

    # --- Fetch Data ---
    balance = get_balance(user_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands_data = game_state.get('player_hands', [])
    current_hand_idx = game_state.get('current_hand_index', -1)
    state = game_state.get('state', 'unknown')
    dealer_value = get_hand_value(dealer_hand)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    # Hide dealer's second card only during player's turn AND if dealer doesn't have BJ
    hide_dealer_card = (state == 'player_turn' and not dealer_has_blackjack)

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
    try:
        if edit_existing and message_id_to_process:
            # Edit existing message
            logger.debug(f"Editing BJ state msg {message_id_to_process} for user {user_id}")
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id_to_process,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            logger.debug(f"Successfully edited BJ state msg {message_id_to_process}")
            result = message_id_to_process # Return message ID on successful edit
        else:
            # Send new message (e.g., initial state or if editing failed)
            logger.debug(f"Sending NEW BJ state message for user {user_id}")
            if message_id_to_process: # Attempt to delete old message if sending new
                try: await context.bot.delete_message(chat_id, message_id_to_process)
                except Exception: pass # Ignore deletion errors

            new_message = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            # IMPORTANT: Update message ID in game state
            game_state['message_id'] = new_message.message_id
            logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}")
            result = new_message # Return the new Message object

    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            result = message_id_to_process # No change needed, treat as success
        elif "message to edit not found" in str(e).lower() or "chat not found" in str(e).lower():
            logger.error(f"CRITICAL: Message {message_id_to_process} or Chat {chat_id} not found for user {user_id}. Cleaning game state.")
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean up broken game
            result = None # Indicate failure
        elif "can't parse entities" in str(e).lower():
             logger.error(f"HTML Parsing Error for user {user_id} (msg {message_id_to_process}): {e}\nText: {text[:500]}...") # Log error and part of the text
             result = None # Indicate failure
        else:
            logger.warning(f"Edit/Send BJ state failed for user {user_id} (msg {message_id_to_process}): {e}")
            result = None # Indicate other failure

    except Exception as e:
        logger.error(f"Unexpected error in blackjack_show_state for user {user_id}: {e}", exc_info=True)
        result = None # Indicate failure

    # Return Message object if new, message_id if edited, None if failed
    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})

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
                if update_balance(uid, -bet) is not None:
                    current_hand_data['bet'] += bet # Double the bet
                    current_hand_data['can_double'] = False
                    current_hand_data['can_split'] = False

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

                    await q.answer(f"Удвоено!{drawn_card_str}", show_alert=("Ошибка" in drawn_card_str))
                    needs_state_update = True
                    move_to_next = True
                else:
                    await q.answer("Ошибка списания средств для удвоения.", show_alert=True)
            else:
                await q.answer("Удвоить сейчас нельзя.", show_alert=True)

        elif action == 'split':
            can_split = current_hand_data.get('can_split', False) # Assumes show_state updated this
            if can_split and balance is not None and balance >= bet:
                if update_balance(uid, -bet) is not None:
                    game['split_count'] = game.get('split_count', 0) + 1
                    card_to_move = hand.pop() # Take second card for the new hand
                    new_hand_data = {
                        'hand': [card_to_move],
                        'bet': bet,
                        'status': 'active',
                        'can_double': False,
                        'can_split': False
                    }
                    player_hands.insert(hand_index + 1, new_hand_data) # Insert new hand

                    # Deal one card to each split hand
                    cards_drawn = [draw_card(deck), draw_card(deck)]
                    if cards_drawn[0]:
                         hand.append(cards_drawn[0])
                         game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                    if cards_drawn[1]:
                         new_hand_data['hand'].append(cards_drawn[1])
                         game['cards_dealt'] = game.get('cards_dealt', 0) + 1

                    # Handle Ace split rule (stand immediately)
                    is_ace_split = get_card_value(hand[0] if hand else None) == 11
                    if is_ace_split:
                        current_hand_data['status'] = 'stand'
                        new_hand_data['status'] = 'stand'
                        current_hand_data['can_double'] = False
                        new_hand_data['can_double'] = False
                        await q.answer("Тузы разделены и стоят.")
                        needs_state_update = True
                        move_to_next = True # Original hand stands, check next action for the new hand later
                    else:
                        # Check for 21 on deal
                        if get_hand_value(hand) == 21: current_hand_data['status'] = 'stand'
                        if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                        # Set initial double/split possibilities for new hands
                        current_hand_data['can_double'] = (len(hand) == 2)
                        new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)
                        # Re-check split possibility (recursive split) - needs balance check too
                        limit_ok = game.get('split_count', 0) < MAX_SPLITS
                        current_balance_after_split = get_balance(uid) # Re-check balance for re-split

                        chd_can_resplit = (len(hand) == 2 and hand[0] and hand[1] and
                                           get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok and
                                           current_balance_after_split is not None and current_balance_after_split >= current_hand_data['bet'])
                        nhd_can_resplit = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and
                                           get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and
                                           current_balance_after_split is not None and current_balance_after_split >= new_hand_data['bet'])
                        current_hand_data['can_split'] = chd_can_resplit
                        new_hand_data['can_split'] = nhd_can_resplit

                        await q.answer("Рука разделена!")
                        needs_state_update = True
                        # move_to_next remains False, turn continues on the first split hand (hand_index)
                else:
                    await q.answer("Ошибка списания средств для разделения.", show_alert=True)
            else:
                await q.answer("Разделить сейчас нельзя.", show_alert=True)

    except IndexError as e: # Catch draw failure
        logger.warning(f"BJ action '{action}' user {uid} failed draw: {e}")
        current_hand_data['status'] = 'stand' # Force stand?
        await q.answer("Не удалось взять карту! Ход завершен.", show_alert=True)
        needs_state_update = True
        move_to_next = True
    except Exception as e:
        logger.error(f"BJ action '{action}' user {uid} unexpected error: {e}", exc_info=True)
        await q.answer("Произошла непредвиденная ошибка.", show_alert=True)
        # Don't update state or move next on unexpected errors? Or try to recover?

    # --- Update State Message ---
    if needs_state_update:
        update_result = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        if not update_result:
             logger.error(f"Failed to update state message after action '{action}' for user {uid}. Game might be stuck.")
             # Consider attempting to send a new message or informing the user.

    # --- Move to Next Action if Hand Ended ---
    if move_to_next:
        await blackjack_next_action(context, chat_id, uid)


async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """ Determines the next step after a player's hand finishes its turn (stand/bust/double/ace split). """
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'player_turn':
        return # Game ended or already moved on

    player_hands = game.get('player_hands', [])
    current_hand_idx = game.get('current_hand_index', -1)

    # Find the index of the *next* hand that is still 'active'
    next_active_idx = -1
    for i in range(current_hand_idx + 1, len(player_hands)):
        if isinstance(player_hands[i], dict) and player_hands[i].get('status') == 'active':
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
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
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

    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    # --- Validations ---
    # Check if game still exists, is in dealer_turn state, and matches the message ID
    if not game or game.get('state') != 'dealer_turn' or game.get('message_id') != message_id:
        logger.info(f"BJ Dealer job for user {user_id} (msg {message_id}): Game ended, state changed, or message mismatch. Job aborted.")
        return

    # --- Get Data ---
    deck = game.get('deck', [])
    dealer_hand = game.get('dealer_hand', [])
    player_hands = game.get('player_hands', [])

    # Check if dealer needs to hit (i.e., if any player hand didn't bust or get BJ)
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack']
        for h in player_hands
    )
    dealer_value = get_hand_value(dealer_hand)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    # If all players busted/got BJ and dealer doesn't have BJ, dealer doesn't need to hit.
    if not player_can_win and not dealer_has_blackjack:
        logger.info(f"BJ Dealer user {user_id}: All players busted/BJ, dealer stands immediately.")
        await blackjack_determine_outcome(context, chat_id, user_id, dealer_has_blackjack)
        return

    # --- Dealer Drawing Loop ---
    dealer_stood = False
    while not dealer_stood:
        current_dealer_value = get_hand_value(dealer_hand)
        num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
        # Check if hand value calculation includes Ace as 11
        is_soft = num_aces > 0 and (current_dealer_value + 10 * num_aces > 21 and current_dealer_value <= 21) # A more robust soft check might be needed depending on get_hand_value impl.
        # Simplified: Let get_hand_value handle ace logic
        is_soft = 'A' in [c[0] for c in dealer_hand if c] and current_dealer_value <= 11 + (len([c for c in dealer_hand if c and c[0] != 'A']) ) # Basic soft check

        # Dealer Stand Conditions
        if current_dealer_value > 17:
            dealer_stood = True
        elif current_dealer_value == 17:
             if not (is_soft and DEALER_HITS_SOFT_17): # Stand on hard 17, or soft 17 if rule applies
                 dealer_stood = True

        if dealer_stood:
            logger.info(f"BJ Dealer user {user_id} stands on {current_dealer_value}{' (soft)' if is_soft and current_dealer_value==17 else ''}.")
            break # Exit the while loop

        # Dealer Hits
        logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}{' (soft)' if is_soft else ''}.")
        card = draw_card(deck)
        if card:
            dealer_hand.append(card)
            game['cards_dealt'] = game.get('cards_dealt', 0) + 1
            # OPTIONAL: Update message to show dealer drawing (can spam edits)
            # await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
            await asyncio.sleep(DEALER_TURN_DELAY / 2) # Shorter delay between hits maybe?
        else:
            # Failed to draw card
            logger.warning(f"BJ Dealer user {user_id} failed to draw card (deck empty?). Standing.")
            dealer_stood = True # Stop hitting if card cannot be drawn
            break # Exit the while loop

    # --- Determine Outcome ---
    # Final dealer value after standing or busting
    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn with value {final_dealer_value}. Determining outcome.")
    await blackjack_determine_outcome(context, chat_id, user_id, dealer_has_blackjack)


async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    """ Calculates results for each hand, updates balance, shows final state, and cleans up. """
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game:
        logger.warning(f"BJ outcome user {user_id}: Game data not found.")
        return

    message_id = game.get('message_id')
    if not message_id:
        logger.error(f"BJ outcome user {user_id}: No message_id found.")
        return

    # Prevent double processing
    if game.get('outcome_determined'):
        logger.info(f"BJ outcome user {user_id}: Outcome already determined. Reshowing state.")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
        # Do not clean up here again, it was done when outcome was first determined
        return

    # --- Get Final Data ---
    player_hands = game.get('player_hands', [])
    dealer_hand = game.get('dealer_hand', [])
    dealer_final_value = get_hand_value(dealer_hand)
    dealer_busted = dealer_final_value > 21

    outcome_lines = []
    total_winnings_to_pay = 0 # Tracks only the amount to PAY the user (bets + profits)
    total_initial_bet = 0    # Sum of all initial bets placed

    # --- Calculate Outcome per Hand ---
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue

        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status')
        player_value = get_hand_value(hand)
        player_had_blackjack = (status == 'blackjack') # From initial deal

        total_initial_bet += bet # Track total bet amount

        payout_amount = 0 # Amount to return to player for THIS hand (0=loss, bet=push, bet*2=win, bet*(1+BJ_PAYOUT)=BJ win)
        outcome_str = ""
        prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

        if status == 'bust':
            outcome_str = f"{prefix}Перебор ({player_value}). Ставка проиграна (-{bet} F)."
            payout_amount = 0
        elif player_had_blackjack:
            if d_had_bj:
                outcome_str = f"{prefix}Блекджек! Ничья с дилером."
                payout_amount = bet # Push
            else:
                win_amount = bet * BLACKJACK_PAYOUT
                outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."
                payout_amount = bet + win_amount # Bet returned + BJ payout
        elif d_had_bj:
            outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{bet} F)."
            payout_amount = 0
        elif dealer_busted:
            outcome_str = f"{prefix}У дилера перебор ({dealer_final_value})! Выигрыш +{bet} F."
            payout_amount = bet * 2 # Bet returned + winnings
        elif player_value > dealer_final_value:
            # *** HTML ESCAPING FIX ***
            outcome_str = f"{prefix}Вы выиграли ({player_value} > {dealer_final_value}). Выигрыш +{bet} F."
            payout_amount = bet * 2
        elif player_value == dealer_final_value:
            outcome_str = f"{prefix}Ничья ({player_value} = {dealer_final_value}). Ставка возвращена."
            payout_amount = bet # Push
        else: # player_value < dealer_final_value
            # *** HTML ESCAPING FIX ***
            outcome_str = f"{prefix}Вы проиграли ({player_value} < {dealer_final_value}). Ставка проиграна (-{bet} F)."
            payout_amount = 0

        outcome_lines.append(outcome_str)
        total_winnings_to_pay += payout_amount # Accumulate total amount to be paid back

    # --- Calculate Net Change and Update Balance ---
    net_change = total_winnings_to_pay - total_initial_bet

    balance_updated_ok = True
    if total_winnings_to_pay > 0:
        current_balance_before_update = get_balance(user_id)
        if update_balance(user_id, total_winnings_to_pay) is None:
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! ❌</b>")
            # If update fails, the net change IS the loss of the initial bets
            net_change = -total_initial_bet # Correct net change if payout failed
            balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED to update balance with payout {total_winnings_to_pay}. Initial bet was {total_initial_bet}. Balance before attempt: {current_balance_before_update}")
        else:
            logger.info(f"BJ outcome user {user_id}: Balance updated by adding {total_winnings_to_pay:.2f}. Net change for round: {net_change:+.2f}")
    else:
        # No winnings to pay, balance already reflects deducted bets
        logger.info(f"BJ outcome user {user_id}: No winnings to pay. Net change: {net_change:+.2f}")


    # --- Finalize Game State ---
    game['state'] = 'game_over'
    # *** HTML ESCAPING FIX ***
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True # Mark as determined

    # --- Show Final State ---
    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    # --- Clean Up Game Data ---
    # Only clean up if balance update was successful OR there were no winnings to pay
    if balance_updated_ok:
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        logger.info(f"BJ game state cleaned for user {user_id}")
    else:
        logger.warning(f"BJ game state NOT cleaned for user {user_id} due to balance update error.")


# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ Handles all button presses. """
    q = update.callback_query
    data = q.data
    u = q.from_user
    if not data:
        return # Ignore empty callback data

    logger.debug(f"Callback query received: '{data}' from user {u.id} ({u.username or 'no_username'})")
    parts = data.split("_", 2) # prefix_action_payload
    prefix = parts[0]
    payload = parts[1:] # List: [action, argument] or [action]

    try:
        if prefix == "bj":
            # Ensure Blackjack actions are in private chat
            if update.effective_chat.type != ChatType.PRIVATE:
                await q.answer("Играть в Блекджек можно только в личном чате.", show_alert=True)
                return

            action = payload[0] if payload else None
            arg = payload[1] if len(payload) > 1 else None

            if action == "bet" and arg:
                await blackjack_handle_bet(update, context, int(arg))
            elif action == "new":
                await blackjack_start_command(update, context) # Re-run start logic
            elif action in ["hit", "stand", "double", "split"] and arg is not None:
                 # Pass action and hand index as list for handler
                await blackjack_handle_action(update, context, [action, arg])
            else:
                logger.warning(f"Unknown or incomplete BJ callback: {data}")
                await q.answer() # Acknowledge silently
        else:
            logger.warning(f"Unknown callback prefix: {prefix}")
            await q.answer() # Acknowledge other callbacks silently

    except ValueError as e:
         logger.error(f"Callback ValueError (likely int conversion) for '{data}' user {u.id}: {e}")
         await q.answer("Ошибка: Неверный формат данных.", show_alert=True)
    except Exception as e:
        logger.error(f"Callback general error for '{data}' user {u.id}: {e}", exc_info=True)
        await q.answer("Произошла внутренняя ошибка.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ Logs errors raised by Handlers or the Dispatcher. """
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

    # Log specific common errors for better debugging
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error detected! Ensure only ONE instance of the bot is running.")
    elif isinstance(context.error, BadRequest):
        logger.warning(f"BadRequest error: {context.error}. Update that caused it: {update}")
    # Add more specific error types if needed (e.g., NetworkError, TimedOut)

    # Avoid sending messages to users on errors unless absolutely necessary and safe
    # If needed, check the update object type first:
    # if isinstance(update, Update) and update.effective_message:
    #     try:
    #         # Careful what you send back
    #         # await update.effective_message.reply_text("An error occurred.")
    #     except Exception as e:
    #         logger.error(f"Failed to send error notification to user: {e}")


# --- Main Bot Setup ---
def main():
    """ Starts the bot. """
    logger.info("Starting bot application...")
    start_keep_alive() # Start the Flask keep-alive thread

    try:
        # Build Application
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True) # Handle multiple updates in parallel
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

        # Callbacks
        application.add_handler(CallbackQueryHandler(button_callback_handler))

        # Errors
        application.add_error_handler(error_handler)

        logger.info("Handlers registered successfully.")
        print("Bot is running... Press Ctrl+C to stop.") # Console feedback

        # --- Start Polling ---
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, # Process all update types
            drop_pending_updates=True # Ignore updates missed while bot was down
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