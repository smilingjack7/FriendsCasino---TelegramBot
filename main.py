# -*- coding: utf-8 -*-
import logging
import os
import random
import datetime
import time
import asyncio # Добавлен импорт asyncio
from collections import defaultdict, deque
from threading import Thread # Для keep_alive
from flask import Flask # Для keep_alive
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
import math
import psycopg2 # Используем PostgreSQL
from psycopg2.extras import RealDictCursor # Для удобного получения словарей
from urllib.parse import urlparse # Для разбора DATABASE_URL

# --- Keep Alive Web Server ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web_server():
  port = int(os.environ.get("PORT", 8080))
  app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN: raise ValueError("Не установлена переменная окружения BOT_TOKEN")
if not DATABASE_URL: raise ValueError("Не установлена переменная окружения DATABASE_URL")

INITIAL_BALANCE = 100
BONUS_AMOUNT = 10
BONUS_COOLDOWN_HOURS = 6
NUM_DECKS = 8
RESHUFFLE_PENETRATION = 0.5
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.2
LEADERBOARD_LIMIT = 10

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Standard Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11}

# --- Database Functions (PostgreSQL) ---

def get_db_conn():
    """Устанавливает соединение с БД PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True # Включаем автокоммит для упрощения
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        raise

def init_db_manual():
    """SQL для ручной инициализации таблицы."""
    sql = """ CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, balance DOUBLE PRECISION DEFAULT 0, last_bonus TIMESTAMP WITHOUT TIME ZONE ); """
    print("--- SQL для инициализации таблицы users ---"); print(sql); print("--- Выполните этот SQL запрос в вашей базе данных один раз. ---")

def get_or_create_user(user_id: int):
    """Получает данные пользователя или создает нового с начальным балансом."""
    select_sql = "SELECT * FROM users WHERE user_id = %s;"
    insert_sql = """INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING;"""
    user_data = None
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()
                if user_data is None:
                    cursor.execute(insert_sql, (user_id, INITIAL_BALANCE, None))
                    logger.info(f"Создан новый пользователь в БД: {user_id}")
                    cursor.execute(select_sql, (user_id,))
                    user_data = cursor.fetchone()
                    if not user_data: return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        return user_data
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_or_create_user) для {user_id}: {e}"); return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_or_create_user) для {user_id}: {e}"); return None

def update_balance(user_id: int, amount_change: float):
    """Обновляет баланс пользователя и возвращает НОВЫЙ баланс."""
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    new_balance = None
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_update, (amount_change, user_id))
                result = cursor.fetchone()
                if result: new_balance = result[0]
        return new_balance
    except psycopg2.Error as e: logger.error(f"Ошибка БД (update_balance) для {user_id}: {e}"); return None
    except Exception as e: logger.error(f"Неожиданная ошибка (update_balance) для {user_id}: {e}"); return None

def get_balance(user_id: int) -> float | None:
    """Получает текущий баланс пользователя."""
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else 0.0 # Возвращаем 0 при ошибке

def update_last_bonus_time(user_id: int, bonus_time: datetime.datetime):
     """Обновляет время последнего получения бонуса."""
     sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
     try:
         with get_db_conn() as conn:
             with conn.cursor() as cursor:
                 cursor.execute(sql, (bonus_time, user_id))
     except psycopg2.Error as e: logger.error(f"Ошибка БД (update_last_bonus_time) для {user_id}: {e}")
     except Exception as e: logger.error(f"Неожиданная ошибка (update_last_bonus_time) для {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Получает время последнего бонуса пользователя."""
    user_data = get_or_create_user(user_id)
    return user_data.get('last_bonus') if user_data else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT):
    """Получает топ пользователей по балансу."""
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    leaders = []
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (limit,))
                leaders = cursor.fetchall()
        return leaders
    except psycopg2.Error as e: logger.error(f"Ошибка БД (get_leaderboard): {e}"); return []
    except Exception as e: logger.error(f"Неожиданная ошибка (get_leaderboard): {e}"); return []

# --- Standard Deck and Hand Utilities ---
def create_deck(num_decks=NUM_DECKS):
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck); return deck

def get_card_value(card):
    rank = card[0]; return RANK_VALUES.get(rank, 0) if card else 0

def get_hand_value(hand):
    value = 0; ace_count = 0
    if not hand: return 0
    for card in hand:
        if card:
            rank = card[0]; value += get_card_value(card)
            if rank == 'A': ace_count += 1
        else: logger.warning("None карта в руке при подсчете.")
    while value > 21 and ace_count > 0: value -= 10; ace_count -= 1
    return value

def format_hand(hand, hide_one=False):
    if not hand: return "Пусто"
    if hide_one and len(hand) > 0:
        fc = hand[0]; return f"[{fc[0]}{fc[1]}, ??]" if fc else "[??, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in hand if c])

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id)
    balance = user_data['balance'] if user_data else 0.0
    balance_str = f"{balance:.2f}"
    await update.message.reply_text(
        f"Добро пожаловать/С возвращением! 👋 Ваш текущий баланс: {balance_str} фишек.\n"
        f"Используйте /blackjack для начала игры или /help для списка команд."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Доступные команды:\n"
        "/start - Начать взаимодействие / Проверить баланс\n"
        "/blackjack - Начать новую игру в Блекджек\n"
        "/balance - Показать ваш текущий баланс фишек\n"
        "/bonus - Получить бонусные фишки (раз в {} часов)\n"
        "/leaderboard - Показать топ игроков\n"
        "/help - Показать это сообщение"
    ).format(BONUS_COOLDOWN_HOURS)
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_balance = get_balance(user_id)
    await update.message.reply_text(f"Ваш баланс: {current_balance:.2f} фишек.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id)
    if not user_data: await update.message.reply_text("Ошибка данных. /start."); return

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus_time = user_data.get('last_bonus')
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time and now - last_bonus_time < cooldown:
        time_left = last_bonus_time + cooldown - now
        h, rem = divmod(time_left.total_seconds(), 3600); m, _ = divmod(rem, 60)
        await update.message.reply_text(f"Бонус уже получен. Попробуйте через {int(h)} ч {int(m)} мин.")
    else:
        new_balance = update_balance(user_id, BONUS_AMOUNT)
        if new_balance is not None:
            update_last_bonus_time(user_id, now)
            await update.message.reply_text(f"✅ Бонус {BONUS_AMOUNT} фишек получен! Новый баланс: {new_balance:.2f} фишек.")
        else: await update.message.reply_text("Не удалось начислить бонус (ошибка БД).")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders: await update.message.reply_text("Пока нет данных для таблицы лидеров."); return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"
    async def get_user_info(user_id):
        try:
            cache = context.bot_data.setdefault('user_cache', {})
            if user_id in cache and (datetime.datetime.now() - cache[user_id]['timestamp']).total_seconds() < 3600: # Кеш на 1 час
                 return cache[user_id]['user']
            user = await context.bot.get_chat(user_id)
            cache[user_id] = {'user': user, 'timestamp': datetime.datetime.now()}
            return user
        except Exception as e: logger.warning(f"Failed get info user {user_id}: {e}"); return None

    user_info_tasks = [get_user_info(l['user_id']) for l in leaders]
    users_info = await asyncio.gather(*user_info_tasks)

    for i, leader in enumerate(leaders):
        user_id = leader['user_id']; balance = leader['balance']; user: User | None = users_info[i]
        user_name = f"ID: {user_id}"
        if user:
            name = user.full_name
            for char in ['_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']: name = name.replace(char, f'\\{char}')
            user_name = user.mention_markdown_v2(name) if user.username else name
        leaderboard_text += f"{i+1}\\. {user_name} \\- `{balance:.2f}` F\n"

    try: await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending leaderboard (MarkdownV2): {e}")
        try: plain_text = update.message.text_markdown_v2; await update.message.reply_text(plain_text)
        except Exception as fe: logger.error(f"Error sending plain LB: {fe}")

# --- Blackjack Game Logic Handlers ---

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id; reply_func = None
    if hasattr(update, 'message'): reply_func = update.message.reply_text
    elif hasattr(update, 'callback_query'): reply_func = update.callback_query.message.reply_text; await update.callback_query.answer()
    else: return

    current_balance = get_balance(user_id)
    if current_balance is None: await reply_func("Ошибка баланса."); return

    if 'games' not in context.bot_data: context.bot_data['games'] = {}
    if chat_id in context.bot_data['games']:
        old = context.bot_data['games'][chat_id]
        if old.get('state') not in ['game_over', 'waiting_bet']: await reply_func("Игра уже идет."); return
        if old.get('message_id'):
             try: await context.bot.delete_message(chat_id, old['message_id'])
             except Exception: pass
        del context.bot_data['games'][chat_id]

    if current_balance <= 0: await reply_func(f"Баланс 0. /bonus."); return
    opts=[1,5,10,25,50,100]; v_bets=[b for b in opts if b <= current_balance]
    if not v_bets: await reply_func(f"Баланс ({current_balance:.2f}) < мин. ставки ({min(opts)})."); return

    keys = [[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in v_bets]]
    markup = InlineKeyboardMarkup(keys)
    try:
        msg = await reply_func(f"Баланс: {current_balance:.2f}. Ваша ставка?", reply_markup=markup)
        context.bot_data['games'][chat_id] = {'player_id':user_id,'state':'waiting_bet','message_id':msg.message_id}
    except Exception as e: logger.error(f"Send bet message error: {e}")

async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    query=update.callback_query; user_id=query.from_user.id; chat_id=query.message.chat_id
    if chat_id not in context.bot_data.get('games',{}) or context.bot_data['games'][chat_id]['player_id']!=user_id: await query.answer("Не ваша игра.", show_alert=True); return
    gs=context.bot_data['games'][chat_id]
    if gs['state']!='waiting_bet': await query.answer(); return

    bal=get_balance(user_id)
    if bal is None: await query.answer("Ошибка баланса.", show_alert=True); return
    if bet_amount > bal: await query.answer(f"Недостаточно ({bal:.2f}).", show_alert=True); return
    if bet_amount <= 0: await query.answer("Ставка > 0.", show_alert=True); return

    if update_balance(user_id, -bet_amount) is None: await query.answer("Ошибка баланса.", show_alert=True); return

    deck = create_deck(NUM_DECKS); dsi = 0.0; cd = 0; ph = []; dh = []
    try:
        card,adj = _draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="player"); ph.append(card); dsi+=adj; cd+=1
        card,adj = _draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="dealer"); dh.append(card); dsi+=adj; cd+=1
        card,adj = _draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="player"); ph.append(card); dsi+=adj; cd+=1
        card,_   = _draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="dealer"); dh.append(card); cd+=1
    except Exception as e: logger.error(f"Draw card error: {e}"); await query.answer("Ошибка раздачи.", show_alert=True); update_balance(user_id, bet_amount); return

    pv=get_hand_value(ph); dv=get_hand_value(dh); d_up_v=get_card_value(dh[0]) if dh else 0
    p_bj=(pv==21 and len(ph)==2); d_ace_ten=(d_up_v==11 or d_up_v==10); d_bj=False
    if d_ace_ten and dv==21 and len(dh)==2: d_bj=True; dsi+=_get_rank_weight(dh[1] if len(dh)>1 else None)

    state='player_turn'; outcome=None; status='active'
    if p_bj:
        status='blackjack'
        if d_bj: update_balance(user_id,bet_amount); outcome=f"Пуш! Оба БЖ."; state='game_over'
        else: w=bet_amount*BLACKJACK_PAYOUT; update_balance(user_id,bet_amount+w); outcome=f"БЖ! +{w:.2f} F."; state='game_over'
    elif d_bj: outcome=f"Дилер БЖ! -{bet_amount} F."; state='game_over'

    gs.update({
        'state':state,'deck':deck,'player_hands':[{'hand':ph,'bet':bet_amount,'status':status,'can_double':(not p_bj),'can_split':False}],
        'current_hand_index':0,'dealer_hand':dh,'_deck_state_index':dsi,'cards_dealt':cd,'initial_bet':bet_amount,'split_count':0,'outcome_text':outcome
    })
    await query.answer(f"Ставка {bet_amount}!"); await show_game_state(context,chat_id,gs['message_id'])

async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int = None):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]; pid=gs.get('player_id')
    if not pid: return # Не должно быть, но для безопасности
    bal=get_balance(pid) if pid else 0.0

    if message_id: gs['message_id']=message_id
    cur_msg_id=gs.get('message_id')
    if not cur_msg_id: return

    dh=gs.get('dealer_hand',[]); phs=gs.get('player_hands',[]); cur_idx=gs.get('current_hand_index',-1)
    hide=gs.get('state')=='player_turn' and not (get_hand_value(dh)==21 and len(dh)==2)

    text = f"**Блекджек** | Баланс: {bal:.2f} F\n"
    total_bet=sum(h.get('bet',0) for h in phs if isinstance(h,dict)); num_h=len(phs)
    text+=f"Ставка{' '*bool(num_h>1)}{'(всего)'*bool(num_h>1)}: {total_bet} F{f' ({num_h} рук)'*bool(num_h>1)}\n"; text+="-"*25+"\n"
    dv=get_hand_value(dh); dv_s="??";
    if not hide: dv_s=str(dv)
    elif dh and dh[0]: dv_s=str(get_card_value(dh[0]))+"+?"
    dh_s=format_hand(dh,hide); text+=f"**Дилер:** {dh_s} ({dv_s})\n\n"; text+="**Вы:**\n"; active_h=None
    for i,hd in enumerate(phs):
        if not isinstance(hd,dict): continue
        h=hd.get('hand');hv=get_hand_value(h);st=hd.get('status');bet=hd.get('bet',0)
        is_cur=(i==cur_idx and st=='active')
        ind="▶️" if is_cur else "✅" if st=='stand' else "❌" if st=='bust' else "💰" if st=='blackjack' else "✔️"
        text+=f"{ind} Р{i+1}: {format_hand(h)}({hv}) [{bet} F]"
        if st=='bust':text+=" Перебор!"; elif st=='blackjack':text+=" БЖ!"; elif st=='stand':text+=" Стоп"
        text+="\n"
        if is_cur: active_h=hd
    text+="\n"; kbd=[]
    if active_h and isinstance(active_h,dict):
        h=active_h.get('hand'); bet=active_h.get('bet',0)
        can_double=active_h.get('can_double',False) and bal>=bet and h and len(h)==2
        can_split=h and len(h)==2 and h[0] and h[1] and h[0][0]==h[1][0] and bal>=bet and gs.get('split_count',0)<MAX_SPLITS
        acts=[InlineKeyboardButton("Еще",callback_data=f"bj_action_hit_{cur_idx}"), InlineKeyboardButton("Хватит",callback_data=f"bj_action_stand_{cur_idx}")]
        subs=[]
        if can_double: subs.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_action_double_{cur_idx}"))
        if can_split: subs.append(InlineKeyboardButton("Разделить", callback_data=f"bj_action_split_{cur_idx}"))
        kbd.append(acts);
        if subs: kbd.append(subs)
    elif gs.get('state')=='game_over':
        if gs.get('outcome_text'): text+=f"**Конец Игры!**\n{gs['outcome_text']}\n"
        text+=f"Итоговый баланс: {bal:.2f} F\n"; kbd.append([InlineKeyboardButton("Новая Игра", callback_data="bj_action_new_game")])
    elif gs.get('state')=='dealer_turn': text+="*Ход дилера...*\n"
    markup=InlineKeyboardMarkup(kbd) if kbd else None
    try: await context.bot.edit_message_text(chat_id=chat_id,message_id=cur_msg_id,text=text,reply_markup=markup,parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        if "not found" in str(e).lower(): logger.warning(f"Msg {cur_msg_id} not found.") # Достаточно лога
        elif "identical" in str(e).lower(): pass # Игнорируем ошибку "message is not modified"
        else: logger.error(f"Update error {cur_msg_id}: {e}")


async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    query=update.callback_query; user_id=query.from_user.id; chat_id=query.message.chat_id
    try: await query.answer()
    except: pass
    if chat_id not in context.bot_data.get('games',{}) or context.bot_data['games'][chat_id]['player_id']!=user_id: return
    gs=context.bot_data['games'][chat_id]
    if gs.get('state')!='player_turn': return
    if not(0<=hand_index<len(gs['player_hands'])) or hand_index!=gs.get('current_hand_index'): return
    hd=gs['player_hands'][hand_index]
    if not isinstance(hd,dict) or hd.get('status')!='active': return

    h=hd.get('hand',[]); deck=gs.get('deck',[]); bal=get_balance(user_id); bet=hd.get('bet',0)
    if bal is None: await query.answer("Ошибка баланса.", show_alert=True); return

    needs_reshuffle=len(deck)<(NUM_DECKS*52*(1.0-RESHUFFLE_PENETRATION))
    if needs_reshuffle: logger.info(f"Reshuffle {chat_id}"); gs['deck']=create_deck(NUM_DECKS); gs['_deck_state_index']=0.0; deck=gs['deck']

    if action=='hit':
        card,adj=_draw_card_from_shoe(deck,gs.get('_deck_state_index',0.0),gs.get('cards_dealt',0),NUM_DECKS,t="player")
        if card:
            h.append(card); gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+adj; gs['cards_dealt']=gs.get('cards_dealt',0)+1
            hd['can_double']=False; hd['can_split']=False; hv=get_hand_value(h)
            if hv>21: hd['status']='bust'
            elif hv==21: hd['status']='stand'
            if hd['status']!='active': await next_player_action_or_dealer(context,chat_id)
            else: await show_game_state(context,chat_id)
        else: hd['status']='stand'; await next_player_action_or_dealer(context,chat_id)
    elif action=='stand': hd['status']='stand'; await next_player_action_or_dealer(context,chat_id)
    elif action=='double':
        if hd.get('can_double',False) and bal>=bet and h and len(h)==2:
            if update_balance(user_id,-bet) is not None:
                hd['bet']+=bet
                card,adj=_draw_card_from_shoe(deck,gs.get('_deck_state_index',0.0),gs.get('cards_dealt',0),NUM_DECKS,t="player")
                if card: h.append(card); gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+adj; gs['cards_dealt']=gs.get('cards_dealt',0)+1; hd['status']='bust' if get_hand_value(h)>21 else 'stand'
                else: hd['status']='stand'
                await next_player_action_or_dealer(context,chat_id)
            else: await query.answer("Ошибка баланса!", show_alert=True)
    elif action=='split':
        if h and len(h)==2 and h[0] and h[1] and h[0][0]==h[1][0] and bal>=bet and gs.get('split_count',0)<MAX_SPLITS:
             if update_balance(user_id,-bet) is not None:
                 gs['split_count']=gs.get('split_count',0)+1; card_m=h.pop()
                 n_hd={'hand':[card_m],'bet':bet,'status':'active','can_double':True,'can_split':False}
                 gs['player_hands'].insert(hand_index+1,n_hd)
                 card1,adj1=_draw_card_from_shoe(deck,gs.get('_deck_state_index',0.0),gs.get('cards_dealt',0),NUM_DECKS,t="player"); gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+adj1; gs['cards_dealt']=gs.get('cards_dealt',0)+1
                 card2,adj2=_draw_card_from_shoe(deck,gs.get('_deck_state_index',0.0),gs.get('cards_dealt',0),NUM_DECKS,t="player"); gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+adj2; gs['cards_dealt']=gs.get('cards_dealt',0)+1
                 if card1: h.append(card1)
                 if card2: n_hd['hand'].append(card2)
                 is_ace=h and h[0] and h[0][0]=='A'
                 if is_ace: hd['status']='stand';hd['can_double']=False; n_hd['status']='stand';n_hd['can_double']=False; await next_player_action_or_dealer(context,chat_id)
                 else:
                     hd['can_double']=bool(card1); hd['can_split']=bool(card1 and len(h)==2 and h[0] and h[1] and h[0][0]==card1[0])
                     n_hd['can_double']=bool(card2); n_hd['can_split']=bool(card2 and len(n_hd['hand'])==2 and n_hd['hand'][0] and n_hd['hand'][1] and n_hd['hand'][0][0]==card2[0])
                     if get_hand_value(h)==21: hd['status']='stand'
                     await show_game_state(context,chat_id)
             else: await query.answer("Ошибка баланса!", show_alert=True)


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs.get('state')!='player_turn': return
    phs=gs.get('player_hands',[]); cur_idx=gs.get('current_hand_index',-1); next_idx=cur_idx+1
    while next_idx<len(phs):
        if isinstance(phs[next_idx],dict) and phs[next_idx].get('status')=='active':
            gs['current_hand_index']=next_idx; await show_game_state(context,chat_id); return
        next_idx+=1
    gs['state']='dealer_turn'; await show_game_state(context,chat_id)
    context.job_queue.run_once(dealer_turn_job,DEALER_TURN_DELAY,chat_id=chat_id,data=chat_id,name=f"dealer_{chat_id}")


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id=context.job.data
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs.get('state')!='dealer_turn': return

    deck=gs.get('deck',[]); dh=gs.get('dealer_hand',[]); phs=gs.get('player_hands',[])
    can_win=any(isinstance(p,dict) and p.get('status') not in ['bust','blackjack'] for p in phs)
    if not can_win:
        if dh and len(dh)==2 and not (get_hand_value(dh)==21): gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+_get_rank_weight(dh[1] if len(dh)>1 else None)
        await determine_outcome(context,chat_id,False); return

    d_bj=dh and get_hand_value(dh)==21 and len(dh)==2
    if dh and not d_bj and len(dh)==2: gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+_get_rank_weight(dh[1] if len(dh)>1 else None)
    while True:
        dv=get_hand_value(dh); ac=sum(1 for c in dh if c and c[0]=='A'); is_s=ac>0 and (dv-11*ac<11)
        hit=(dv<17) or (dv==17 and is_s and DEALER_HITS_SOFT_17)
        if not hit: break
        if len(deck)<(NUM_DECKS*52*(1.0-RESHUFFLE_PENETRATION)): logger.info(f"Reshuffle dealer {chat_id}"); gs['deck']=create_deck(NUM_DECKS); gs['_deck_state_index']=0.0; deck=gs['deck']
        card,adj=_draw_card_from_shoe(deck,gs.get('_deck_state_index',0.0),gs.get('cards_dealt',0),NUM_DECKS,t="dealer")
        if card: dh.append(card); gs['_deck_state_index']=gs.get('_deck_state_index',0.0)+adj; gs['cards_dealt']=gs.get('cards_dealt',0)+1
        else: break
    await determine_outcome(context,chat_id,d_bj)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack: bool):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs.get('state')=='game_over' and gs.get('outcome_text'): await show_game_state(context,chat_id); return

    pid=gs.get('player_id'); phs=gs.get('player_hands',[]); dh=gs.get('dealer_hand',[])
    if not pid: return
    dv=get_hand_value(dh); d_bust=dv>21
    bal_before=get_balance(pid); outcomes=[]; total_payout=0
    if bal_before is None: bal_before=0.0

    for i,hd in enumerate(phs):
        if not isinstance(hd,dict): continue
        h=hd.get('hand'); bet=hd.get('bet',0); st=hd.get('status'); pv=get_hand_value(h); p_bj=st=='blackjack'
        mult=0; outc=""; pfx=f"Р{i+1}: " if len(phs)>1 else ""
        if st=='bust': outc=f"{pfx}Перебор({pv}). {-bet} F"; mult=0
        elif p_bj: w=bet*BLACKJACK_PAYOUT; outc=f"{pfx}БЖ! +{w:.2f} F"; mult=1+BLACKJACK_PAYOUT
        elif dealer_had_blackjack: outc=f"{pfx}{pv} vs БЖ. {-bet} F"; mult=0
        elif d_bust: outc=f"{pfx}{pv} vs Перебор({dv})! +{bet} F"; mult=2
        elif pv>dv: outc=f"{pfx}{pv} > {dv}. +{bet} F"; mult=2
        elif pv==dv: outc=f"{pfx}{pv} = {dv}. Пуш."; mult=1
        else: outc=f"{pfx}{pv} < {dv}. {-bet} F"; mult=0
        total_payout+=bet*mult; outcomes.append(outc)

    if total_payout>0: update_balance(pid,total_payout)
    final_bal=get_balance(pid)
    if final_bal is None: final_bal=bal_before+total_payout
    total_bet=sum(h.get('bet',0) for h in phs if isinstance(h,dict))
    net_change=total_payout-total_bet
    gs['state']='game_over'; gs['outcome_text']="\n".join(outcomes)+f"\n\n**Итог: {net_change:+.2f} F**"
    # Удаляем игру из памяти после определения исхода (опционально, можно оставить до /blackjack)
    # if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id]
    await show_game_state(context,chat_id) # Показываем итоговое сообщение

# <<< --- Internal Dealing Logic --- >>>
_RANK_WEIGHTS={"2":0.5,"7":0.5,"3":1,"4":1,"6":1,"5":1.5,"8":0,"9":-0.5,"T":-1,"J":-1,"Q":-1,"K":-1,"A":-1}
def _get_rank_weight(c): return _RANK_WEIGHTS.get(c[0],0) if c else 0
def _calculate_dealing_preference(i,r):
    if r<=0: return 0; d=max(0.5,r/52.0); return i/d
def _draw_card_from_shoe(d,i,c,n,t="player"): # Используем 't'
    if not d: logger.warning("_draw_card_from_shoe: Deck empty"); return None,0
    r=(n*52)-c;p=_calculate_dealing_preference(i,r);u=1.5;l=-1.0
    h=['T','J','Q','K','A'];w=['4','5','6'];pot=[];sel=random.random()<0.85
    if sel:
        can=None
        if t=="player":
            if p>=u: can=h
            elif p<=l: can=w
        elif t=="dealer":
            if p>=u: can=w
            elif p<=l: can=h
        if can: pot=[x for x in d if x and x[0] in can]
    if not pot: pot=list(d)
    pot_f=[card for card in pot if card is not None]
    if not pot_f: logger.error("Filtered potential cards empty."); return None,0
    ch=random.choice(pot_f)
    try: d.remove(ch)
    except ValueError:
        logger.warning(f"Card {ch} VE, remove eq."); removed=False
        for idx,cin_d in enumerate(d):
            if cin_d==ch: del d[idx]; removed=True; break
        if not removed: logger.error(f"Failed remove eq {ch}."); return None,0
    except Exception as e: logger.error(f"Remove card error {ch}: {e}"); return None,0
    adj=_get_rank_weight(ch); return ch,adj
# <<< --- End of Internal Dealing Logic --- >>>


# --- Callback Query Handler (Исправленный) ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all inline button presses."""
    query = update.callback_query; data = query.data; user_id = query.from_user.id
    user_data = get_or_create_user(user_id)
    if user_data is None: await query.answer("Ошибка данных пользователя.", show_alert=True); return

    if data.startswith("bj_bet_"):
        try: bet = int(data.split("_")[2]); await handle_blackjack_bet(update, context, bet)
        except Exception as e: logger.error(f"Bet callback error: {e}"); await query.answer("Ошибка ставки.", show_alert=True)

    elif data == "bj_action_new_game": # <<< ЯВНАЯ ПРОВЕРКА ЗДЕСЬ
         try:
             await query.answer()
             await blackjack_start(update, context) # Передаем update от callback'а
             try: await query.delete_message()
             except Exception as e: logger.warning(f"Could not delete 'New Game' msg: {e}")
         except Exception as e:
             logger.error(f"Error starting new game from button: {e}", exc_info=True)
             try: await query.answer("Ошибка запуска новой игры.", show_alert=True)
             except Exception: pass

    elif data.startswith("bj_action_"): # <<< ОБРАБОТКА ОСТАЛЬНЫХ ДЕЙСТВИЙ
        parts = data.split("_")
        if len(parts) == 4: # Ожидаем 4 части: bj, action, name, index
            try:
                action_type = parts[2]
                hand_index = int(parts[3]) # Пытаемся преобразовать индекс
                await handle_blackjack_action(update, context, action_type, hand_index)
            except ValueError:
                logger.warning(f"Invalid hand index in action data: {data}")
                await query.answer("Неверный индекс руки.", show_alert=True)
            except IndexError:
                 logger.warning(f"Invalid action data format (IndexError): {data}")
                 await query.answer("Неверный формат действия.", show_alert=True)
            except Exception as e:
                 logger.error(f"Action callback processing error: {e}", exc_info=True)
                 try: await query.answer("Ошибка обработки действия.", show_alert=True)
                 except Exception: pass
        else:
             logger.warning(f"Invalid action data format (parts count): {data}")
             await query.answer("Неверный формат данных действия.", show_alert=True)

    else: # Неизвестный callback
        logger.warning(f"Received unknown callback data: {data}")
        try: await query.answer()
        except Exception: pass

# --- Main Function ---
def main():
    """Starts the bot."""
    logger.info("Запуск функции main().")
    keep_alive() # Запускаем веб-сервер для Render

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    if 'games' not in application.bot_data: application.bot_data['games'] = {}
    if 'user_cache' not in application.bot_data: application.bot_data['user_cache'] = {}

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))
    application.add_handler(CallbackQueryHandler(button_callback_handler)) # Используем исправленный обработчик

    logger.info("Обработчики зарегистрированы.")
    print("Бот запускается...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    print("Бот остановлен.")
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    print("Запуск скрипта...")
    # init_db_manual() # Раскомментируйте, если хотите увидеть SQL для создания таблицы
    main()