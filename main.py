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
from html import escape as html_escape # Для экранирования в HTML

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode, ChatType # Добавляем ChatType
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
DEALER_TURN_DELAY = 0.3
LEADERBOARD_LIMIT = 10

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# --- Web Server for Keep-Alive ---
keep_alive_app = Flask('')
@keep_alive_app.route('/')
def keep_alive_home(): return "Bot is alive!"
def run_web_server():
    port = int(os.environ.get("PORT", 8080)); logging.getLogger('werkzeug').setLevel(logging.WARNING)
    keep_alive_app.run(host='0.0.0.0', port=port, use_reloader=False)
def start_keep_alive():
    t = Thread(target=run_web_server, daemon=True); t.start(); logger.info("Keep-alive web server started.")

# --- Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]; RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Database Interaction (Simplified) ---
def get_db_conn():
    try: conn = psycopg2.connect(DATABASE_URL, sslmode='require'); conn.autocommit = True; return conn
    except Exception as e: logger.error(f"DB connection error: {e}"); raise

def get_or_create_user(user_id: int) -> dict | None:
    sql_s = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    sql_i = "INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, NULL) ON CONFLICT (user_id) DO NOTHING;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql_s, (user_id,)); data = cur.fetchone()
            if not data: cur.execute(sql_i, (user_id, INITIAL_BALANCE)); logger.info(f"New user: {user_id}"); cur.execute(sql_s, (user_id,)); data = cur.fetchone() or {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        if data and data.get('last_bonus') and not isinstance(data['last_bonus'], datetime.datetime): data['last_bonus'] = None
        return data
    except Exception as e: logger.error(f"DB (get_user) {user_id}: {e}"); return None

def update_balance(user_id: int, change: float) -> float | None:
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id,)); res = cur.fetchone()
            if res: logger.info(f"Balance {user_id}: {change:+.2f}. New: {res[0]:.2f}"); return res[0]
            logger.warning(f"Update balance fail {user_id}"); return None
    except Exception as e: logger.error(f"DB (upd_balance) {user_id}: {e}"); return None

def get_balance(user_id: int) -> float | None:
    d = get_or_create_user(user_id); return d['balance'] if d else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"; ts = ts_utc.replace(tzinfo=None)
    try:
        with get_db_conn() as conn, conn.cursor() as cur: cur.execute(sql, (ts, user_id)); logger.info(f"Bonus time {user_id} -> {ts}")
    except Exception as e: logger.error(f"DB (upd_bonus_ts) {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    d = get_or_create_user(user_id); return d.get('last_bonus') if d else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur: cur.execute(sql, (limit,)); return cur.fetchall()
    except Exception as e: logger.error(f"DB (get_leaderboard): {e}"); return []

# --- Game Utilities ---
def create_deck(num=NUM_DECKS)->list: d=[(r,s) for _ in range(num) for s in SUITS for r in RANKS]; random.shuffle(d); return d
def get_card_value(c:tuple|None)->int: return RANK_VALUES.get(c[0],0) if c else 0
def get_hand_value(h: list) -> int:
    v=sum(get_card_value(c) for c in h if c); a=sum(1 for c in h if c and c[0]=='A')
    while v > 21 and a > 0: v -= 10; a -= 1
    return v
def format_hand(h:list, hide:bool=False)->str:
    if not h: return "Пусто"
    if hide and len(h)>1: return f"[{h[0][0]}{h[0][1]}, ??]" if h[0] else "[??, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in h if c])

# --- Obfuscated Card Drawing ---
def _get_next_item(src:list, p1:int, p2:int)->tuple: # Renamed + params
    _s_ok, _s_err = 0, -1; n = len(src)
    if n == 0: logger.warning("Item source empty."); return None, _s_err
    try: i = random.randrange(n); itm = src.pop(i); return itm, _s_ok
    except Exception as e: logger.error(f"Item fetch error: {e}"); return None, _s_err

# --- Helper to get User Mention (HTML) ---
async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    cache = context.bot_data.setdefault('user_mention_cache', {})
    now = time.monotonic(); cache_ttl = 3600
    if user_id in cache and (now - cache[user_id]['ts']) < cache_ttl: return cache[user_id]['mention']
    try: user = await context.bot.get_chat(user_id); mention = user.mention_html()
    except Exception as e: logger.warning(f"Failed get mention {user_id}: {e}"); mention = f"User {user_id}"
    cache[user_id] = {'mention': mention, 'ts': now}; return mention

# --- Core Bot Commands ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; logger.info(f"/start from {user.id}")
    get_or_create_user(user.id); balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        f"Баланс: <b>{balance_str}</b> фишек.\n\n"
        f"Играть в Блекджек: /blackjack (в личке)\n"
        f"Помощь: /help",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/help from {update.effective_user.id}")
    help_text = (
        "<b>ℹ️ Справка по боту:</b>\n\n"
        "<b>Личные сообщения со мной:</b>\n"
        "/start - Приветствие\n"
        "/blackjack - Начать игру в Блекджек\n"
        "/balance - Показать ваш баланс\n"
        f"/bonus - Ежедневный бонус ({BONUS_AMOUNT} фишек)\n\n"
        "<b>Групповые чаты:</b>\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Это сообщение\n\n"
        "<i>Другие игры (например, Рулетка) будут добавлены позже и могут работать в группах.</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat
    logger.info(f"/balance from {user.id} in chat {chat.id} ({chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Используйте команду /balance в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    balance = get_balance(user.id)
    if balance is not None: await update.message.reply_text(f"Ваш баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else: await update.message.reply_text("Не удалось получить баланс.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat
    logger.info(f"/bonus from {user.id} in chat {chat.id} ({chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Используйте команду /bonus в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    if not get_or_create_user(user.id): await update.message.reply_text("Ошибка данных."); return

    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus = get_last_bonus_time(user.id); cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus and (now_utc < last_bonus + cooldown):
        td = last_bonus + cooldown - now_utc; h, r = divmod(td.total_seconds(), 3600); m, _ = divmod(r, 60)
        await update.message.reply_text(f"⏳ Бонус уже получен. Осталось: {int(h)} ч {int(m)} мин.")
        return

    new_b = update_balance(user.id, BONUS_AMOUNT)
    if new_b is not None:
        update_last_bonus_time(user.id, now_utc)
        await update.message.reply_text(f"🎉 Бонус {BONUS_AMOUNT} F начислен!\nБаланс: <b>{new_b:.2f}</b> F.", parse_mode=ParseMode.HTML)
    else: await update.message.reply_text("Ошибка начисления бонуса.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat
    logger.info(f"/leaderboard from {user.id} in chat {chat.id} ({chat.type})")

    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Используйте команду /leaderboard в <b>групповом чате</b>.", parse_mode=ParseMode.HTML)
        return

    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders: await update.message.reply_text("Таблица лидеров пуста."); return

    leaderboard_text = "🏆 <b>Таблица Лидеров Группы</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]
    # Fetch mentions concurrently
    mentions = await asyncio.gather(*(get_user_mention(context, l['user_id']) for l in leaders))

    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i+1}.</b>"
        name = mentions[i] # Use fetched mention
        balance_str = f"{leader['balance']:.2f}"
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> фишек\n"

    try: await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e: logger.error(f"Error sending leaderboard: {e}", exc_info=True)

# --- Blackjack Game (Private Chat Only) ---

BJ_GAME_KEY = 'blackjack_game' # Key for user_data

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat
    logger.info(f"BJ start cmd from {user.id} in chat {chat.id} ({chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Для игры в Блекджек, напишите мне /blackjack в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return

    reply_func = update.message.reply_text; delete_prev_msg_id = None
    if update.callback_query: # Started from "New Game" button in private chat
        reply_func = update.callback_query.message.reply_text
        delete_prev_msg_id = update.callback_query.message.message_id
        try: await update.callback_query.answer()
        except: pass

    # Check existing game in user_data
    user_game_data = context.user_data.get(BJ_GAME_KEY, {})
    if user_game_data.get('state') not in [None, 'game_over', 'waiting_bet']:
        await reply_func("Вы уже в игре. Завершите текущую партию."); return
    if user_game_data and delete_prev_msg_id != user_game_data.get('message_id'): # Clean previous message if needed
        try: await context.bot.delete_message(chat.id, user_game_data.get('message_id'))
        except: pass

    balance = get_balance(user.id)
    if balance is None or balance <= 0: await reply_func(f"Баланс ({balance:.2f if balance is not None else 'N/A'}) недостаточен."); return

    bet_options = [1, 5, 10, 25, 50, 100, 250, 500]
    valid_bets = [b for b in bet_options if b <= balance]
    if not valid_bets: await reply_func(f"Баланс < мин. ставки ({min(bet_options)})."); return

    buttons = [[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in r] for r in [valid_bets[i:i+4] for i in range(0, len(valid_bets), 4)]]
    markup = InlineKeyboardMarkup(buttons)

    try:
        text = f"Ваш баланс: <b>{balance:.2f}</b>. Ставка?"
        if delete_prev_msg_id:
            try: await context.bot.delete_message(chat.id, delete_prev_msg_id)
            except: pass
            sent_message = await context.bot.send_message(chat.id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        else: sent_message = await reply_func(text, reply_markup=markup, parse_mode=ParseMode.HTML)

        # Store game state in user_data
        context.user_data[BJ_GAME_KEY] = {'state': 'waiting_bet', 'message_id': sent_message.message_id}
        logger.info(f"BJ game started for user {user.id}")
    except Exception as e: logger.error(f"BJ start error for {user.id}: {e}", exc_info=True)

async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    query = update.callback_query; user = query.from_user; chat_id = query.message.chat_id # Chat ID is User ID here
    user_game = context.user_data.get(BJ_GAME_KEY, {}) # Get game state from user_data

    if not user_game or user_game.get('state') != 'waiting_bet':
        await query.answer("Не время делать ставку.", show_alert=False); return

    balance = get_balance(user.id)
    if balance is None or bet_amount <= 0 or bet_amount > balance: await query.answer("Неверная ставка/баланс.", show_alert=True); return
    if update_balance(user.id, -bet_amount) is None: await query.answer("Ошибка списания.", show_alert=True); return

    deck = create_deck(); p_hand, d_hand = [], []; dealt = 0
    try:
        for _ in range(2):
            cp, sp = _get_next_item(deck, dealt, NUM_DECKS); p_hand.append(cp); dealt += 1
            cd, sd = _get_next_item(deck, dealt, NUM_DECKS); d_hand.append(cd); dealt += 1
            if sp < 0 or sd < 0: raise ValueError("Draw fail")
    except Exception as e:
        logger.error(f"BJ deal error {user.id}: {e}"); update_balance(user.id, bet_amount)
        await query.edit_message_text(f"Ошибка раздачи ({e}). Ставка возвращена."); context.user_data.pop(BJ_GAME_KEY, None); return

    p_val, d_val = get_hand_value(p_hand), get_hand_value(d_hand)
    p_bj = (p_val == 21 and len(p_hand) == 2); d_bj = (d_val == 21 and len(d_hand) == 2)
    outcome, state, p_status = None, 'player_turn', 'active'

    if p_bj:
        p_status = 'blackjack'
        if d_bj: outcome = f"⚖️ Ничья! БЖ у обоих."; update_balance(user.id, bet_amount); state = 'game_over'
        else: w = bet_amount * BLACKJACK_PAYOUT; update_balance(user.id, bet_amount + w); outcome = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {w:.2f} F!"; state = 'game_over'
    elif d_bj: outcome = f"😥 У дилера Блекджек!"; state = 'game_over'

    # Update user_data with game details
    user_game.update({
        'state': state, 'deck': deck, 'cards_dealt': dealt,
        'player_hands': [{'hand': p_hand, 'bet': bet_amount, 'status': p_status, 'can_double': (not p_bj and not d_bj), 'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': d_hand,
        'initial_bet': bet_amount, 'split_count': 0, 'outcome_text': outcome
    })
    await blackjack_show_state(context, user.id, query.message.message_id) # Pass user_id
    try: await query.answer(f"Ставка {bet_amount} F!")
    except: pass

async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int):
    # Get game state from user_data using user_id
    user_game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not user_game: logger.warning(f"BJ show_state: No game found for user {user_id}"); return

    balance = get_balance(user_id); balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    d_hand, p_hands = user_game.get('dealer_hand', []), user_game.get('player_hands', [])
    cur_idx, status = user_game.get('current_hand_index', -1), user_game.get('state', '?')

    hide_dealer = (status == 'player_turn') and not (get_hand_value(d_hand) == 21 and len(d_hand) == 2)

    text = f"<b>Блекджек</b> | Ваш баланс: <b>{balance_str}</b> F\n" # Simplified header for private chat
    total_bet = sum(h.get('bet', 0) for h in p_hands if isinstance(h, dict)); num_h = len(p_hands)
    text += f"Общая ставка: <b>{total_bet}</b> F{' ({num_h} руки)' * (num_h > 1)}\n"
    text += "--------------------\n"
    d_val = get_hand_value(d_hand)
    d_val_str = "??" if not d_hand else (str(d_val) if not hide_dealer else f"{get_card_value(d_hand[0])}+?")
    text += f"<b>Дилер:</b> {format_hand(d_hand, hide_one=hide_dealer)} ({d_val_str})\n\n"
    text += f"<b>Вы:</b>\n" # Simplified player header

    active_h_data = None
    for i, h_data in enumerate(p_hands):
        if not isinstance(h_data, dict): continue
        hand, h_val, h_stat, h_bet = h_data.get('hand', []), get_hand_value(h_data.get('hand', [])), h_data.get('status', '?'), h_data.get('bet', 0)
        is_cur = (i == cur_idx and h_stat == 'active' and status == 'player_turn')
        ind = "▶️" if is_cur else ("✅" if h_stat == 'stand' else ("❌" if h_stat == 'bust' else ("💰" if h_stat == 'blackjack' else "▫️")))
        text += f"{ind} Рука {i+1}: {format_hand(hand)} ({h_val}) [<i>{h_bet}</i> F]"
        if h_stat == 'bust': text += " - <b>Перебор!</b>"
        elif h_stat == 'blackjack': text += " - <b>Блекджек!</b>"
        elif h_stat == 'stand' and not is_cur: text += " - <i>Стоп</i>"
        text += "\n"
        if is_cur: active_h_data = h_data

    keyboard = []
    if active_h_data and status == 'player_turn':
        p_hand, p_bet = active_h_data.get('hand', []), active_h_data.get('bet', 0)
        can_double = (active_h_data.get('can_double', False) and len(p_hand) == 2 and balance is not None and balance >= p_bet)
        can_split = (len(p_hand) == 2 and p_hand[0] and p_hand[1] and get_card_value(p_hand[0]) == get_card_value(p_hand[1]) and balance is not None and balance >= p_bet and user_game.get('split_count', 0) < MAX_SPLITS)
        active_h_data['can_split'] = can_split

        keyboard.append([InlineKeyboardButton("Еще", callback_data=f"bj_hit_{cur_idx}"), InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{cur_idx}")])
        specials = [btn for cond, btn in [(can_double, InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{cur_idx}")), (can_split, InlineKeyboardButton("Разделить", callback_data=f"bj_split_{cur_idx}"))] if cond]
        if specials: keyboard.append(specials)

    elif status == 'game_over':
        text += "\n<b>Игра завершена!</b> 🎉\n" + user_game.get('outcome_text', "") + "\n"
        fin_bal = get_balance(user_id)
        text += f"\nВаш итоговый баланс: <b>{fin_bal:.2f}</b> F." if fin_bal is not None else ""
        keyboard.append([InlineKeyboardButton("🔄 Новая Игра", callback_data="bj_new_game")])

    elif status == 'dealer_turn': text += "\n<i>Ход дилера...</i>"

    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try: await context.bot.edit_message_text(user_id, message_id, text, reply_markup=markup, parse_mode=ParseMode.HTML) # Use user_id as chat_id
    except BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Edit BJ state failed {message_id} user {user_id}: {e}")
    except Exception as e: logger.error(f"Show BJ state error user {user_id}: {e}", exc_info=True)

async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action_parts: list):
    query = update.callback_query; user = query.from_user; user_id = user.id
    user_game = context.user_data.get(BJ_GAME_KEY, {}) # Get game state from user_data
    action, hand_idx_str = action_parts[0], action_parts[1]
    try: hand_idx = int(hand_idx_str)
    except ValueError: return

    # Basic state checks
    if not user_game or user_game.get('state') != 'player_turn': return
    p_hands = user_game.get('player_hands', [])
    if not (0 <= hand_idx < len(p_hands)) or hand_idx != user_game.get('current_hand_index'):
        await query.answer("Ход другой руки.", show_alert=False); return
    h_data = p_hands[hand_idx]
    if not isinstance(h_data, dict) or h_data.get('status') != 'active':
        await query.answer("Действие неактуально.", show_alert=False); return

    # --- Action Logic (mostly unchanged, uses user_game) ---
    hand, deck = h_data.get('hand', []), user_game.get('deck', [])
    balance, bet = get_balance(user.id), h_data.get('bet', 0)
    dealt = user_game.get('cards_dealt', 0)

    needs_update = False
    try:
        if action == 'hit':
            card, sc = _get_next_item(deck, dealt, NUM_DECKS)
            if sc == 0 and card:
                hand.append(card); user_game['cards_dealt'] += 1; h_data['can_double']=False; h_data['can_split']=False
                h_val = get_hand_value(hand); await query.answer(f"Карта: {card[0]}{card[1]}")
                if h_val > 21: h_data['status']='bust'; await blackjack_next_action(context, user_id) # Pass user_id
                elif h_val == 21: h_data['status']='stand'; await blackjack_next_action(context, user_id) # Pass user_id
                else: needs_update = True
            else: raise IndexError("Draw fail")

        elif action == 'stand':
            h_data['status']='stand'; await query.answer("Стоп."); await blackjack_next_action(context, user_id) # Pass user_id

        elif action == 'double':
            can = (h_data.get('can_double', False) and len(hand) == 2 and balance is not None and balance >= bet)
            if can and update_balance(user.id, -bet) is not None:
                h_data['bet'] += bet; h_data['can_double']=False; h_data['can_split']=False
                card, sc = _get_next_item(deck, user_game['cards_dealt'], NUM_DECKS)
                if sc == 0 and card:
                    hand.append(card); user_game['cards_dealt'] += 1; h_val = get_hand_value(hand)
                    h_data['status'] = 'bust' if h_val > 21 else 'stand'
                    await query.answer(f"Удвоено! Карта: {card[0]}{card[1]}. Итог: {h_val}{' (Перебор!)'*(h_val > 21)}")
                else: h_data['status'] = 'stand'; await query.answer("Удвоено! Не взята карта.", show_alert=True)
                await blackjack_next_action(context, user_id) # Pass user_id
            else: await query.answer("Удвоение невозможно / ошибка.", show_alert=True)

        elif action == 'split':
             can = h_data.get('can_split', False)
             if can and balance is not None and balance >= bet and update_balance(user.id, -bet) is not None:
                 user_game['split_count'] += 1; card_moved = hand.pop()
                 new_h = {'hand': [card_moved], 'bet': bet, 'status': 'active', 'can_double': False, 'can_split': False}
                 p_hands.insert(hand_idx + 1, new_h)
                 cards = [];
                 for _ in range(2): c, sc = _get_next_item(deck, user_game['cards_dealt'], NUM_DECKS); cards.append(c if sc==0 else None)
                 if cards[0]: hand.append(cards[0]); user_game['cards_dealt'] += 1
                 if cards[1]: new_h['hand'].append(cards[1]); user_game['cards_dealt'] += 1

                 is_ace = get_card_value(hand[0]) == 11
                 if is_ace:
                     h_data['status'] = new_h['status'] = 'stand'; h_data['can_double'] = new_h['can_double'] = False
                     await query.answer("Тузы разделены."); await blackjack_next_action(context, user_id) # Pass user_id
                 else:
                     h_data['can_double'] = new_h['can_double'] = (len(hand) == 2)
                     limit_ok = user_game['split_count'] < MAX_SPLITS
                     h_data['can_split'] = (len(hand)==2 and hand[0] and hand[1] and get_card_value(hand[0])==get_card_value(hand[1]) and limit_ok)
                     new_h['can_split'] = (len(new_h['hand'])==2 and new_h['hand'][0] and new_h['hand'][1] and get_card_value(new_h['hand'][0])==get_card_value(new_h['hand'][1]) and limit_ok)
                     if get_hand_value(hand) == 21: h_data['status']='stand'
                     if get_hand_value(new_h['hand']) == 21: new_h['status']='stand'
                     await query.answer("Рука разделена!"); needs_update = True
             else: await query.answer("Разделение невозможно / ошибка.", show_alert=True)

        if needs_update: await blackjack_show_state(context, user_id, query.message.message_id) # Pass user_id

    except IndexError:
         logger.warning(f"BJ action '{action}' failed {user.id} - Deck empty?")
         h_data['status'] = 'stand'; await query.answer("Не взять карту! Рука 'Стоп'.", show_alert=True)
         await blackjack_next_action(context, user_id) # Pass user_id
    except Exception as e: logger.error(f"BJ action '{action}' error {user.id}: {e}", exc_info=True); await query.answer("Ошибка хода.", show_alert=True)

async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, user_id: int): # Takes user_id
    user_game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY) # Access user_data via application
    if not user_game or user_game.get('state') != 'player_turn': return

    p_hands = user_game.get('player_hands', []); cur_idx = user_game.get('current_hand_index', -1)
    next_idx = next((i for i, h in enumerate(p_hands[cur_idx+1:], start=cur_idx+1) if isinstance(h, dict) and h.get('status') == 'active'), -1)

    if next_idx != -1: # Found next hand
        user_game['current_hand_index'] = next_idx
        await blackjack_show_state(context, user_id, user_game['message_id']) # Pass user_id
    else: # No more active hands -> dealer turn
        user_game['state'] = 'dealer_turn'
        await blackjack_show_state(context, user_id, user_game['message_id']) # Pass user_id
        context.job_queue.run_once(blackjack_dealer_turn_job, DEALER_TURN_DELAY, data=user_id, name=f"dealer_{user_id}") # Pass user_id in data

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data # Get user_id from job data
    user_game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY) # Access user_data
    logger.info(f"BJ Dealer job started for user {user_id}")

    if not user_game or user_game.get('state') != 'dealer_turn': return

    deck, d_hand, p_hands = user_game.get('deck', []), user_game.get('dealer_hand', []), user_game.get('player_hands', [])
    dealt = user_game.get('cards_dealt', 0)
    can_win = any(isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] for h in p_hands)
    d_bj = (get_hand_value(d_hand) == 21 and len(d_hand) == 2)

    if not can_win and not d_bj: await blackjack_determine_outcome(context, user_id, d_bj); return # Pass user_id

    while True:
        d_val = get_hand_value(d_hand); aces = sum(1 for c in d_hand if c and c[0]=='A')
        soft = aces > 0 and (d_val - aces * 11) < 11
        if d_val > 17 or (d_val == 17 and not (soft and DEALER_HITS_SOFT_17)): break
        try:
            card, sc = _get_next_item(deck, dealt, NUM_DECKS)
            if sc == 0 and card: d_hand.append(card); user_game['cards_dealt'] += 1; dealt = user_game['cards_dealt']
            else: raise IndexError("Dealer draw fail")
        except IndexError: logger.warning(f"BJ Dealer {user_id} stopped - deck empty?"); break

    await blackjack_determine_outcome(context, user_id, d_bj) # Pass user_id

async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, user_id: int, dealer_had_bj: bool): # Takes user_id
    user_game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY) # Access user_data
    if not user_game: return
    logger.info(f"BJ Determining outcome for user {user_id}")

    if user_game.get('state') == 'game_over' and 'outcome_determined' in user_game:
        await blackjack_show_state(context, user_id, user_game['message_id']); return # Pass user_id

    p_hands, d_hand = user_game.get('player_hands', []), user_game.get('dealer_hand', [])
    d_val = get_hand_value(d_hand); d_bust = d_val > 21
    outcomes, total_winnings, total_bet = [], 0, 0
    for i, h_data in enumerate(p_hands):
        if not isinstance(h_data, dict): continue
        hand, bet, status = h_data.get('hand', []), h_data.get('bet', 0), h_data.get('status')
        p_val = get_hand_value(hand); p_bj = (status == 'blackjack')
        total_bet += bet; multiplier = 0; outcome = ""; prefix = f"Рука {i+1}: " if len(p_hands) > 1 else ""

        if status == 'bust': outcome = f"{prefix}Перебор ({p_val}). Проигрыш {bet} F."
        elif p_bj: outcome = f"{prefix}Блекджек! " + (f"Ничья." if dealer_had_bj else f"Выигрыш {(bet*BLACKJACK_PAYOUT):.2f} F."); multiplier = 1 if dealer_had_bj else 1 + BLACKJACK_PAYOUT
        elif dealer_had_bj: outcome = f"{prefix}У дилера Блекджек. Проигрыш {bet} F."
        elif d_bust: outcome = f"{prefix}Дилер перебор ({d_val})! Выигрыш {bet} F."; multiplier = 2
        elif p_val > d_val: outcome = f"{prefix}{p_val} > {d_val}. Выигрыш {bet} F."; multiplier = 2
        elif p_val == d_val: outcome = f"{prefix}{p_val} = {d_val}. Ничья."; multiplier = 1
        else: outcome = f"{prefix}{p_val} < {d_val}. Проигрыш {bet} F."
        outcomes.append(outcome); total_winnings += bet * multiplier

    net_change = total_winnings - total_bet
    if total_winnings > 0 and update_balance(user_id, total_winnings) is None:
        logger.error(f"CRITICAL: Balance update failed {user_id} after BJ!"); outcomes.append("\n<b>ОШИБКА НАЧИСЛЕНИЯ!</b>"); net_change = -total_bet

    user_game['state'] = 'game_over'
    user_game['outcome_text'] = "\n".join(outcomes) + f"\n\n<b>Ваш итог: {net_change:+.2f} F</b>" # Changed outcome text slightly
    user_game['outcome_determined'] = True
    await blackjack_show_state(context, user_id, user_game.get('message_id')) # Pass user_id

# --- General Handlers ---

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data; user = query.from_user
    if not data: return
    logger.debug(f"Callback: '{data}' from {user.id}")

    parts = data.split("_", 2); prefix = parts[0]; action_data = parts[1:]
    try:
        if prefix == "bj":
            # Blackjack actions always relate to the user's private game state
            action = action_data[0]; payload = action_data[1] if len(action_data) > 1 else None
            if action == "bet": await blackjack_handle_bet(update, context, int(payload))
            elif action == "new": await blackjack_start_command(update, context) # Restart in private chat
            elif action in ["hit", "stand", "double", "split"]: await blackjack_handle_action(update, context, [action, payload])
            else: logger.warning(f"Unknown BJ action: {action}"); await query.answer()
        # --- Add other game prefixes here (roulette, etc.) ---
        # elif prefix == "rl": ...
        # ------------------------------------------------------
        else: logger.warning(f"Unknown callback prefix: {prefix}"); await query.answer()
    except Exception as e:
        logger.error(f"Error processing callback '{data}': {e}", exc_info=True)
        try: await query.answer("Произошла ошибка.", show_alert=True)
        except: pass

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, Conflict): logger.critical("Conflict error! Multiple instances?")
    elif isinstance(context.error, BadRequest): logger.warning(f"BadRequest: {context.error}.") # Update obj might be None

# --- Main Bot Setup ---
def main():
    logger.info("Starting bot...")
    start_keep_alive()
    application = None
    try:
        # Enable persistence for user_data if needed (requires specifying a persistence class)
        # from telegram.ext import PicklePersistence
        # persistence = PicklePersistence(filepath="bot_data.pkl")
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            # .persistence(persistence) # Uncomment to enable persistence
            .build()
        )
        # user_data is now automatically handled per user by the library if persistence is off,
        # or loaded/saved if persistence is on. No need for manual init.
        application.bot_data.setdefault('user_mention_cache', {}) # bot_data is still useful for global cache

        handlers = [
            CommandHandler("start", start_command), CommandHandler("help", help_command),
            CommandHandler("balance", balance_command), CommandHandler("bonus", bonus_command),
            CommandHandler("leaderboard", leaderboard_command), CommandHandler("blackjack", blackjack_start_command),
            CallbackQueryHandler(button_callback_handler),
        ]
        application.add_handlers(handlers); application.add_error_handler(error_handler)

        logger.info("Handlers registered. Starting polling...")
        print("Bot is running. Press Ctrl+C to stop.")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Conflict as e: logger.critical(f"Startup Conflict error: {e}")
    except Exception as e: logger.critical(f"Bot runtime critical error: {e}", exc_info=True)
    finally: print("Bot stopped."); logger.info("Bot stopped.")

if __name__ == "__main__": main()