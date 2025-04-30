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
  # use_reloader=False важен для избежания двойного запуска в Render
  app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web_server, daemon=True)
    t.start()
    logger.info("Keep-alive web server started.")

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Проверка наличия переменных окружения при старте модуля
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
# Уменьшаем логирование от веб-сервера keep-alive
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

# init_db() лучше выполнять вручную один раз.
def init_db_manual():
    """SQL для ручной инициализации таблицы."""
    sql = """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            balance DOUBLE PRECISION DEFAULT 0,
            last_bonus TIMESTAMP WITHOUT TIME ZONE
        );
    """
    print("--- SQL для инициализации таблицы users ---")
    print(sql)
    print("--- Выполните этот SQL запрос в вашей базе данных один раз. ---")
    # В реальном коде эта функция не вызывается автоматически.

def get_or_create_user(user_id: int):
    """Получает данные пользователя или создает нового с начальным балансом."""
    select_sql = "SELECT * FROM users WHERE user_id = %s;"
    insert_sql = """INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING;"""
    user_data = None
    try:
        # Используем 'with' для управления соединением и курсором
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()
                if user_data is None:
                    cursor.execute(insert_sql, (user_id, INITIAL_BALANCE, None))
                    logger.info(f"Создан новый пользователь в БД: {user_id}")
                    # Повторно получаем данные после вставки
                    cursor.execute(select_sql, (user_id,))
                    user_data = cursor.fetchone()
                    # Если все еще None после создания (маловероятно), возвращаем дефолтные
                    if not user_data:
                        return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        return user_data # Возвращаем словарь
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_or_create_user) для {user_id}: {e}")
        # Возвращаем None или дефолтное значение в случае ошибки
        return None # Или {'user_id': user_id, 'balance': 0, 'last_bonus': None} если хотим избежать None
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_or_create_user) для {user_id}: {e}")
        return None

def update_balance(user_id: int, amount_change: float):
    """Обновляет баланс пользователя и возвращает НОВЫЙ баланс."""
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    new_balance = None
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_update, (amount_change, user_id))
                result = cursor.fetchone()
                if result:
                    new_balance = result[0]
        return new_balance
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (update_balance) для {user_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка (update_balance) для {user_id}: {e}")
        return None

def get_balance(user_id: int) -> float | None:
    """Получает текущий баланс пользователя."""
    user_data = get_or_create_user(user_id)
    # Обрабатываем случай, если get_or_create_user вернул None из-за ошибки
    return user_data['balance'] if user_data else 0.0 # Возвращаем 0 при ошибке, чтобы избежать None

def update_last_bonus_time(user_id: int, bonus_time: datetime.datetime):
     """Обновляет время последнего получения бонуса."""
     sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
     try:
         with get_db_conn() as conn:
             with conn.cursor() as cursor:
                 cursor.execute(sql, (bonus_time, user_id))
     except psycopg2.Error as e:
         logger.error(f"Ошибка БД (update_last_bonus_time) для {user_id}: {e}")
     except Exception as e:
        logger.error(f"Неожиданная ошибка (update_last_bonus_time) для {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Получает время последнего бонуса пользователя."""
    user_data = get_or_create_user(user_id)
    # get_or_create_user может вернуть None при ошибке
    return user_data.get('last_bonus') if user_data else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT):
    """Получает топ пользователей по балансу."""
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;" # Исключаем нулевые балансы
    leaders = []
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (limit,))
                leaders = cursor.fetchall()
        return leaders
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_leaderboard): {e}")
        return []
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_leaderboard): {e}")
        return []

# --- Standard Deck and Hand Utilities ---
def create_deck(num_decks=NUM_DECKS):
    """Creates and shuffles a deck consisting of multiple standard decks."""
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card):
    """Returns the numerical value of a card."""
    rank = card[0]
    # Обработка случая, если card == None (хотя не должно быть)
    return RANK_VALUES.get(rank, 0) if card else 0

def get_hand_value(hand):
    """Calculates the value of a hand, handling Aces correctly."""
    value = 0
    ace_count = 0
    if not hand: # Проверка на пустую руку
        return 0
    for card in hand:
        # Добавим проверку, что card не None
        if card:
            rank = card[0]
            value += get_card_value(card)
            if rank == 'A':
                ace_count += 1
        else:
            logger.warning("Обнаружена None карта в руке при подсчете значения.")

    # Adjust for Aces if busting
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
    return value

def format_hand(hand, hide_one=False):
    """Formats a hand for display."""
    if not hand: # Обработка пустой руки
        return "Пусто"
    if hide_one and len(hand) > 0:
        first_card = hand[0]
        return f"[{first_card[0]}{first_card[1]}, ??]" if first_card else "[??, ??]"
    # Формируем строку, пропуская None карты, если они вдруг появятся
    return ", ".join([f"{card[0]}{card[1]}" for card in hand if card])

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id) # Создаст пользователя если его нет
    balance = user_data['balance'] if user_data else 0.0 # Безопасное значение по умолчанию
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
    current_balance = get_balance(user_id) # get_balance теперь возвращает 0.0 при ошибке
    await update.message.reply_text(f"Ваш баланс: {current_balance:.2f} фишек.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Убедимся, что пользователь существует и получаем его данные
    user_data = get_or_create_user(user_id)
    if not user_data:
       await update.message.reply_text("Произошла ошибка с вашими данными. Попробуйте /start."); return

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus_time = user_data.get('last_bonus') # Берем из полученных данных
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time and now - last_bonus_time < cooldown:
        time_left = last_bonus_time + cooldown - now
        hours, remainder = divmod(time_left.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(f"Бонус уже получен. Попробуйте через {int(hours)} ч {int(minutes)} мин.")
    else:
        new_balance = update_balance(user_id, BONUS_AMOUNT) # Обновляем баланс в БД
        if new_balance is not None:
            update_last_bonus_time(user_id, now) # Обновляем время бонуса в БД
            await update.message.reply_text(f"✅ Бонус {BONUS_AMOUNT} фишек получен! Новый баланс: {new_balance:.2f} фишек.")
        else:
             await update.message.reply_text("Не удалось начислить бонус (ошибка БД). Попробуйте позже.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("Пока нет данных для таблицы лидеров."); return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"

    async def get_user_info(user_id):
        try:
            # Используем кеширование в context.bot_data для уменьшения запросов к API
            # Ключ кеша - user_id
            if user_id in context.bot_data.get('user_cache', {}):
                return context.bot_data['user_cache'][user_id]
            user = await context.bot.get_chat(user_id)
            if 'user_cache' not in context.bot_data:
                 context.bot_data['user_cache'] = {}
            context.bot_data['user_cache'][user_id] = user # Сохраняем в кеш
            return user
        except BadRequest as e:
             # Пользователь мог заблокировать бота или не существует
             logger.warning(f"BadRequest getting info for user {user_id}: {e}")
             return None
        except Exception as e:
            logger.warning(f"Failed to get info for user {user_id} in leaderboard: {e}")
            return None

    user_info_tasks = [get_user_info(l['user_id']) for l in leaders]
    users_info = await asyncio.gather(*user_info_tasks)

    # Очистка старого кеша пользователей (например, раз в час) - опционально
    # context.job_queue.run_repeating(clear_user_cache, interval=3600, first=3600, name='clear_user_cache')

    for i, leader in enumerate(leaders):
        user_id = leader['user_id']
        balance = leader['balance']
        user: User | None = users_info[i]

        user_name = f"ID: {user_id}" # Имя по умолчанию
        if user:
            name = user.full_name
            # Экранирование символов для MarkdownV2
            for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
                name = name.replace(char, f'\\{char}')
            user_name = user.mention_markdown_v2(name) if user.username else name
        # else: Используем имя по умолчанию

        # Используем \ для экранирования точки после номера
        leaderboard_text += f"{i+1}\\. {user_name} \\- `{balance:.2f}` F\n"

    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending leaderboard (MarkdownV2): {e}")
        try:
            # Пробуем отправить без Markdown как запасной вариант
            plain_text = update.message.text_markdown_v2 # Получаем текст без форматирования
            await update.message.reply_text(plain_text)
        except Exception as fallback_e:
             logger.error(f"Error sending plain leaderboard fallback: {fallback_e}")


# --- Blackjack Game Logic Handlers ---

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    reply_func = None

    if hasattr(update, 'message') and update.message:
        reply_func = update.message.reply_text
    elif hasattr(update, 'callback_query') and update.callback_query:
        reply_func = update.callback_query.message.reply_text
        try: await update.callback_query.answer()
        except Exception: pass
    else: return

    current_balance = get_balance(user_id)
    if current_balance is None: # Проверка на ошибку БД
        await reply_func("Ошибка получения баланса. Попробуйте позже.")
        return

    if 'games' not in context.bot_data: context.bot_data['games'] = {}
    if chat_id in context.bot_data['games']:
        old_game = context.bot_data['games'][chat_id]
        if old_game.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Игра уже идет в этом чате.")
            return
        if old_game.get('message_id'):
            try: await context.bot.delete_message(chat_id, old_game['message_id'])
            except Exception: pass
        del context.bot_data['games'][chat_id]

    if current_balance <= 0:
        await reply_func(f"Баланс 0 фишек. Используйте /bonus.")
        return

    bet_options = [1, 5, 10, 25, 50, 100]
    valid_bets = [b for b in bet_options if b <= current_balance]
    if not valid_bets:
        await reply_func(f"Баланс ({current_balance:.2f}) меньше мин. ставки ({min(bet_options)}).")
        return

    keyboard = [[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in valid_bets]]
    markup = InlineKeyboardMarkup(keyboard)
    try:
        msg = await reply_func(f"Баланс: {current_balance:.2f}. Ваша ставка?", reply_markup=markup)
        # Сохраняем состояние игры в памяти
        context.bot_data['games'][chat_id] = {
            'player_id': user_id,
            'state': 'waiting_bet',
            'message_id': msg.message_id
        }
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение для ставки: {e}")


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if chat_id not in context.bot_data.get('games', {}) or context.bot_data['games'][chat_id]['player_id'] != user_id:
        await query.answer("Не ваша игра.", show_alert=True); return
    game_state = context.bot_data['games'][chat_id]
    if game_state['state'] != 'waiting_bet':
        await query.answer(); return

    current_balance = get_balance(user_id)
    if current_balance is None:
         await query.answer("Ошибка проверки баланса.", show_alert=True); return
    if bet_amount > current_balance:
        await query.answer(f"Недостаточно средств ({current_balance:.2f}).", show_alert=True); return
    if bet_amount <= 0:
        await query.answer("Ставка > 0.", show_alert=True); return

    # Списываем ставку
    if update_balance(user_id, -bet_amount) is None:
        await query.answer("Ошибка обновления баланса.", show_alert=True); return

    # --- Раздача карт ---
    deck = create_deck(NUM_DECKS); dsi = 0.0; cd = 0; ph = []; dh = []
    try:
        # Используем t= вместо target=
        card, adj = _draw_card_from_shoe(deck, dsi, cd, NUM_DECKS, t="player"); ph.append(card); dsi += adj; cd += 1
        card, adj = _draw_card_from_shoe(deck, dsi, cd, NUM_DECKS, t="dealer"); dh.append(card); dsi += adj; cd += 1
        card, adj = _draw_card_from_shoe(deck, dsi, cd, NUM_DECKS, t="player"); ph.append(card); dsi += adj; cd += 1
        card, _   = _draw_card_from_shoe(deck, dsi, cd, NUM_DECKS, t="dealer"); dh.append(card); cd += 1
    except TypeError as e:
        logger.error(f"Ошибка при раздаче карт (_draw_card_from_shoe): {e}")
        await query.answer("Ошибка раздачи карт.", show_alert=True)
        update_balance(user_id, bet_amount) # Возвращаем ставку при ошибке
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id] # Удаляем игру
        return
    except IndexError: # Если колода неожиданно закончилась
         logger.error(f"Колода закончилась во время раздачи для игры {chat_id}")
         await query.answer("Ошибка: закончилась колода.", show_alert=True)
         update_balance(user_id, bet_amount) # Возвращаем ставку
         if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id]
         return


    # --- Проверка на Блекджек ---
    pv = get_hand_value(ph); dv = get_hand_value(dh); d_up_v = get_card_value(dh[0]) if dh else 0
    p_bj = (pv == 21 and len(ph) == 2)
    d_ace_ten = (d_up_v == 11 or d_up_v == 10)
    d_bj = False
    if d_ace_ten and dv == 21 and len(dh) == 2:
        d_bj = True; dsi += _get_rank_weight(dh[1]) if len(dh) > 1 else 0

    # --- Определение исхода Блекджека ---
    state = 'player_turn'; outcome = None; status = 'active'
    if p_bj:
        status = 'blackjack'
        if d_bj:
            update_balance(user_id, bet_amount) # Возврат
            outcome = f"Ничья! У обоих Блекджек. Ставка {bet_amount} F возвращена."
            state = 'game_over'
        else:
            w = bet_amount * BLACKJACK_PAYOUT
            update_balance(user_id, bet_amount + w) # Выплата
            outcome = f"БЛЕКДЖЕК! Вы выиграли {w:.2f} F."
            state = 'game_over'
    elif d_bj:
        outcome = f"У дилера Блекджек! Проигрыш {bet_amount} F."
        state = 'game_over'

    # --- Обновление состояния игры ---
    game_state.update({
        'state': state, 'deck': deck,
        'player_hands': [{'hand': ph, 'bet': bet_amount, 'status': status, 'can_double': (not p_bj), 'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': dh,
        '_deck_state_index': dsi, 'cards_dealt': cd,
        'initial_bet': bet_amount, 'split_count': 0, 'outcome_text': outcome
    })

    await query.answer(f"Ставка {bet_amount} принята!")
    await show_game_state(context, chat_id, game_state['message_id'])


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int = None):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]; pid = gs['player_id']
    bal = get_balance(pid) # Получаем актуальный баланс
    if bal is None: bal = 0.0 # Значение по умолчанию при ошибке

    if message_id: gs['message_id'] = message_id
    current_message_id = gs.get('message_id')
    if not current_message_id: return # Не можем обновить без ID

    dh = gs['dealer_hand']; phs = gs['player_hands']; cur_idx = gs['current_hand_index']
    hide = gs['state'] == 'player_turn' and not (get_hand_value(dh) == 21 and len(dh) == 2)

    # --- Формирование текста (с проверками) ---
    text = f"**Блекджек** | Баланс: {bal:.2f} F\n"
    total_bet = sum(h['bet'] for h in phs if isinstance(h, dict) and 'bet' in h); num_h = len(phs)
    text += f"Ставка{' (всего)' if num_h > 1 else ''}: {total_bet} F{f' ({num_h} рук)' if num_h > 1 else ''}\n"
    text += "---------------------------------\n"
    dv = get_hand_value(dh); dv_s = "??"
    if not hide: dv_s = str(dv)
    elif dh: dv_s = str(get_card_value(dh[0])) + "+?" if dh[0] else "??"
    dh_s = format_hand(dh, hide); text += f"**Дилер:** {dh_s} ({dv_s})\n\n"; text += "**Вы:**\n"; active_h = None
    for i, hd in enumerate(phs):
        if not isinstance(hd, dict): continue # Пропускаем некорректные элементы
        h=hd.get('hand'); hv=get_hand_value(h); st=hd.get('status'); bet=hd.get('bet', 0)
        is_cur = (i == cur_idx and st == 'active')
        ind = "▶️" if is_cur else "✅" if st=='stand' else "❌" if st=='bust' else "💰" if st=='blackjack' else "✔️"
        text += f"{ind} Рука {i+1}: {format_hand(h)} ({hv}) [{bet} F]"
        if st == 'bust': text += " Перебор!"
        elif st == 'blackjack': text += " БЖ!"
        elif st == 'stand': text += " Стоп"
        text += "\n"
        if is_cur: active_h = hd
    text += "\n"
    # --- Формирование кнопок (с проверками) ---
    kbd = []
    if active_h and isinstance(active_h, dict):
        h = active_h.get('hand'); bet = active_h.get('bet', 0)
        # Используем актуальный баланс из БД
        can_double = active_h.get('can_double', False) and bal >= bet and h and len(h) == 2
        can_split = h and len(h) == 2 and h[0] and h[1] and h[0][0] == h[1][0] and bal >= bet and gs.get('split_count', 0) < MAX_SPLITS
        acts = [InlineKeyboardButton("Еще", callback_data=f"bj_action_hit_{cur_idx}"),
                InlineKeyboardButton("Хватит", callback_data=f"bj_action_stand_{cur_idx}")]
        subs = []
        if can_double: subs.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_action_double_{cur_idx}"))
        if can_split: subs.append(InlineKeyboardButton("Разделить", callback_data=f"bj_action_split_{cur_idx}"))
        kbd.append(acts)
        if subs: kbd.append(subs)
    elif gs.get('state') == 'game_over':
        if gs.get('outcome_text'): text += f"**Конец Игры!**\n{gs['outcome_text']}\n"
        text += f"Итоговый баланс: {bal:.2f} F\n"; kbd.append([InlineKeyboardButton("Новая Игра", callback_data="bj_action_new_game")])
    elif gs.get('state') == 'dealer_turn': text += "*Ход дилера...*\n"
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    # --- Отправка/Редактирование сообщения ---
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=current_message_id, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        if "not found" in str(e).lower():
            logger.warning(f"Msg {current_message_id} not found.");
            if gs.get('state') != 'game_over':
                try: n_msg = await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN); gs['message_id'] = n_msg.message_id
                except Exception as e2: logger.error(f"Failed resend: {e2}")
        else: logger.error(f"Update error {current_message_id}: {e}")


async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    query = update.callback_query; user_id = query.from_user.id; chat_id = query.message.chat_id
    try: await query.answer()
    except: pass
    if chat_id not in context.bot_data.get('games', {}) or context.bot_data['games'][chat_id]['player_id'] != user_id: return
    gs = context.bot_data['games'][chat_id]
    if gs['state'] != 'player_turn': return
    if not (0 <= hand_index < len(gs['player_hands'])) or hand_index != gs['current_hand_index']: return
    hd = gs['player_hands'][hand_index]
    if hd['status'] != 'active': return

    h = hd['hand']; deck = gs['deck']; bal = get_balance(user_id); bet = hd['bet']
    if bal is None: await query.answer("Ошибка баланса.", show_alert=True); return # Проверка баланса

    needs_reshuffle = len(deck) < (NUM_DECKS * 52 * (1.0 - RESHUFFLE_PENETRATION))
    if needs_reshuffle: logger.info(f"Reshuffle {chat_id}"); gs['deck'] = create_deck(NUM_DECKS); gs['_deck_state_index'] = 0.0; deck = gs['deck']

    if action == 'hit':
        card, adj = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, t="player") # Используем t=
        if card:
            h.append(card); gs['_deck_state_index'] += adj; gs['cards_dealt'] += 1
            hd['can_double'] = False; hd['can_split'] = False; hv = get_hand_value(h)
            if hv > 21: hd['status'] = 'bust'
            elif hv == 21: hd['status'] = 'stand'
            if hd['status'] != 'active': await next_player_action_or_dealer(context, chat_id)
            else: await show_game_state(context, chat_id)
        else: hd['status'] = 'stand'; await next_player_action_or_dealer(context, chat_id)

    elif action == 'stand':
        hd['status'] = 'stand'; await next_player_action_or_dealer(context, chat_id)

    elif action == 'double':
        if hd.get('can_double', False) and bal >= bet and h and len(h) == 2:
            if update_balance(user_id, -bet) is not None:
                hd['bet'] += bet
                card, adj = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, t="player") # Используем t=
                if card: h.append(card); gs['_deck_state_index'] += adj; gs['cards_dealt'] += 1; hd['status'] = 'bust' if get_hand_value(h) > 21 else 'stand'
                else: hd['status'] = 'stand'
                await next_player_action_or_dealer(context, chat_id)
            else: await query.answer("Ошибка баланса!", show_alert=True)

    elif action == 'split':
        if h and len(h) == 2 and h[0] and h[1] and h[0][0] == h[1][0] and bal >= bet and gs.get('split_count', 0) < MAX_SPLITS:
             if update_balance(user_id, -bet) is not None:
                 gs['split_count'] = gs.get('split_count', 0) + 1; card_m = h.pop()
                 n_hd = {'hand': [card_m], 'bet': bet, 'status': 'active', 'can_double': True, 'can_split': False}
                 gs['player_hands'].insert(hand_index + 1, n_hd)
                 # Используем t=
                 card1, adj1 = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, t="player"); gs['_deck_state_index'] += adj1; gs['cards_dealt'] += 1
                 card2, adj2 = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, t="player"); gs['_deck_state_index'] += adj2; gs['cards_dealt'] += 1
                 if card1: h.append(card1)
                 if card2: n_hd['hand'].append(card2)
                 is_ace = h and h[0] and h[0][0] == 'A'
                 if is_ace: hd['status']='stand'; hd['can_double']=False; n_hd['status']='stand'; n_hd['can_double']=False; await next_player_action_or_dealer(context, chat_id)
                 else:
                     hd['can_double']=bool(card1); hd['can_split']=bool(card1 and len(h)==2 and h[0] and h[1] and h[0][0]==card1[0])
                     n_hd['can_double']=bool(card2); n_hd['can_split']=bool(card2 and len(n_hd['hand'])==2 and n_hd['hand'][0] and n_hd['hand'][1] and n_hd['hand'][0][0]==card2[0])
                     if get_hand_value(h)==21: hd['status']='stand'
                     await show_game_state(context, chat_id)
             else: await query.answer("Ошибка баланса!", show_alert=True)


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs.get('state') != 'player_turn': return # Проверяем get для безопасности
    phs = gs.get('player_hands', []); cur_idx = gs.get('current_hand_index', -1); next_idx = cur_idx + 1
    while next_idx < len(phs):
        # Проверяем статус следующей руки
        if isinstance(phs[next_idx], dict) and phs[next_idx].get('status') == 'active':
            gs['current_hand_index'] = next_idx; await show_game_state(context, chat_id); return
        next_idx += 1
    # Если не нашли активную руку, переходим к дилеру
    gs['state'] = 'dealer_turn'; await show_game_state(context, chat_id)
    context.job_queue.run_once(dealer_turn_job, DEALER_TURN_DELAY, chat_id=chat_id, data=chat_id, name=f"dealer_{chat_id}")


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs.get('state') != 'dealer_turn': return

    deck = gs.get('deck', []); dh = gs.get('dealer_hand', [])
    phs = gs.get('player_hands', [])
    can_win = any(isinstance(p, dict) and p.get('status') not in ['bust', 'blackjack'] for p in phs)

    if not can_win: # Если игрок не может выиграть, дилер не играет
        if dh and len(dh) == 2 and not (get_hand_value(dh) == 21):
             gs['_deck_state_index'] = gs.get('_deck_state_index', 0.0) + _get_rank_weight(dh[1] if len(dh) > 1 else None)
        await determine_outcome(context, chat_id, False); return

    d_bj = dh and get_hand_value(dh) == 21 and len(dh) == 2
    # Добавляем вес скрытой карты, если не было БЖ у дилера на раздаче
    if dh and not d_bj and len(dh) == 2:
        gs['_deck_state_index'] = gs.get('_deck_state_index', 0.0) + _get_rank_weight(dh[1] if len(dh) > 1 else None)

    # Цикл добора дилера
    while True:
        dv = get_hand_value(dh); ac = sum(1 for c in dh if c and c[0] == 'A'); is_s = ac > 0 and (dv - 11 * ac < 11)
        hit = (dv < 17) or (dv == 17 and is_s and DEALER_HITS_SOFT_17)
        if not hit: break # Стоп
        # Проверка колоды и перемешивание
        if len(deck) < (NUM_DECKS * 52 * (1.0 - RESHUFFLE_PENETRATION)):
             logger.info(f"Reshuffle dealer turn {chat_id}"); gs['deck'] = create_deck(NUM_DECKS); gs['_deck_state_index'] = 0.0; deck = gs['deck']
        # Дилер берет карту
        card, adj = _draw_card_from_shoe(deck, gs.get('_deck_state_index', 0.0), gs.get('cards_dealt', 0), NUM_DECKS, t="dealer") # Используем t=
        if card: dh.append(card); gs['_deck_state_index'] = gs.get('_deck_state_index', 0.0) + adj; gs['cards_dealt'] = gs.get('cards_dealt', 0) + 1
        else: break # Колода пуста
    await determine_outcome(context, chat_id, d_bj)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack: bool):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs.get('state') == 'game_over' and gs.get('outcome_text'): await show_game_state(context, chat_id); return

    pid = gs.get('player_id'); phs = gs.get('player_hands', []); dh = gs.get('dealer_hand', [])
    if not pid: return # Не нашли ID игрока
    dv = get_hand_value(dh); d_bust = dv > 21
    bal_before = get_balance(pid); outcomes = []; total_payout = 0
    if bal_before is None: bal_before = 0.0 # Безопасное значение

    for i, hd in enumerate(phs):
        if not isinstance(hd, dict): continue
        h = hd.get('hand'); bet = hd.get('bet', 0); st = hd.get('status'); pv = get_hand_value(h); p_bj = st == 'blackjack'
        mult = 0; outc = ""; pfx = f"Р{i+1}: " if len(phs) > 1 else ""
        if st == 'bust': outc = f"{pfx}Перебор({pv}). {-bet} F"; mult = 0
        elif p_bj: w = bet * BLACKJACK_PAYOUT; outc = f"{pfx}БЖ! +{w:.2f} F"; mult = 1 + BLACKJACK_PAYOUT
        elif dealer_had_blackjack: outc = f"{pfx}{pv} vs БЖ. {-bet} F"; mult = 0
        elif d_bust: outc = f"{pfx}{pv} vs Перебор({dv})! +{bet} F"; mult = 2
        elif pv > dv: outc = f"{pfx}{pv} > {dv}. +{bet} F"; mult = 2
        elif pv == dv: outc = f"{pfx}{pv} = {dv}. Пуш."; mult = 1
        else: outc = f"{pfx}{pv} < {dv}. {-bet} F"; mult = 0
        total_payout += bet * mult; outcomes.append(outc)

    # Обновляем баланс в БД одним запросом
    if total_payout > 0:
        update_balance(pid, total_payout)

    # Получаем итоговый баланс для расчета чистого изменения
    final_bal = get_balance(pid)
    if final_bal is None: final_bal = bal_before + total_payout # Примерный расчет при ошибке
    total_bet = sum(h.get('bet', 0) for h in phs if isinstance(h, dict))
    net_change = total_payout - total_bet # Считаем разницу между выплатой и общей ставкой
    gs['state'] = 'game_over'; gs['outcome_text'] = "\n".join(outcomes) + f"\n\n**Итог: {net_change:+.2f} F**"
    await show_game_state(context, chat_id) # Показываем финальный результат


# <<< --- Internal Dealing Logic (Moved Down, Unchanged) --- >>>
_RANK_WEIGHTS = { "2": 0.5, "7": 0.5, "3": 1, "4": 1, "6": 1, "5": 1.5, "8": 0, "9": -0.5, "T": -1, "J": -1, "Q": -1, "K": -1, "A": -1 }
def _get_rank_weight(card): return _RANK_WEIGHTS.get(card[0], 0) if card else 0 # Добавлена проверка на None
def _calculate_dealing_preference(i,r):
    if r <= 0: return 0
    d = max(0.5, r / 52.0); return i / d
def _draw_card_from_shoe(d, i, c, n, t="player"): # Используем t=
    if not d: logger.warning("Attempted to draw from empty deck"); return None, 0
    r = (n * 52) - c; p = _calculate_dealing_preference(i, r); u = 1.5; l = -1.0
    h = ['T', 'J', 'Q', 'K', 'A']; w = ['4', '5', '6']; pot = []; sel = random.random() < 0.85
    if sel:
        can = None
        if t == "player": # Используем t==
            if p >= u: can = h
            elif p <= l: can = w
        elif t == "dealer": # Используем t==
            if p >= u: can = w
            elif p <= l: can = h
        if can: pot = [x for x in d if x and x[0] in can] # Проверка на None карту
    if not pot: pot = list(d) # Создаем копию
    if not pot: # Если и копия пуста (не должно быть, но на всякий случай)
        logger.error("Deck copy is empty, original deck might be empty.")
        return None, 0
    # Удаляем None элементы из списка кандидатов перед выбором
    pot_filtered = [card for card in pot if card is not None]
    if not pot_filtered:
         logger.error("Filtered potential cards list is empty.")
         return None, 0

    chosen_card = random.choice(pot_filtered)
    try:
        d.remove(chosen_card) # Удаляем из оригинальной колоды
    except ValueError:
        # Это может случиться, если одна и та же карта (дубликат) была в pot_filtered несколько раз
        # и была выбрана, но уже удалена из d. Попробуем найти и удалить эквивалентную.
        logger.warning(f"Card {chosen_card} not directly found in deck after choice, attempting to remove equivalent.")
        removed = False
        for index, card_in_deck in enumerate(d):
            if card_in_deck == chosen_card:
                del d[index]
                removed = True
                break
        if not removed:
            logger.error(f"Failed to remove equivalent card {chosen_card} from deck.")
            # В этом случае, возможно, стоит вернуть None или обработать иначе
            return None, 0 # Возвращаем None, т.к. не смогли гарантировать удаление
    except Exception as e:
         logger.error(f"Unexpected error removing card {chosen_card}: {e}")
         return None, 0

    adj = _get_rank_weight(chosen_card)
    return chosen_card, adj
# <<< --- End of Internal Dealing Logic --- >>>


# --- Callback Query Handler ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data; user_id = query.from_user.id
    # Получаем/создаем пользователя и проверяем результат
    user_data = get_or_create_user(user_id)
    if user_data is None: # Если произошла ошибка БД при получении/создании
        await query.answer("Ошибка данных пользователя. Попробуйте /start.", show_alert=True); return

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
                 except Exception: pass # Игнорируем ошибки удаления
                 return
            h_idx = int(parts[3]); await handle_blackjack_action(update, context, action, h_idx)
        except (IndexError, ValueError) as e:
            logger.warning(f"Invalid action data: {data} - {e}")
            await query.answer("Неверное действие.", show_alert=True)
        except Exception as e:
             logger.error(f"Action callback error: {e}", exc_info=True) # Логируем traceback
             try: await query.answer("Внутренняя ошибка.", show_alert=True)
             except Exception: pass
    else:
        try: await query.answer() # Отвечаем на неизвестные каллбеки
        except Exception: pass

# --- Main Function ---
def main():
    """Starts the bot."""
    logger.info("Запуск функции main().")
    # Запускаем keep-alive веб-сервер в отдельном потоке
    keep_alive()

    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Убедимся, что словарь для игр существует в bot_data
    if 'games' not in application.bot_data: application.bot_data['games'] = {}
    if 'user_cache' not in application.bot_data: application.bot_data['user_cache'] = {} # Для кеша юзеров

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    logger.info("Обработчики зарегистрированы.")
    print("Бот запускается...") # Вывод в консоль Render
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    print("Бот остановлен.") # Это сообщение обычно не будет видно на Render
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    # Проверка переменных окружения выполняется при импорте, ошибки будут выведены там
    print("Запуск скрипта...")
    main()