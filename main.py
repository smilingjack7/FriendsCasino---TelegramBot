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
                logger.info(f"New user: {user_id}")
                cur.execute(sql_s, (user_id,))
                data = cur.fetchone() or {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        if data and data.get('last_bonus') and not isinstance(data['last_bonus'], datetime.datetime):
            data['last_bonus'] = None
        return data
    except Exception as e:
        logger.error(f"DB (get_user) {user_id}: {e}")
        return None

def update_balance(user_id: int, change: float) -> float | None:
    sql = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (change, user_id,))
            res = cur.fetchone()
            if res:
                logger.info(f"Balance {user_id}: {change:+.2f}. New: {res[0]:.2f}")
                return res[0]
            logger.warning(f"Update balance fail {user_id}")
            return None
    except Exception as e:
        logger.error(f"DB (upd_balance) {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    d = get_or_create_user(user_id)
    return d['balance'] if d else None

def update_last_bonus_time(user_id: int, ts_utc: datetime.datetime):
    sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
    ts = ts_utc.replace(tzinfo=None)
    try:
        with get_db_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (ts, user_id))
            logger.info(f"Bonus time {user_id} -> {ts}")
    except Exception as e:
        logger.error(f"DB (upd_bonus_ts) {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    d = get_or_create_user(user_id)
    return d.get('last_bonus') if d else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT) -> list[dict]:
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    try:
        with get_db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"DB (get_leaderboard): {e}")
        return []

# --- Game Utilities ---
def create_deck(num=NUM_DECKS)->list:
    d=[(r,s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(d)
    return d
def get_card_value(c:tuple|None)->int:
    return RANK_VALUES.get(c[0],0) if c else 0
def get_hand_value(h: list) -> int:
    v=sum(get_card_value(c) for c in h if c)
    a=sum(1 for c in h if c and c[0]=='A')
    while v > 21 and a > 0:
        v -= 10
        a -= 1
    return v
def format_hand(h:list, hide_one:bool=False)->str:
    if not h: return "Пусто"
    if hide_one and len(h)>1:
        return f"[{h[0][0]}{h[0][1]}, ??]" if h[0] else "[??, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in h if c])

# --- Obfuscated Card Drawing ---
def _get_next_item(src:list, p1:int, p2:int)->tuple:
    _s_ok, _s_err = 0, -1
    n = len(src)
    if n == 0:
        logger.warning("Item source empty.")
        return None, _s_err
    try:
        i = random.randrange(n)
        itm = src.pop(i)
        return itm, _s_ok
    except Exception as e:
        logger.error(f"Item fetch error: {e}")
        return None, _s_err

# --- Helper to get User Mention (HTML) ---
async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    cache = context.bot_data.setdefault('user_mention_cache', {})
    now = time.monotonic()
    cache_ttl = 3600
    if user_id in cache and (now - cache[user_id]['ts']) < cache_ttl:
        return cache[user_id]['mention']
    try:
        user = await context.bot.get_chat(user_id)
        mention = user.mention_html()
    except Exception as e:
        logger.warning(f"Failed get mention {user_id}: {e}")
        mention = f"User {user_id}"
    cache[user_id] = {'mention': mention, 'ts': now}
    return mention

# --- Core Bot Commands ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"/start from {user.id}")
    get_or_create_user(user.id)
    balance = get_balance(user.id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\nБаланс: <b>{balance_str}</b> фишек.\n\nИграть: /blackjack (в личке)\nПомощь: /help",
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/help from {update.effective_user.id}")
    help_text = (
        "<b>ℹ️ Справка:</b>\n\n"
        "<b>Личка:</b>\n"
        "/start\n/blackjack\n/balance\n/bonus\n\n"
        "<b>Группы+Личка:</b>\n"
        "/leaderboard\n/help\n\n"
        "<i>Другие игры позже.</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/balance from {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return
    balance = get_balance(user.id)
    if balance is not None:
        await update.message.reply_text(f"Ваш баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Не удалось получить баланс.")

async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/bonus from {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return
    if not get_or_create_user(user.id):
        await update.message.reply_text("Ошибка данных.")
        return
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus = get_last_bonus_time(user.id)
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)
    if last_bonus and (now_utc < last_bonus + cooldown):
        td=last_bonus+cooldown-now_utc
        h,r=divmod(td.total_seconds(),3600)
        m,_=divmod(r,60)
        await update.message.reply_text(f"⏳ Уже получено. Осталось: {int(h)}ч {int(m)}м.")
        return
    new_b = update_balance(user.id, BONUS_AMOUNT)
    if new_b is not None:
        update_last_bonus_time(user.id, now_utc)
        await update.message.reply_text(f"🎉 +{BONUS_AMOUNT} F!\nБаланс: <b>{new_b:.2f}</b> F.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Ошибка начисления.")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"/leaderboard from {user.id} in {chat.id} ({chat.type})")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("Лидеров пока нет.")
        return
    leaderboard_text = "🏆 <b>Таблица Лидеров</b> 🏆\n\n"
    place_emojis = ["🥇", "🥈", "🥉"]
    mentions = await asyncio.gather(*(get_user_mention(context, l['user_id']) for l in leaders))
    for i, leader in enumerate(leaders):
        place = place_emojis[i] if i < len(place_emojis) else f"<b>{i+1}.</b>"
        name = mentions[i]
        balance_str = f"{leader['balance']:.2f}"
        leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"
    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)

# --- Blackjack Game (Private Chat Only) ---
BJ_GAME_KEY = 'blackjack_game'

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user
    chat=update.effective_chat
    logger.info(f"BJ start {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Играть в БЖ только в <b>личном чате</b>.", parse_mode=ParseMode.HTML)
        return

    callback_message_id=None
    source_message=update.message

    async def reply_error(text: str):
        target = source_message or (update.callback_query.message if update.callback_query else None)
        if target:
            await target.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)

    if update.callback_query:
        source_message=update.callback_query.message
        callback_message_id=source_message.message_id
        await update.callback_query.answer()

    user_game=context.user_data.get(BJ_GAME_KEY, {})
    previous_message_id = user_game.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(chat.id, previous_message_id)
        except Exception as e:
            logger.debug(f"Old BJ msg {previous_message_id} deletion failed: {e}")

    context.user_data.pop(BJ_GAME_KEY, None)
    balance=get_balance(user.id)
    if balance is None or balance <= 0:
        await reply_error(f"Баланс ({balance:.2f if balance is not None else 'N/A'}) мал.")
        return

    opts=[1,5,10,25,50,100,250,500]
    valid=[b for b in opts if b <= balance]
    if not valid:
        await reply_error(f"Баланс < мин. ставки ({min(opts)}).")
        return

    btns=[[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in r] for r in [valid[i:i+4] for i in range(0, len(valid), 4)]]
    markup=InlineKeyboardMarkup(btns)
    text=f"Баланс: <b>{balance:.2f}</b>. Ставка?"

    try:
        if callback_message_id:
            await context.bot.delete_message(chat.id, callback_message_id)
        sent = await context.bot.send_message(chat.id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        context.user_data[BJ_GAME_KEY]={'state':'waiting_bet', 'message_id':sent.message_id}
        logger.info(f"BJ game started for {user.id}, prompt msg {sent.message_id}")
    except Exception as e:
        logger.error(f"BJ start error {user.id}: {e}")
        await reply_error("Ошибка начала игры.")

async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q=update.callback_query
    u=q.from_user
    uid=u.id
    chat_id=q.message.chat_id
    game=context.user_data.get(BJ_GAME_KEY,{})
    bet_prompt_message_id = q.message.message_id

    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id:
        await q.answer("Неактуально.", show_alert=False)
        return

    bal=get_balance(uid)
    if bal is None or bet <= 0 or bet > bal:
        await q.answer("Неверная ставка/баланс.", show_alert=True)
        return

    if update_balance(uid, -bet) is None:
        await q.answer("Ошибка списания.", show_alert=True)
        return

    deck=create_deck()
    ph, dh = [], []
    dlt = 0
    try:
        for _ in range(2):
            cp,sp=_get_next_item(deck, dlt, NUM_DECKS)
            ph.append(cp)
            dlt+=1
            cd,sd=_get_next_item(deck, dlt, NUM_DECKS)
            dh.append(cd)
            dlt+=1
            assert sp==0 and sd==0 and cp and cd
    except Exception as e:
        logger.error(f"BJ deal {uid}: {e}")
        update_balance(uid, bet) # Return bet on error
        await q.edit_message_text(f"Ошибка ({e}). Ставка возвр.")
        context.user_data.pop(BJ_GAME_KEY, None)
        return

    # --- ПРОВЕРКА BJ ---
    p_val, dv = get_hand_value(ph), get_hand_value(dh)
    p_bj = (p_val == 21 and len(ph) == 2)
    d_bj = (dv == 21 and len(dh) == 2)
    out, st, ps = None, 'player_turn', 'active'

    if p_bj:
        ps = 'blackjack'
        st = 'game_over'
        if d_bj:
            out = f"⚖️ Ничья! У обоих Блекджек."
            update_balance(uid, bet) # Return bet
        else:
            w = bet * BLACKJACK_PAYOUT
            update_balance(uid, bet + w) # Return bet + winnings
            out = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {w:.2f} F!"
    elif d_bj:
        out = f"😥 У дилера Блекджек!"
        st = 'game_over'
    # --- КОНЕЦ ПРОВЕРКИ BJ ---

    game.update({
        'state': st,
        'deck': deck,
        'cards_dealt': dlt,
        'player_hands': [{
            'hand': ph,
            'bet': bet,
            'status': ps,
            'can_double': (not p_bj and not d_bj), # Can double only if neither has BJ initially
            'can_split': False # Splitting evaluated later
        }],
        'current_hand_index': 0,
        'dealer_hand': dh,
        'initial_bet': bet,
        'split_count': 0,
        'outcome_text': out # Store initial outcome (BJ/push)
    })

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e:
        logger.warning(f"Could not delete bet prompt {bet_prompt_message_id}: {e}")

    new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=False) # Send new message

    if new_message_info and isinstance(new_message_info, Message):
        game['message_id'] = new_message_info.message_id
        logger.info(f"BJ initial state sent {new_message_info.message_id} for {uid}")
    elif not new_message_info:
        logger.error(f"Failed to send initial BJ state for {uid}")
        context.user_data.pop(BJ_GAME_KEY, None) # Clean up if sending failed

    await q.answer(f"Ставка {bet} F!")


async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, edit_existing: bool = True) -> Message | int | None:
    if game_state is None:
        game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state:
        return None

    message_id_to_process = game_state.get('message_id')
    if edit_existing and not message_id_to_process:
        logger.error(f"BJ show_state: No message_id to edit for user {user_id}")
        return None

    bal=get_balance(user_id)
    bal_s = f"{bal:.2f}" if bal is not None else "N/A"
    dh, phs = game_state.get('dealer_hand', []), game_state.get('player_hands', [])
    ci, st = game_state.get('current_hand_index', -1), game_state.get('state', '?')

    # Hide dealer's second card only during player's turn AND if dealer doesn't have BJ
    hide_d = (st == 'player_turn') and not (get_hand_value(dh) == 21 and len(dh) == 2)

    txt = f"<b>Блекджек</b> | Баланс: <b>{bal_s}</b> F\n"
    tb = sum(h.get('bet', 0) for h in phs if isinstance(h, dict)) # Total bet across all hands
    nh = len(phs)
    txt += f"Ставка: <b>{tb}</b> F{' (Рук: ' + str(nh) + ')' if nh > 1 else ''}\n" + "-"*20 + "\n"

    dv = get_hand_value(dh)
    dvs = "??" if not dh else (str(dv) if not hide_d else f"{get_card_value(dh[0])}+?")
    txt += f"<b>Диллер:</b> {format_hand(dh, hide_one=hide_d)} ({dvs})\n\n<b>Вы:</b>\n"

    act_h = None # Active player hand data
    for i, h_data in enumerate(phs):
        if not isinstance(h_data, dict): continue
        h = h_data.get('hand', [])
        hv = get_hand_value(h)
        hs = h_data.get('status', '?')
        hb = h_data.get('bet', 0)
        cur = (i == ci and hs == 'active' and st == 'player_turn') # Is this the current active hand?
        ind = "▶" if cur else ("✅" if hs == 'stand' else ("❌" if hs == 'bust' else ("💰" if hs == 'blackjack' else "▫"))) # Indicator emoji
        txt += f"{ind} Рука {i+1}: {format_hand(h)} ({hv}) [<i>{hb} F</i>]"
        # Add status text if not active or stand
        status_text = {'bust': " - <b>Перебор!</b>", 'blackjack': " - <b>БЖ!</b>"}.get(hs)
        if status_text: txt += status_text
        elif hs == 'stand' and not cur: txt += " - <i>Стоп</i>" # Indicate stand for non-active hands
        txt += "\n"
        if cur: act_h = h_data # Store current active hand data

    kbd = [] # Keyboard buttons
    if act_h and st == 'player_turn':
        p_h = act_h.get('hand', [])
        p_b = act_h.get('bet', 0)
        can_d = (act_h.get('can_double', False) and len(p_h) == 2 and bal is not None and bal >= p_b)
        # Check split eligibility: 2 cards, same value, enough balance, under split limit
        can_s = (len(p_h) == 2 and p_h[0] and p_h[1] and
                 get_card_value(p_h[0]) == get_card_value(p_h[1]) and
                 bal is not None and bal >= p_b and
                 game_state.get('split_count', 0) < MAX_SPLITS)
        act_h['can_split'] = can_s # Update game state with split possibility

        kbd.append([
            InlineKeyboardButton("Еще", callback_data=f"bj_hit_{ci}"),
            InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{ci}")
        ])
        # Add Double/Split buttons if applicable
        spc_btns = [btn for condition, btn in [
            (can_d, InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{ci}")),
            (can_s, InlineKeyboardButton("Разделить", callback_data=f"bj_split_{ci}"))
        ] if condition]
        if spc_btns: kbd.append(spc_btns)

    elif st == 'game_over':
        txt += f"\n<b>Игра окончена!</b> 🎉\n{game_state.get('outcome_text','')}\n"
        fin_b = get_balance(user_id)
        txt += f"\nИтоговый баланс: <b>{fin_b:.2f}</b> F." if fin_b is not None else ""
        kbd.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
    elif st == 'dealer_turn':
        txt += "\n<i>Ход дилера...</i>"

    mrk=InlineKeyboardMarkup(kbd) if kbd else None
    result: Message | int | None = None

    try:
        if edit_existing and message_id_to_process:
             logger.debug(f"Attempting edit msg {message_id_to_process} chat {chat_id} user {user_id}")
             await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id_to_process, text=txt, reply_markup=mrk, parse_mode=ParseMode.HTML)
             logger.debug(f"Edited BJ state msg {message_id_to_process} for user {user_id}")
             result = message_id_to_process # Indicate success by returning message ID
        else: # Send new message
             logger.debug(f"Attempting send new for user {user_id}")
             if message_id_to_process: # Try to delete old one if sending new
                 try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id_to_process)
                 except Exception as e: logger.debug(f"Failed delete previous msg {message_id_to_process}: {e}")
             new_message = await context.bot.send_message(chat_id, txt, reply_markup=mrk, parse_mode=ParseMode.HTML)
             game_state['message_id'] = new_message.message_id # IMPORTANT: Update message ID in game state
             logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}")
             result = new_message # Return the new message object
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            result = message_id_to_process # Not an error, just no change needed
        elif "message to edit not found" in str(e).lower() or "chat not found" in str(e).lower():
             logger.error(f"CRITICAL: Msg {message_id_to_process}/Chat {chat_id} not found for user {user_id}. Cleaning game.")
             # Clean up game state if the message/chat is gone
             context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
             result = None
        else:
            logger.warning(f"Edit/Send BJ fail msg {message_id_to_process} chat {chat_id} (user {user_id}): {e}")
            result = None # Indicate failure
    except Exception as e:
        logger.error(f"Show BJ state generic error chat {chat_id} user {user_id}: {e}", exc_info=True)
        result = None # Indicate failure

    if isinstance(result, Message): return result # Return Message object if new message sent
    elif isinstance(result, int): return result   # Return message ID if edited existing
    else: return None                           # Return None on failure


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.user_data.get(BJ_GAME_KEY, {})

    act, h_idx_s = parts[0], parts[1]
    try:
        h_idx = int(h_idx_s)
    except (ValueError, TypeError):
        logger.warning(f"Invalid hand index received: {h_idx_s}")
        await q.answer("Ошибка индекса.", show_alert=True)
        return

    action_message_id = q.message.message_id
    # Validate game state and message ID
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != action_message_id:
        await q.answer("Неактуально.", show_alert=False)
        return

    phs = game.get('player_hands', [])
    # Validate hand index and current turn
    if not (0 <= h_idx < len(phs)) or h_idx != game.get('current_hand_index', -1):
        await q.answer("Ход другой руки.", show_alert=False)
        return

    hd = phs[h_idx]  # Current hand data
    # Validate hand status
    if not isinstance(hd, dict) or hd.get('status') != 'active':
        await q.answer("Эта рука неактивна.", show_alert=False)
        return

    h = hd.get('hand', [])
    dk = game.get('deck', [])
    bal = get_balance(uid)
    b = hd.get('bet', 0)
    dlt = game.get('cards_dealt', 0)
    # needs_edit flag is removed, call show_state explicitly

    try:  # Main action block
        if act == 'hit':
            c, sc = _get_next_item(dk, dlt, NUM_DECKS)
            if sc == 0 and c:
                h.append(c)
                game['cards_dealt'] += 1
                hd['can_double'] = False  # Cannot double/split after hit
                hd['can_split'] = False
                hv = get_hand_value(h)
                await q.answer(f"Взяли: {c[0]}{c[1]}")

                # Check hand value AFTER hitting
                if hv > 21:
                    hd['status'] = 'bust'
                    # Update message IMMEDIATELY to show bust
                    await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                    await blackjack_next_action(context, chat_id, uid) # Move to next action
                elif hv == 21:
                    hd['status'] = 'stand' # Auto-stand on 21
                     # Update message IMMEDIATELY to show stand/21
                    await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                    await blackjack_next_action(context, chat_id, uid) # Move to next action
                else: # Hand value < 21, turn continues for this hand
                    # Update message IMMEDIATELY to show new card and value
                    await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                    # Do NOT call next_action here, player can hit again
            else:
                # Failed to draw a card
                raise IndexError("Draw fail")

        elif act == 'stand':
            hd['status'] = 'stand'
            await q.answer("Стоп.")
            # Update message IMMEDIATELY to show stand status
            await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
            await blackjack_next_action(context, chat_id, uid) # Move to next action

        elif act == 'double':
            can_double = (hd.get('can_double', False) and len(h) == 2 and bal is not None and bal >= b)
            if can_double:
                if update_balance(uid, -b) is not None:  # Try to deduct bet
                    hd['bet'] += b  # Double the bet on this hand
                    hd['can_double'] = False  # Can't double again
                    hd['can_split'] = False
                    c, sc = _get_next_item(dk, game['cards_dealt'], NUM_DECKS)
                    drawn_card_str = ""
                    if sc == 0 and c:
                        h.append(c)
                        game['cards_dealt'] += 1
                        hv = get_hand_value(h)
                        hd['status'] = 'bust' if hv > 21 else 'stand' # Hand automatically stands or busts
                        drawn_card_str = f" Карта: {c[0]}{c[1]}. Итог: {hv}{' (Перебор!)' if hv > 21 else ''}"
                    else:  # Failed to draw card after doubling bet
                        hd['status'] = 'stand' # Stand with original 2 cards
                        drawn_card_str = " Ошибка взятия карты."
                        await q.answer(f"Удвоено!{drawn_card_str}", show_alert=True if "Ошибка" in drawn_card_str else False)
                    # Update message IMMEDIATELY to show doubled hand/status
                    await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                    await blackjack_next_action(context, chat_id, uid) # Move to next action
                else:
                    await q.answer("Ошибка списания для удвоения.", show_alert=True)
            else:
                await q.answer("Нельзя удвоить.", show_alert=True)

        elif act == 'split':
             can_split = hd.get('can_split', False)
             if can_split and bal is not None and bal >= b:
                 if update_balance(uid, -b) is not None: # Deduct bet for the new hand
                     game['split_count'] += 1
                     card_to_move = h.pop()
                     new_hand_data = {'hand': [card_to_move], 'bet': b, 'status': 'active', 'can_double': False, 'can_split': False}
                     phs.insert(h_idx + 1, new_hand_data)

                     cards_drawn = []
                     for _ in range(2):
                         c, sc = _get_next_item(dk, game['cards_dealt'], NUM_DECKS)
                         cards_drawn.append(c if sc == 0 else None)
                         if c: game['cards_dealt'] += 1

                     if cards_drawn[0]: h.append(cards_drawn[0])
                     if cards_drawn[1]: new_hand_data['hand'].append(cards_drawn[1])

                     is_ace_split = get_card_value(h[0]) == 11
                     if is_ace_split:
                         hd['status'] = 'stand'
                         new_hand_data['status'] = 'stand'
                         hd['can_double'] = False
                         new_hand_data['can_double'] = False
                         await q.answer("Тузы разделены и стоят.")
                         # Update message IMMEDIATELY to show split hands (standing)
                         await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                         await blackjack_next_action(context, chat_id, uid)
                     else:
                         # Check if hands are 21 after split and deal
                         if get_hand_value(h) == 21: hd['status'] = 'stand'
                         if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                         hd['can_double'] = (len(h) == 2)
                         new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)

                         limit_ok = game['split_count'] < MAX_SPLITS
                         h_can_resplit = (len(h) == 2 and h[0] and h[1] and get_card_value(h[0]) == get_card_value(h[1]) and limit_ok and bal >= hd['bet']) # Check balance for resplit too
                         nh_can_resplit = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and bal >= new_hand_data['bet'])
                         hd['can_split'] = h_can_resplit
                         new_hand_data['can_split'] = nh_can_resplit

                         await q.answer("Рука разделена!")
                         # Update message IMMEDIATELY to show the two new hands
                         await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
                         # Do NOT call next_action, turn continues on the first split hand (h_idx)
                 else:
                     await q.answer("Ошибка списания для разделения.", show_alert=True)
             else:
                await q.answer("Нельзя разделить.", show_alert=True)

    except IndexError as e: # Catch the "Draw fail" specifically
        logger.warning(f"BJ action '{act}' u {uid} failed draw: {e}")
        hd['status']='stand' # Force stand if card draw fails? Or just error out? Let's force stand for now.
        await q.answer("Не удалось взять карту! Ход завершен.", show_alert=True)
        # Update message to show the forced stand?
        await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        await blackjack_next_action(context, chat_id, uid) # Proceed as if stood
    except Exception as e:
        logger.error(f"BJ action '{act}' u {uid} error: {e}", exc_info=True)
        await q.answer("Произошла ошибка.", show_alert=True)
        # Optionally update state or clean up game here if error is severe

    # The final 'if needs_edit:' check is removed as updates are handled within each action block.

async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'player_turn':
        return # Game ended or not player's turn

    phs=game.get('player_hands', [])
    ci=game.get('current_hand_index', -1)

    # Find the index of the next hand that is still 'active'
    next_active_hand_index = -1
    for i in range(ci + 1, len(phs)):
        if isinstance(phs[i], dict) and phs[i].get('status') == 'active':
            next_active_hand_index = i
            break

    message_id = game.get('message_id')
    if not message_id:
        logger.error(f"BJ next_action: No message_id for user {user_id}")
        # Attempt to recover or clean up? For now, just return.
        return

    if next_active_hand_index != -1:
        # Found another active hand, switch to it
        game['current_hand_index'] = next_active_hand_index
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True) # Edit message for new hand
    else:
        # No more active player hands, move to dealer's turn
        game['state'] = 'dealer_turn'
        # Update the message to show "Dealer's turn..." and hide buttons
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
        # Schedule the dealer's turn logic
        context.job_queue.run_once(
            blackjack_dealer_turn_job,
            DEALER_TURN_DELAY,
            data={'chat_id': chat_id, 'user_id': user_id},
            name=f"dealer_{user_id}"
        )

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    if not user_id or not chat_id:
        logger.error(f"BJ Dealer job missing IDs: {job_data}")
        return

    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    # Check if game still exists and is in the correct state
    if not game or game.get('state') != 'dealer_turn':
        logger.info(f"BJ Dealer job u {user_id}: Game ended or state changed prematurely.")
        return

    dk = game.get('deck',[])
    dh = game.get('dealer_hand',[])
    phs = game.get('player_hands',[])
    dlt = game.get('cards_dealt', 0)

    # Check if any player hand can potentially win (not bust or already blackjack)
    # No need for dealer to hit if all player hands are bust/blackjack already (unless dealer has BJ too for pushes)
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack']
        for h in phs
    )
    dealer_has_blackjack = (get_hand_value(dh) == 21 and len(dh) == 2)

    # If no player can win and dealer doesn't have blackjack (meaning all players busted), dealer stands immediately.
    if not player_can_win and not dealer_has_blackjack:
        logger.info(f"BJ Dealer u {user_id}: All players busted/BJ, dealer stands.")
        await blackjack_determine_outcome(context, chat_id, user_id, dealer_has_blackjack)
        return

    # Dealer hits according to rules
    dealer_stood = False
    while not dealer_stood:
        dv = get_hand_value(dh)
        num_aces = sum(1 for c in dh if c and c[0] == 'A')
        is_soft = num_aces > 0 and (dv - num_aces * 11) < 11 # Check if Ace counts as 11 without busting

        # Dealer stand condition
        if dv > 17 or (dv == 17 and not (is_soft and DEALER_HITS_SOFT_17)):
            logger.info(f"BJ Dealer u {user_id} stands on {dv}{' (soft)' if is_soft and dv==17 else ''}.")
            dealer_stood = True
            break # Exit the while loop

        # Dealer hits
        logger.info(f"BJ Dealer u {user_id} hits on {dv}{' (soft)' if is_soft else ''}.")
        try:
            c, sc = _get_next_item(dk, dlt, NUM_DECKS)
            if sc == 0 and c:
                dh.append(c)
                game['cards_dealt'] += 1
                dlt = game['cards_dealt'] # Update local count for next potential draw
                # Update message briefly showing dealer took a card? (Optional, adds complexity)
                # await blackjack_show_state(...) # Might cause rate limits if dealer hits fast
                await asyncio.sleep(DEALER_TURN_DELAY) # Small delay between dealer hits
            else:
                # Failed to draw card
                raise IndexError("Dealer draw failed (status error or None card)")
        except IndexError:
            logger.warning(f"BJ Dealer u {user_id} stopped hitting - deck empty or draw error.")
            dealer_stood = True # Stop hitting if card cannot be drawn
            break # Exit the while loop

    # After dealer finishes or loop breaks, determine outcome
    await blackjack_determine_outcome(context, chat_id, user_id, dealer_has_blackjack)

async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game:
        logger.warning(f"BJ outcome u {user_id}: Game data not found.")
        return

    message_id = game.get('message_id')
    if not message_id:
        logger.error(f"BJ outcome u {user_id}: No message_id found.")
        return # Cannot update status without message ID

    if game.get('state') == 'game_over' and game.get('outcome_determined'):
        logger.info(f"BJ outcome u {user_id}: Already determined.")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
        return

    phs = game.get('player_hands', [])
    dh = game.get('dealer_hand', [])
    dv = get_hand_value(dh)
    db = dv > 21 # Dealer busted
    outcome_lines = []
    total_winnings = 0
    total_bet = 0

    for i, hd in enumerate(phs):
        if not isinstance(hd, dict): continue

        h = hd.get('hand', [])
        b = hd.get('bet', 0)
        st = hd.get('status')
        pv = get_hand_value(h)
        p_bj = (st == 'blackjack')
        total_bet += b

        payout_multiplier = 0
        outcome_str = ""
        prefix = f"Рука {i+1}: " if len(phs) > 1 else ""

        if st == 'bust':
            outcome_str = f"{prefix}Перебор ({pv}). Ставка проиграна (-{b} F)."
            payout_multiplier = 0
        elif p_bj:
            if d_had_bj:
                outcome_str = f"{prefix}Блекджек! Ничья с дилером."
                payout_multiplier = 1 # Push
            else:
                win_amount = b * BLACKJACK_PAYOUT
                outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."
                payout_multiplier = 1 + BLACKJACK_PAYOUT
        elif d_had_bj:
            outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{b} F)."
            payout_multiplier = 0
        elif db:
            outcome_str = f"{prefix}У дилера перебор ({dv})! Выигрыш +{b} F."
            payout_multiplier = 2
        elif pv > dv:
            # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
            outcome_str = f"{prefix}Вы выиграли ({pv} > {dv}). Выигрыш +{b} F."
            payout_multiplier = 2
        elif pv == dv:
            outcome_str = f"{prefix}Ничья ({pv} = {dv}). Ставка возвращена."
            payout_multiplier = 1 # Push
        else: # pv < dv
            # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
            outcome_str = f"{prefix}Вы проиграли ({pv} < {dv}). Ставка проиграна (-{b} F)."
            payout_multiplier = 0

        outcome_lines.append(outcome_str)
        total_winnings += b * payout_multiplier

    net_change = total_winnings - total_bet

    if total_winnings > 0:
        current_balance_before_update = get_balance(user_id) # Get balance before potential update
        if update_balance(user_id, total_winnings) is None:
            outcome_lines.append("\n<b>ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША!</b>")
            # If update fails, the net change IS the loss of the bets placed earlier
            # We don't need to adjust net_change here as it was already calculated based on bets.
            # The total_winnings just weren't added back.
            logger.error(f"BJ outcome u {user_id}: Failed to update balance with winnings {total_winnings}. Initial bet was {total_bet}. Balance before attempt: {current_balance_before_update}")
        else:
             logger.info(f"BJ outcome u {user_id}: Balance updated by adding {total_winnings:.2f}. Net change for round: {net_change:+.2f}")

    game['state'] = 'game_over'
    # It's safer to escape the final net_change string too, just in case
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True

    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
    logger.info(f"BJ game state cleaned for user {user_id}")
# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    data=q.data
    u=q.from_user
    if not data:
        return

    logger.debug(f"Callback query received: '{data}' from user {u.id}")
    parts=data.split("_", 2) # Split max 2 times (e.g., bj_action_payload)
    prefix=parts[0]
    payload=parts[1:] # List containing action and potentially arguments

    try:
        if prefix=="bj":
            if update.effective_chat.type != ChatType.PRIVATE:
                await q.answer("Играть в Блекджек можно только в личном чате.", show_alert=True)
                return

            action = payload[0] if payload else None
            arg = payload[1] if len(payload) > 1 else None

            if action == "bet" and arg:
                await blackjack_handle_bet(update, context, int(arg))
            elif action == "new":
                await blackjack_start_command(update, context) # Use start command logic for new game
            elif action in ["hit", "stand", "double", "split"] and arg:
                await blackjack_handle_action(update, context, [action, arg]) # Pass action and hand index
            else:
                logger.warning(f"Unknown BJ callback action/payload: {data}")
                await q.answer() # Acknowledge callback without alert
        else:
            logger.warning(f"Unknown callback prefix: {prefix}")
            await q.answer() # Acknowledge other callbacks silently

    except ValueError as e:
         logger.error(f"Callback ValueError ('{data}' u {u.id}): {e}")
         await q.answer("Ошибка данных.", show_alert=True)
    except Exception as e:
        logger.error(f"Callback general error ('{data}' u {u.id}): {e}", exc_info=True)
        await q.answer("Произошла ошибка.", show_alert=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Log specific common errors
    if isinstance(context.error, Conflict):
        logger.critical("Conflict error detected! Make sure only one instance of the bot is running.")
    elif isinstance(context.error, BadRequest):
        # Log bad requests, potentially indicating issues with message formatting, IDs, etc.
        logger.warning(f"BadRequest error: {context.error}. Update: {update}")
    # Add more specific error handling if needed
    # elif isinstance(context.error, TimedOut):
    #     logger.warning("Request timed out.")
    # elif isinstance(context.error, NetworkError):
    #     logger.warning("Network error occurred.")

    # Optionally, inform the user about the error if it's relevant and safe to do so
    # Example (use with caution):
    # if update and isinstance(update, Update) and update.effective_message:
    #     try:
    #         await update.effective_message.reply_text("Извините, произошла внутренняя ошибка.")
    #     except Exception as e:
    #         logger.error(f"Failed to send error message to user: {e}")

# --- Main Bot Setup ---
def main():
    logger.info("Starting bot application...")
    start_keep_alive() # Start the Flask keep-alive thread
    app = None
    try:
        app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

        # Initialize bot_data if needed (e.g., for caches)
        app.bot_data.setdefault('user_mention_cache', {})

        # Register command handlers
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("balance", balance_command))
        app.add_handler(CommandHandler("bonus", bonus_command))
        app.add_handler(CommandHandler("leaderboard", leaderboard_command))
        app.add_handler(CommandHandler("blackjack", blackjack_start_command)) # BJ game start

        # Register callback query handler for buttons
        app.add_handler(CallbackQueryHandler(button_callback_handler))

        # Register error handler
        app.add_error_handler(error_handler)

        logger.info("Handlers registered. Starting polling...")
        print("Bot is running...") # Console message indicates bot is active

        # Start polling
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Conflict as e:
        logger.critical(f"Could not start bot due to Conflict error: {e}. Is another instance running?")
    except ValueError as e:
         logger.critical(f"Configuration error: {e}")
    except Exception as e:
        logger.critical(f"A critical error occurred during bot runtime: {e}", exc_info=True)
    finally:
        print("Bot stopped.")
        logger.info("Bot application stopped.")

if __name__ == "__main__":
    main()