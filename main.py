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
# RESHUFFLE_PENETRATION = 0.5 # Убрано, так как перемешивание только в начале
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.2
LEADERBOARD_LIMIT = 10
# RESHUFFLE_MESSAGE = "🔄 Колода была перемешана!" # Убрано, уведомление не нужно

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
                    # Повторно запрашиваем данные после вставки
                    cursor.execute(select_sql, (user_id,))
                    user_data = cursor.fetchone()
                    # Если все еще None после вставки (маловероятно с ON CONFLICT), возвращаем дефолтные
                    if not user_data:
                        logger.warning(f"Не удалось получить данные пользователя {user_id} сразу после создания.")
                        return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        return user_data
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_or_create_user) для {user_id}: {e}")
        return None # Возвращаем None при ошибке БД
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_or_create_user) для {user_id}: {e}")
        return None # Возвращаем None при других ошибках

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
    # Возвращаем None, если не удалось получить данные пользователя
    return user_data['balance'] if user_data else None

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
    # Возвращаем None, если не удалось получить данные
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
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_leaderboard): {e}")
        return []
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_leaderboard): {e}")
        return []

# --- Standard Deck and Hand Utilities ---
def create_deck(num_decks=NUM_DECKS):
    """Создает и перемешивает колоду из указанного количества стандартных колод."""
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    logger.info(f"Создана и перемешана новая колода из {num_decks} стандартных колод ({len(deck)} карт).")
    return deck

def get_card_value(card):
    """Возвращает числовое значение карты."""
    rank = card[0]
    return RANK_VALUES.get(rank, 0) if card else 0

def get_hand_value(hand):
    """Рассчитывает стоимость руки в Блекджеке."""
    value = 0
    ace_count = 0
    if not hand:
        return 0
    for card in hand:
        if card: # Проверка, что карта не None
            rank = card[0]
            value += get_card_value(card)
            if rank == 'A':
                ace_count += 1
        else:
            logger.warning("Обнаружена None карта в руке при подсчете очков.")
    # Корректировка значения тузов
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
    return value

def format_hand(hand, hide_one=False):
    """Форматирует руку для отображения."""
    if not hand:
        return "Пусто"
    if hide_one and len(hand) > 0:
        first_card = hand[0]
        # Убедимся, что первая карта не None перед форматированием
        return f"[{first_card[0]}{first_card[1]}, ??]" if first_card else "[??, ??]"
    # Фильтруем None карты перед форматированием
    return ", ".join([f"{c[0]}{c[1]}" for c in hand if c])

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id) # Создаст пользователя, если его нет
    balance = get_balance(user_id) # Получаем баланс (может быть None при ошибке)

    if balance is not None:
        balance_str = f"{balance:.2f}"
        await update.message.reply_text(
            f"Добро пожаловать/С возвращением! 👋 Ваш текущий баланс: {balance_str} фишек.\n"
            f"Используйте /blackjack для начала игры или /help для списка команд."
        )
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start еще раз или свяжитесь с администратором.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help."""
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
    """Обработчик команды /balance."""
    user_id = update.effective_user.id
    current_balance = get_balance(user_id)
    if current_balance is not None:
        await update.message.reply_text(f"Ваш баланс: {current_balance:.2f} фишек.")
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /bonus."""
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id)
    if not user_data: # Проверка, что данные пользователя получены
        await update.message.reply_text("Произошла ошибка при получении данных пользователя. Попробуйте /start.")
        return

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) # Убедимся, что работаем с offset-naive
    last_bonus_time = user_data.get('last_bonus')
    # Убедимся, что last_bonus_time тоже offset-naive, если оно не None
    if last_bonus_time and last_bonus_time.tzinfo:
        last_bonus_time = last_bonus_time.replace(tzinfo=None)

    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time and now - last_bonus_time < cooldown:
        time_left = last_bonus_time + cooldown - now
        hours, remainder = divmod(time_left.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(f"Бонус уже получен. Попробуйте снова через {int(hours)} ч {int(minutes)} мин.")
    else:
        new_balance = update_balance(user_id, BONUS_AMOUNT)
        if new_balance is not None:
            update_last_bonus_time(user_id, now) # Сохраняем now (уже offset-naive)
            await update.message.reply_text(f"✅ Бонус в размере {BONUS_AMOUNT} фишек успешно начислен! Ваш новый баланс: {new_balance:.2f} фишек.")
        else:
            await update.message.reply_text("Не удалось начислить бонус из-за ошибки обновления баланса. Попробуйте позже.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /leaderboard."""
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("Таблица лидеров пока пуста или произошла ошибка при загрузке.")
        return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"

    async def get_user_info(user_id):
        try:
            # Кэш пользователей для уменьшения запросов к API Telegram
            cache = context.bot_data.setdefault('user_cache', {})
            cache_expiry_seconds = 3600 # Кэшировать на 1 час
            now = datetime.datetime.now()

            if user_id in cache and (now - cache[user_id]['timestamp']).total_seconds() < cache_expiry_seconds:
                 return cache[user_id]['user']

            user = await context.bot.get_chat(user_id) # get_chat работает и для пользователей
            cache[user_id] = {'user': user, 'timestamp': now}
            return user
        except BadRequest as e:
            logger.warning(f"Failed to get chat info for user {user_id} (BadRequest: {e}) - User might have blocked the bot or ID is invalid.")
            return None
        except Exception as e:
            logger.warning(f"Failed get info for user {user_id}: {e}")
            return None

    user_info_tasks = [get_user_info(leader['user_id']) for leader in leaders]
    users_info = await asyncio.gather(*user_info_tasks) # Получаем информацию асинхронно

    # Функция для безопасного экранирования MarkdownV2 символов
    def escape_markdown(text):
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return ''.join(f'\\{char}' if char in escape_chars else char for char in str(text))

    place_emojis = ["🥇", "🥈", "🥉"]

    for i, leader in enumerate(leaders):
        user_id = leader['user_id']
        balance = leader['balance']
        user: User | None = users_info[i] # Может быть None, если информация не получена

        user_name_display = f"ID: {user_id}" # Запасной вариант
        if user:
            # Используем first_name или full_name, если есть
            name_to_display = user.first_name or user.full_name or f"User {user_id}"
            safe_name = escape_markdown(name_to_display)
            # Пытаемся создать упоминание, если есть username, иначе просто имя
            user_name_display = user.mention_markdown_v2(safe_name) if user.username else safe_name
        else:
             user_name_display = escape_markdown(f"User {user_id}") # Экранируем и ID

        place_indicator = place_emojis[i] if i < len(place_emojis) else f"{i+1}\\."
        balance_str = escape_markdown(f"{balance:.2f}") # Экранируем баланс

        leaderboard_text += f"{place_indicator} {user_name_display} \\- `{balance_str}` фишек\n"

    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Error sending leaderboard (MarkdownV2 BadRequest): {e}")
        # Попытка отправить как обычный текст в случае ошибки форматирования
        plain_text = leaderboard_text.replace("\\", "") # Убираем экранирование для простого текста
        try:
            await update.message.reply_text(plain_text)
        except Exception as fe:
            logger.error(f"Error sending plain text leaderboard fallback: {fe}")
    except Exception as e:
        logger.error(f"Error sending leaderboard (Other): {e}")


# --- Blackjack Game Logic Handlers ---

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает новую игру или предлагает начать, если нет активной."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    reply_func = None
    message_to_delete = None # Сообщение для удаления (например, кнопки старой игры)

    if update.callback_query:
        reply_func = update.callback_query.message.reply_text
        message_to_delete = update.callback_query.message.message_id
        try:
            await update.callback_query.answer() # Отвечаем на callback
        except BadRequest as e:
            if "query is too old" not in str(e).lower(): logger.warning(f"BJ Start CB Answer Error: {e}")
            else: pass # Игнорируем старые запросы
    elif update.message:
        reply_func = update.message.reply_text
    else:
        logger.warning("blackjack_start called without message or callback_query")
        return

    current_balance = get_balance(user_id)
    if current_balance is None:
        await reply_func("Не удалось проверить ваш баланс. Попробуйте /start.")
        return

    # Инициализация хранилища игр, если его нет
    if 'games' not in context.bot_data:
        context.bot_data['games'] = {}

    # Проверка существующей игры в этом чате
    if chat_id in context.bot_data['games']:
        game_state = context.bot_data['games'][chat_id]
        # Позволяем начать новую игру только если старая завершена или ожидает ставки
        if game_state.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Вы не можете начать новую игру, пока текущая не завершена.")
            return
        # Если есть ID старого сообщения с кнопками, попробуем его удалить
        old_message_id = game_state.get('message_id')
        if old_message_id and old_message_id != message_to_delete: # Не удаляем сообщение, с которого пришел callback
             try:
                 await context.bot.delete_message(chat_id, old_message_id)
                 logger.debug(f"Deleted previous game message {old_message_id} in chat {chat_id}")
             except BadRequest as e:
                 if "message to delete not found" not in str(e).lower():
                     logger.warning(f"Could not delete old game message {old_message_id}: {e}")
             except Exception as e:
                 logger.error(f"Unexpected error deleting old game msg {old_message_id}: {e}")
        # Удаляем старую игру из памяти
        del context.bot_data['games'][chat_id]
        logger.debug(f"Removed previous game state for chat {chat_id}")


    if current_balance <= 0:
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) равен нулю или меньше. Используйте /bonus, чтобы получить фишки.")
        return

    # Предлагаем ставки
    bet_options = [1, 5, 10, 25, 50, 100, 250, 500] # Можно настроить
    valid_bets = [b for b in bet_options if b <= current_balance]

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) меньше минимальной ставки ({min_bet} фишек). Используйте /bonus.")
        return

    # Создаем кнопки для ставок
    buttons = []
    row = []
    max_buttons_per_row = 4 # Для лучшего отображения на мобильных
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) >= max_buttons_per_row:
            buttons.append(row)
            row = []
    if row: # Добавляем оставшиеся кнопки, если есть
        buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)

    try:
        sent_message = await reply_func(f"Ваш баланс: {current_balance:.2f} фишек. Сделайте вашу ставку:", reply_markup=markup)
        # Сохраняем состояние ожидания ставки
        context.bot_data['games'][chat_id] = {
            'player_id': user_id,
            'state': 'waiting_bet',
            'message_id': sent_message.message_id # Сохраняем ID сообщения с кнопками ставок
        }
        logger.info(f"Game initiated for user {user_id} in chat {chat_id}. Waiting for bet.")
    except Exception as e:
        logger.error(f"Failed to send bet selection message to chat {chat_id}: {e}")


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    """Обрабатывает выбор ставки игроком и начинает раздачу."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Проверки состояния игры
    if chat_id not in context.bot_data.get('games', {}):
        await query.answer("Не найдена активная игра для вас в этом чате. Начните новую с /blackjack.", show_alert=True)
        return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user_id:
        await query.answer("Это не ваша игра.", show_alert=True)
        return
    if game_state.get('state') != 'waiting_bet':
        await query.answer("Игра уже началась или ставка сделана.", show_alert=True) # Уведомляем игрока
        return

    # Проверка баланса
    current_balance = get_balance(user_id)
    if current_balance is None:
        await query.answer("Ошибка при проверке баланса.", show_alert=True)
        return
    if bet_amount <= 0:
        await query.answer("Ставка должна быть положительной.", show_alert=True)
        return
    if bet_amount > current_balance:
        await query.answer(f"Недостаточно средств. Ваш баланс: {current_balance:.2f} фишек.", show_alert=True)
        return

    # Списываем ставку
    new_balance = update_balance(user_id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка при списании ставки.", show_alert=True)
        return

    # --- <<< ИЗМЕНЕНИЕ: Создание колоды ТОЛЬКО ЗДЕСЬ >>> ---
    deck = create_deck(NUM_DECKS) # Создаем и перемешиваем новую колоду
    player_hand = []
    dealer_hand = []
    cards_dealt_count = 0 # Счетчик розданных карт

    # Раздача карт (по одной)
    try:
        # Используем _draw_card_from_shoe без индекса состояния (он больше не нужен)
        card1, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS) # Передаем только колоду, счетчик и кол-во колод
        player_hand.append(card1); cards_dealt_count += 1

        card2, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        dealer_hand.append(card2); cards_dealt_count += 1

        card3, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        player_hand.append(card3); cards_dealt_count += 1

        card4, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        dealer_hand.append(card4); cards_dealt_count += 1

        # Проверка на None карты после раздачи
        if None in player_hand or None in dealer_hand:
             raise ValueError("Ошибка раздачи: получена пустая карта.")

    except IndexError: # Если колода закончилась во время начальной раздачи (очень маловероятно)
        logger.error(f"Критическая ошибка: колода закончилась во время начальной раздачи для чата {chat_id}.")
        update_balance(user_id, bet_amount) # Возвращаем ставку
        await query.edit_message_text("Произошла ошибка с колодой во время раздачи. Ставка возвращена. Попробуйте начать заново /blackjack.")
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id] # Чистим состояние игры
        return
    except Exception as e:
        logger.error(f"Ошибка во время начальной раздачи карт: {e}", exc_info=True)
        update_balance(user_id, bet_amount) # Возвращаем ставку
        await query.edit_message_text(f"Произошла ошибка во время раздачи карт: {e}. Ставка возвращена. Попробуйте начать заново /blackjack.")
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id] # Чистим состояние игры
        return

    # Проверка на Блекджек у игрока и/или дилера
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    dealer_up_card_value = get_card_value(dealer_hand[0]) if dealer_hand else 0

    player_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    outcome_text = None
    current_state = 'player_turn' # Изначально ход игрока
    player_hand_status = 'active' # Статус первой руки игрока

    if player_blackjack:
        player_hand_status = 'blackjack' # Устанавливаем статус БЖ
        if dealer_blackjack:
            # Оба Блекджека - Пуш
            outcome_text = f"Ничья! У обоих Блекджек. Ваша ставка {bet_amount} F возвращена."
            update_balance(user_id, bet_amount) # Возвращаем ставку
            current_state = 'game_over'
        else:
            # Только у игрока Блекджек - Выигрыш
            win_amount = bet_amount * BLACKJACK_PAYOUT
            total_return = bet_amount + win_amount
            update_balance(user_id, total_return) # Возвращаем ставку + выигрыш
            outcome_text = f"♠️♥️ БЛЕКДЖЕК! ♦️♣️ Вы выиграли {win_amount:.2f} фишек!"
            current_state = 'game_over'
    elif dealer_blackjack:
        # Только у дилера Блекджек - Проигрыш
        # Ставка уже списана, ничего не возвращаем
        outcome_text = f"У дилера Блекджек! Вы проиграли ставку {bet_amount} F."
        current_state = 'game_over'

    # Обновляем состояние игры
    game_state.update({
        'state': current_state,
        'deck': deck, # Сохраняем созданную колоду
        'player_hands': [{ # Список рук игрока (начинаем с одной)
            'hand': player_hand,
            'bet': bet_amount,
            'status': player_hand_status, # 'active', 'blackjack', 'bust', 'stand'
            'can_double': (not player_blackjack and not dealer_blackjack), # Удвоить можно только если нет БЖ и ход игрока
            'can_split': False # Разделить можно будет проверить позже
        }],
        'current_hand_index': 0, # Индекс текущей руки игрока
        'dealer_hand': dealer_hand,
        'cards_dealt': cards_dealt_count, # Сохраняем кол-во розданных карт
        'initial_bet': bet_amount,
        'split_count': 0, # Счетчик сплитов
        'outcome_text': outcome_text, # Текст исхода, если игра закончилась сразу
        # '_deck_state_index' больше не нужен
    })

    # Показываем начальное состояние игры (или результат, если игра сразу закончилась)
    await show_game_state(context, chat_id, query.message.message_id)
    # Отвечаем на первоначальный callback ставки (можно сделать пустым)
    try: await query.answer(f"Ставка {bet_amount} F принята!")
    except: pass # Игнорируем ошибки, если ответ уже не актуален


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id_to_edit: int):
    """Отображает текущее состояние игры (руки, ставки, кнопки действий)."""
    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"show_game_state called for chat {chat_id}, but no game found.")
        return
    game_state = context.bot_data['games'][chat_id]
    player_id = game_state.get('player_id')
    if not player_id:
        logger.error(f"Game state for chat {chat_id} is missing player_id.")
        return # Не можем продолжить без ID игрока

    # Получаем актуальный баланс
    current_balance = get_balance(player_id)
    balance_str = f"{current_balance:.2f}" if current_balance is not None else "Ошибка"

    dealer_hand = game_state.get('dealer_hand', [])
    player_hands = game_state.get('player_hands', [])
    current_hand_index = game_state.get('current_hand_index', -1)
    game_status = game_state.get('state', 'unknown')

    # Скрывать вторую карту дилера, если ход игрока и у дилера не блекджек
    hide_dealer_card = (game_status == 'player_turn') and not (get_hand_value(dealer_hand) == 21 and len(dealer_hand) == 2)

    # Формируем текст сообщения
    text = f"**Блекджек** | Баланс: {balance_str} F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands if isinstance(h, dict))
    num_hands = len(player_hands)
    text += f"Общая ставка: {total_bet} F"
    if num_hands > 1: text += f" ({num_hands} руки)"
    text += "\n" + "-"*25 + "\n" # Разделитель

    # Рука дилера
    dealer_value = get_hand_value(dealer_hand)
    dealer_value_str = "??" # Значение по умолчанию (если скрыто)
    if not hide_dealer_card:
        dealer_value_str = str(dealer_value)
    elif dealer_hand and dealer_hand[0]: # Показываем значение первой карты, если скрываем вторую
         dealer_value_str = str(get_card_value(dealer_hand[0])) + "+?"

    dealer_hand_str = format_hand(dealer_hand, hide=hide_dealer_card)
    text += f"**Дилер:** {dealer_hand_str} ({dealer_value_str})\n\n"

    # Руки игрока
    text += "**Вы:**\n"
    active_hand_data = None # Данные активной руки для кнопок
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue # Пропускаем невалидные записи рук

        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', 'unknown')
        hand_bet = hand_data.get('bet', 0)

        is_current_hand = (i == current_hand_index and hand_status == 'active' and game_status == 'player_turn')

        # Индикатор текущей/завершенной руки
        indicator = "⚪️" # По умолчанию
        if is_current_hand: indicator = "▶️" # Активная рука
        elif hand_status == 'stand': indicator = "✅" # Стоп
        elif hand_status == 'bust': indicator = "❌" # Перебор
        elif hand_status == 'blackjack': indicator = "💰" # Блекджек

        text += f"{indicator} Рука {i+1}: {format_hand(hand)} ({hand_value}) [{hand_bet} F]"

        # Добавляем статус словами, если он не 'active' или 'stand'
        if hand_status == 'bust': text += " - Перебор!"
        elif hand_status == 'blackjack': text += " - Блекджек!"
        elif hand_status == 'stand' and not is_current_hand: text += " - Стоп" # Показываем "Стоп" только для неактивных рук

        text += "\n"

        if is_current_hand:
            active_hand_data = hand_data # Сохраняем для кнопок

    # --- Кнопки действий ---
    keyboard = []
    if active_hand_data and game_status == 'player_turn':
        current_hand = active_hand_data.get('hand', [])
        current_bet = active_hand_data.get('bet', 0)
        can_hit = True # Почти всегда можно взять еще
        can_stand = True # Всегда можно остановиться

        # Проверка возможности удвоения
        can_double = (active_hand_data.get('can_double', False) # Флаг из состояния
                      and len(current_hand) == 2 # Только на первых двух картах
                      and current_balance is not None and current_balance >= current_bet) # Хватает баланса

        # Проверка возможности разделения
        can_split = (len(current_hand) == 2 # Только 2 карты
                     and current_hand[0] and current_hand[1] # Убедимся что карты существуют
                     and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) # Одинаковые по значению (не рангу!)
                     and current_balance is not None and current_balance >= current_bet # Хватает баланса
                     and game_state.get('split_count', 0) < MAX_SPLITS) # Не превышен лимит сплитов

        action_buttons = []
        if can_hit: action_buttons.append(InlineKeyboardButton("Еще (Hit)", callback_data=f"bj_action_hit_{current_hand_index}"))
        if can_stand: action_buttons.append(InlineKeyboardButton("Хватит (Stand)", callback_data=f"bj_action_stand_{current_hand_index}"))
        keyboard.append(action_buttons)

        special_buttons = []
        if can_double: special_buttons.append(InlineKeyboardButton("Удвоить (Double)", callback_data=f"bj_action_double_{current_hand_index}"))
        # Обновляем флаг 'can_split' в состоянии руки перед отображением кнопки
        active_hand_data['can_split'] = can_split
        if can_split: special_buttons.append(InlineKeyboardButton("Разделить (Split)", callback_data=f"bj_action_split_{current_hand_index}"))
        if special_buttons: keyboard.append(special_buttons)

    elif game_status == 'game_over':
        text += "\n**Игра завершена!**\n"
        outcome = game_state.get('outcome_text', "Результат не определен.")
        text += outcome + "\n"
        final_balance = get_balance(player_id) # Получаем финальный баланс
        final_balance_str = f"{final_balance:.2f}" if final_balance is not None else "Ошибка"
        text += f"\nИтоговый баланс: {final_balance_str} фишек."
        # Кнопка "Новая игра"
        keyboard.append([InlineKeyboardButton("🔄 Новая Игра", callback_data="bj_action_new_game")])

    elif game_status == 'dealer_turn':
        text += "\n*Ход дилера...*" # Сообщение о ходе дилера

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # Редактируем сообщение
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id_to_edit,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN # Используем Markdown V1 для простоты
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            pass # Игнорируем, если сообщение не изменилось
        elif "message to edit not found" in str(e).lower():
             logger.warning(f"Message {message_id_to_edit} to edit not found in chat {chat_id}. Maybe deleted?")
             # Попробуем отправить новое сообщение, если старое удалено, но игра еще не закончена
             if game_status != 'game_over' and chat_id in context.bot_data['games']:
                 try:
                     new_msg = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                     context.bot_data['games'][chat_id]['message_id'] = new_msg.message_id # Обновляем ID сообщения
                     logger.info(f"Sent new game state message {new_msg.message_id} as old one was not found.")
                 except Exception as send_e:
                     logger.error(f"Failed to send new game state message after edit failed: {send_e}")
        else:
            logger.error(f"Error editing game state message {message_id_to_edit}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error showing game state for chat {chat_id}: {e}", exc_info=True)


async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    """Обрабатывает действия игрока (Hit, Stand, Double, Split)."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # --- Проверки состояния игры ---
    if chat_id not in context.bot_data.get('games', {}): return # Игра не найдена
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user_id: return # Не игра этого пользователя
    if game_state.get('state') != 'player_turn': return # Не ход игрока
    player_hands = game_state.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)): return # Неверный индекс руки
    if hand_index != game_state.get('current_hand_index'): return # Действие для неактивной руки

    hand_data = player_hands[hand_index]
    if not isinstance(hand_data, dict) or hand_data.get('status') != 'active': return # Рука не активна

    # --- Получение данных для действия ---
    current_hand = hand_data.get('hand', [])
    deck = game_state.get('deck', []) # Берем текущую колоду из состояния
    current_balance = get_balance(user_id)
    current_bet = hand_data.get('bet', 0)
    cards_dealt_count = game_state.get('cards_dealt', 0) # Получаем счетчик карт

    if current_balance is None: # Проверка баланса перед действиями, требующими ставки
        if action in ['double', 'split']:
            await query.answer("Ошибка получения баланса.", show_alert=True)
            return

    # --- <<< ИЗМЕНЕНИЕ: Убрана проверка и логика перемешивания колоды во время игры >>> ---
    # needs_reshuffle = len(deck) < (NUM_DECKS * 52 * (1.0 - RESHUFFLE_PENETRATION))
    # if needs_reshuffle: ... # Этот блок удален

    try:
        # --- Обработка действий ---
        if action == 'hit':
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                current_hand.append(card)
                game_state['cards_dealt'] += 1
                hand_data['can_double'] = False # Нельзя удваивать после хита
                hand_data['can_split'] = False # И разделять тоже
                new_value = get_hand_value(current_hand)
                if new_value > 21:
                    hand_data['status'] = 'bust'
                    await query.answer("Перебор!") # Краткий ответ
                    await next_player_action_or_dealer(context, chat_id)
                elif new_value == 21:
                    hand_data['status'] = 'stand' # Авто-стоп на 21
                    await query.answer("21!") # Краткий ответ
                    await next_player_action_or_dealer(context, chat_id)
                else:
                    # Просто обновляем состояние, если игра продолжается
                    await show_game_state(context, chat_id, query.message.message_id)
                    await query.answer() # Пустой ответ, т.к. состояние обновлено
            else:
                # Карта не получена (колода закончилась?) - считаем как Stand
                logger.warning(f"Hit failed for user {user_id} in chat {chat_id} - deck likely empty.")
                hand_data['status'] = 'stand'
                await query.answer("Не удалось взять карту (колода?). Рука остается.", show_alert=True)
                await next_player_action_or_dealer(context, chat_id)

        elif action == 'stand':
            hand_data['status'] = 'stand'
            await query.answer("Стоп.") # Краткий ответ
            await next_player_action_or_dealer(context, chat_id)

        elif action == 'double':
            # Повторная проверка условий (на всякий случай)
            can_double = (hand_data.get('can_double', False)
                          and len(current_hand) == 2
                          and current_balance is not None and current_balance >= current_bet)
            if can_double:
                new_balance = update_balance(user_id, -current_bet) # Списываем доп. ставку
                if new_balance is not None:
                    hand_data['bet'] += current_bet
                    hand_data['can_double'] = False # Удвоить можно только раз
                    hand_data['can_split'] = False
                    card, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                    if card:
                        current_hand.append(card)
                        game_state['cards_dealt'] += 1
                        new_value = get_hand_value(current_hand)
                        hand_data['status'] = 'bust' if new_value > 21 else 'stand' # После удвоения ход завершается
                        await query.answer(f"Удвоено! Карта: {card[0]}{card[1]}.{' Перебор!'* (new_value > 21)}")
                        await next_player_action_or_dealer(context, chat_id)
                    else:
                        # Карта не получена - считаем как Stand без доп. карты
                        logger.warning(f"Double failed for user {user_id} in chat {chat_id} - deck likely empty.")
                        hand_data['status'] = 'stand'
                        await query.answer("Удвоено! Не удалось взять карту (колода?). Рука остается.", show_alert=True)
                        await next_player_action_or_dealer(context, chat_id)
                else:
                    await query.answer("Ошибка обновления баланса при удвоении.", show_alert=True)
            else:
                 await query.answer("Удвоение невозможно для этой руки или недостаточно средств.", show_alert=True)


        elif action == 'split':
             # Повторная проверка
             can_split = (hand_data.get('can_split', False) # Используем флаг из show_game_state
                         and len(current_hand) == 2
                         # and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) # Проверка уже сделана в show_game_state
                         and current_balance is not None and current_balance >= current_bet
                         and game_state.get('split_count', 0) < MAX_SPLITS)
             if can_split:
                 new_balance = update_balance(user_id, -current_bet) # Списываем ставку для новой руки
                 if new_balance is not None:
                     game_state['split_count'] += 1
                     # Создаем новую руку
                     card_to_move = current_hand.pop() # Забираем вторую карту
                     new_hand_data = {
                         'hand': [card_to_move], # Новая рука начинается с этой карты
                         'bet': current_bet,
                         'status': 'active',
                         'can_double': False, # Будет установлено после раздачи карт
                         'can_split': False
                     }
                     # Вставляем новую руку сразу после текущей
                     player_hands.insert(hand_index + 1, new_hand_data)

                     # Раздаем по одной карте в каждую руку
                     card1, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card1:
                         current_hand.append(card1)
                         game_state['cards_dealt'] += 1
                     else: logger.warning(f"Split card draw 1 failed in {chat_id}")

                     card2, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card2:
                         new_hand_data['hand'].append(card2)
                         game_state['cards_dealt'] += 1
                     else: logger.warning(f"Split card draw 2 failed in {chat_id}")

                     # Особое правило для сплита тузов: игра на каждой руке завершается
                     is_ace_split = get_card_value(current_hand[0]) == 11 # Проверяем по первой карте (вторая была такая же)

                     if is_ace_split:
                         hand_data['status'] = 'stand'
                         new_hand_data['status'] = 'stand'
                         hand_data['can_double'] = False
                         new_hand_data['can_double'] = False
                         await query.answer("Тузы разделены. Раздано по одной карте.")
                         # Сразу переходим к следующему действию (может быть следующая рука или дилер)
                         await next_player_action_or_dealer(context, chat_id)
                     else:
                         # Обновляем флаги can_double/can_split для обеих рук
                         hand_data['can_double'] = (len(current_hand) == 2)
                         new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)
                         # Сплитить повторно можно, если карты одинаковые по значению
                         hand_data['can_split'] = (len(current_hand) == 2 and current_hand[0] and current_hand[1] and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) and game_state['split_count'] < MAX_SPLITS)
                         new_hand_data['can_split'] = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and game_state['split_count'] < MAX_SPLITS)

                         # Проверяем БЖ на новых руках (хотя это не классический БЖ после сплита)
                         if get_hand_value(current_hand) == 21: hand_data['status'] = 'stand'
                         if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                         await query.answer("Рука разделена!")
                         # Остаемся на текущей руке (hand_index), показываем обновленное состояние
                         await show_game_state(context, chat_id, query.message.message_id)

                 else:
                     await query.answer("Ошибка обновления баланса при разделении.", show_alert=True)
             else:
                  await query.answer("Разделение невозможно: неподходящие карты, достигнут лимит сплитов или недостаточно средств.", show_alert=True)

    except IndexError:
         # Эта ошибка теперь более вероятна, если колода закончится
         logger.warning(f"Действие '{action}' не удалось для user {user_id} в chat {chat_id} - колода закончилась.")
         hand_data['status'] = 'stand' # Завершаем ход для этой руки
         await query.answer(f"Не удалось выполнить '{action}', так как в колоде закончились карты. Ваша рука остается.", show_alert=True)
         await next_player_action_or_dealer(context, chat_id) # Передаем ход дальше

    except Exception as e:
         logger.error(f"Error handling action '{action}' for user {user_id} in chat {chat_id}: {e}", exc_info=True)
         try: await query.answer("Произошла ошибка при обработке вашего хода.", show_alert=True)
         except: pass


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Переключает на следующую руку игрока или начинает ход дилера."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('state') != 'player_turn': return # Не должны быть здесь, если не ход игрока

    player_hands = game_state.get('player_hands', [])
    current_index = game_state.get('current_hand_index', -1)

    # Ищем следующую активную руку
    next_index = current_index + 1
    while next_index < len(player_hands):
        if isinstance(player_hands[next_index], dict) and player_hands[next_index].get('status') == 'active':
            # Нашли следующую активную руку
            game_state['current_hand_index'] = next_index
            await show_game_state(context, chat_id, game_state.get('message_id'))
            return # Выход, игрок продолжает ходить
        next_index += 1

    # Активных рук игрока больше нет, переходим к ходу дилера
    game_state['state'] = 'dealer_turn'
    logger.info(f"Player {game_state.get('player_id')} finished turn in chat {chat_id}. Starting dealer turn.")
    # Обновляем сообщение, чтобы показать "Ход дилера..."
    await show_game_state(context, chat_id, game_state.get('message_id'))
    # Запускаем ход дилера с небольшой задержкой
    context.job_queue.run_once(
        dealer_turn_job,
        when=datetime.timedelta(seconds=DEALER_TURN_DELAY), # Используем timedelta для ясности
        chat_id=chat_id,
        data=chat_id,
        name=f"dealer_turn_{chat_id}" # Уникальное имя для джоба
    )


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Выполняет ход дилера (берет карты до 17 или Soft 17)."""
    chat_id = context.job.data # Получаем chat_id из данных джоба
    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"Dealer turn job executed for chat {chat_id}, but game not found.")
        return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('state') != 'dealer_turn':
        logger.warning(f"Dealer turn job executed for chat {chat_id}, but state is not 'dealer_turn' ({game_state.get('state')}).")
        return

    deck = game_state.get('deck', [])
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands = game_state.get('player_hands', [])
    cards_dealt_count = game_state.get('cards_dealt', 0)

    # Проверяем, есть ли у игрока руки, которые не проиграли (не 'bust')
    # Если все руки игрока 'bust' или 'blackjack' (уже обработан), дилеру нет смысла ходить
    player_can_win = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack']
        for h in player_hands
    )

    dealer_value_initial = get_hand_value(dealer_hand)
    dealer_had_blackjack = (dealer_value_initial == 21 and len(dealer_hand) == 2)

    if not player_can_win and not dealer_had_blackjack:
        logger.info(f"Dealer turn skipped in chat {chat_id} as all player hands are bust/blackjack.")
        await determine_outcome(context, chat_id, dealer_had_blackjack) # Просто определяем исход
        return

    # Дилер ходит
    while True:
        dealer_value = get_hand_value(dealer_hand)
        ace_count = sum(1 for card in dealer_hand if card and card[0] == 'A')
        is_soft_hand = ace_count > 0 and (dealer_value - 11 * ace_count < 11) # Проверка на мягкую руку

        # Правило взятия карты дилером
        should_hit = (dealer_value < 17) or (dealer_value == 17 and is_soft_hand and DEALER_HITS_SOFT_17)

        if not should_hit:
            break # Дилер останавливается

        # --- <<< ИЗМЕНЕНИЕ: Убрана проверка и логика перемешивания колоды >>> ---
        # needs_reshuffle = len(deck) < ...
        # if needs_reshuffle: ... # Этот блок удален

        # Берем карту
        try:
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                dealer_hand.append(card)
                game_state['cards_dealt'] += 1
                cards_dealt_count += 1 # Обновляем локальный счетчик тоже
                # Показываем обновленное состояние дилера (опционально, для "анимации")
                # await show_game_state(context, chat_id, game_state.get('message_id'))
                # await asyncio.sleep(DEALER_TURN_DELAY * 2) # Задержка между картами дилера
            else:
                logger.warning(f"Dealer turn hit failed in chat {chat_id} - deck likely empty.")
                break # Прерываем цикл, если карта не может быть взята
        except IndexError:
            logger.warning(f"Dealer turn hit failed in chat {chat_id} - deck ran out of cards.")
            break # Колода закончилась

    # Ход дилера завершен, определяем результат
    await determine_outcome(context, chat_id, dealer_had_blackjack)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack: bool):
    """Определяет результат игры для каждой руки игрока и обновляет баланс."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]

    # Предотвращаем повторное определение исхода, если он уже есть
    if game_state.get('state') == 'game_over' and game_state.get('outcome_text'):
        await show_game_state(context, chat_id, game_state.get('message_id'))
        return

    player_id = game_state.get('player_id')
    player_hands = game_state.get('player_hands', [])
    dealer_hand = game_state.get('dealer_hand', [])
    if not player_id: return # Нужен ID игрока

    dealer_value = get_hand_value(dealer_hand)
    dealer_is_bust = dealer_value > 21

    outcomes = [] # Текстовые результаты для каждой руки
    total_winnings = 0 # Сумма, которую нужно вернуть/выплатить игроку (включая ставки)

    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue

        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status')
        player_value = get_hand_value(hand)
        # Был ли у игрока блекджек НА ЭТОЙ РУКЕ (важно для сплитов)
        player_had_blackjack_this_hand = (status == 'blackjack')

        payout_multiplier = 0 # 0=проигрыш, 1=пуш, 2=выигрыш 1:1, 1+BJ_PAYOUT=БЖ
        outcome_str = ""
        hand_prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

        if status == 'bust':
            payout_multiplier = 0 # Ставка проиграна
            outcome_str = f"{hand_prefix}Перебор ({player_value}). Ставка {bet} F проиграна."
        elif player_had_blackjack_this_hand:
             # Игрок получил БЖ еще на раздаче (проверка на БЖ дилера была раньше)
             # Этот кейс обрабатывается в handle_blackjack_bet, но оставим для полноты
             payout_multiplier = 1 + BLACKJACK_PAYOUT
             win_amount = bet * BLACKJACK_PAYOUT
             outcome_str = f"{hand_prefix}Блекджек! Выигрыш {win_amount:.2f} F."
        elif dealer_had_blackjack: # У дилера БЖ, игрок проиграл (если у игрока не было БЖ)
            payout_multiplier = 0
            outcome_str = f"{hand_prefix}У дилера Блекджек ({format_hand(dealer_hand)}). Ставка {bet} F проиграна."
        elif dealer_is_bust:
            payout_multiplier = 2 # Выигрыш 1:1
            outcome_str = f"{hand_prefix}У дилера перебор ({dealer_value})! Выигрыш {bet} F."
        elif player_value > dealer_value:
            payout_multiplier = 2 # Выигрыш 1:1
            outcome_str = f"{hand_prefix}{player_value} > {dealer_value}. Выигрыш {bet} F."
        elif player_value == dealer_value:
            payout_multiplier = 1 # Пуш, возврат ставки
            outcome_str = f"{hand_prefix}{player_value} = {dealer_value}. Ничья, ставка {bet} F возвращена."
        else: # player_value < dealer_value
            payout_multiplier = 0 # Проигрыш
            outcome_str = f"{hand_prefix}{player_value} < {dealer_value}. Ставка {bet} F проиграна."

        outcomes.append(outcome_str)
        total_winnings += bet * payout_multiplier

    # Обновляем баланс игрока одним махом
    if total_winnings > 0:
        final_balance = update_balance(player_id, total_winnings)
        if final_balance is None:
             logger.error(f"Критическая ошибка: не удалось обновить баланс для user {player_id} после игры в chat {chat_id}.")
             # Пытаемся сообщить об ошибке, но не перезаписываем исход игры
             outcomes.append("\n**ОШИБКА:** Не удалось начислить выигрыш!")
    else:
        final_balance = get_balance(player_id) # Просто получаем баланс, если выигрыша не было

    # Рассчитываем чистый итог игры
    total_bet = sum(h.get('bet', 0) for h in player_hands if isinstance(h, dict))
    net_change = total_winnings - total_bet

    # Сохраняем результат в состоянии игры
    game_state['state'] = 'game_over'
    game_state['outcome_text'] = "\n".join(outcomes) + f"\n\n**Общий итог: {net_change:+.2f} F**" # Форматируем с + или -

    # Показываем финальное сообщение с результатами и кнопкой "Новая игра"
    await show_game_state(context, chat_id, game_state.get('message_id'))

    # Опционально: можно удалить игру из памяти здесь или оставить до /blackjack
    # if chat_id in context.bot_data['games']:
    #     del context.bot_data['games'][chat_id]
    #     logger.info(f"Game state for chat {chat_id} cleared after outcome determination.")


# --- <<< Internal Dealing Logic --- >>>
# _RANK_WEIGHTS и _calculate_dealing_preference удалены, так как "умная" раздача больше не используется
# _deck_state_index тоже не используется

def _draw_card_from_shoe(deck: list, cards_dealt: int, num_decks: int):
    """
    Просто берет случайную карту из оставшейся колоды.
    Возвращает (карта, 0) или (None, 0) при ошибке.
    Второй элемент (0) оставлен для совместимости сигнатуры.
    """
    if not deck:
        logger.warning("_draw_card_from_shoe: Попытка взять карту из пустой колоды.")
        return None, 0
    try:
        # Просто выбираем случайную карту и удаляем ее
        chosen_card = random.choice(deck)
        deck.remove(chosen_card)
        return chosen_card, 0
    except IndexError: # Может случиться, если deck пустой между random.choice и remove (маловероятно)
        logger.warning("_draw_card_from_shoe: IndexError при взятии карты.")
        return None, 0
    except ValueError: # Если карту не удалось найти для удаления (очень странно)
         logger.error(f"_draw_card_from_shoe: ValueError при удалении карты {chosen_card}. Состояние колоды может быть некорректным.")
         # Попробуем найти и удалить вручную
         removed = False
         for i, card_in_deck in enumerate(deck):
             if card_in_deck == chosen_card:
                 del deck[i]
                 removed = True
                 logger.info(f"Карта {chosen_card} удалена вручную после ValueError.")
                 return chosen_card, 0
         if not removed:
              logger.error(f"Критическая ошибка: не удалось удалить карту {chosen_card} после ValueError.")
              return None, 0 # Возвращаем None, если все плохо
    except Exception as e:
        logger.error(f"Неожиданная ошибка в _draw_card_from_shoe: {e}", exc_info=True)
        return None, 0

# <<< --- End of Internal Dealing Logic --- >>>


# --- Callback Query Handler ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все нажатия на инлайн-кнопки."""
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # Базовая проверка пользователя (на всякий случай)
    user_data = get_or_create_user(user_id)
    if user_data is None:
        try: await query.answer("Ошибка получения данных пользователя.", show_alert=True)
        except: pass
        return

    logger.debug(f"Callback received: data='{data}', user={user_id}, chat={query.message.chat_id}, msg={query.message.message_id}")

    # Обработка ставок
    if data.startswith("bj_bet_"):
        try:
            bet_amount = int(data.split("_")[2])
            await handle_blackjack_bet(update, context, bet_amount)
        except (ValueError, IndexError) as e:
            logger.error(f"Invalid bet callback data: {data} - {e}")
            try: await query.answer("Ошибка в данных ставки.", show_alert=True)
            except: pass
        except Exception as e:
            logger.error(f"Error processing bet callback {data}: {e}", exc_info=True)
            try: await query.answer("Произошла ошибка при обработке ставки.", show_alert=True)
            except: pass

    # Обработка кнопки "Новая игра"
    elif data == "bj_action_new_game":
         try:
             # Не отвечаем на query сразу, так как blackjack_start может отправить свое сообщение
             # Попробуем удалить сообщение с кнопкой "Новая игра"
             try: await query.delete_message()
             except Exception as e: logger.debug(f"Could not delete 'New Game' msg: {e}")
             # Запускаем процесс начала новой игры (он отправит сообщение со ставками)
             await blackjack_start(update, context)
         except Exception as e:
             logger.error(f"Error starting new game from button: {e}", exc_info=True)
             # Пытаемся ответить, если что-то пошло не так
             try: await query.answer("Ошибка запуска новой игры.", show_alert=True)
             except Exception: pass

    # Обработка действий в игре (Hit, Stand, Double, Split)
    elif data.startswith("bj_action_"):
        parts = data.split("_")
        # Ожидаем формат "bj_action_{тип}_{индекс_руки}" -> 4 части
        if len(parts) == 4:
            try:
                action_type = parts[2]
                hand_index = int(parts[3])
                # Вызываем обработчик действия, он сам ответит на query или обновит сообщение
                await handle_blackjack_action(update, context, action_type, hand_index)
            except ValueError:
                logger.warning(f"Invalid hand index in action data: {data}")
                try: await query.answer("Неверный индекс руки.", show_alert=True)
                except: pass
            except IndexError:
                 logger.warning(f"Invalid action data format (IndexError): {data}")
                 try: await query.answer("Неверный формат действия.", show_alert=True)
                 except: pass
            except Exception as e:
                 logger.error(f"Action callback processing error ({data}): {e}", exc_info=True)
                 try: await query.answer("Ошибка обработки действия.", show_alert=True)
                 except: pass
        else:
             logger.warning(f"Invalid action data format (parts count != 4): {data}")
             try: await query.answer("Неверный формат данных действия.", show_alert=True)
             except: pass

    # Обработка неизвестных callback'ов
    else:
        logger.warning(f"Received unknown callback data: {data} from user {user_id}")
        try:
            # Просто отвечаем, чтобы убрать "часики" на кнопке
            await query.answer()
        except Exception as e:
             if "query is too old" not in str(e).lower():
                 logger.warning(f"Error answering unknown callback '{data}': {e}")


# --- Main Function ---
def main():
    """Запускает бота."""
    logger.info("Запуск функции main().")
    keep_alive() # Запускаем веб-сервер для Render/платформы

    # Настройка приложения
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True) # Разрешаем обработку нескольких апдейтов одновременно
        .build()
    )

    # Инициализация хранилищ в bot_data
    if 'games' not in application.bot_data:
        application.bot_data['games'] = {}
        logger.info("Хранилище 'games' инициализировано.")
    if 'user_cache' not in application.bot_data:
        application.bot_data['user_cache'] = {}
        logger.info("Хранилище 'user_cache' инициализировано.")


    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))

    # Регистрация обработчика для всех инлайн-кнопок
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # Можно добавить обработчик сообщений, если нужно реагировать на текст
    # application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler_func))

    logger.info("Обработчики команд и callback'ов зарегистрированы.")
    print("Бот запускается... Нажмите Ctrl+C для остановки.")

    # Запуск бота в режиме опроса (polling)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    print("Бот остановлен.")
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    print("Запуск скрипта blackjack_bot.py...")
    # init_db_manual() # Раскомментируйте, если нужно увидеть SQL для создания таблицы users
    main()