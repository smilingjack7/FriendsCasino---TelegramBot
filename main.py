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
# RESHUFFLE_PENETRATION = 0.5 # Убрано
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.2 # Задержка перед/между ходами дилера (в секундах)
LEADERBOARD_LIMIT = 10
# RESHUFFLE_MESSAGE = "..." # Убрано

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING) # Уменьшаем логи Flask/Werkzeug
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
        raise # Пробрасываем исключение дальше

def init_db_manual():
    """SQL для ручной инициализации таблицы."""
    sql = """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        balance DOUBLE PRECISION DEFAULT 0,
        last_bonus TIMESTAMP WITHOUT TIME ZONE -- Храним без таймзоны (UTC)
    );
    """
    print("--- SQL для инициализации таблицы users ---")
    print(sql)
    print("--- Выполните этот SQL запрос в вашей базе данных один раз. ---")

def get_or_create_user(user_id: int):
    """Получает данные пользователя или создает нового с начальным балансом."""
    select_sql = "SELECT user_id, balance, last_bonus FROM users WHERE user_id = %s;"
    insert_sql = """
    INSERT INTO users (user_id, balance, last_bonus)
    VALUES (%s, %s, %s)
    ON CONFLICT (user_id) DO NOTHING;
    """
    user_data = None
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Пытаемся получить пользователя
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()

                # Если пользователя нет, создаем его
                if user_data is None:
                    # last_bonus при создании ставим NULL
                    cursor.execute(insert_sql, (user_id, INITIAL_BALANCE, None))
                    logger.info(f"Создан новый пользователь в БД: {user_id} с балансом {INITIAL_BALANCE}")
                    # Повторно запрашиваем данные после вставки
                    cursor.execute(select_sql, (user_id,))
                    user_data = cursor.fetchone()
                    # Если все еще None после вставки (маловероятно с ON CONFLICT), возвращаем дефолтные
                    if not user_data:
                        logger.warning(f"Не удалось получить данные пользователя {user_id} сразу после создания.")
                        # Возвращаем словарь с дефолтными значениями, но без записи в БД (она не удалась)
                        return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        # Убедимся, что last_bonus, если он есть, является datetime объектом
        if user_data and user_data.get('last_bonus') and not isinstance(user_data['last_bonus'], datetime.datetime):
            # Попытка конвертации, если это строка (зависит от формата в БД)
             try: user_data['last_bonus'] = datetime.datetime.fromisoformat(str(user_data['last_bonus']))
             except: logger.warning(f"Could not convert last_bonus '{user_data['last_bonus']}' to datetime for user {user_id}"); user_data['last_bonus'] = None

        return user_data # Возвращаем dict или None, если SELECT не нашел после INSERT
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_or_create_user) для {user_id}: {e}")
        return None # Возвращаем None при ошибке БД
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_or_create_user) для {user_id}: {e}", exc_info=True)
        return None # Возвращаем None при других ошибках

def update_balance(user_id: int, amount_change: float):
    """Обновляет баланс пользователя и возвращает НОВЫЙ баланс или None при ошибке."""
    # Используем `balance = balance + %s` для атомарного обновления
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;"
    new_balance = None
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_update, (amount_change, user_id))
                result = cursor.fetchone()
                if result:
                    new_balance = result[0]
                    logger.info(f"Баланс пользователя {user_id} изменен на {amount_change:+}. Новый баланс: {new_balance:.2f}")
                else:
                     logger.warning(f"Не удалось обновить баланс для пользователя {user_id} (не найден?).")
        return new_balance # Может быть None, если пользователь не найден
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (update_balance) для {user_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка (update_balance) для {user_id}: {e}", exc_info=True)
        return None

def get_balance(user_id: int) -> float | None:
    """Получает текущий баланс пользователя или None при ошибке."""
    user_data = get_or_create_user(user_id)
    # Возвращаем None, если не удалось получить данные пользователя (ошибка или не создан)
    return user_data['balance'] if user_data else None

def update_last_bonus_time(user_id: int, bonus_time: datetime.datetime):
     """Обновляет время последнего получения бонуса (в UTC)."""
     # Убедимся, что время в UTC и без информации о таймзоне перед сохранением
     if bonus_time.tzinfo:
         bonus_time_utc = bonus_time.astimezone(datetime.timezone.utc).replace(tzinfo=None)
     else:
         # Если время уже без таймзоны, предполагаем, что оно в UTC
         bonus_time_utc = bonus_time

     sql = "UPDATE users SET last_bonus = %s WHERE user_id = %s;"
     try:
         with get_db_conn() as conn:
             with conn.cursor() as cursor:
                 cursor.execute(sql, (bonus_time_utc, user_id))
         logger.info(f"Время последнего бонуса для {user_id} обновлено на {bonus_time_utc}")
     except psycopg2.Error as e:
         logger.error(f"Ошибка БД (update_last_bonus_time) для {user_id}: {e}")
     except Exception as e:
         logger.error(f"Неожиданная ошибка (update_last_bonus_time) для {user_id}: {e}", exc_info=True)

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Получает время последнего бонуса пользователя (как datetime объект UTC) или None."""
    user_data = get_or_create_user(user_id)
    last_bonus = user_data.get('last_bonus') if user_data else None
    # last_bonus из БД должно быть datetime объектом (или None) после get_or_create_user
    # Если оно None или уже datetime, просто возвращаем
    if isinstance(last_bonus, datetime.datetime) or last_bonus is None:
        return last_bonus
    else:
        # Этот случай не должен происходить, если get_or_create_user работает правильно
        logger.warning(f"Получено некорректное значение last_bonus ({type(last_bonus)}) для user {user_id}.")
        return None


def get_leaderboard(limit: int = LEADERBOARD_LIMIT):
    """Получает топ пользователей по балансу."""
    # Исключаем пользователей с нулевым или отрицательным балансом
    sql = "SELECT user_id, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT %s;"
    leaders = []
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (limit,))
                leaders = cursor.fetchall() # fetchall вернет список словарей
        return leaders
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД (get_leaderboard): {e}")
        return [] # Возвращаем пустой список при ошибке
    except Exception as e:
        logger.error(f"Неожиданная ошибка (get_leaderboard): {e}", exc_info=True)
        return []

# --- Standard Deck and Hand Utilities ---
def create_deck(num_decks=NUM_DECKS):
    """Создает и перемешивает колоду из указанного количества стандартных колод."""
    # Используем list comprehension для эффективности
    deck = [(rank, suit) for _ in range(num_decks) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    logger.info(f"Создана и перемешана новая колода из {num_decks} стандартных колод ({len(deck)} карт).")
    return deck

def get_card_value(card):
    """Возвращает числовое значение карты (туз = 11)."""
    # Добавим проверку на None для безопасности
    if not card: return 0
    rank = card[0]
    return RANK_VALUES.get(rank, 0) # Возвращает 0 для неизвестных рангов

def get_hand_value(hand):
    """Рассчитывает стоимость руки в Блекджеке, корректно обрабатывая тузы."""
    value = 0
    ace_count = 0
    if not hand: # Возвращаем 0 для пустой руки
        return 0
    for card in hand:
        if card: # Проверка, что карта не None
            rank = card[0]
            value += get_card_value(card)
            if rank == 'A':
                ace_count += 1
        else:
            logger.warning("Обнаружена None карта в руке при подсчете очков.")
    # Корректировка значения тузов: пока сумма > 21 и есть тузы, считаем туз как 1
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
    return value

def format_hand(hand, hide_one=False): # <<< Имя параметра: hide_one
    """Форматирует руку для отображения. `hide_one=True` скрывает вторую карту."""
    if not hand:
        return "Пусто"
    if hide_one and len(hand) > 1: # Скрываем вторую карту, если их 2 или больше
        first_card = hand[0]
        # Убедимся, что первая карта не None перед форматированием
        first_card_str = f"{first_card[0]}{first_card[1]}" if first_card else "??"
        # Показываем остальные карты, если их больше двух (хотя обычно скрывается только вторая)
        # other_cards_str = ", ".join([f"{c[0]}{c[1]}" for c in hand[2:] if c])
        # return f"[{first_card_str}, ??{', ' + other_cards_str if other_cards_str else ''}]"
        # Упрощенный вариант: скрываем все после первой карты
        return f"[{first_card_str}, ??]"

    # Фильтруем None карты перед форматированием, если не скрываем
    return ", ".join([f"{c[0]}{c[1]}" for c in hand if c])

# --- Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    user = update.effective_user
    user_id = user.id
    logger.info(f"Команда /start от пользователя {user.full_name} (ID: {user_id})")
    # Убедимся, что пользователь существует в БД
    user_data = get_or_create_user(user_id)
    # Получаем баланс (может быть None при ошибке)
    balance = get_balance(user_id)

    if balance is not None:
        balance_str = f"{balance:.2f}" # Форматируем с 2 знаками после запятой
        await update.message.reply_text(
            f"Добро пожаловать, {user.first_name}! 👋\n"
            f"Ваш текущий баланс: {balance_str} фишек.\n\n"
            f"Используйте /blackjack для начала игры или /help для списка команд."
        )
    else:
        # Сообщаем об ошибке, если баланс получить не удалось
        await update.message.reply_text(
            "Не удалось получить ваш баланс. Пожалуйста, попробуйте команду /start еще раз позже."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help."""
    user_id = update.effective_user.id
    logger.info(f"Команда /help от пользователя {user_id}")
    help_text = (
        "ℹ️ *Список доступных команд:*\n\n"
        "/start - Приветствие и проверка баланса\n"
        "/blackjack - Начать новую игру в Блекджек\n"
        "/balance - Показать ваш текущий баланс\n"
        f"/bonus - Получить ежедневный бонус ({BONUS_AMOUNT} фишек, раз в {BONUS_COOLDOWN_HOURS} ч)\n"
        "/leaderboard - Показать таблицу лидеров\n"
        "/help - Показать это сообщение помощи"
    )
    # Используем MarkdownV2 для форматирования
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /balance."""
    user_id = update.effective_user.id
    logger.info(f"Команда /balance от пользователя {user_id}")
    current_balance = get_balance(user_id)
    if current_balance is not None:
        await update.message.reply_text(f"💰 Ваш текущий баланс: {current_balance:.2f} фишек.")
    else:
        await update.message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /bonus."""
    user_id = update.effective_user.id
    logger.info(f"Команда /bonus от пользователя {user_id}")
    user_data = get_or_create_user(user_id)
    if not user_data: # Проверка, что данные пользователя получены
        await update.message.reply_text("Произошла ошибка при получении ваших данных. Попробуйте /start.")
        return

    # Используем UTC для сравнения времени
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    last_bonus_time_utc = get_last_bonus_time(user_id) # Эта функция возвращает UTC datetime или None
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time_utc:
        # Убедимся, что last_bonus_time_utc осведомлен о таймзоне для корректного сравнения
        if last_bonus_time_utc.tzinfo is None:
           last_bonus_time_utc = last_bonus_time_utc.replace(tzinfo=datetime.timezone.utc)

        if now_utc < last_bonus_time_utc + cooldown:
            time_left = last_bonus_time_utc + cooldown - now_utc
            # Форматируем оставшееся время
            hours, remainder = divmod(time_left.total_seconds(), 3600)
            minutes, seconds = divmod(remainder, 60)
            await update.message.reply_text(
                f"⏳ Бонус уже был получен. Попробуйте снова примерно через {int(hours)} ч {int(minutes)} мин."
            )
            return # Выходим, если бонус еще не доступен

    # Если проверки пройдены, начисляем бонус
    new_balance = update_balance(user_id, BONUS_AMOUNT)
    if new_balance is not None:
        # Обновляем время последнего бонуса на текущее время UTC
        update_last_bonus_time(user_id, now_utc)
        await update.message.reply_text(
            f"🎉 Бонус в размере {BONUS_AMOUNT} фишек успешно начислен!\n"
            f"Ваш новый баланс: {new_balance:.2f} фишек."
        )
    else:
        await update.message.reply_text("Не удалось начислить бонус из-за ошибки обновления баланса. Попробуйте позже.")


# Функция для безопасного экранирования MarkdownV2 символов
def escape_markdown(text):
    """Экранирует специальные символы Markdown V2."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in str(text))

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /leaderboard."""
    user_id = update.effective_user.id
    logger.info(f"Команда /leaderboard от пользователя {user_id}")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)

    if not leaders:
        await update.message.reply_text("Таблица лидеров пока пуста или произошла ошибка при загрузке.")
        return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"

    async def get_user_info(user_id_to_fetch):
        """Асинхронно получает информацию о пользователе с кэшированием."""
        try:
            cache = context.bot_data.setdefault('user_cache', {})
            cache_expiry_seconds = 3600 # Кэшировать на 1 час
            now = datetime.datetime.now()

            if user_id_to_fetch in cache and (now - cache[user_id_to_fetch]['timestamp']).total_seconds() < cache_expiry_seconds:
                 # logger.debug(f"Cache hit for user {user_id_to_fetch}")
                 return cache[user_id_to_fetch]['user']

            # logger.debug(f"Cache miss for user {user_id_to_fetch}, fetching from API...")
            user_chat = await context.bot.get_chat(user_id_to_fetch)
            cache[user_id_to_fetch] = {'user': user_chat, 'timestamp': now}
            return user_chat
        except BadRequest as e:
            # Частая ошибка, если юзер заблокировал бота или ID невалидный
            if "chat not found" in str(e).lower():
                 logger.warning(f"Failed to get chat info for user {user_id_to_fetch} (BadRequest: Chat not found)")
            else:
                 logger.warning(f"Failed to get chat info for user {user_id_to_fetch} (BadRequest: {e})")
            return None
        except Exception as e:
            logger.warning(f"Failed to get user info for user {user_id_to_fetch}: {e}", exc_info=True)
            return None

    # Асинхронно собираем информацию о пользователях из топ-листа
    user_info_tasks = [get_user_info(leader['user_id']) for leader in leaders]
    users_info: list[User | None] = await asyncio.gather(*user_info_tasks)

    place_emojis = ["🥇", "🥈", "🥉"]

    for i, leader in enumerate(leaders):
        db_user_id = leader['user_id']
        balance = leader['balance']
        user_chat_info: User | None = users_info[i] # Может быть None

        # Формирование имени пользователя для отображения
        user_name_display = escape_markdown(f"User ID: {db_user_id}") # Запасной вариант
        if user_chat_info:
            name_to_display = user_chat_info.first_name or user_chat_info.full_name
            if name_to_display: # Если есть имя
                 safe_name = escape_markdown(name_to_display)
                 # Пытаемся создать упоминание (@username), если есть username, иначе просто имя
                 user_name_display = user_chat_info.mention_markdown_v2(safe_name) if user_chat_info.username else safe_name
            # Если имени нет, но есть username
            elif user_chat_info.username:
                 user_name_display = escape_markdown(f"@{user_chat_info.username}")

        # Индикатор места (эмодзи или номер)
        place_indicator = place_emojis[i] if i < len(place_emojis) else f"{escape_markdown(str(i+1))}."
        # Экранированный баланс
        balance_str = escape_markdown(f"{balance:.2f}")

        leaderboard_text += f"{place_indicator} {user_name_display} \\- `{balance_str}` фишек\n"

    try:
        # Отправляем сообщение с использованием MarkdownV2
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        # Если ошибка связана с форматированием Markdown
        logger.error(f"Error sending leaderboard (MarkdownV2 BadRequest): {e}")
        # Попытка отправить как обычный текст (убираем символы экранирования)
        plain_text = leaderboard_text.replace("\\", "").replace("`", "")
        try:
            await update.message.reply_text(plain_text)
            logger.info("Sent leaderboard as plain text fallback due to MarkdownV2 error.")
        except Exception as fe:
            logger.error(f"Error sending plain text leaderboard fallback: {fe}")
    except Exception as e:
        # Другие возможные ошибки при отправке
        logger.error(f"Error sending leaderboard (Other): {e}", exc_info=True)


# --- Blackjack Game Logic Handlers ---

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает новую игру или предлагает начать, если нет активной."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"Команда /blackjack от пользователя {user.full_name} ({user.id}) в чате {chat_id}")

    reply_func = None
    message_to_delete_id = None # ID сообщения, которое нужно удалить (старое игровое или с кнопкой "Новая игра")

    # Определяем, как отвечать (на сообщение или callback)
    if update.callback_query:
        # Запрос пришел от кнопки (например, "Новая игра")
        reply_func = update.callback_query.message.reply_text # Отвечаем новым сообщением в чат
        message_to_delete_id = update.callback_query.message.message_id # Запоминаем ID сообщения с кнопкой
        try:
            await update.callback_query.answer() # Отвечаем на callback, чтобы убрать "часики"
        except BadRequest as e:
            # Игнорируем ошибки для старых запросов
            if "query is too old" not in str(e).lower(): logger.warning(f"BJ Start CB Answer Error: {e}")
            else: pass
    elif update.message:
        # Запрос пришел от команды /blackjack
        reply_func = update.message.reply_text
    else:
        # Неожиданный случай, не должно происходить
        logger.warning("blackjack_start вызван без message или callback_query")
        return

    current_balance = get_balance(user.id)
    if current_balance is None:
        await reply_func("Не удалось проверить ваш баланс. Попробуйте /start.")
        return

    # Инициализация хранилища игр, если его нет
    context.bot_data.setdefault('games', {})

    # --- Обработка существующей игры или завершение старой ---
    if chat_id in context.bot_data['games']:
        game_state = context.bot_data['games'][chat_id]
        old_game_msg_id = game_state.get('message_id')

        # Позволяем начать новую игру только если старая завершена или ожидает ставки
        if game_state.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Вы не можете начать новую игру, пока текущая не завершена.")
            # Опционально: можно отправить текущее состояние игры, если оно есть
            if old_game_msg_id: await show_game_state(context, chat_id, old_game_msg_id)
            return

        # Удаляем старое игровое сообщение (если оно было и не то, с которого пришел callback)
        if old_game_msg_id and old_game_msg_id != message_to_delete_id:
             try:
                 await context.bot.delete_message(chat_id, old_game_msg_id)
                 logger.debug(f"Удалено предыдущее игровое сообщение {old_game_msg_id} в чате {chat_id}")
             except BadRequest as e:
                 # Игнорируем, если сообщение уже удалено
                 if "message to delete not found" not in str(e).lower():
                     logger.warning(f"Не удалось удалить старое игровое сообщение {old_game_msg_id}: {e}")
             except Exception as e:
                 logger.error(f"Неожиданная ошибка при удалении старого игрового сообщения {old_game_msg_id}: {e}")

        # Удаляем старую игру из памяти в любом случае (если начинаем новую)
        del context.bot_data['games'][chat_id]
        logger.debug(f"Удалено состояние предыдущей игры для чата {chat_id}")

    # --- Проверка возможности сделать ставку ---
    if current_balance <= 0:
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) равен нулю. Используйте /bonus, чтобы получить фишки.")
        return

    # Предлагаем ставки, доступные по балансу
    bet_options = [1, 5, 10, 25, 50, 100, 250, 500] # Настраиваемый список ставок
    valid_bets = [b for b in bet_options if b <= current_balance]

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) меньше минимальной ставки ({min_bet} фишек). Используйте /bonus.")
        return

    # --- Создаем кнопки для ставок ---
    buttons = []
    row = []
    max_buttons_per_row = 4 # Для компактности на мобильных
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) >= max_buttons_per_row:
            buttons.append(row)
            row = []
    if row: # Добавляем последнюю строку, если она не пустая
        buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)

    # --- Отправляем сообщение с предложением ставки ---
    try:
        # Если запрос был от кнопки, удаляем старое сообщение и отправляем новое
        if message_to_delete_id:
            try:
                 await context.bot.delete_message(chat_id=chat_id, message_id=message_to_delete_id)
                 logger.debug(f"Сообщение {message_to_delete_id} с кнопкой 'Новая игра' удалено.")
            except Exception as e:
                 logger.warning(f"Не удалось удалить сообщение {message_to_delete_id} с кнопкой 'Новая игра': {e}")
            # Отправляем новое сообщение с кнопками ставок
            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=f"Ваш баланс: {current_balance:.2f} фишек. Сделайте вашу ставку:",
                reply_markup=markup
            )
        else:
            # Если запрос от /blackjack, используем reply_func
             sent_message = await reply_func(
                 f"Ваш баланс: {current_balance:.2f} фишек. Сделайте вашу ставку:",
                 reply_markup=markup
             )

        # Сохраняем состояние ожидания ставки
        context.bot_data['games'][chat_id] = {
            'player_id': user.id,
            'state': 'waiting_bet',
            'message_id': sent_message.message_id # Сохраняем ID сообщения с кнопками ставок
        }
        logger.info(f"Игра инициирована для пользователя {user.id} в чате {chat_id}. Ожидание ставки.")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение с выбором ставки в чат {chat_id}: {e}", exc_info=True)
        # Пытаемся уведомить пользователя об ошибке
        try: await reply_func("Произошла ошибка при попытке начать игру. Попробуйте еще раз.")
        except: pass # Если и это не удалось, просто логируем


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    """Обрабатывает выбор ставки игроком и начинает раздачу."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.info(f"Получена ставка {bet_amount} от {user.full_name} ({user.id}) в чате {chat_id}")

    # Проверки состояния игры
    if chat_id not in context.bot_data.get('games', {}):
        await query.answer("Не найдена активная игра для вас. Начните новую: /blackjack", show_alert=True)
        return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user.id:
        await query.answer("Это не ваша игра.", show_alert=True)
        return
    if game_state.get('state') != 'waiting_bet':
        # Игра уже идет или ставка сделана, игнорируем повторное нажатие
        await query.answer("Ставка уже сделана или игра идет.", show_alert=False) # Не показываем alert
        return

    # Проверка баланса
    current_balance = get_balance(user.id)
    if current_balance is None:
        await query.answer("Ошибка при проверке баланса. Попробуйте снова.", show_alert=True)
        return
    if bet_amount <= 0:
        await query.answer("Ставка должна быть положительной.", show_alert=True)
        return
    if bet_amount > current_balance:
        await query.answer(f"Недостаточно средств. Ваш баланс: {current_balance:.2f} фишек.", show_alert=True)
        return

    # Списываем ставку
    new_balance = update_balance(user.id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка при списании ставки. Попробуйте снова.", show_alert=True)
        return

    # --- <<< Создание колоды ТОЛЬКО ЗДЕСЬ >>> ---
    deck = create_deck(NUM_DECKS) # Создаем и перемешиваем новую колоду
    player_hand = []
    dealer_hand = []
    cards_dealt_count = 0 # Счетчик розданных карт

    # --- Раздача начальных карт ---
    try:
        # Используем _draw_card_from_shoe для получения карт
        card1, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        player_hand.append(card1); cards_dealt_count += 1

        card2, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        dealer_hand.append(card2); cards_dealt_count += 1

        card3, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        player_hand.append(card3); cards_dealt_count += 1

        card4, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
        dealer_hand.append(card4); cards_dealt_count += 1

        # Проверка на None карты после раздачи (если _draw_card_from_shoe вернула None)
        if None in player_hand or None in dealer_hand:
             raise ValueError("Ошибка раздачи: получена пустая карта (колода закончилась?).")

    except IndexError: # Если колода закончилась во время начальной раздачи (крайне маловероятно при NUM_DECKS > 1)
        logger.error(f"Критическая ошибка: колода закончилась во время начальной раздачи для чата {chat_id}.")
        update_balance(user.id, bet_amount) # Возвращаем ставку
        await query.edit_message_text("Произошла ошибка с колодой во время раздачи. Ставка возвращена. Попробуйте начать заново /blackjack.")
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id] # Чистим состояние игры
        return
    except Exception as e:
        logger.error(f"Ошибка во время начальной раздачи карт: {e}", exc_info=True)
        update_balance(user.id, bet_amount) # Возвращаем ставку
        await query.edit_message_text(f"Произошла ошибка во время раздачи карт ({e}). Ставка возвращена. Попробуйте начать заново /blackjack.")
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id] # Чистим состояние игры
        return

    # --- Проверка на Блекджек сразу после раздачи ---
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    dealer_up_card_value = get_card_value(dealer_hand[0]) if dealer_hand else 0

    player_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    outcome_text = None
    current_state = 'player_turn' # Изначально ход игрока
    player_hand_status = 'active' # Статус первой руки игрока

    if player_blackjack:
        player_hand_status = 'blackjack' # Устанавливаем статус БЖ игроку
        if dealer_blackjack:
            # Оба Блекджека - Пуш (возвращаем ставку)
            outcome_text = f"⚖️ Ничья! У вас и у дилера Блекджек. Ваша ставка {bet_amount} F возвращена."
            update_balance(user.id, bet_amount) # Возвращаем ставку
            current_state = 'game_over'
        else:
            # Только у игрока Блекджек - Выигрыш (ставка + выигрыш)
            win_amount = bet_amount * BLACKJACK_PAYOUT
            total_return = bet_amount + win_amount
            update_balance(user.id, total_return) # Возвращаем ставку + выигрыш
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Вы выиграли {win_amount:.2f} фишек!"
            current_state = 'game_over'
    elif dealer_blackjack:
        # Только у дилера Блекджек - Проигрыш (ставка уже списана)
        outcome_text = f"😥 У дилера Блекджек! Вы проиграли ставку {bet_amount} F."
        current_state = 'game_over'

    # --- Обновляем состояние игры ---
    game_state.update({
        'state': current_state,
        'deck': deck, # Сохраняем созданную колоду
        'player_hands': [{ # Список рук игрока (начинаем с одной)
            'hand': player_hand,
            'bet': bet_amount,
            'status': player_hand_status, # 'active', 'blackjack', 'bust', 'stand'
            'can_double': (not player_blackjack and not dealer_blackjack), # Удвоить можно только если нет БЖ и ход игрока
            'can_split': False # Возможность сплита определится в show_game_state
        }],
        'current_hand_index': 0, # Индекс текущей руки игрока (начинаем с 0)
        'dealer_hand': dealer_hand,
        'cards_dealt': cards_dealt_count, # Сохраняем кол-во розданных карт
        'initial_bet': bet_amount,
        'split_count': 0, # Счетчик сплитов
        'outcome_text': outcome_text, # Текст исхода, если игра закончилась сразу
    })

    # --- Показываем начальное состояние игры ---
    # Передаем ID сообщения, которое нужно отредактировать (то, где были кнопки ставок)
    await show_game_state(context, chat_id, query.message.message_id)

    # Отвечаем на callback ставки (можно сделать пустым или информативным)
    try: await query.answer(f"Ставка {bet_amount} F принята!")
    except BadRequest as e: # Игнорируем старые запросы
        if "query is too old" not in str(e).lower(): logger.warning(f"Bet CB Answer Error: {e}")


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id_to_edit: int):
    """Отображает текущее состояние игры (руки, ставки, кнопки действий)."""
    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"show_game_state вызван для чата {chat_id}, но игра не найдена.")
        return
    game_state = context.bot_data['games'][chat_id]
    player_id = game_state.get('player_id')
    if not player_id:
        logger.error(f"Состояние игры для чата {chat_id} не содержит player_id.")
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

    # --- Формируем текст сообщения ---
    # Используем Markdown V1 (ParseMode.MARKDOWN) для простоты и совместимости
    text = f"*Блекджек* | Баланс: {balance_str} F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands if isinstance(h, dict))
    num_hands = len(player_hands)
    text += f"Общая ставка: {total_bet} F"
    if num_hands > 1: text += f" ({num_hands} руки)"
    text += "\n" + "--------------------\n" # Разделитель

    # --- Рука дилера ---
    dealer_value = get_hand_value(dealer_hand)
    dealer_value_str = "???" # Значение по умолчанию (если скрыто или рука пуста)
    if dealer_hand: # Проверяем, что рука дилера не пуста
        if not hide_dealer_card:
            dealer_value_str = str(dealer_value)
        elif dealer_hand[0]: # Показываем значение первой карты, если скрываем вторую
             dealer_value_str = str(get_card_value(dealer_hand[0])) + "+?"

    # <<< ИСПРАВЛЕНИЕ ЗДЕСЬ >>>
    dealer_hand_str = format_hand(dealer_hand, hide_one=hide_dealer_card) # Используем hide_one=
    text += f"*Дилер:* {dealer_hand_str} ({dealer_value_str})\n\n"

    # --- Руки игрока ---
    text += "*Вы:*\n"
    active_hand_data = None # Данные активной руки для кнопок
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue # Пропускаем невалидные записи рук

        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', 'unknown')
        hand_bet = hand_data.get('bet', 0)

        is_current_hand = (i == current_hand_index and hand_status == 'active' and game_status == 'player_turn')

        # Индикатор текущей/завершенной руки
        indicator = "▫️" # По умолчанию
        if is_current_hand: indicator = "▶️" # Активная рука
        elif hand_status == 'stand': indicator = "✅" # Стоп
        elif hand_status == 'bust': indicator = "❌" # Перебор
        elif hand_status == 'blackjack': indicator = "💰" # Блекджек

        text += f"{indicator} Рука {i+1}: {format_hand(hand)} ({hand_value}) [{hand_bet} F]"

        # Добавляем статус словами для ясности
        if hand_status == 'bust': text += " - *Перебор!*"
        elif hand_status == 'blackjack': text += " - *Блекджек!*"
        elif hand_status == 'stand' and not is_current_hand: text += " - *Стоп*" # Показываем "Стоп" для неактивных рук

        text += "\n"

        if is_current_hand:
            active_hand_data = hand_data # Сохраняем для кнопок

    # --- Кнопки действий ---
    keyboard = []
    if active_hand_data and game_status == 'player_turn':
        current_hand = active_hand_data.get('hand', [])
        current_bet = active_hand_data.get('bet', 0)

        # Проверка возможности удвоения (только на первых двух картах)
        can_double = (active_hand_data.get('can_double', False)
                      and len(current_hand) == 2
                      and current_balance is not None and current_balance >= current_bet)

        # Проверка возможности разделения (2 карты, одинаковое ЗНАЧЕНИЕ, хватает баланса, не превышен лимит)
        can_split = (len(current_hand) == 2
                     and current_hand[0] and current_hand[1] # Убедимся что карты существуют
                     and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) # Одинаковые по значению
                     and current_balance is not None and current_balance >= current_bet
                     and game_state.get('split_count', 0) < MAX_SPLITS)
        # Сохраняем возможность сплита в состоянии руки для handle_blackjack_action
        active_hand_data['can_split'] = can_split

        action_buttons = [
            InlineKeyboardButton("Еще", callback_data=f"bj_action_hit_{current_hand_index}"),
            InlineKeyboardButton("Хватит", callback_data=f"bj_action_stand_{current_hand_index}")
        ]
        keyboard.append(action_buttons)

        special_buttons = []
        if can_double: special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_action_double_{current_hand_index}"))
        if can_split: special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_action_split_{current_hand_index}"))
        if special_buttons: keyboard.append(special_buttons)

    elif game_status == 'game_over':
        text += "\n*Игра завершена!* 🎉\n"
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

    # --- Редактируем сообщение ---
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id_to_edit,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN # Используем Markdown V1
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            pass # Игнорируем, если сообщение не изменилось (часто при быстром нажатии)
        elif "message to edit not found" in str(e).lower():
             logger.warning(f"Сообщение {message_id_to_edit} для редактирования не найдено в чате {chat_id}. Возможно, удалено?")
             # Попробуем отправить новое сообщение, если старое удалено, а игра еще идет
             if game_status != 'game_over' and chat_id in context.bot_data.get('games', {}):
                 try:
                     new_msg = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                     # Обновляем ID сообщения в состоянии игры!
                     context.bot_data['games'][chat_id]['message_id'] = new_msg.message_id
                     logger.info(f"Отправлено новое сообщение {new_msg.message_id} т.к. старое не найдено.")
                 except Exception as send_e:
                     logger.error(f"Не удалось отправить новое сообщение после ошибки редактирования: {send_e}")
        else:
            # Другие ошибки BadRequest (например, неправильный Markdown)
            logger.error(f"Ошибка редактирования сообщения {message_id_to_edit} (BadRequest): {e}")
            # Попробуем отправить без форматирования
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id_to_edit, text=text.replace("*","").replace("_",""), reply_markup=reply_markup)
                logger.info("Отправлено состояние игры без Markdown из-за ошибки BadRequest.")
            except Exception as fallback_e:
                 logger.error(f"Не удалось отправить состояние игры даже без Markdown: {fallback_e}")

    except Exception as e:
        logger.error(f"Неожиданная ошибка при отображении состояния игры для чата {chat_id}: {e}", exc_info=True)


async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    """Обрабатывает действия игрока (Hit, Stand, Double, Split)."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.info(f"Действие '{action}' для руки {hand_index} от {user.full_name} ({user.id}) в чате {chat_id}")

    # --- Проверки состояния игры ---
    if chat_id not in context.bot_data.get('games', {}): return # Игра не найдена
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user.id: return # Не игра этого пользователя
    if game_state.get('state') != 'player_turn': return # Не ход игрока
    player_hands = game_state.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)): # Проверка корректности индекса
        logger.warning(f"Некорректный hand_index {hand_index} в действии '{action}' для чата {chat_id}")
        return
    # Проверка, что действие пришло для текущей активной руки
    if hand_index != game_state.get('current_hand_index'):
        await query.answer("Сейчас ход другой руки.", show_alert=False) # Не алерт, просто уведомление
        return

    hand_data = player_hands[hand_index]
    if not isinstance(hand_data, dict) or hand_data.get('status') != 'active':
        # Действие для неактивной руки (уже bust, stand или blackjack)
        await query.answer("Действие для этой руки уже выполнено.", show_alert=False)
        return

    # --- Получение данных для действия ---
    current_hand = hand_data.get('hand', [])
    deck = game_state.get('deck', []) # Берем текущую колоду из состояния
    current_balance = get_balance(user.id)
    current_bet = hand_data.get('bet', 0)
    cards_dealt_count = game_state.get('cards_dealt', 0) # Получаем текущий счетчик карт

    # Проверка баланса перед действиями, требующими ставки
    if action in ['double', 'split'] and current_balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True)
        return

    # --- Обработка действий ---
    should_update_state = False # Флаг, нужно ли перерисовать сообщение
    try:
        if action == 'hit':
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                current_hand.append(card)
                game_state['cards_dealt'] += 1 # Обновляем счетчик в состоянии игры
                hand_data['can_double'] = False # Нельзя удваивать/делить после хита
                hand_data['can_split'] = False
                new_value = get_hand_value(current_hand)
                await query.answer(f"Ваша карта: {card[0]}{card[1]}") # Краткий ответ о карте

                if new_value > 21:
                    hand_data['status'] = 'bust'
                    # await query.answer("Перебор!") # Заменено на лог ниже
                    logger.info(f"Рука {hand_index} игрока {user.id} - Перебор ({new_value})")
                    await next_player_action_or_dealer(context, chat_id) # Переход хода
                elif new_value == 21:
                    hand_data['status'] = 'stand' # Авто-стоп на 21
                    logger.info(f"Рука {hand_index} игрока {user.id} - 21 ({new_value}), авто-стоп.")
                    await next_player_action_or_dealer(context, chat_id) # Переход хода
                else:
                    # Игра продолжается на этой руке, просто обновляем состояние
                    should_update_state = True
            else:
                # Карта не получена (колода закончилась?) - считаем как Stand
                logger.warning(f"Hit не удался для user {user.id} в чате {chat_id} - колода пуста?")
                hand_data['status'] = 'stand'
                await query.answer("Не удалось взять карту (колода?). Рука остается.", show_alert=True)
                await next_player_action_or_dealer(context, chat_id) # Переход хода

        elif action == 'stand':
            hand_data['status'] = 'stand'
            await query.answer("Стоп.") # Краткий ответ
            await next_player_action_or_dealer(context, chat_id) # Переход хода

        elif action == 'double':
            # Повторная проверка условий (на всякий случай + баланс)
            can_double = (hand_data.get('can_double', False)
                          and len(current_hand) == 2
                          and current_balance is not None and current_balance >= current_bet)
            if can_double:
                new_balance = update_balance(user.id, -current_bet) # Списываем доп. ставку
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
                        await query.answer(f"Удвоено! Карта: {card[0]}{card[1]}. Итог: {new_value}{' (Перебор!)' * (new_value > 21)}")
                        await next_player_action_or_dealer(context, chat_id) # Переход хода
                    else:
                        # Карта не получена - считаем как Stand без доп. карты
                        logger.warning(f"Double не удался для user {user.id} в чате {chat_id} - колода пуста?")
                        hand_data['status'] = 'stand'
                        await query.answer("Удвоено! Не удалось взять карту (колода?). Рука остается.", show_alert=True)
                        await next_player_action_or_dealer(context, chat_id) # Переход хода
                else:
                    await query.answer("Ошибка обновления баланса при удвоении.", show_alert=True)
            else:
                 # Сообщаем, почему нельзя удвоить
                 reason = ""
                 if not hand_data.get('can_double', False): reason = "действие недоступно"
                 elif len(current_hand) != 2: reason = "не 2 карты"
                 elif current_balance is None or current_balance < current_bet: reason = "недостаточно средств"
                 await query.answer(f"Удвоение невозможно ({reason}).", show_alert=True)

        elif action == 'split':
             # Используем флаг 'can_split', установленный в show_game_state
             can_split = hand_data.get('can_split', False)
             if can_split:
                 # Убедимся еще раз в наличии баланса
                 if current_balance is None or current_balance < current_bet:
                      await query.answer("Недостаточно средств для разделения.", show_alert=True)
                      return # Выход, если денег нет

                 new_balance = update_balance(user.id, -current_bet) # Списываем ставку для новой руки
                 if new_balance is not None:
                     game_state['split_count'] += 1
                     # Создаем новую руку
                     card_to_move = current_hand.pop() # Забираем вторую карту из текущей руки
                     new_hand_data = {
                         'hand': [card_to_move], # Новая рука начинается с этой карты
                         'bet': current_bet,
                         'status': 'active',
                         'can_double': False, # Будет установлено после раздачи карт
                         'can_split': False
                     }
                     # Вставляем новую руку сразу после текущей (индекс + 1)
                     player_hands.insert(hand_index + 1, new_hand_data)
                     logger.info(f"Рука {hand_index} разделена игроком {user.id}. Новая рука на индексе {hand_index + 1}.")

                     # Раздаем по одной карте в КАЖДУЮ из разделенных рук
                     card1, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card1:
                         current_hand.append(card1); game_state['cards_dealt'] += 1
                         logger.debug(f"Карта {card1[0]}{card1[1]} добавлена в руку {hand_index} после сплита.")
                     else: logger.warning(f"Не удалось взять карту 1 после сплита в {chat_id}")

                     card2, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card2:
                         new_hand_data['hand'].append(card2); game_state['cards_dealt'] += 1
                         logger.debug(f"Карта {card2[0]}{card2[1]} добавлена в новую руку {hand_index + 1} после сплита.")
                     else: logger.warning(f"Не удалось взять карту 2 после сплита в {chat_id}")

                     # Особое правило для сплита тузов: игра на каждой руке сразу завершается (stand)
                     is_ace_split = get_card_value(current_hand[0]) == 11 # Проверяем по первой карте (вторая была такая же)

                     if is_ace_split:
                         hand_data['status'] = 'stand'
                         new_hand_data['status'] = 'stand'
                         hand_data['can_double'] = False # Удваивать тузы после сплита нельзя
                         new_hand_data['can_double'] = False
                         await query.answer("Тузы разделены. Раздано по одной карте. Ход завершен.")
                         await next_player_action_or_dealer(context, chat_id) # Сразу переход хода
                     else:
                         # Обновляем флаги can_double/can_split для обеих рук (если не тузы)
                         # Удваивать можно, если после раздачи 2 карты
                         hand_data['can_double'] = (len(current_hand) == 2)
                         new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)
                         # Повторный сплит возможен, если опять одинаковые карты и лимит не достигнут
                         hand_data['can_split'] = (len(current_hand) == 2 and current_hand[0] and current_hand[1] and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) and game_state['split_count'] < MAX_SPLITS)
                         new_hand_data['can_split'] = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and game_state['split_count'] < MAX_SPLITS)

                         # Авто-стоп, если на какой-то руке сразу 21 (не БЖ)
                         if get_hand_value(current_hand) == 21: hand_data['status'] = 'stand'
                         if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                         await query.answer("Рука разделена!")
                         # Остаемся на текущей руке (hand_index), просто обновляем состояние
                         should_update_state = True

                 else:
                     await query.answer("Ошибка обновления баланса при разделении.", show_alert=True)
             else:
                  # Сообщаем, почему нельзя разделить
                  reason = ""
                  if len(current_hand) != 2: reason = "не 2 карты"
                  elif not (current_hand[0] and current_hand[1] and get_card_value(current_hand[0]) == get_card_value(current_hand[1])): reason = "карты не одинаковы по значению"
                  elif current_balance is None or current_balance < current_bet: reason = "недостаточно средств"
                  elif game_state.get('split_count', 0) >= MAX_SPLITS: reason = f"макс. {MAX_SPLITS} сплита"
                  else: reason = "неизвестная причина" # На всякий случай
                  await query.answer(f"Разделение невозможно ({reason}).", show_alert=True)

        # Обновляем сообщение игры, если нужно (после hit или split без завершения хода)
        if should_update_state:
            await show_game_state(context, chat_id, query.message.message_id)

    except IndexError:
         # Ошибка возникает, если _draw_card_from_shoe не может взять карту (колода пуста)
         logger.warning(f"Действие '{action}' не удалось для user {user.id} в chat {chat_id} - IndexError (колода закончилась?).")
         hand_data['status'] = 'stand' # Завершаем ход для этой руки как "стоп"
         await query.answer(f"Не удалось выполнить '{action}', так как в колоде закончились карты! Ваша рука остается.", show_alert=True)
         await next_player_action_or_dealer(context, chat_id) # Передаем ход дальше

    except Exception as e:
         logger.error(f"Ошибка при обработке действия '{action}' для user {user.id} в chat {chat_id}: {e}", exc_info=True)
         try: await query.answer("Произошла ошибка при обработке вашего хода.", show_alert=True)
         except: pass


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Переключает на следующую активную руку игрока или начинает ход дилера."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    # Эта функция вызывается только из player_turn, но проверим на всякий случай
    if game_state.get('state') != 'player_turn':
        logger.warning(f"next_player_action_or_dealer вызван в состоянии {game_state.get('state')} для чата {chat_id}")
        # Если ход дилера уже начался, ничего не делаем
        if game_state.get('state') == 'dealer_turn': return
        # Если игра окончена, тоже выходим
        if game_state.get('state') == 'game_over': return
        # Иначе пробуем перейти к дилеру (нештатная ситуация)
        game_state['state'] = 'dealer_turn'

    player_hands = game_state.get('player_hands', [])
    current_index = game_state.get('current_hand_index', -1)

    # Ищем следующую руку со статусом 'active'
    next_active_index = -1
    for i in range(current_index + 1, len(player_hands)):
        if isinstance(player_hands[i], dict) and player_hands[i].get('status') == 'active':
            next_active_index = i
            break

    if next_active_index != -1:
        # Нашли следующую активную руку
        game_state['current_hand_index'] = next_active_index
        logger.info(f"Переход к руке {next_active_index} для игрока {game_state.get('player_id')} в чате {chat_id}")
        await show_game_state(context, chat_id, game_state.get('message_id'))
    else:
        # Активных рук игрока больше нет, переходим к ходу дилера
        game_state['state'] = 'dealer_turn'
        logger.info(f"Игрок {game_state.get('player_id')} завершил ход в чате {chat_id}. Начинается ход дилера.")
        # Обновляем сообщение, чтобы показать "Ход дилера..."
        await show_game_state(context, chat_id, game_state.get('message_id'))
        # Запускаем ход дилера с небольшой задержкой через job_queue
        context.job_queue.run_once(
            dealer_turn_job,
            when=DEALER_TURN_DELAY, # Задержка в секундах
            chat_id=chat_id,
            data=chat_id, # Передаем chat_id в job
            name=f"dealer_turn_{chat_id}" # Уникальное имя для джоба
        )


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Выполняет ход дилера (берет карты до 17 или Soft 17) - запускается через Job Queue."""
    chat_id = context.job.data # Получаем chat_id из данных джоба
    logger.info(f"Запущен job dealer_turn для чата {chat_id}")

    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"Job хода дилера выполнен для чата {chat_id}, но игра не найдена.")
        return
    game_state = context.bot_data['games'][chat_id]
    # Важно: проверяем, что состояние все еще 'dealer_turn' (могло измениться)
    if game_state.get('state') != 'dealer_turn':
        logger.warning(f"Job хода дилера выполнен для чата {chat_id}, но состояние уже не 'dealer_turn' ({game_state.get('state')}). Прерывание.")
        return

    deck = game_state.get('deck', [])
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands = game_state.get('player_hands', [])
    cards_dealt_count = game_state.get('cards_dealt', 0)

    # Проверяем, есть ли смысл дилеру ходить
    # Если все руки игрока bust или blackjack (уже оплачен), дилер не ходит
    player_can_win_or_push = any(
        isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack']
        for h in player_hands
    )

    dealer_value_initial = get_hand_value(dealer_hand)
    dealer_had_blackjack_on_deal = (dealer_value_initial == 21 and len(dealer_hand) == 2)

    if not player_can_win_or_push and not dealer_had_blackjack_on_deal:
        logger.info(f"Ход дилера пропущен в чате {chat_id}, т.к. все руки игрока проиграли или получили БЖ.")
        # Дилер не ходит, но результат все равно определяем (дилер вскрывает карты)
        await determine_outcome(context, chat_id, dealer_had_blackjack_on_deal)
        return

    # --- Дилер берет карты ---
    dealer_hit_count = 0
    while True:
        dealer_value = get_hand_value(dealer_hand)
        # Проверка на мягкую руку (туз считается как 11)
        ace_count = sum(1 for card in dealer_hand if card and card[0] == 'A')
        is_soft = ace_count > 0 and (dealer_value - ace_count * 11) < 11 # True если есть туз(ы), считаемый(е) как 11

        # Правило взятия карты дилером
        should_hit = (dealer_value < 17) or (dealer_value == 17 and is_soft and DEALER_HITS_SOFT_17)

        if not should_hit:
            logger.info(f"Дилер останавливается на {dealer_value} ({format_hand(dealer_hand)}) в чате {chat_id}.")
            break # Дилер останавливается

        dealer_hit_count += 1
        logger.debug(f"Дилер берет карту {dealer_hit_count} (текущее значение: {dealer_value}) в чате {chat_id}")

        # Берем карту
        try:
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                dealer_hand.append(card)
                game_state['cards_dealt'] += 1
                cards_dealt_count = game_state['cards_dealt'] # Обновляем локальный счетчик
                logger.debug(f"Дилер взял карту {card[0]}{card[1]}. Новая рука: {format_hand(dealer_hand)}")
                # Опционально: обновить сообщение после каждой карты дилера для "анимации"
                # await show_game_state(context, chat_id, game_state.get('message_id'))
                # await asyncio.sleep(DEALER_TURN_DELAY * 1.5) # Доп. задержка между картами дилера
            else:
                # Карта не получена - колода закончилась
                logger.warning(f"Ход дилера прерван в чате {chat_id} - не удалось взять карту (колода пуста?).")
                break # Прерываем цикл, если карта не может быть взята
        except IndexError:
            logger.warning(f"Ход дилера прерван в чате {chat_id} - IndexError (колода закончилась?).")
            break # Колода закончилась

    # --- Ход дилера завершен, определяем результат ---
    await determine_outcome(context, chat_id, dealer_had_blackjack_on_deal)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack_on_deal: bool):
    """Определяет результат игры для каждой руки игрока и обновляет баланс."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    logger.info(f"Определение исхода игры в чате {chat_id}")

    # Предотвращаем повторное определение исхода, если он уже есть
    # Но позволяем показать финальное сообщение еще раз
    if game_state.get('state') == 'game_over' and 'outcome_determined' in game_state:
        logger.debug(f"Исход для чата {chat_id} уже определен, просто показываем состояние.")
        await show_game_state(context, chat_id, game_state.get('message_id'))
        return

    player_id = game_state.get('player_id')
    player_hands = game_state.get('player_hands', [])
    dealer_hand = game_state.get('dealer_hand', [])
    if not player_id:
        logger.error(f"Не найден player_id при определении исхода в чате {chat_id}")
        return # Не можем продолжить без ID игрока

    dealer_value = get_hand_value(dealer_hand)
    dealer_is_bust = dealer_value > 21
    dealer_hand_final_str = format_hand(dealer_hand) # Полная рука дилера для логов/сообщений

    logger.info(f"Рука дилера: {dealer_hand_final_str} ({dealer_value}), Перебор: {dealer_is_bust}, Был БЖ: {dealer_had_blackjack_on_deal}")

    outcomes = [] # Текстовые результаты для каждой руки
    total_winnings = 0 # Сумма, которую нужно ВЕРНУТЬ игроку (включая ставки)
    total_bet = 0 # Общая сумма ставок во всех руках

    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue

        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status') # Статус руки на момент завершения хода игрока
        player_value = get_hand_value(hand)
        player_hand_str = format_hand(hand)
        player_had_blackjack_this_hand = (status == 'blackjack') # Был ли БЖ на этой руке

        total_bet += bet # Суммируем все ставки
        payout_multiplier = 0 # 0=проигрыш, 1=пуш, 2=выигрыш 1:1, (1 + BJ_PAYOUT)=БЖ
        outcome_str = ""
        hand_prefix = f"Рука {i+1}: " if len(player_hands) > 1 else "" # Префикс для мульти-рук

        logger.debug(f"Обработка руки {i}: Статус={status}, Карты={player_hand_str}, Очки={player_value}, Ставка={bet}")

        if status == 'bust':
            payout_multiplier = 0 # Ставка проиграна
            outcome_str = f"{hand_prefix}Перебор ({player_value}). Ставка {bet} F проиграна."
        elif player_had_blackjack_this_hand:
             # Этот случай должен был обработаться при раздаче, но проверим
             if dealer_had_blackjack_on_deal: # Если у дилера тоже БЖ
                 payout_multiplier = 1 # Пуш
                 outcome_str = f"{hand_prefix}Блекджек! Но у дилера тоже. Ничья, ставка {bet} F возвращена."
             else: # Только у игрока БЖ
                 payout_multiplier = 1 + BLACKJACK_PAYOUT
                 win_amount = bet * BLACKJACK_PAYOUT
                 outcome_str = f"{hand_prefix}Блекджек! Выигрыш {win_amount:.2f} F."
        elif dealer_had_blackjack_on_deal: # У дилера БЖ, игрок проиграл (т.к. у игрока не БЖ)
            payout_multiplier = 0
            outcome_str = f"{hand_prefix}У дилера Блекджек. Ставка {bet} F проиграна."
        elif dealer_is_bust:
             # У дилера перебор, игрок выигрывает (если у игрока не перебор)
            payout_multiplier = 2 # Возврат ставки + выигрыш 1:1
            outcome_str = f"{hand_prefix}У дилера перебор ({dealer_value})! Выигрыш {bet} F."
        # Сравниваем очки, если ни у кого нет перебора или БЖ
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
        total_winnings += bet * payout_multiplier # Суммируем возврат/выигрыш
        logger.debug(f"Результат руки {i}: {outcome_str}, Множитель={payout_multiplier}, Выигрыш/Возврат={bet * payout_multiplier}")

    # --- Обновляем баланс игрока ---
    net_change = total_winnings - total_bet # Чистое изменение баланса
    logger.info(f"Общий итог для {player_id}: Ставки={total_bet}, Выигрыш/Возврат={total_winnings}, Изменение={net_change:+.2f}")

    if total_winnings > 0: # Обновляем баланс только если есть возврат/выигрыш
        final_balance = update_balance(player_id, total_winnings)
        if final_balance is None:
             logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: не удалось обновить баланс для user {player_id} после игры в chat {chat_id}.")
             # Пытаемся сообщить об ошибке, но не перезаписываем исход игры
             outcomes.append("\n**ОШИБКА:** Не удалось начислить выигрыш! Свяжитесь с администратором.")
             net_change = 0 # Считаем, что изменения не было, раз не записали
    # Если выигрыша не было (total_winnings == 0), баланс не обновляем (ставка уже списана)

    # --- Сохраняем результат в состоянии игры ---
    game_state['state'] = 'game_over'
    game_state['outcome_text'] = "\n".join(outcomes) + f"\n\n*Общий итог раунда: {net_change:+.2f} F*" # Форматируем с + или -
    game_state['outcome_determined'] = True # Флаг, что исход определен

    # --- Показываем финальное сообщение ---
    await show_game_state(context, chat_id, game_state.get('message_id'))

    # Опционально: очистка состояния игры после показа результата
    # if chat_id in context.bot_data['games']:
    #     # Можно добавить задержку перед удалением, чтобы пользователь успел увидеть результат
    #     # await asyncio.sleep(60) # Например, 1 минута
    #     # del context.bot_data['games'][chat_id]
    #     # logger.info(f"Состояние игры для чата {chat_id} очищено после определения исхода.")
    #     pass


# --- <<< Internal Dealing Logic >>> ---

def _draw_card_from_shoe(deck: list, cards_dealt: int, num_decks: int):
    """
    Просто берет случайную карту из оставшейся колоды и удаляет ее.
    Возвращает (карта, 0) или (None, 0), если колода пуста.
    Второй элемент (0) - заглушка для совместимости старой сигнатуры.
    """
    if not deck: # Проверка на пустую колоду
        logger.warning("_draw_card_from_shoe: Попытка взять карту из пустой колоды.")
        return None, 0
    try:
        # Выбираем случайную карту из оставшихся
        chosen_card_index = random.randrange(len(deck))
        chosen_card = deck.pop(chosen_card_index) # Удаляем карту по индексу (эффективнее для больших списков)
        # logger.debug(f"Взята карта: {chosen_card}. Карт осталось: {len(deck)}")
        return chosen_card, 0
    except IndexError: # На случай, если колода опустела между проверкой и pop
        logger.warning("_draw_card_from_shoe: IndexError при взятии карты (колода опустела?).")
        return None, 0
    except Exception as e:
        logger.error(f"Неожиданная ошибка в _draw_card_from_shoe: {e}", exc_info=True)
        return None, 0

# <<< --- End of Internal Dealing Logic --- >>>


# --- Callback Query Handler ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все нажатия на инлайн-кнопки."""
    query = update.callback_query
    data = query.data
    user = query.from_user

    # Отвечаем на callback как можно раньше, чтобы убрать "часики" (кроме случаев, где нужен alert)
    # Это можно сделать позже в специфических обработчиках, если нужно показать alert
    # await query.answer() # Пока закомментируем, ответы будут в обработчиках

    # Базовая проверка пользователя (на всякий случай)
    user_data = get_or_create_user(user.id)
    if user_data is None:
        try: await query.answer("Ошибка получения данных пользователя.", show_alert=True)
        except BadRequest: pass # Игнорируем ошибки ответа на старые запросы
        return

    logger.debug(f"Callback: data='{data}', user={user.id} ({user.full_name}), chat={query.message.chat_id}, msg={query.message.message_id}")

    # --- Обработка ставок ---
    if data.startswith("bj_bet_"):
        try:
            bet_amount = int(data.split("_")[2])
            # Вызываем обработчик ставки, он сам ответит на query
            await handle_blackjack_bet(update, context, bet_amount)
        except (ValueError, IndexError) as e:
            logger.error(f"Некорректные данные callback'а ставки: {data} - {e}")
            try: await query.answer("Ошибка в данных ставки.", show_alert=True)
            except BadRequest: pass
        except Exception as e:
            logger.error(f"Ошибка обработки callback'а ставки {data}: {e}", exc_info=True)
            try: await query.answer("Произошла ошибка при обработке ставки.", show_alert=True)
            except BadRequest: pass

    # --- Обработка кнопки "Новая игра" ---
    elif data == "bj_action_new_game":
         try:
             # Запускаем процесс начала новой игры
             # blackjack_start сам удалит старое сообщение (если нужно) и отправит новое
             # Он также сам ответит на query внутри себя
             await blackjack_start(update, context)
         except Exception as e:
             logger.error(f"Ошибка запуска новой игры с кнопки: {e}", exc_info=True)
             # Пытаемся ответить пользователю, если что-то пошло не так
             try: await query.answer("Ошибка запуска новой игры.", show_alert=True)
             except BadRequest: pass

    # --- Обработка действий в игре (Hit, Stand, Double, Split) ---
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
                logger.warning(f"Неверный индекс руки в данных действия: {data}")
                try: await query.answer("Неверный индекс руки.", show_alert=True)
                except BadRequest: pass
            except IndexError:
                 logger.warning(f"Неверный формат данных действия (IndexError): {data}")
                 try: await query.answer("Неверный формат действия.", show_alert=True)
                 except BadRequest: pass
            except Exception as e:
                 logger.error(f"Ошибка обработки callback'а действия ({data}): {e}", exc_info=True)
                 try: await query.answer("Ошибка обработки действия.", show_alert=True)
                 except BadRequest: pass
        else:
             logger.warning(f"Неверный формат данных действия (количество частей != 4): {data}")
             try: await query.answer("Неверный формат данных действия.", show_alert=True)
             except BadRequest: pass

    # --- Обработка неизвестных callback'ов ---
    else:
        logger.warning(f"Получены неизвестные данные callback'а: {data} от пользователя {user.id}")
        try:
            # Просто отвечаем, чтобы убрать "часики" на кнопке
            await query.answer()
        except BadRequest: pass # Игнорируем ошибки ответа на старые запросы


# --- Main Function ---
def main():
    """Запускает бота."""
    logger.info("Инициализация и запуск бота...")
    keep_alive() # Запускаем веб-сервер для Render/платформы

    # --- Настройка приложения PTB ---
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True) # Разрешаем обработку нескольких апдейтов одновременно
        # .connection_pool_size(10) # Можно увеличить пул соединений при необходимости
        # .read_timeout(30) # Увеличить таймаут чтения (если нужно)
        # .write_timeout(30) # Увеличить таймаут записи (если нужно)
        .build()
    )

    # --- Инициализация хранилищ в bot_data ---
    # Используем setdefault для потокобезопасной инициализации
    application.bot_data.setdefault('games', {})
    application.bot_data.setdefault('user_cache', {})
    logger.info("Хранилища 'games' и 'user_cache' инициализированы в bot_data.")

    # --- Регистрация обработчиков ---
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))

    # Обработчик для всех инлайн-кнопок
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # Можно добавить обработчик ошибок для логирования необработанных исключений
    # application.add_error_handler(error_handler_callback)

    logger.info("Обработчики команд и callback'ов зарегистрированы.")
    print("Бот запускается... Нажмите Ctrl+C для остановки.")

    # --- Запуск бота ---
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске или работе бота: {e}", exc_info=True)
    finally:
        print("Бот остановлен.")
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    print("Запуск скрипта blackjack_bot.py...")
    # init_db_manual() # Раскомментируйте для вывода SQL инициализации БД
    main()