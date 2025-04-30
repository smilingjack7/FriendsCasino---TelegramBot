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
from telegram.error import BadRequest, Conflict # Добавили Conflict
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
  # Уменьшаем логи werkzeug
  log = logging.getLogger('werkzeug')
  log.setLevel(logging.WARNING)
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
DEALER_TURN_DELAY = 0.3 # Немного увеличим задержку для наглядности
LEADERBOARD_LIMIT = 10
# RESHUFFLE_MESSAGE = "..." # Убрано

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger('werkzeug').setLevel(logging.WARNING) # Уменьшаем логи Flask/Werkzeug - уже сделано в run_web_server
logging.getLogger("telegram.ext").setLevel(logging.INFO) # Можно поставить WARNING для уменьшения логов библиотеки
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
                    logger.info(f"Баланс пользователя {user_id} изменен на {amount_change:+.2f}. Новый баланс: {new_balance:.2f}")
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
    # --- ИЗМЕНЕНИЕ: Экранируем дефисы для MarkdownV2 ---
    help_text = (
        "ℹ️ *Список доступных команд:*\n\n"
        "/start \\- Приветствие и проверка баланса\n"
        "/blackjack \\- Начать новую игру в Блекджек\n"
        "/balance \\- Показать ваш текущий баланс\n"
        f"/bonus \\- Получить ежедневный бонус ({BONUS_AMOUNT} фишек, раз в {BONUS_COOLDOWN_HOURS} ч)\n"
        "/leaderboard \\- Показать таблицу лидеров\n"
        "/help \\- Показать это сообщение помощи"
    )
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    # Используем MarkdownV2 для форматирования
    try:
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Ошибка отправки /help с MarkdownV2: {e}")
        # Фоллбэк на обычный текст, если Markdown не сработал
        plain_text = help_text.replace("\\-","-").replace("*","").replace("_","").replace("ℹ️","") # Убираем форматирование
        try:
            await update.message.reply_text(plain_text)
        except Exception as fe:
             logger.error(f"Не удалось отправить /help даже как обычный текст: {fe}")

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
    # ВАЖНО: Символ `\` сам должен быть экранирован последним!
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    # Экранируем все символы, кроме `\`
    temp_text = ''.join(f'\\{char}' if char in escape_chars else char for char in str(text))
    # Экранируем `\` отдельно
    # return temp_text.replace('\\', '\\\\') # Не нужно, PTB делает это сама? Проверим. Нет, нужно.
    # Нет, PTB не экранирует \ сама. Правильно так:
    return ''.join(f'\\{char}' if char in escape_chars + '\\' else char for char in str(text))


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /leaderboard."""
    user_id = update.effective_user.id
    logger.info(f"Команда /leaderboard от пользователя {user_id}")
    leaders = get_leaderboard(LEADERBOARD_LIMIT)

    if not leaders:
        await update.message.reply_text("Таблица лидеров пока пуста или произошла ошибка при загрузке.")
        return

    leaderboard_text = "🏆 *Таблица Лидеров*\n\n" # Используем Markdown V1

    async def get_user_info(user_id_to_fetch):
        """Асинхронно получает информацию о пользователе с кэшированием."""
        try:
            cache = context.bot_data.setdefault('user_cache', {})
            cache_expiry_seconds = 3600 # Кэшировать на 1 час
            now = datetime.datetime.now()

            if user_id_to_fetch in cache and (now - cache[user_id_to_fetch]['timestamp']).total_seconds() < cache_expiry_seconds:
                 return cache[user_id_to_fetch]['user']

            user_chat = await context.bot.get_chat(user_id_to_fetch)
            cache[user_id_to_fetch] = {'user': user_chat, 'timestamp': now}
            return user_chat
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                 logger.warning(f"Failed to get chat info for user {user_id_to_fetch} (Chat not found)")
            else:
                 logger.warning(f"Failed to get chat info for user {user_id_to_fetch} (BadRequest: {e})")
            return None
        except Exception as e:
            logger.warning(f"Failed to get user info for user {user_id_to_fetch}: {e}", exc_info=True)
            return None

    user_info_tasks = [get_user_info(leader['user_id']) for leader in leaders]
    users_info: list[User | None] = await asyncio.gather(*user_info_tasks)

    place_emojis = ["🥇", "🥈", "🥉"]

    for i, leader in enumerate(leaders):
        db_user_id = leader['user_id']
        balance = leader['balance']
        user_chat_info: User | None = users_info[i]

        # Формирование имени пользователя (используем Markdown V1 - HTML escape)
        user_name_display = f"User ID: {db_user_id}" # Запасной вариант
        if user_chat_info:
            # Получаем имя и экранируем HTML сущности
            from html import escape as html_escape
            name_to_display = html_escape(user_chat_info.first_name or user_chat_info.full_name or f"User_{db_user_id}")
            # Создаем ссылку на пользователя, если есть username
            if user_chat_info.username:
                user_name_display = f"@{html_escape(user_chat_info.username)}" # Не ссылка, а просто @username
                # Или можно сделать реальную ссылку (но менее красиво выглядит)
                # user_name_display = f'<a href="tg://user?id={db_user_id}">{name_to_display}</a>'
            else: # Если нет username, просто имя
                 user_name_display = name_to_display

        place_indicator = place_emojis[i] if i < len(place_emojis) else f"{i+1}."
        balance_str = f"{balance:.2f}" # Баланс не нужно экранировать для V1

        leaderboard_text += f"{place_indicator} {user_name_display} - `{balance_str}` фишек\n" # Используем ` для моноширинного шрифта баланса

    try:
        # Отправляем сообщение с использованием Markdown V1
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        # Ошибки при отправке
        logger.error(f"Error sending leaderboard: {e}", exc_info=True)
        # Попытка отправить как обычный текст
        plain_text = leaderboard_text.replace("*","").replace("`","") # Убираем форматирование
        try:
            await update.message.reply_text(plain_text)
        except Exception as fe:
            logger.error(f"Error sending plain text leaderboard fallback: {fe}")


# --- Blackjack Game Logic Handlers ---

async def blackjack_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает новую игру или предлагает начать, если нет активной."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"Команда /blackjack от пользователя {user.full_name} ({user.id}) в чате {chat_id}")

    reply_func = None
    message_to_delete_id = None # ID сообщения, которое нужно удалить

    if update.callback_query:
        reply_func = update.callback_query.message.reply_text
        message_to_delete_id = update.callback_query.message.message_id
        try: await update.callback_query.answer()
        except BadRequest as e:
            if "query is too old" not in str(e).lower(): logger.warning(f"BJ Start CB Answer Error: {e}")
    elif update.message:
        reply_func = update.message.reply_text
    else:
        logger.warning("blackjack_start вызван без message или callback_query")
        return

    current_balance = get_balance(user.id)
    if current_balance is None:
        await reply_func("Не удалось проверить ваш баланс. Попробуйте /start.")
        return

    context.bot_data.setdefault('games', {})

    if chat_id in context.bot_data['games']:
        game_state = context.bot_data['games'][chat_id]
        old_game_msg_id = game_state.get('message_id')
        if game_state.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Вы не можете начать новую игру, пока текущая не завершена.")
            if old_game_msg_id: await show_game_state(context, chat_id, old_game_msg_id)
            return

        if old_game_msg_id and old_game_msg_id != message_to_delete_id:
             try:
                 await context.bot.delete_message(chat_id, old_game_msg_id)
                 logger.debug(f"Удалено предыдущее игровое сообщение {old_game_msg_id} в чате {chat_id}")
             except BadRequest as e:
                 if "message to delete not found" not in str(e).lower():
                     logger.warning(f"Не удалось удалить старое игровое сообщение {old_game_msg_id}: {e}")
             except Exception as e:
                 logger.error(f"Неожиданная ошибка при удалении старого игрового сообщения {old_game_msg_id}: {e}")
        del context.bot_data['games'][chat_id]
        logger.debug(f"Удалено состояние предыдущей игры для чата {chat_id}")

    if current_balance <= 0:
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) равен нулю. Используйте /bonus.")
        return

    bet_options = [1, 5, 10, 25, 50, 100, 250, 500]
    valid_bets = [b for b in bet_options if b <= current_balance]
    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        await reply_func(f"Ваш баланс ({current_balance:.2f} фишек) меньше мин. ставки ({min_bet}). Используйте /bonus.")
        return

    buttons = []
    row = []
    max_buttons_per_row = 4
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) >= max_buttons_per_row:
            buttons.append(row); row = []
    if row: buttons.append(row)
    markup = InlineKeyboardMarkup(buttons)

    try:
        text_to_send = f"Ваш баланс: {current_balance:.2f} фишек. Сделайте вашу ставку:"
        if message_to_delete_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=message_to_delete_id)
            except Exception as e: logger.warning(f"Не удалось удалить сообщение {message_to_delete_id} с кнопкой 'Новая игра': {e}")
            sent_message = await context.bot.send_message(chat_id=chat_id, text=text_to_send, reply_markup=markup)
        else:
             sent_message = await reply_func(text=text_to_send, reply_markup=markup)

        context.bot_data['games'][chat_id] = {
            'player_id': user.id, 'state': 'waiting_bet', 'message_id': sent_message.message_id
        }
        logger.info(f"Игра инициирована для пользователя {user.id} в чате {chat_id}. Ожидание ставки.")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение с выбором ставки в чат {chat_id}: {e}", exc_info=True)
        try: await reply_func("Произошла ошибка при попытке начать игру. Попробуйте еще раз.")
        except: pass


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    """Обрабатывает выбор ставки игроком и начинает раздачу."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.info(f"Получена ставка {bet_amount} от {user.full_name} ({user.id}) в чате {chat_id}")

    if chat_id not in context.bot_data.get('games', {}):
        await query.answer("Не найдена активная игра. /blackjack", show_alert=True); return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user.id:
        await query.answer("Это не ваша игра.", show_alert=True); return
    if game_state.get('state') != 'waiting_bet':
        await query.answer("Ставка уже сделана.", show_alert=False); return

    current_balance = get_balance(user.id)
    if current_balance is None:
        await query.answer("Ошибка баланса.", show_alert=True); return
    if bet_amount <= 0:
        await query.answer("Ставка > 0.", show_alert=True); return
    if bet_amount > current_balance:
        await query.answer(f"Недостаточно средств ({current_balance:.2f}).", show_alert=True); return

    new_balance = update_balance(user.id, -bet_amount)
    if new_balance is None:
        await query.answer("Ошибка списания ставки.", show_alert=True); return

    deck = create_deck(NUM_DECKS)
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0
    try:
        for _ in range(2): # Раздаем по 2 карты
            card_p, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS); player_hand.append(card_p); cards_dealt_count += 1
            card_d, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS); dealer_hand.append(card_d); cards_dealt_count += 1
        if None in player_hand or None in dealer_hand: raise ValueError("Ошибка раздачи: пустая карта.")
    except Exception as e:
        logger.error(f"Ошибка раздачи: {e}", exc_info=True)
        update_balance(user.id, bet_amount) # Возврат ставки
        await query.edit_message_text(f"Ошибка раздачи ({e}). Ставка возвращена. /blackjack")
        if chat_id in context.bot_data['games']: del context.bot_data['games'][chat_id]
        return

    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    player_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    outcome_text, current_state, player_hand_status = None, 'player_turn', 'active'

    if player_blackjack:
        player_hand_status = 'blackjack'
        if dealer_blackjack:
            outcome_text = f"⚖️ Ничья! Блекджек у обоих. Ставка {bet_amount} F возвращена."
            update_balance(user.id, bet_amount); current_state = 'game_over'
        else:
            win_amount = bet_amount * BLACKJACK_PAYOUT; total_return = bet_amount + win_amount
            update_balance(user.id, total_return)
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {win_amount:.2f} F!"
            current_state = 'game_over'
    elif dealer_blackjack:
        outcome_text = f"😥 У дилера Блекджек! Ставка {bet_amount} F проиграна."
        current_state = 'game_over'

    game_state.update({
        'state': current_state, 'deck': deck,
        'player_hands': [{'hand': player_hand, 'bet': bet_amount, 'status': player_hand_status, 'can_double': (not player_blackjack and not dealer_blackjack), 'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': dealer_hand, 'cards_dealt': cards_dealt_count,
        'initial_bet': bet_amount, 'split_count': 0, 'outcome_text': outcome_text,
    })

    await show_game_state(context, chat_id, query.message.message_id)
    try: await query.answer(f"Ставка {bet_amount} F принята!")
    except BadRequest as e:
        if "query is too old" not in str(e).lower(): logger.warning(f"Bet CB Answer Error: {e}")


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id_to_edit: int):
    """Отображает текущее состояние игры (руки, ставки, кнопки действий)."""
    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"show_game_state: игра {chat_id} не найдена.")
        return
    game_state = context.bot_data['games'][chat_id]
    player_id = game_state.get('player_id')
    if not player_id:
        logger.error(f"show_game_state: нет player_id для {chat_id}.")
        return

    current_balance = get_balance(player_id)
    balance_str = f"{current_balance:.2f}" if current_balance is not None else "Ошибка"
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands = game_state.get('player_hands', [])
    current_hand_index = game_state.get('current_hand_index', -1)
    game_status = game_state.get('state', 'unknown')

    hide_dealer_card = (game_status == 'player_turn') and not (get_hand_value(dealer_hand) == 21 and len(dealer_hand) == 2)

    # --- Текст сообщения (Markdown V1) ---
    text = f"*Блекджек* | Баланс: {balance_str} F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands if isinstance(h, dict))
    num_hands = len(player_hands)
    text += f"Общая ставка: {total_bet} F{' ({num_hands} руки)'* (num_hands > 1)}\n"
    text += "--------------------\n"

    # Рука дилера
    dealer_value = get_hand_value(dealer_hand)
    dealer_value_str = "???"
    if dealer_hand:
        if not hide_dealer_card: dealer_value_str = str(dealer_value)
        elif dealer_hand[0]: dealer_value_str = str(get_card_value(dealer_hand[0])) + "+?"
    dealer_hand_str = format_hand(dealer_hand, hide_one=hide_dealer_card)
    text += f"*Дилер:* {dealer_hand_str} ({dealer_value_str})\n\n"

    # Руки игрока
    text += "*Вы:*\n"
    active_hand_data = None
    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', 'unknown')
        hand_bet = hand_data.get('bet', 0)
        is_current_hand = (i == current_hand_index and hand_status == 'active' and game_status == 'player_turn')
        indicator = "▶️" if is_current_hand else ("✅" if hand_status == 'stand' else ("❌" if hand_status == 'bust' else ("💰" if hand_status == 'blackjack' else "▫️")))
        text += f"{indicator} Рука {i+1}: {format_hand(hand)} ({hand_value}) [{hand_bet} F]"
        if hand_status == 'bust': text += " - *Перебор!*"
        elif hand_status == 'blackjack': text += " - *Блекджек!*"
        elif hand_status == 'stand' and not is_current_hand: text += " - *Стоп*"
        text += "\n"
        if is_current_hand: active_hand_data = hand_data

    # --- Кнопки ---
    keyboard = []
    if active_hand_data and game_status == 'player_turn':
        current_hand = active_hand_data.get('hand', [])
        current_bet = active_hand_data.get('bet', 0)
        can_double = (active_hand_data.get('can_double', False) and len(current_hand) == 2 and current_balance is not None and current_balance >= current_bet)
        can_split = (len(current_hand) == 2 and current_hand[0] and current_hand[1] and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) and current_balance is not None and current_balance >= current_bet and game_state.get('split_count', 0) < MAX_SPLITS)
        active_hand_data['can_split'] = can_split # Сохраняем для handle_action

        keyboard.append([InlineKeyboardButton("Еще", callback_data=f"bj_action_hit_{current_hand_index}"), InlineKeyboardButton("Хватит", callback_data=f"bj_action_stand_{current_hand_index}")])
        special_buttons = []
        if can_double: special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_action_double_{current_hand_index}"))
        if can_split: special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_action_split_{current_hand_index}"))
        if special_buttons: keyboard.append(special_buttons)

    elif game_status == 'game_over':
        text += "\n*Игра завершена!* 🎉\n" + game_state.get('outcome_text', "") + "\n"
        final_balance = get_balance(player_id)
        text += f"\nИтоговый баланс: {final_balance:.2f} фишек." if final_balance is not None else ""
        keyboard.append([InlineKeyboardButton("🔄 Новая Игра", callback_data="bj_action_new_game")])

    elif game_status == 'dealer_turn':
        text += "\n*Ход дилера...*"

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Редактирование сообщения ---
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id_to_edit, text=text,
            reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            if "message to edit not found" in str(e).lower():
                 logger.warning(f"Сообщение {message_id_to_edit} для ред. не найдено {chat_id}.")
                 if game_status != 'game_over' and chat_id in context.bot_data.get('games', {}):
                     try:
                         new_msg = await context.bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                         context.bot_data['games'][chat_id]['message_id'] = new_msg.message_id
                         logger.info(f"Отправлено новое сообщение {new_msg.message_id}.")
                     except Exception as send_e: logger.error(f"Не удалось отправить новое сообщение: {send_e}")
            else:
                 logger.error(f"Ошибка ред. сообщения {message_id_to_edit} (BadRequest): {e}")
                 try: # Фоллбэк на текст без Markdown
                     await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id_to_edit, text=text.replace("*","").replace("_","").replace("`",""), reply_markup=reply_markup)
                 except Exception as fallback_e: logger.error(f"Фоллбэк ред. без Markdown не удался: {fallback_e}")
    except Exception as e:
        logger.error(f"Ошибка show_game_state для {chat_id}: {e}", exc_info=True)


async def handle_blackjack_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index: int):
    """Обрабатывает действия игрока (Hit, Stand, Double, Split)."""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    logger.info(f"Действие '{action}' рука {hand_index} от {user.id} в {chat_id}")

    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('player_id') != user.id: return
    if game_state.get('state') != 'player_turn': return
    player_hands = game_state.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)): return
    if hand_index != game_state.get('current_hand_index'):
        await query.answer("Сейчас ход другой руки.", show_alert=False); return

    hand_data = player_hands[hand_index]
    if not isinstance(hand_data, dict) or hand_data.get('status') != 'active':
        await query.answer("Действие для этой руки уже выполнено.", show_alert=False); return

    current_hand = hand_data.get('hand', [])
    deck = game_state.get('deck', [])
    current_balance = get_balance(user.id)
    current_bet = hand_data.get('bet', 0)
    cards_dealt_count = game_state.get('cards_dealt', 0)

    if action in ['double', 'split'] and current_balance is None:
        await query.answer("Ошибка получения баланса.", show_alert=True); return

    should_update_state = False
    try:
        if action == 'hit':
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                current_hand.append(card); game_state['cards_dealt'] += 1
                hand_data['can_double'] = False; hand_data['can_split'] = False
                new_value = get_hand_value(current_hand)
                await query.answer(f"Ваша карта: {card[0]}{card[1]}")
                if new_value > 21:
                    hand_data['status'] = 'bust'; logger.info(f"Рука {hand_index} игрока {user.id} - Перебор ({new_value})")
                    await next_player_action_or_dealer(context, chat_id)
                elif new_value == 21:
                    hand_data['status'] = 'stand'; logger.info(f"Рука {hand_index} игрока {user.id} - 21, стоп.")
                    await next_player_action_or_dealer(context, chat_id)
                else: should_update_state = True
            else:
                logger.warning(f"Hit не удался для {user.id} в {chat_id} - колода пуста?")
                hand_data['status'] = 'stand'
                await query.answer("Не удалось взять карту (колода?).", show_alert=True)
                await next_player_action_or_dealer(context, chat_id)

        elif action == 'stand':
            hand_data['status'] = 'stand'
            await query.answer("Стоп.")
            await next_player_action_or_dealer(context, chat_id)

        elif action == 'double':
            can_double = (hand_data.get('can_double', False) and len(current_hand) == 2 and current_balance is not None and current_balance >= current_bet)
            if can_double:
                new_balance = update_balance(user.id, -current_bet)
                if new_balance is not None:
                    hand_data['bet'] += current_bet; hand_data['can_double'] = False; hand_data['can_split'] = False
                    card, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                    if card:
                        current_hand.append(card); game_state['cards_dealt'] += 1
                        new_value = get_hand_value(current_hand)
                        hand_data['status'] = 'bust' if new_value > 21 else 'stand'
                        await query.answer(f"Удвоено! Карта: {card[0]}{card[1]}. Итог: {new_value}{' (Перебор!)'*(new_value > 21)}")
                        await next_player_action_or_dealer(context, chat_id)
                    else:
                        logger.warning(f"Double не удался для {user.id} в {chat_id} - колода пуста?")
                        hand_data['status'] = 'stand'
                        await query.answer("Удвоено! Не удалось взять карту (колода?).", show_alert=True)
                        await next_player_action_or_dealer(context, chat_id)
                else: await query.answer("Ошибка баланса при удвоении.", show_alert=True)
            else: await query.answer("Удвоение невозможно.", show_alert=True)

        elif action == 'split':
             can_split = hand_data.get('can_split', False)
             if can_split:
                 if current_balance is None or current_balance < current_bet:
                      await query.answer("Недостаточно средств.", show_alert=True); return
                 new_balance = update_balance(user.id, -current_bet)
                 if new_balance is not None:
                     game_state['split_count'] += 1
                     card_to_move = current_hand.pop()
                     new_hand_data = {'hand': [card_to_move], 'bet': current_bet, 'status': 'active', 'can_double': False, 'can_split': False}
                     player_hands.insert(hand_index + 1, new_hand_data)
                     logger.info(f"Рука {hand_index} разделена игроком {user.id}. Новая рука {hand_index + 1}.")

                     card1, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card1: current_hand.append(card1); game_state['cards_dealt'] += 1
                     else: logger.warning(f"Сплит карта 1 не взята {chat_id}")

                     card2, _ = _draw_card_from_shoe(deck, game_state['cards_dealt'], NUM_DECKS)
                     if card2: new_hand_data['hand'].append(card2); game_state['cards_dealt'] += 1
                     else: logger.warning(f"Сплит карта 2 не взята {chat_id}")

                     is_ace_split = get_card_value(current_hand[0]) == 11
                     if is_ace_split:
                         hand_data['status'] = 'stand'; new_hand_data['status'] = 'stand'
                         hand_data['can_double'] = False; new_hand_data['can_double'] = False
                         await query.answer("Тузы разделены. Ход завершен.")
                         await next_player_action_or_dealer(context, chat_id)
                     else:
                         hand_data['can_double'] = (len(current_hand) == 2)
                         new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2)
                         hand_data['can_split'] = (len(current_hand) == 2 and current_hand[0] and current_hand[1] and get_card_value(current_hand[0]) == get_card_value(current_hand[1]) and game_state['split_count'] < MAX_SPLITS)
                         new_hand_data['can_split'] = (len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and game_state['split_count'] < MAX_SPLITS)
                         if get_hand_value(current_hand) == 21: hand_data['status'] = 'stand'
                         if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'
                         await query.answer("Рука разделена!")
                         should_update_state = True
                 else: await query.answer("Ошибка баланса при разделении.", show_alert=True)
             else: await query.answer("Разделение невозможно.", show_alert=True)

        if should_update_state:
            await show_game_state(context, chat_id, query.message.message_id)

    except IndexError:
         logger.warning(f"Действие '{action}' не удалось {user.id} в {chat_id} - IndexError (колода?).")
         hand_data['status'] = 'stand'
         await query.answer(f"Не удалось '{action}', колода пуста! Рука остается.", show_alert=True)
         await next_player_action_or_dealer(context, chat_id)
    except Exception as e:
         logger.error(f"Ошибка handle_action '{action}' {user.id} {chat_id}: {e}", exc_info=True)
         try: await query.answer("Ошибка обработки хода.", show_alert=True)
         except: pass


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Переключает на следующую активную руку игрока или начинает ход дилера."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('state') != 'player_turn': return

    player_hands = game_state.get('player_hands', [])
    current_index = game_state.get('current_hand_index', -1)
    next_active_index = -1
    for i in range(current_index + 1, len(player_hands)):
        if isinstance(player_hands[i], dict) and player_hands[i].get('status') == 'active':
            next_active_index = i; break

    if next_active_index != -1:
        game_state['current_hand_index'] = next_active_index
        logger.info(f"Переход к руке {next_active_index} для {game_state.get('player_id')} в {chat_id}")
        await show_game_state(context, chat_id, game_state.get('message_id'))
    else:
        game_state['state'] = 'dealer_turn'
        logger.info(f"Игрок {game_state.get('player_id')} завершил ход в {chat_id}. Ход дилера.")
        await show_game_state(context, chat_id, game_state.get('message_id'))
        context.job_queue.run_once(
            dealer_turn_job, when=DEALER_TURN_DELAY, chat_id=chat_id,
            data=chat_id, name=f"dealer_turn_{chat_id}"
        )


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    """Выполняет ход дилера (берет карты до 17 или Soft 17)."""
    chat_id = context.job.data
    logger.info(f"Запущен job dealer_turn для {chat_id}")

    if chat_id not in context.bot_data.get('games', {}):
        logger.warning(f"Job хода дилера {chat_id}: игра не найдена."); return
    game_state = context.bot_data['games'][chat_id]
    if game_state.get('state') != 'dealer_turn':
        logger.warning(f"Job хода дилера {chat_id}: состояние {game_state.get('state')} != 'dealer_turn'."); return

    deck = game_state.get('deck', [])
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands = game_state.get('player_hands', [])
    cards_dealt_count = game_state.get('cards_dealt', 0)

    player_can_win_or_push = any(isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] for h in player_hands)
    dealer_value_initial = get_hand_value(dealer_hand)
    dealer_had_blackjack_on_deal = (dealer_value_initial == 21 and len(dealer_hand) == 2)

    if not player_can_win_or_push and not dealer_had_blackjack_on_deal:
        logger.info(f"Ход дилера пропущен в {chat_id} (все руки игрока проиграли/БЖ).")
        await determine_outcome(context, chat_id, dealer_had_blackjack_on_deal); return

    dealer_hit_count = 0
    while True:
        dealer_value = get_hand_value(dealer_hand)
        ace_count = sum(1 for card in dealer_hand if card and card[0] == 'A')
        is_soft = ace_count > 0 and (dealer_value - ace_count * 11) < 11
        should_hit = (dealer_value < 17) or (dealer_value == 17 and is_soft and DEALER_HITS_SOFT_17)
        if not should_hit:
            logger.info(f"Дилер стоп на {dealer_value} ({format_hand(dealer_hand)}) в {chat_id}."); break

        dealer_hit_count += 1
        logger.debug(f"Дилер hit {dealer_hit_count} ({dealer_value}) в {chat_id}")
        try:
            card, _ = _draw_card_from_shoe(deck, cards_dealt_count, NUM_DECKS)
            if card:
                dealer_hand.append(card); game_state['cards_dealt'] += 1; cards_dealt_count = game_state['cards_dealt']
                logger.debug(f"Дилер взял {card[0]}{card[1]}. Рука: {format_hand(dealer_hand)}")
                # Опциональная задержка/обновление для анимации
                # await show_game_state(context, chat_id, game_state.get('message_id'))
                # await asyncio.sleep(DEALER_TURN_DELAY * 1.5)
            else: logger.warning(f"Ход дилера прерван {chat_id} - карта не взята."); break
        except IndexError: logger.warning(f"Ход дилера прерван {chat_id} - IndexError."); break

    await determine_outcome(context, chat_id, dealer_had_blackjack_on_deal)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack_on_deal: bool):
    """Определяет результат игры для каждой руки игрока и обновляет баланс."""
    if chat_id not in context.bot_data.get('games', {}): return
    game_state = context.bot_data['games'][chat_id]
    logger.info(f"Определение исхода игры в {chat_id}")

    if game_state.get('state') == 'game_over' and 'outcome_determined' in game_state:
        logger.debug(f"Исход для {chat_id} уже определен."); await show_game_state(context, chat_id, game_state.get('message_id')); return

    player_id = game_state.get('player_id'); player_hands = game_state.get('player_hands', []); dealer_hand = game_state.get('dealer_hand', [])
    if not player_id: logger.error(f"Нет player_id при определении исхода {chat_id}"); return

    dealer_value = get_hand_value(dealer_hand); dealer_is_bust = dealer_value > 21
    dealer_hand_final_str = format_hand(dealer_hand)
    logger.info(f"Дилер {chat_id}: {dealer_hand_final_str} ({dealer_value}), Перебор: {dealer_is_bust}, Был БЖ: {dealer_had_blackjack_on_deal}")

    outcomes = []; total_winnings = 0; total_bet = 0

    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', []); bet = hand_data.get('bet', 0); status = hand_data.get('status'); player_value = get_hand_value(hand)
        player_hand_str = format_hand(hand); player_had_blackjack_this_hand = (status == 'blackjack')
        total_bet += bet; payout_multiplier = 0; outcome_str = ""; hand_prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""
        logger.debug(f"Рука {i}: Ст={status}, Карты={player_hand_str}, Очки={player_value}, Ставка={bet}")

        if status == 'bust': payout_multiplier = 0; outcome_str = f"{hand_prefix}Перебор ({player_value}). Ставка {bet} F проиграна."
        elif player_had_blackjack_this_hand:
             if dealer_had_blackjack_on_deal: payout_multiplier = 1; outcome_str = f"{hand_prefix}Блекджек! Но у дилера тоже. Ничья, ставка {bet} F возвращена."
             else: payout_multiplier = 1 + BLACKJACK_PAYOUT; win_amount = bet * BLACKJACK_PAYOUT; outcome_str = f"{hand_prefix}Блекджек! Выигрыш {win_amount:.2f} F."
        elif dealer_had_blackjack_on_deal: payout_multiplier = 0; outcome_str = f"{hand_prefix}У дилера Блекджек. Ставка {bet} F проиграна."
        elif dealer_is_bust: payout_multiplier = 2; outcome_str = f"{hand_prefix}У дилера перебор ({dealer_value})! Выигрыш {bet} F."
        elif player_value > dealer_value: payout_multiplier = 2; outcome_str = f"{hand_prefix}{player_value} > {dealer_value}. Выигрыш {bet} F."
        elif player_value == dealer_value: payout_multiplier = 1; outcome_str = f"{hand_prefix}{player_value} = {dealer_value}. Ничья, ставка {bet} F возвращена."
        else: payout_multiplier = 0; outcome_str = f"{hand_prefix}{player_value} < {dealer_value}. Ставка {bet} F проиграна."

        outcomes.append(outcome_str); total_winnings += bet * payout_multiplier
        logger.debug(f"Результат руки {i}: {outcome_str}, Множитель={payout_multiplier}")

    net_change = total_winnings - total_bet
    logger.info(f"Итог {player_id}: Ставки={total_bet}, Выигрыш/Возврат={total_winnings}, Изменение={net_change:+.2f}")
    if total_winnings > 0:
        final_balance = update_balance(player_id, total_winnings)
        if final_balance is None:
             logger.error(f"КРИТ. ОШИБКА: update_balance не удался для {player_id} в {chat_id}.")
             outcomes.append("\n*ОШИБКА:* Не удалось начислить выигрыш!")
             net_change = 0 # Считаем, что не изменился

    game_state['state'] = 'game_over'; game_state['outcome_text'] = "\n".join(outcomes) + f"\n\n*Общий итог раунда: {net_change:+.2f} F*"
    game_state['outcome_determined'] = True
    await show_game_state(context, chat_id, game_state.get('message_id'))


# --- <<< Internal Dealing Logic >>> ---

def _draw_card_from_shoe(deck: list, cards_dealt: int, num_decks: int):
    """Берет случайную карту из колоды и удаляет ее."""
    if not deck: logger.warning("_draw_card: пустая колода."); return None, 0
    try:
        chosen_card_index = random.randrange(len(deck))
        chosen_card = deck.pop(chosen_card_index)
        return chosen_card, 0
    except IndexError: logger.warning("_draw_card: IndexError."); return None, 0
    except Exception as e: logger.error(f"Ошибка _draw_card: {e}", exc_info=True); return None, 0

# <<< --- End of Internal Dealing Logic --- >>>


# --- Callback Query Handler ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все нажатия на инлайн-кнопки."""
    query = update.callback_query; data = query.data; user = query.from_user
    user_data = get_or_create_user(user.id)
    if user_data is None:
        try: await query.answer("Ошибка данных пользователя.", show_alert=True); return
        except BadRequest: pass

    logger.debug(f"Callback: data='{data}', user={user.id}, chat={query.message.chat_id}")

    if data.startswith("bj_bet_"):
        try: bet_amount = int(data.split("_")[2]); await handle_blackjack_bet(update, context, bet_amount)
        except (ValueError, IndexError) as e: logger.error(f"Callback ставки: {data} - {e}"); await query.answer("Ошибка ставки.", show_alert=True)
        except Exception as e: logger.error(f"Ошибка callback ставки {data}: {e}", exc_info=True); await query.answer("Ошибка обработки ставки.", show_alert=True)

    elif data == "bj_action_new_game":
         try: await blackjack_start(update, context)
         except Exception as e: logger.error(f"Ошибка новой игры с кнопки: {e}", exc_info=True); await query.answer("Ошибка запуска.", show_alert=True)

    elif data.startswith("bj_action_"):
        parts = data.split("_")
        if len(parts) == 4:
            try: action_type, hand_index = parts[2], int(parts[3]); await handle_blackjack_action(update, context, action_type, hand_index)
            except ValueError: logger.warning(f"Неверный индекс руки: {data}"); await query.answer("Неверный индекс.", show_alert=True)
            except IndexError: logger.warning(f"Неверный формат действия: {data}"); await query.answer("Неверный формат.", show_alert=True)
            except Exception as e: logger.error(f"Ошибка callback действия ({data}): {e}", exc_info=True); await query.answer("Ошибка действия.", show_alert=True)
        else: logger.warning(f"Неверный формат данных действия: {data}"); await query.answer("Неверный формат.", show_alert=True)

    else: logger.warning(f"Неизвестный callback: {data} от {user.id}"); await query.answer()


# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и отправляет сообщение пользователю при необходимости."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Обработка конфликта getUpdates (запущено несколько инстансов)
    if isinstance(context.error, Conflict):
        logger.critical("Обнаружен конфликт getUpdates! Убедитесь, что запущена только одна копия бота с этим токеном.")
        # Здесь можно попытаться остановить текущий процесс, если он лишний, но это сложно надежно реализовать.
        # Лучше решить проблему на уровне деплоя/запуска.
        return # Не спамим пользователю об этой ошибке

    # Другие ошибки BadRequest часто связаны с форматированием или невалидными запросами
    # Можно добавить более детальную обработку, если нужно
    if isinstance(context.error, BadRequest):
         logger.warning(f"BadRequest Error: {context.error}. Update: {update}")
         # Можно попытаться извлечь chat_id и отправить сообщение, но update может быть None
         # if update and hasattr(update, 'effective_chat') and update.effective_chat:
         #    try: await context.bot.send_message(update.effective_chat.id, "Произошла ошибка обработки запроса.")
         #    except: pass # Игнорируем ошибки отправки сообщения об ошибке
         return

    # Для других ошибок можно добавить отправку сообщения пользователю, если это имеет смысл
    # logger.exception(f"Unhandled error: {context.error}") # Логируем с полным traceback


# --- Main Function ---
def main():
    """Запускает бота."""
    logger.info("Инициализация и запуск бота...")
    keep_alive() # Запускаем веб-сервер

    application = ( Application.builder().token(BOT_TOKEN).concurrent_updates(True).build() )
    application.bot_data.setdefault('games', {}); application.bot_data.setdefault('user_cache', {})
    logger.info("Хранилища 'games' и 'user_cache' инициализированы.")

    # --- Регистрация обработчиков ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bonus", bonus))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("blackjack", blackjack_start))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    # Регистрируем обработчик ошибок
    application.add_error_handler(error_handler)
    logger.info("Обработчики команд, callback'ов и ошибок зарегистрированы.")

    print("Бот запускается... Нажмите Ctrl+C для остановки.")
    try:
        # Запускаем бота
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True) # drop_pending_updates может помочь с конфликтами при перезапуске
    except Conflict as e:
         logger.critical(f"Критическая ошибка Conflict при запуске polling: {e}. Убедитесь, что не запущена другая копия бота!")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске или работе бота: {e}", exc_info=True)
    finally:
        print("Бот остановлен.")
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    print(f"Запуск скрипта {os.path.basename(__file__)}...")
    # init_db_manual() # Раскомментируйте для вывода SQL инициализации БД
    main()