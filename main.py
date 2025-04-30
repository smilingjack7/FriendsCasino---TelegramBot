import logging
import os
import random
import datetime
import time
from collections import defaultdict, deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
import math
import psycopg2 # <<< Заменяем sqlite3
from psycopg2.extras import RealDictCursor # <<< Для удобного получения словарей
from urllib.parse import urlparse # <<< Для разбора DATABASE_URL

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") # <<< Читаем из переменных окружения
DATABASE_URL = os.environ.get("DATABASE_URL") # <<< Читаем из переменных окружения

if not BOT_TOKEN:
    raise ValueError("Не установлена переменная окружения BOT_TOKEN")
if not DATABASE_URL:
    raise ValueError("Не установлена переменная окружения DATABASE_URL")

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
logger = logging.getLogger(__name__)

# --- Standard Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]; RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11}

# --- Database Functions (PostgreSQL) ---

def get_db_conn():
    """Устанавливает соединение с БД PostgreSQL."""
    try:
        # Используем DATABASE_URL напрямую
        conn = psycopg2.connect(DATABASE_URL, sslmode='require') # Render часто требует sslmode=require для внешних БД
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        raise # Передаем ошибку дальше, чтобы бот не запустился без БД

def init_db():
    """Инициализирует таблицы в БД PostgreSQL, если их нет."""
    # Эту функцию лучше выполнить один раз вручную или при первом запуске
    # на хостинге, добавив проверку существования таблицы.
    # В Render ее можно добавить в Build Command или Start Command.
    # Пример: python -c 'from main import init_db; init_db()'
    # Но безопаснее выполнить вручную через psql или GUI.
    sql = """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance DOUBLE PRECISION DEFAULT 0,
            last_bonus TIMESTAMP WITHOUT TIME ZONE
        );
    """
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
        logger.info("Проверка/создание таблицы users выполнена.")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")

def get_or_create_user(user_id: int):
    """Получает данные пользователя или создает нового с начальным балансом."""
    # Сначала пытаемся получить пользователя
    select_sql = "SELECT * FROM users WHERE user_id = %s;"
    insert_sql = """
        INSERT INTO users (user_id, balance, last_bonus)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING;
    """
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()
                if user_data is None:
                    # Пользователя нет, создаем
                    cursor.execute(insert_sql, (user_id, INITIAL_BALANCE, None))
                    conn.commit() # Фиксируем вставку
                    logger.info(f"Создан новый пользователь в БД: {user_id}")
                    # Повторно получаем данные
                    cursor.execute(select_sql, (user_id,))
                    user_data = cursor.fetchone()
                    if not user_data: # На случай редкой гонки состояний или ошибки
                        return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        return user_data # Возвращаем словарь
    except Exception as e:
        logger.error(f"Ошибка get_or_create_user для {user_id}: {e}")
        return None # Возвращаем None в случае ошибки БД

def update_balance(user_id: int, amount_change: float):
    """Обновляет баланс пользователя на указанную величину."""
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s;"
    sql_select = "SELECT balance FROM users WHERE user_id = %s;"
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_update, (amount_change, user_id))
                conn.commit() # Фиксируем обновление
                # Получаем обновленный баланс
                cursor.execute(sql_select, (user_id,))
                result = cursor.fetchone()
        return result[0] if result else None # Возвращаем только значение баланса
    except Exception as e:
        logger.error(f"Ошибка update_balance для {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    """Получает текущий баланс пользователя."""
    user_data = get_or_create_user(user_id)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, bonus_time: datetime.datetime):
     """Обновляет время последнего получения бонуса."""
     sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
     try:
         with get_db_conn() as conn:
             with conn.cursor() as cursor:
                 cursor.execute(sql, (bonus_time, user_id))
             conn.commit() # Фиксируем
     except Exception as e:
         logger.error(f"Ошибка update_last_bonus_time для {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Получает время последнего бонуса пользователя."""
    user_data = get_or_create_user(user_id)
    # psycopg2 обычно возвращает datetime объект
    return user_data.get('last_bonus') if user_data else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT):
    """Получает топ пользователей по балансу."""
    sql = "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT %s;"
    leaders = []
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (limit,))
                leaders = cursor.fetchall() # Получаем список словарей
        return leaders
    except Exception as e:
        logger.error(f"Ошибка get_leaderboard: {e}")
        return [] # Возвращаем пустой список в случае ошибки

# --- Standard Deck and Hand Utilities ---
# (create_deck, get_card_value, get_hand_value, format_hand - без изменений)
def create_deck(num_decks=NUM_DECKS):
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck); return deck
def get_card_value(card): return RANK_VALUES[card[0]]
def get_hand_value(hand):
    v=0;a=0
    for c in hand: r=c[0];v+=get_card_value(c);if r=='A': a+=1
    while v>21 and a>0: v-=10;a-=1
    return v
def format_hand(hand, hide_one=False):
    if hide_one and len(hand)>0: return f"[{hand[0][0]}{hand[0][1]}, ??]"
    return ", ".join([f"{c[0]}{c[1]}" for c in hand])

# --- Bot Command Handlers ---
# (start, help_command, balance_command, bonus, leaderboard - используют новые функции БД)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id)
    balance = user_data['balance'] if user_data else "Ошибка"
    await update.message.reply_text(
        f"Добро пожаловать/С возвращением! 👋 Ваш текущий баланс: {balance if isinstance(balance, (int, float)) else '??':.2f} фишек.\n"
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
    if current_balance is not None:
        await update.message.reply_text(f"Ваш баланс: {current_balance:.2f} фишек.")
    else:
        await update.message.reply_text("Не удалось получить ваш баланс.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if get_balance(user_id) is None: # Убедимся, что пользователь существует
       await update.message.reply_text("Произошла ошибка. Попробуйте /start сначала."); return

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus_time = get_last_bonus_time(user_id)
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
        else:
             await update.message.reply_text("Не удалось начислить бонус. Попробуйте позже.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders: await update.message.reply_text("Пока нет данных для таблицы лидеров."); return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"; tasks = []
    # Получаем информацию о пользователях асинхронно
    async def get_user_info(user_id):
        try: return await context.bot.get_chat(user_id)
        except Exception: return None

    users_info = await asyncio.gather(*(get_user_info(l['user_id']) for l in leaders))

    for i, leader in enumerate(leaders):
        user_id = leader['user_id']; balance = leader['balance']
        user: User | None = users_info[i]
        user_name = "Неизвестный игрок"
        if user:
            name = user.full_name.replace('[', '\\[').replace(']', '\\]').replace('_', '\\_').replace('*', '\\*').replace('`', '\\`') # Экранируем больше символов для V2
            user_name = user.mention_markdown_v2(name) if user.username else name
        else: logger.warning(f"Не удалось получить инфо для user_id {user_id} в лидерборде")

        leaderboard_text += f"{i+1}\\. {user_name} \\- `{balance:.2f}` F\n"

    await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)


# --- Blackjack Game Logic Handlers ---
# (blackjack_start, handle_blackjack_bet, show_game_state, handle_blackjack_action,
#  next_player_action_or_dealer, dealer_turn_job, determine_outcome)
# Используют новые функции БД для баланса и могут вызывать _draw_card_from_shoe
# Код этих функций почти не меняется, за исключением вызовов update_balance/get_balance

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id; chat_id = update.effective_chat.id
    reply_func = None
    if hasattr(update, 'message') and update.message: reply_func = update.message.reply_text
    elif hasattr(update, 'callback_query') and update.callback_query:
        reply_func = update.callback_query.message.reply_text; await update.callback_query.answer()
    else: return

    current_balance = get_balance(user_id)
    if current_balance is None: await reply_func("Ошибка баланса."); return

    if 'games' not in context.bot_data: context.bot_data['games'] = {}
    if chat_id in context.bot_data['games']:
        old = context.bot_data['games'][chat_id]
        if old.get('state') not in ['game_over', 'waiting_bet']: await reply_func("Игра уже идет."); return
        elif old.get('message_id'):
             try: await context.bot.delete_message(chat_id, old['message_id'])
             except Exception: pass
        del context.bot_data['games'][chat_id]

    if current_balance <= 0: await reply_func(f"Баланс 0. Возьмите /bonus."); return
    opts=[1,5,10,25,50,100]; v_bets=[b for b in opts if b <= current_balance]
    if not v_bets: await reply_func(f"Баланс ({current_balance:.2f}) < мин. ставки ({min(opts)})."); return

    keys = [[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in v_bets]]
    markup = InlineKeyboardMarkup(keys)
    msg = await reply_func(f"Баланс: {current_balance:.2f}. Ставка:", reply_markup=markup)
    context.bot_data['games'][chat_id] = {'player_id':user_id,'state':'waiting_bet','message_id':msg.message_id}


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    query = update.callback_query; user_id = query.from_user.id; chat_id = query.message.chat_id
    if chat_id not in context.bot_data.get('games',{}) or context.bot_data['games'][chat_id]['player_id'] != user_id: await query.answer("Не ваша игра.", show_alert=True); return
    game_state = context.bot_data['games'][chat_id]
    if game_state['state'] != 'waiting_bet': await query.answer(); return

    current_balance = get_balance(user_id)
    if current_balance is None or bet_amount > current_balance: await query.answer(f"Нет средств ({current_balance:.2f}).", show_alert=True); return
    if bet_amount <= 0: await query.answer("Ставка > 0.", show_alert=True); return

    update_balance(user_id, -bet_amount) # Снимаем ставку из БД

    deck = create_deck(NUM_DECKS); dsi=0.0; cd=0; ph=[]; dh=[]
    card,adj=_draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="player"); ph.append(card); dsi+=adj; cd+=1
    card,adj=_draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="dealer"); dh.append(card); dsi+=adj; cd+=1
    card,adj=_draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="player"); ph.append(card); dsi+=adj; cd+=1
    card,_ =_draw_card_from_shoe(deck,dsi,cd,NUM_DECKS,t="dealer"); dh.append(card); cd+=1

    pv=get_hand_value(ph); dv=get_hand_value(dh); d_up_v=get_card_value(dh[0])
    p_bj=pv==21 and len(ph)==2; d_ace_ten=d_up_v==11 or d_up_v==10; d_bj=False

    if d_ace_ten:
        if dv==21 and len(dh)==2: d_bj=True; dsi+=_get_rank_weight(dh[1])

    state='player_turn'; outcome=None; status='active'
    if p_bj:
        status='blackjack'
        if d_bj: update_balance(user_id, bet_amount); outcome=f"Пуш! Оба БЖ."; state='game_over'
        else: w=bet_amount*BLACKJACK_PAYOUT; update_balance(user_id, bet_amount+w); outcome=f"БЖ! Выигрыш {w:.2f} F."; state='game_over'
    elif d_bj: outcome=f"Дилер БЖ! Проигрыш {bet_amount} F."; state='game_over'

    game_state.update({
        'state':state,'deck':deck,'player_hands':[{'hand':ph,'bet':bet_amount,'status':status,'can_double':(not p_bj),'can_split':False}],
        'current_hand_index':0,'dealer_hand':dh,'_deck_state_index':dsi,'cards_dealt':cd,
        'initial_bet':bet_amount,'split_count':0,'outcome_text':outcome
    })
    await query.answer(f"Ставка {bet_amount}!"); await show_game_state(context, chat_id, game_state['message_id'])


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int = None):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]; pid=gs['player_id']; bal=get_balance(pid)
    if message_id: gs['message_id']=message_id
    if not gs.get('message_id'): return

    dh=gs['dealer_hand']; phs=gs['player_hands']; cur_idx=gs['current_hand_index']
    hide=gs['state']=='player_turn' and not(get_hand_value(dh)==21 and len(dh)==2)
    text=f"**Блекджек** | Баланс: {bal:.2f} F\n"; total_bet=sum(h['bet'] for h in phs); num_h=len(phs)
    text+=f"Ставка{' '*bool(num_h>1)}{'(всего)'*bool(num_h>1)}: {total_bet} F{f' ({num_h} рук)'*bool(num_h>1)}\n"; text+="-"*25+"\n"
    dv_s="??";
    if not hide: dv_s=str(get_hand_value(dh))
    elif dh: dv_s=str(get_card_value(dh[0]))+"+?"
    dh_s=format_hand(dh,hide); text+=f"**Дилер:** {dh_s} ({dv_s})\n\n"; text+="**Вы:**\n"; active_h=None
    for i,hd in enumerate(phs):
        h=hd['hand'];hv=get_hand_value(h);st=hd['status'];bet=hd['bet']; is_cur=(i==cur_idx and st=='active')
        ind = "▶️" if is_cur else "✅" if st=='stand' else "❌" if st=='bust' else "💰" if st=='blackjack' else "✔️"
        text+=f"{ind} Рука {i+1}: {format_hand(h)} ({hv}) [{bet} F]"
        if st=='bust':text+=" Перебор!"; elif st=='blackjack':text+=" БЖ!"; elif st=='stand': text+=" Стоп"; text+="\n"
        if is_cur: active_h=hd
    text+="\n"; kbd=[]
    if active_h:
        h=active_h['hand'];bet=active_h['bet']
        cd=active_h.get('can_double',False) and bal>=bet and len(h)==2
        cs=len(h)==2 and h[0][0]==h[1][0] and bal>=bet and gs['split_count']<MAX_SPLITS
        acts=[InlineKeyboardButton("Еще",callback_data=f"bj_action_hit_{cur_idx}"), InlineKeyboardButton("Хватит",callback_data=f"bj_action_stand_{cur_idx}")]
        subs=[]
        if cd: subs.append(InlineKeyboardButton(f"Удвоить", callback_data=f"bj_action_double_{cur_idx}"))
        if cs: subs.append(InlineKeyboardButton(f"Разделить", callback_data=f"bj_action_split_{cur_idx}"))
        kbd.append(acts);
        if subs: kbd.append(subs)
    elif gs['state']=='game_over':
        if gs.get('outcome_text'): text+=f"**Конец!**\n{gs['outcome_text']}\n"
        text+=f"Итог. баланс: {bal:.2f} F\n"; kbd.append([InlineKeyboardButton("Новая Игра", callback_data="bj_action_new_game")])
    elif gs['state']=='dealer_turn': text+="*Ход дилера...*\n"
    markup=InlineKeyboardMarkup(kbd) if kbd else None
    try: await context.bot.edit_message_text(chat_id=chat_id,message_id=gs['message_id'],text=text,reply_markup=markup,parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        if "not found" in str(e).lower():
            logger.warning(f"Msg {gs['message_id']} not found.");
            if gs['state']!='game_over':
                try: n_msg=await context.bot.send_message(chat_id,text,reply_markup=markup,parse_mode=ParseMode.MARKDOWN); gs['message_id']=n_msg.message_id
                except Exception as e2: logger.error(f"Failed resend: {e2}")
        else: logger.error(f"Update error {gs['message_id']}: {e}")

async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    query=update.callback_query; user_id=query.from_user.id; chat_id=query.message.chat_id
    try: await query.answer()
    except: pass
    if chat_id not in context.bot_data.get('games',{}) or context.bot_data['games'][chat_id]['player_id']!=user_id: return
    gs=context.bot_data['games'][chat_id]
    if gs['state']!='player_turn': return
    if not(0<=hand_index<len(gs['player_hands'])) or hand_index!=gs['current_hand_index']: return
    hd=gs['player_hands'][hand_index]
    if hd['status']!='active': return

    h=hd['hand']; deck=gs['deck']; bal=get_balance(user_id); bet=hd['bet']
    needs_reshuffle=len(deck)<(NUM_DECKS*52*(1.0-RESHUFFLE_PENETRATION))
    if needs_reshuffle: logger.info(f"Reshuffle mid-game {chat_id}"); gs['deck']=create_deck(NUM_DECKS); gs['_deck_state_index']=0.0; deck=gs['deck']

    proc=False
    if action=='hit':
        proc=True; card,adj=_draw_card_from_shoe(deck,gs['_deck_state_index'],gs['cards_dealt'],NUM_DECKS,t="player")
        if card:
            h.append(card); gs['_deck_state_index']+=adj; gs['cards_dealt']+=1; hd['can_double']=False; hd['can_split']=False
            hv=get_hand_value(h);
            if hv>21: hd['status']='bust'
            elif hv==21: hd['status']='stand'
            if hd['status']!='active': await next_player_action_or_dealer(context,chat_id)
            else: await show_game_state(context,chat_id)
        else: hd['status']='stand'; await next_player_action_or_dealer(context,chat_id)
    elif action=='stand': proc=True; hd['status']='stand'; await next_player_action_or_dealer(context,chat_id)
    elif action=='double':
        if hd.get('can_double',False) and bal>=bet and len(h)==2:
            proc=True; update_balance(user_id,-bet); hd['bet']+=bet
            card,adj=_draw_card_from_shoe(deck,gs['_deck_state_index'],gs['cards_dealt'],NUM_DECKS,t="player")
            if card: h.append(card); gs['_deck_state_index']+=adj; gs['cards_dealt']+=1; hd['status']='bust' if get_hand_value(h)>21 else 'stand'
            else: hd['status']='stand'
            await next_player_action_or_dealer(context,chat_id)
    elif action=='split':
        if len(h)==2 and h[0][0]==h[1][0] and bal>=bet and gs['split_count']<MAX_SPLITS:
            proc=True; update_balance(user_id,-bet); gs['split_count']+=1
            card_m=h.pop(); n_hd={'hand':[card_m],'bet':bet,'status':'active','can_double':True,'can_split':False}
            gs['player_hands'].insert(hand_index+1,n_hd)
            card1,adj1=_draw_card_from_shoe(deck,gs['_deck_state_index'],gs['cards_dealt'],NUM_DECKS,t="player"); gs['_deck_state_index']+=adj1; gs['cards_dealt']+=1
            card2,adj2=_draw_card_from_shoe(deck,gs['_deck_state_index'],gs['cards_dealt'],NUM_DECKS,t="player"); gs['_deck_state_index']+=adj2; gs['cards_dealt']+=1
            if card1: h.append(card1)
            if card2: n_hd['hand'].append(card2)
            is_ace=h[0][0]=='A'
            if is_ace: hd['status']='stand';hd['can_double']=False; n_hd['status']='stand';n_hd['can_double']=False; await next_player_action_or_dealer(context,chat_id)
            else:
                hd['can_double']=bool(card1); hd['can_split']=bool(card1 and len(h)==2 and h[0][0]==card1[0])
                n_hd['can_double']=bool(card2); n_hd['can_split']=bool(card2 and len(n_hd['hand'])==2 and n_hd['hand'][0][0]==card2[0])
                if get_hand_value(h)==21: hd['status']='stand'
                await show_game_state(context,chat_id)


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs['state']!='player_turn': return
    phs=gs['player_hands']; cur_idx=gs['current_hand_index']; next_idx=cur_idx+1
    while next_idx<len(phs):
        if phs[next_idx]['status']=='active': gs['current_hand_index']=next_idx; await show_game_state(context,chat_id); return
        next_idx+=1
    gs['state']='dealer_turn'; await show_game_state(context,chat_id)
    context.job_queue.run_once(dealer_turn_job,DEALER_TURN_DELAY,chat_id=chat_id,data=chat_id,name=f"dealer_{chat_id}")


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id=context.job.data
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs['state']!='dealer_turn': return
    deck=gs['deck']; dh=gs['dealer_hand']
    can_win=any(p['status'] not in ['bust','blackjack'] for p in gs['player_hands'])
    if not can_win:
        if len(dh)==2 and not(get_hand_value(dh)==21): gs['_deck_state_index']+=_get_rank_weight(dh[1])
        await determine_outcome(context,chat_id,False); return
    d_bj=get_hand_value(dh)==21 and len(dh)==2
    if not d_bj and len(dh)==2: gs['_deck_state_index']+=_get_rank_weight(dh[1])
    while True:
        dv=get_hand_value(dh); ac=sum(1 for c in dh if c[0]=='A'); is_s=ac>0 and (dv-11*ac<11)
        hit=False;
        if dv<17: hit=True
        elif dv==17 and is_s and DEALER_HITS_SOFT_17: hit=True
        if not hit: break
        if len(deck)<(NUM_DECKS*52*(1.0-RESHUFFLE_PENETRATION)): gs['deck']=create_deck(NUM_DECKS);gs['_deck_state_index']=0.0;deck=gs['deck']
        card,adj=_draw_card_from_shoe(deck,gs['_deck_state_index'],gs['cards_dealt'],NUM_DECKS,t="dealer")
        if card: dh.append(card); gs['_deck_state_index']+=adj; gs['cards_dealt']+=1
        else: break
    await determine_outcome(context,chat_id,d_bj)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack: bool):
    if chat_id not in context.bot_data.get('games',{}): return
    gs=context.bot_data['games'][chat_id]
    if gs['state']=='game_over' and gs.get('outcome_text'): await show_game_state(context,chat_id); return

    pid=gs['player_id']; phs=gs['player_hands']; dh=gs['dealer_hand']; dv=get_hand_value(dh); d_bust=dv>21
    bal_before=get_balance(pid); outcomes=[]; total_payout=0

    for i, hd in enumerate(phs):
        h=hd['hand']; bet=hd['bet']; st=hd['status']; pv=get_hand_value(h); p_bj=st=='blackjack'
        mult=0; outc=""; pfx=f"Р{i+1}: " if len(phs)>1 else ""
        if st=='bust': outc=f"{pfx}Перебор({pv}). {-bet} F"; mult=0
        elif p_bj: w=bet*BLACKJACK_PAYOUT; outc=f"{pfx}БЖ! +{w:.2f} F"; mult=1+BLACKJACK_PAYOUT
        elif dealer_had_blackjack: outc=f"{pfx}{pv} vs БЖ. {-bet} F"; mult=0
        elif d_bust: outc=f"{pfx}{pv} vs Перебор({dv})! +{bet} F"; mult=2
        elif pv>dv: outc=f"{pfx}{pv} > {dv}. +{bet} F"; mult=2
        elif pv==dv: outc=f"{pfx}{pv} = {dv}. Пуш."; mult=1
        else: outc=f"{pfx}{pv} < {dv}. {-bet} F"; mult=0
        total_payout+=bet*mult; outcomes.append(outc)

    if total_payout>0: update_balance(pid,total_payout) # Обновляем баланс в БД
    final_bal=get_balance(pid); total_bet=sum(h['bet'] for h in phs); net_change=total_payout-total_bet
    gs['state']='game_over'; gs['outcome_text']="\n".join(outcomes)+f"\n\n**Итог: {net_change:+.2f} F**"
    await show_game_state(context,chat_id)


# <<< --- Internal Dealing Logic (Moved Down) --- >>>
# (Код _RANK_WEIGHTS, _get_rank_weight, _calculate_dealing_preference, _draw_card_from_shoe остается здесь)
_RANK_WEIGHTS = { "2": 0.5, "7": 0.5, "3": 1, "4": 1, "6": 1, "5": 1.5, "8": 0, "9": -0.5, "T": -1, "J": -1, "Q": -1, "K": -1, "A": -1 }
def _get_rank_weight(card): return _RANK_WEIGHTS.get(card[0], 0)
def _calculate_dealing_preference(i,r):
    if r<=0: return 0
    d=max(0.5,r/52.0); return i/d
def _draw_card_from_shoe(d, i, c, n, t="player"):
    if not d: return None,0
    r=(n*52)-c; p=_calculate_dealing_preference(i,r); u=1.5; l=-1.0
    h=['T','J','Q','K','A']; w=['4','5','6']; pot=[]; sel=random.random()<0.85
    if sel:
        can=None
        if t=="player":
            if p>=u: can=h
            elif p<=l: can=w
        elif t=="dealer":
            if p>=u: can=w
            elif p<=l: can=h
        if can: pot=[x for x in d if x[0] in can]
    if not pot: pot=list(d)
    if not pot:
        if not d: return None,0
        pot=list(d)
    try: ch=random.choice(pot); d.remove(ch)
    except ValueError:
        logger.error(f"Card {ch} not found. Choosing random.");
        if d: ch=random.choice(d); d.remove(ch)
        else: return None,0
    adj=_get_rank_weight(ch); return ch, adj
# <<< --- End of Internal Dealing Logic --- >>>

# --- Callback Query Handler ---
# (button_callback_handler - без изменений, вызывает функции выше)
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; data=query.data; user_id=query.from_user.id
    if get_balance(user_id) is None: await query.answer("Нет данных, /start.", show_alert=True); return

    if data.startswith("bj_bet_"):
        try: bet = int(data.split("_")[2]); await handle_blackjack_bet(update, context, bet)
        except Exception as e: logger.error(f"Bet callback error: {e}"); await query.answer("Ошибка ставки.", show_alert=True)
    elif data.startswith("bj_action_"):
        parts = data.split("_")
        try:
            action = parts[2]
            if action == "new_game":
                 await query.answer(); await blackjack_start(update, context)
                 try: await query.delete_message()
                 except Exception: pass
                 return
            h_idx = int(parts[3]); await handle_blackjack_action(update, context, action, h_idx)
        except Exception as e:
             logger.error(f"Action callback error: {e}")
             try: await query.answer("Ошибка.", show_alert=True)
             except Exception: pass
    else:
        try: await query.answer()
        except Exception: pass

# --- Main Function ---
def main():
    """Starts the bot."""
    # Инициализацию БД лучше сделать вручную или через Build Command на Render
    # init_db() # Закомментировано, т.к. может вызвать проблемы при частых перезапусках
    import asyncio # Нужен для leaderboard

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    if 'games' not in application.bot_data: application.bot_data['games'] = {}

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    print("Бот запускается...")
    application.run_polling()
    print("Бот остановлен.")

if __name__ == "__main__":
    # Проверка наличия переменных окружения при запуске
    if not BOT_TOKEN or not DATABASE_URL:
        print("Ошибка: Не заданы переменные окружения BOT_TOKEN и/или DATABASE_URL")
        exit(1)
    # Можно выполнить инициализацию БД здесь один раз, если нужно
    init_db()
    main()