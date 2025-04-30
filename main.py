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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Message
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
def format_hand(h:list, hide_one:bool=False)->str:
    if not h: return "Пусто"
    if hide_one and len(h)>1: return f"[{h[0][0]}{h[0][1]}, ??]" if h[0] else "[??, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in h if c])

# --- Obfuscated Card Drawing ---
def _get_next_item(src:list, p1:int, p2:int)->tuple:
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
    get_or_create_user(user.id); balance = get_balance(user.id); balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    await update.message.reply_text(f"Привет, {user.first_name}! 👋\nБаланс: <b>{balance_str}</b> фишек.\n\nИграть: /blackjack (в личке)\nПомощь: /help", parse_mode=ParseMode.HTML)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/help from {update.effective_user.id}")
    help_text = ("<b>ℹ️ Справка:</b>\n\n<b>Личка:</b>\n/start\n/blackjack\n/balance\n/bonus\n\n<b>Группы+Личка:</b>\n/leaderboard\n/help\n\n<i>Другие игры позже.</i>")
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat; logger.info(f"/balance from {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE: await update.message.reply_text("Только в <b>личном чате</b>.", parse_mode=ParseMode.HTML); return
    balance = get_balance(user.id)
    if balance is not None: await update.message.reply_text(f"Ваш баланс: <b>{balance:.2f}</b> фишек.", parse_mode=ParseMode.HTML)
    else: await update.message.reply_text("Не удалось получить баланс.")
async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat; logger.info(f"/bonus from {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE: await update.message.reply_text("Только в <b>личном чате</b>.", parse_mode=ParseMode.HTML); return
    if not get_or_create_user(user.id): await update.message.reply_text("Ошибка данных."); return
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus = get_last_bonus_time(user.id); cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)
    if last_bonus and (now_utc < last_bonus + cooldown): td=last_bonus+cooldown-now_utc; h,r=divmod(td.total_seconds(),3600); m,_=divmod(r,60); await update.message.reply_text(f"⏳ Уже получено. Осталось: {int(h)}ч {int(m)}м."); return
    new_b = update_balance(user.id, BONUS_AMOUNT)
    if new_b is not None: update_last_bonus_time(user.id, now_utc); await update.message.reply_text(f"🎉 +{BONUS_AMOUNT} F!\nБаланс: <b>{new_b:.2f}</b> F.", parse_mode=ParseMode.HTML)
    else: await update.message.reply_text("Ошибка начисления.")
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; chat = update.effective_chat; logger.info(f"/leaderboard from {user.id} in {chat.id} ({chat.type})")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders: await update.message.reply_text("Лидеров пока нет."); return
    leaderboard_text = "🏆 <b>Таблица Лидеров</b> 🏆\n\n"; place_emojis = ["🥇", "🥈", "🥉"]
    mentions = await asyncio.gather(*(get_user_mention(context, l['user_id']) for l in leaders))
    for i, leader in enumerate(leaders): place = place_emojis[i] if i < len(place_emojis) else f"<b>{i+1}.</b>"; name = mentions[i]; balance_str = f"{leader['balance']:.2f}"; leaderboard_text += f"{place} {name} - <b>{balance_str}</b> F\n"
    try: await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e: logger.error(f"Error sending leaderboard: {e}", exc_info=True)

# --- Blackjack Game (Private Chat Only) ---
BJ_GAME_KEY = 'blackjack_game'
async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; chat=update.effective_chat; logger.info(f"BJ start {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE: await update.message.reply_text("Играть в БЖ только в <b>личном чате</b>.", parse_mode=ParseMode.HTML); return
    callback_message_id = None
    source_message = update.message
    async def reply_error(text: str):
        target = source_message or (update.callback_query.message if update.callback_query else None)
        if target and hasattr(target, 'reply_text'): await target.reply_text(text, parse_mode=ParseMode.HTML)
        else: await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
    if update.callback_query: source_message = update.callback_query.message; callback_message_id = source_message.message_id; await update.callback_query.answer()
    user_game=context.user_data.get(BJ_GAME_KEY, {}); previous_message_id = user_game.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try: await context.bot.delete_message(chat.id, previous_message_id)
        except Exception as e: logger.debug(f"Old BJ msg {previous_message_id} deletion failed: {e}")
    context.user_data.pop(BJ_GAME_KEY, None)
    balance=get_balance(user.id)
    if balance is None or balance <= 0: await reply_error(f"Баланс ({balance:.2f if balance is not None else 'N/A'}) мал."); return
    opts=[1,5,10,25,50,100,250,500]; valid=[b for b in opts if b <= balance]
    if not valid: await reply_error(f"Баланс < мин. ставки ({min(opts)})."); return
    btns=[[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in r] for r in [valid[i:i+4] for i in range(0, len(valid), 4)]]
    markup=InlineKeyboardMarkup(btns); text=f"Баланс: <b>{balance:.2f}</b>. Ставка?"
    try:
        if callback_message_id: await context.bot.delete_message(chat.id, callback_message_id)
        sent = await context.bot.send_message(chat.id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        context.user_data[BJ_GAME_KEY]={'state':'waiting_bet', 'message_id':sent.message_id}; logger.info(f"BJ game started for {user.id}, prompt msg {sent.message_id}")
    except Exception as e: logger.error(f"BJ start error {user.id}: {e}"); await reply_error("Ошибка начала игры.")

async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q=update.callback_query; u=q.from_user; uid=u.id; chat_id=q.message.chat_id
    game=context.user_data.get(BJ_GAME_KEY,{})
    bet_prompt_message_id = q.message.message_id
    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id: await q.answer("Неактуально.", show_alert=False); return
    bal=get_balance(uid)
    if bal is None or bet <= 0 or bet > bal: await q.answer("Неверная ставка/баланс.", show_alert=True); return
    if update_balance(uid, -bet) is None: await q.answer("Ошибка списания.", show_alert=True); return
    deck=create_deck(); ph, dh = [], []; dlt = 0
    try:
        for _ in range(2): cp,sp=_get_next_item(deck, dlt, NUM_DECKS); ph.append(cp); dlt+=1; cd,sd=_get_next_item(deck, dlt, NUM_DECKS); dh.append(cd); dlt+=1; assert sp==0 and sd==0 and cp and cd
    except Exception as e: logger.error(f"BJ deal {uid}: {e}"); update_balance(uid, bet); await q.edit_message_text(f"Ошибка ({e}). Ставка возвр."); context.user_data.pop(BJ_GAME_KEY, None); return

    # --- ИСПРАВЛЕННЫЙ БЛОК ПРОВЕРКИ BJ ---
    p_val, dv = get_hand_value(ph), get_hand_value(dh)
    p_bj = (p_val == 21 and len(ph) == 2)
    d_bj = (dv == 21 and len(dh) == 2)
    out, st, ps = None, 'player_turn', 'active'

    if p_bj:
        ps = 'blackjack'
        st = 'game_over'
        if d_bj:
            out = f"⚖️ Ничья! У обоих Блекджек."
            update_balance(uid, bet) # Возвращаем ставку
        else:
            w = bet * BLACKJACK_PAYOUT
            update_balance(uid, bet + w) # Возвращаем ставку + выигрыш
            out = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {w:.2f} F!"
    elif d_bj:
        out = f"😥 У дилера Блекджек!"
        st = 'game_over'
    # --- КОНЕЦ ИСПРАВЛЕННОГО БЛОКА ---

    game.update({'state':st,'deck':deck,'cards_dealt':dlt,'player_hands':[{'hand':ph,'bet':bet,'status':ps,'can_double':(not p_bj and not d_bj),'can_split':False}],'current_hand_index':0,'dealer_hand':dh,'initial_bet':bet,'split_count':0,'outcome_text':out})
    try: await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
    except Exception as e: logger.warning(f"Could not delete bet prompt {bet_prompt_message_id}: {e}")
    new_message = await blackjack_show_state(context, chat_id, uid, game_state=game, send_new=True)
    if new_message: game['message_id'] = new_message.message_id; logger.info(f"BJ initial state sent {new_message.message_id} for {uid}")
    else: logger.error(f"Failed to send initial BJ state for {uid}"); context.user_data.pop(BJ_GAME_KEY, None)
    await q.answer(f"Ставка {bet} F!")

async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, message_id: int | None = None, send_new: bool = False) -> Message | None:
    if game_state is None: game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state: return None
    previous_message_id = message_id or game_state.get('message_id')
    bal=get_balance(user_id); bal_s = f"{bal:.2f}" if bal is not None else "N/A"
    dh, phs = game_state.get('dealer_hand', []), game_state.get('player_hands', []); ci, st = game_state.get('current_hand_index', -1), game_state.get('state', '?')
    hide_d = (st == 'player_turn') and not (get_hand_value(dh) == 21 and len(dh) == 2)
    txt = f"<b>Блекджек</b>|Баланс:<b>{bal_s}</b> F\n"; tb = sum(h.get('bet', 0) for h in phs if isinstance(h, dict)); nh = len(phs)
    txt += f"Ст:<b>{tb}</b> F{'({nh})'*(nh>1)}\n"+"-"*20+"\n"; dv = get_hand_value(dh); dvs = "??" if not dh else (str(dv) if not hide_d else f"{get_card_value(dh[0])}+?")
    txt += f"<b>Д:</b> {format_hand(dh, hide_one=hide_d)} ({dvs})\n\n<b>Вы:</b>\n"; act_h = None
    for i, h_data in enumerate(phs):
        if not isinstance(h_data, dict): continue
        h,hv,hs,hb = h_data.get('hand',[]),get_hand_value(h_data.get('hand',[])),h_data.get('status','?'),h_data.get('bet',0)
        cur = (i==ci and hs=='active' and st=='player_turn'); ind = "▶" if cur else ("✅" if hs=='stand' else ("❌" if hs=='bust' else ("💰" if hs=='blackjack' else "▫")))
        txt += f"{ind}Р{i+1}:{format_hand(h)}({hv})[<i>{hb}</i> F]"; txt+={'bust':"-<b>Перебор!</b>",'blackjack':"-<b>БЖ!</b>",'stand':"-<i>Стоп</i>"}.get(hs,'') if hs!='stand' or not cur else ''; txt+="\n"
        if cur: act_h = h_data
    kbd = []
    if act_h and st=='player_turn':
        p_h, p_b = act_h.get('hand',[]), act_h.get('bet',0); can_d = (act_h.get('can_double',False) and len(p_h)==2 and bal is not None and bal >= p_b)
        can_s = (len(p_h)==2 and p_h[0] and p_h[1] and get_card_value(p_h[0])==get_card_value(p_h[1]) and bal is not None and bal >= p_b and game_state.get('split_count',0)<MAX_SPLITS)
        act_h['can_split'] = can_s; kbd.append([InlineKeyboardButton("Еще",callback_data=f"bj_hit_{ci}"), InlineKeyboardButton("Хватит",callback_data=f"bj_stand_{ci}")])
        spc=[b for c,b in [(can_d,InlineKeyboardButton("Удв",callback_data=f"bj_double_{ci}")),(can_s,InlineKeyboardButton("Разд",callback_data=f"bj_split_{ci}"))] if c]
        if spc: kbd.append(spc)
    elif st=='game_over': txt+=f"\n<b>Игра окончена!</b>🎉\n{game_state.get('outcome_text','')}\n"; fin_b=get_balance(user_id); txt+=f"\nБаланс:<b>{fin_b:.2f}</b> F." if fin_b is not None else ""; kbd.append([InlineKeyboardButton("🔄Новая",callback_data="bj_new_game")])
    elif st=='dealer_turn': txt += "\n<i>Ход дилера...</i>"
    mrk=InlineKeyboardMarkup(kbd) if kbd else None
    new_message = None
    try:
        if previous_message_id:
            try: await context.bot.delete_message(chat_id, previous_message_id)
            except Exception as e: logger.debug(f"Failed to delete previous msg {previous_message_id} for user {user_id}: {e}")
        new_message = await context.bot.send_message(chat_id, txt, reply_markup=mrk, parse_mode=ParseMode.HTML)
        game_state['message_id'] = new_message.message_id # Update message ID in game state
        logger.debug(f"Sent new BJ state msg {new_message.message_id} for user {user_id}")
    except Exception as e:
        logger.error(f"Error deleting/sending BJ state for user {user_id}: {e}")
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None) # Clean state on error
    return new_message

async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q=update.callback_query; u=q.from_user; uid=u.id; chat_id=q.message.chat_id; game=context.user_data.get(BJ_GAME_KEY,{})
    act,h_idx_s = parts[0], parts[1]
    try: h_idx = int(h_idx_s)
    except: return
    action_message_id = q.message.message_id
    if not game or game.get('state')!='player_turn' or game.get('message_id') != action_message_id: await q.answer("Неактуально.",show_alert=False); return
    phs=game.get('player_hands',[]);
    if not(0<=h_idx<len(phs)) or h_idx!=game.get('current_hand_index',-1): await q.answer("Ход др. руки.",show_alert=False); return
    hd=phs[h_idx];
    if not isinstance(hd,dict) or hd.get('status')!='active': await q.answer("Неактуально.",show_alert=False); return
    h,dk=hd.get('hand',[]), game.get('deck',[]); bal,b=get_balance(uid),hd.get('bet',0); dlt=game.get('cards_dealt',0); show_new_state=False
    try: # Main action block
        if act=='hit':
            c,sc=_get_next_item(dk,dlt,NUM_DECKS);
            if sc==0 and c:
                h.append(c); game['cards_dealt']+=1; hd['can_double']=hd['can_split']=False
                hv=get_hand_value(h); await q.answer(f"{c[0]}{c[1]}")
                if hv>21: hd['status']='bust'; await blackjack_next_action(context, chat_id, uid)
                elif hv==21: hd['status']='stand'; await blackjack_next_action(context, chat_id, uid)
                else: show_new_state = True
            else: raise IndexError("Draw fail")
        elif act=='stand':
            hd['status']='stand'; await q.answer("Стоп."); await blackjack_next_action(context, chat_id, uid)
        elif act=='double':
            can=(hd.get('can_double',False) and len(h)==2 and bal is not None and bal>=b)
            if can and update_balance(uid, -b) is not None:
                hd['bet']+=b; hd['can_double']=hd['can_split']=False; c,sc=_get_next_item(dk, game['cards_dealt'], NUM_DECKS)
                if sc==0 and c:
                    h.append(c); game['cards_dealt']+=1; hv=get_hand_value(h)
                    hd['status']='bust' if hv>21 else 'stand'; await q.answer(f"Удв!{c[0]}{c[1]}.Ит:{hv}{'!'*(hv>21)}")
                else:
                    hd['status']='stand'; await q.answer("Удв!Не взята карта.",show_alert=True)
                await blackjack_next_action(context, chat_id, uid)
            else: await q.answer("Нельзя удвоить.",show_alert=True)
        elif act=='split':
             can=hd.get('can_split', False)
             if can and bal is not None and bal>=b and update_balance(uid, -b) is not None:
                 game['split_count']+=1; cm=h.pop(); nh={'hand':[cm],'bet':b,'status':'active','can_double':False,'can_split':False}; phs.insert(h_idx+1, nh); cs=[];
                 for _ in range(2): c,sc=_get_next_item(dk, game['cards_dealt'], NUM_DECKS); cs.append(c if sc==0 else None); game['cards_dealt']+= (1 if c else 0)
                 if cs[0]: h.append(cs[0])
                 if cs[1]: nh['hand'].append(cs[1])
                 is_a=get_card_value(h[0])==11
                 if is_a:
                     hd['status']=nh['status']='stand'; hd['can_double']=nh['can_double']=False; await q.answer("Тузы разд."); await blackjack_next_action(context, chat_id, uid)
                 else:
                     hd['can_double']=(len(h) == 2)
                     nh['can_double']=(len(nh['hand']) == 2)
                     lo=game['split_count']<MAX_SPLITS
                     hd['can_split']=(len(h)==2 and h[0] and h[1] and get_card_value(h[0])==get_card_value(h[1]) and lo)
                     nh['can_split']=(len(nh['hand'])==2 and nh['hand'][0] and nh['hand'][1] and get_card_value(nh['hand'][0])==get_card_value(nh['hand'][1]) and lo)
                     if get_hand_value(h)==21: hd['status']='stand'
                     if get_hand_value(nh['hand'])==21: nh['status']='stand'
                     await q.answer("Разделено!"); show_new_state = True
             else: await q.answer("Нельзя разделить.",show_alert=True)
    except IndexError: hd['status']='stand'; await q.answer("Не взять карту!",show_alert=True); await blackjack_next_action(context, chat_id, uid)
    except Exception as e: logger.error(f"BJ act '{act}' u {uid}: {e}", exc_info=True); await q.answer("Ошибка.",show_alert=True)
    if show_new_state:
        await blackjack_show_state(context, chat_id, uid, game_state=game, send_new=True)

async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'player_turn': return
    phs=game.get('player_hands',[]); ci=game.get('current_hand_index',-1)
    ni=next((i for i,h in enumerate(phs[ci+1:],start=ci+1) if isinstance(h,dict) and h.get('status')=='active'),-1)
    if ni!=-1: # Found next hand
        game['current_hand_index']=ni
        await blackjack_show_state(context, chat_id, user_id, game_state=game, send_new=True)
    else: # No more active hands -> dealer turn
        game['state']='dealer_turn'
        await blackjack_show_state(context, chat_id, user_id, game_state=game, send_new=True)
        context.job_queue.run_once(blackjack_dealer_turn_job, DEALER_TURN_DELAY, data={'chat_id': chat_id, 'user_id': user_id}, name=f"dealer_{user_id}")

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data; user_id = job_data.get('user_id'); chat_id = job_data.get('chat_id')
    if not user_id or not chat_id: logger.error(f"BJ Dealer job missing IDs: {job_data}"); return
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state')!='dealer_turn': return
    dk,dh,phs=game.get('deck',[]),game.get('dealer_hand',[]),game.get('player_hands',[]); dlt=game.get('cards_dealt',0)
    can_w=any(isinstance(h,dict) and h.get('status') not in ['bust','blackjack'] for h in phs); d_bj=(get_hand_value(dh)==21 and len(dh)==2)
    if not can_w and not d_bj: await blackjack_determine_outcome(context, chat_id, user_id, d_bj); return
    dealer_stood = False
    while not dealer_stood:
        dv=get_hand_value(dh); ac=sum(1 for c in dh if c and c[0]=='A'); soft=ac>0 and (dv-ac*11)<11
        if dv>17 or (dv==17 and not (soft and DEALER_HITS_SOFT_17)): logger.info(f"Дилер стоп {dv} у {user_id}."); dealer_stood = True; break
        try:
            c, sc = _get_next_item(dk, dlt, NUM_DECKS)
            if sc == 0 and c: dh.append(c); game['cards_dealt'] += 1; dlt = game['cards_dealt']
            else: raise IndexError("Dealer draw fail")
        except IndexError: logger.warning(f"BJ Dealer {user_id} остановился - колода пуста?"); dealer_stood = True; break
    await blackjack_determine_outcome(context, chat_id, user_id, d_bj)

async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game: return
    if game.get('state')=='game_over' and 'outcome_determined' in game:
        await blackjack_show_state(context, chat_id, user_id, game_state=game, send_new=True); return
    phs,dh=game.get('player_hands',[]), game.get('dealer_hand',[]); dv=get_hand_value(dh); db=dv>21; outs,tw,tb=[],0,0
    for i,hd in enumerate(phs):
        if not isinstance(hd,dict): continue
        h,b,st=hd.get('hand',[]),hd.get('bet',0),hd.get('status'); pv=get_hand_value(h); p_bj=(st=='blackjack'); tb+=b; mult=0; out=""; pfx=f"Р{i+1}: " if len(phs)>1 else ""
        if st=='bust': out=f"{pfx}Перебор({pv}). {-b} F."
        elif p_bj: out=f"{pfx}БЖ! "+(f"Ничья." if d_had_bj else f"+{(b*BLACKJACK_PAYOUT):.2f} F."); mult=1 if d_had_bj else 1+BLACKJACK_PAYOUT
        elif d_had_bj: out=f"{pfx}Дилер БЖ. {-b} F."
        elif db: out=f"{pfx}Дилер переб({dv})! +{b} F."; mult=2
        elif pv>dv: out=f"{pfx}{pv}>{dv}. +{b} F."; mult=2
        elif pv==dv: out=f"{pfx}{pv}={dv}. Ничья."; mult=1
        else: out=f"{pfx}{pv}<{dv}. {-b} F."
        outs.append(out); tw+=b*mult
    nc=tw-tb
    if tw>0 and update_balance(user_id, tw) is None: outs.append("\n<b>ОШИБКА!</b>"); nc=-tb
    game['state']='game_over'; game['outcome_text']="\n".join(outs)+f"\n\n<b>Ваш итог:{nc:+.2f} F</b>"; game['outcome_determined']=True
    await blackjack_show_state(context, chat_id, user_id, game_state=game, send_new=True) # Send final state
    context.application.user_data[user_id].pop(BJ_GAME_KEY, None); logger.info(f"BJ game state cleaned {user_id}")

# --- General Handlers ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; data=q.data; u=q.from_user
    if not data: return
    logger.debug(f"CB:'{data}' u:{u.id}")
    prts=data.split("_", 2); pfx=prts[0]; ad=prts[1:]
    try:
        if pfx=="bj":
            if update.effective_chat.type != ChatType.PRIVATE: await q.answer("БЖ только в личке.", show_alert=True); return
            act=ad[0]; pay=ad[1] if len(ad)>1 else None
            if act=="bet": await blackjack_handle_bet(update, context, int(pay))
            elif act=="new": await blackjack_start_command(update, context)
            elif act in ["hit","stand","double","split"]: await blackjack_handle_action(update, context, [act, pay])
            else: await q.answer()
        else: await q.answer()
    except Exception as e: logger.error(f"Cb error '{data}': {e}"); await q.answer("Ошибка.", show_alert=True)
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception handling update:", exc_info=context.error)
    if isinstance(context.error, Conflict): logger.critical("Conflict error! Multiple instances?")
    elif isinstance(context.error, BadRequest): logger.warning(f"BadRequest: {context.error}.")

# --- Main Bot Setup ---
def main():
    logger.info("Starting bot..."); start_keep_alive(); app=None
    try:
        app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
        app.bot_data.setdefault('user_mention_cache', {})
        hs=[CommandHandler("start", start_command), CommandHandler("help", help_command),
            CommandHandler("balance", balance_command), CommandHandler("bonus", bonus_command),
            CommandHandler("leaderboard", leaderboard_command), CommandHandler("blackjack", blackjack_start_command),
            CallbackQueryHandler(button_callback_handler), ]
        app.add_handlers(hs); app.add_error_handler(error_handler)
        logger.info("Handlers registered. Starting polling..."); print("Bot running...")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Conflict as e: logger.critical(f"Startup Conflict error: {e}")
    except Exception as e: logger.critical(f"Runtime critical error: {e}", exc_info=True)
    finally: print("Bot stopped."); logger.info("Bot stopped.")

if __name__ == "__main__": main()