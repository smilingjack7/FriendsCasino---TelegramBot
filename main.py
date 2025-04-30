import logging
import os
import random
import datetime
import time
import asyncio # Добавлен импорт asyncio
from collections import defaultdict, deque
from threading import Thread # <<< Для keep_alive
from flask import Flask # <<< Для keep_alive
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
import math
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

# --- Keep Alive Web Server ---
# (Запускает простой веб-сервер в отдельном потоке)
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!" # Простое сообщение для проверки

def run_web_server():
  # Render предоставляет порт в переменной PORT
  port = int(os.environ.get("PORT", 8080)) # Используем порт от Render или 8080 по умолчанию
  app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web_server, daemon=True) # daemon=True позволяет потоку завершиться с основной программой
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
logger = logging.getLogger(__name__)

# --- Standard Card Definitions ---
SUITS = ["♠", "♥", "♦", "♣"]; RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11}

# --- Database Functions (PostgreSQL) ---

def get_db_conn():
    """Устанавливает соединение с БД PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        raise

# def init_db(): # Оставляем закомментированным, лучше выполнить вручную
#     sql = """ CREATE TABLE IF NOT EXISTS users (...) """
#     try: ...
#     except Exception as e: logger.error(f"Ошибка инициализации БД: {e}")

def get_or_create_user(user_id: int):
    """Получает данные пользователя или создает нового с начальным балансом."""
    select_sql = "SELECT * FROM users WHERE user_id = %s;"
    insert_sql = """INSERT INTO users (user_id, balance, last_bonus) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING;"""
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(select_sql, (user_id,))
                user_data = cursor.fetchone()
                if user_data is None:
                    cursor.execute(insert_sql, (user_id, INITIAL_BALANCE, None))
                    # conn.commit() не нужен если with get_db_conn() обрабатывает транзакцию
                    logger.info(f"Создан новый пользователь в БД: {user_id}")
                    cursor.execute(select_sql, (user_id,)) # Повторный запрос
                    user_data = cursor.fetchone()
                    if not user_data: return {'user_id': user_id, 'balance': INITIAL_BALANCE, 'last_bonus': None}
        return user_data
    except Exception as e:
        logger.error(f"Ошибка get_or_create_user для {user_id}: {e}")
        return None

def update_balance(user_id: int, amount_change: float):
    """Обновляет баланс пользователя на указанную величину."""
    sql_update = "UPDATE users SET balance = balance + %s WHERE user_id = %s RETURNING balance;" # Возвращаем новый баланс
    new_balance = None
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql_update, (amount_change, user_id))
                result = cursor.fetchone()
                if result:
                    new_balance = result[0]
        return new_balance
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
     except Exception as e:
         logger.error(f"Ошибка update_last_bonus_time для {user_id}: {e}")

def get_last_bonus_time(user_id: int) -> datetime.datetime | None:
    """Получает время последнего бонуса пользователя."""
    user_data = get_or_create_user(user_id)
    return user_data.get('last_bonus') if user_data else None

def get_leaderboard(limit: int = LEADERBOARD_LIMIT):
    """Получает топ пользователей по балансу."""
    sql = "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT %s;"
    leaders = []
    try:
        with get_db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, (limit,))
                leaders = cursor.fetchall()
        return leaders
    except Exception as e:
        logger.error(f"Ошибка get_leaderboard: {e}")
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
    return RANK_VALUES[rank]

# --- ИСПРАВЛЕННАЯ ВЕРСИЯ get_hand_value ---
def get_hand_value(hand):
    """Calculates the value of a hand, handling Aces correctly."""
    value = 0
    ace_count = 0
    for card in hand:
        rank = card[0]
        value += get_card_value(card)
        if rank == 'A':
            ace_count += 1

    # Adjust for Aces if busting
    while value > 21 and ace_count > 0:
        value -= 10
        ace_count -= 1
    return value
# --- КОНЕЦ ИСПРАВЛЕННОЙ ВЕРСИИ ---

def format_hand(hand, hide_one=False):
    """Formats a hand for display."""
    if hide_one and len(hand) > 0:
        return f"[{hand[0][0]}{hand[0][1]}, ??]"
    return ", ".join([f"{card[0]}{card[1]}" for card in hand])

# --- Bot Command Handlers ---
# (start, help_command, balance_command, bonus, leaderboard - используют исправленные функции БД)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_or_create_user(user_id)
    balance = user_data['balance'] if user_data else "Ошибка"
    balance_str = f"{balance:.2f}" if isinstance(balance, (int, float)) else "??"
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
    if current_balance is not None:
        await update.message.reply_text(f"Ваш баланс: {current_balance:.2f} фишек.")
    else:
        await update.message.reply_text("Не удалось получить ваш баланс.")

async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Убедимся, что пользователь существует
    if get_balance(user_id) is None:
       await update.message.reply_text("Произошла ошибка. Попробуйте /start сначала."); return

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    last_bonus_time = get_last_bonus_time(user_id)
    cooldown = datetime.timedelta(hours=BONUS_COOLDOWN_HOURS)

    if last_bonus_time and now - last_bonus_time < cooldown:
        time_left = last_bonus_time + cooldown - now
        hours, remainder = divmod(time_left.total_seconds(), 3600)
        minutes, _ = divmod(remainder, 60)
        await update.message.reply_text(f"Бонус уже получен. Попробуйте через {int(hours)} ч {int(minutes)} мин.")
    else:
        new_balance = update_balance(user_id, BONUS_AMOUNT)
        if new_balance is not None:
            update_last_bonus_time(user_id, now)
            await update.message.reply_text(f"✅ Бонус {BONUS_AMOUNT} фишек получен! Новый баланс: {new_balance:.2f} фишек.")
        else:
             await update.message.reply_text("Не удалось начислить бонус. Попробуйте позже.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    leaders = get_leaderboard(LEADERBOARD_LIMIT)
    if not leaders:
        await update.message.reply_text("Пока нет данных для таблицы лидеров."); return

    leaderboard_text = "🏆 **Таблица Лидеров** 🏆\n\n"

    # Асинхронное получение информации о пользователях
    async def get_user_info(user_id):
        try:
            return await context.bot.get_chat(user_id)
        except Exception as e:
            logger.warning(f"Failed to get info for user {user_id} in leaderboard: {e}")
            return None

    user_info_tasks = [get_user_info(l['user_id']) for l in leaders]
    users_info = await asyncio.gather(*user_info_tasks)

    for i, leader in enumerate(leaders):
        user_id = leader['user_id']
        balance = leader['balance']
        user: User | None = users_info[i] # Результат из gather

        user_name = f"ID: {user_id}" # Имя по умолчанию
        if user:
            # Экранирование для MarkdownV2
            name = user.full_name
            for char in ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
                name = name.replace(char, f'\\{char}')
            user_name = user.mention_markdown_v2(name) if user.username else name
        else:
            logger.warning(f"Using default name for user_id {user_id} in leaderboard")

        leaderboard_text += f"{i+1}\\. {user_name} \\- `{balance:.2f}` F\n" # Экранирование точки и использование ` для баланса

    try:
        await update.message.reply_text(leaderboard_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending leaderboard: {e}")
        # Попытка отправить без форматирования как запасной вариант
        try:
            plain_text = leaderboard_text.replace("\\", "") # Убрать экранирование
            await update.message.reply_text(plain_text)
        except Exception as fallback_e:
             logger.error(f"Error sending plain leaderboard fallback: {fallback_e}")


# --- Blackjack Game Logic Handlers ---
# (Переписаны с нормальным форматированием)

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
    else:
        return # Неизвестный тип

    current_balance = get_balance(user_id)
    if current_balance is None:
        await reply_func("Ошибка получения баланса.")
        return

    # --- Управление состоянием игры ---
    if 'games' not in context.bot_data: context.bot_data['games'] = {}
    if chat_id in context.bot_data['games']:
        old_game = context.bot_data['games'][chat_id]
        if old_game.get('state') not in ['game_over', 'waiting_bet']:
            await reply_func("Игра уже идет в этом чате.")
            return
        # Удаляем старую игру перед началом новой
        if old_game.get('message_id'):
            try: await context.bot.delete_message(chat_id, old_game['message_id'])
            except Exception: pass # Игнорируем ошибки удаления
        del context.bot_data['games'][chat_id]
    # --- Конец управления состоянием ---

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
    msg = await reply_func(f"Баланс: {current_balance:.2f}. Ваша ставка?", reply_markup=markup)

    # Сохраняем состояние игры в памяти
    context.bot_data['games'][chat_id] = {
        'player_id': user_id,
        'state': 'waiting_bet',
        'message_id': msg.message_id
    }


async def handle_blackjack_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet_amount: int):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Проверка состояния игры
    if chat_id not in context.bot_data.get('games', {}) or context.bot_data['games'][chat_id]['player_id'] != user_id:
        await query.answer("Не ваша игра.", show_alert=True)
        return
    game_state = context.bot_data['games'][chat_id]
    if game_state['state'] != 'waiting_bet':
        await query.answer() # Игнорировать повторное нажатие
        return

    # Проверка баланса
    current_balance = get_balance(user_id)
    if current_balance is None or bet_amount > current_balance:
        await query.answer(f"Недостаточно средств ({current_balance if current_balance is not None else '??':.2f}).", show_alert=True)
        return
    if bet_amount <= 0:
        await query.answer("Ставка должна быть > 0.", show_alert=True)
        return

    # Списание ставки
    update_balance(user_id, -bet_amount)

    # --- Раздача карт ---
    deck = create_deck(NUM_DECKS)
    deck_state_index = 0.0
    cards_dealt = 0
    player_hand = []
    dealer_hand = []

    # Используем _draw_card_from_shoe (определена ниже)
    card, adj = _draw_card_from_shoe(deck, deck_state_index, cards_dealt, NUM_DECKS, target="player"); player_hand.append(card); deck_state_index += adj; cards_dealt += 1
    card, adj = _draw_card_from_shoe(deck, deck_state_index, cards_dealt, NUM_DECKS, target="dealer"); dealer_hand.append(card); deck_state_index += adj; cards_dealt += 1
    card, adj = _draw_card_from_shoe(deck, deck_state_index, cards_dealt, NUM_DECKS, target="player"); player_hand.append(card); deck_state_index += adj; cards_dealt += 1
    card, _   = _draw_card_from_shoe(deck, deck_state_index, cards_dealt, NUM_DECKS, target="dealer"); dealer_hand.append(card); cards_dealt += 1

    # --- Проверка на Блекджек ---
    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    dealer_up_value = get_card_value(dealer_hand[0])
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_shows_ace_or_ten = (dealer_up_value == 11 or dealer_up_value == 10)
    dealer_has_blackjack = False

    if dealer_shows_ace_or_ten and dealer_value == 21 and len(dealer_hand) == 2:
        dealer_has_blackjack = True
        deck_state_index += _get_rank_weight(dealer_hand[1]) # Учитываем скрытую карту

    # --- Определение исхода Блекджека ---
    initial_state = 'player_turn'
    outcome_text = None
    hand_status = 'active'

    if player_has_blackjack:
        hand_status = 'blackjack'
        if dealer_has_blackjack:
            update_balance(user_id, bet_amount) # Возврат ставки
            outcome_text = f"Ничья! У обоих Блекджек. Ставка {bet_amount} F возвращена."
            initial_state = 'game_over'
        else:
            winnings = bet_amount * BLACKJACK_PAYOUT
            update_balance(user_id, bet_amount + winnings) # Возврат + выигрыш
            outcome_text = f"БЛЕКДЖЕК! Вы выиграли {winnings:.2f} F (ставка {bet_amount} F)."
            initial_state = 'game_over'
    elif dealer_has_blackjack:
        # Ставка уже снята
        outcome_text = f"У дилера Блекджек! Вы проиграли {bet_amount} F."
        initial_state = 'game_over'

    # --- Обновление состояния игры ---
    game_state.update({
        'state': initial_state, 'deck': deck,
        'player_hands': [{'hand': player_hand, 'bet': bet_amount, 'status': hand_status, 'can_double': (not player_has_blackjack), 'can_split': False}],
        'current_hand_index': 0, 'dealer_hand': dealer_hand,
        '_deck_state_index': deck_state_index, 'cards_dealt': cards_dealt,
        'initial_bet': bet_amount, 'split_count': 0, 'outcome_text': outcome_text
    })

    await query.answer(f"Ставка {bet_amount} принята!")
    await show_game_state(context, chat_id, game_state['message_id'])


async def show_game_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int = None):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    pid = gs['player_id']
    bal = get_balance(pid) # Актуальный баланс из БД

    if message_id: gs['message_id'] = message_id
    if not gs.get('message_id'): return

    dh = gs['dealer_hand']; phs = gs['player_hands']; cur_idx = gs['current_hand_index']
    hide = gs['state'] == 'player_turn' and not (get_hand_value(dh) == 21 and len(dh) == 2)

    # --- Формирование текста ---
    text = f"**Блекджек** | Баланс: {bal:.2f} F\n"
    total_bet = sum(h['bet'] for h in phs); num_h = len(phs)
    text += f"Ставка{' (всего)' if num_h > 1 else ''}: {total_bet} F{f' ({num_h} рук)' if num_h > 1 else ''}\n"
    text += "---------------------------------\n"
    dv_s = "??"
    if not hide: dv_s = str(get_hand_value(dh))
    elif dh: dv_s = str(get_card_value(dh[0])) + "+?"
    dh_s = format_hand(dh, hide); text += f"**Дилер:** {dh_s} ({dv_s})\n\n"; text += "**Вы:**\n"; active_h = None
    for i, hd in enumerate(phs):
        h=hd['hand']; hv=get_hand_value(h); st=hd['status']; bet=hd['bet']
        is_cur = (i == cur_idx and st == 'active')
        ind = "▶️" if is_cur else "✅" if st=='stand' else "❌" if st=='bust' else "💰" if st=='blackjack' else "✔️"
        text += f"{ind} Рука {i+1}: {format_hand(h)} ({hv}) [{bet} F]"
        if st == 'bust': text += " Перебор!"
        elif st == 'blackjack': text += " БЖ!"
        elif st == 'stand': text += " Стоп"
        text += "\n"
        if is_cur: active_h = hd
    text += "\n"
    # --- Формирование кнопок ---
    kbd = []
    if active_h:
        h = active_h['hand']; bet = active_h['bet']
        # Проверяем баланс из БД
        can_double = active_h.get('can_double', False) and bal >= bet and len(h) == 2
        can_split = len(h) == 2 and h[0][0] == h[1][0] and bal >= bet and gs['split_count'] < MAX_SPLITS
        acts = [InlineKeyboardButton("Еще", callback_data=f"bj_action_hit_{cur_idx}"),
                InlineKeyboardButton("Хватит", callback_data=f"bj_action_stand_{cur_idx}")]
        subs = []
        if can_double: subs.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_action_double_{cur_idx}"))
        if can_split: subs.append(InlineKeyboardButton("Разделить", callback_data=f"bj_action_split_{cur_idx}"))
        kbd.append(acts)
        if subs: kbd.append(subs)
    elif gs['state'] == 'game_over':
        if gs.get('outcome_text'): text += f"**Конец Игры!**\n{gs['outcome_text']}\n"
        text += f"Итоговый баланс: {bal:.2f} F\n"; kbd.append([InlineKeyboardButton("Новая Игра", callback_data="bj_action_new_game")])
    elif gs['state'] == 'dealer_turn': text += "*Ход дилера...*\n"
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    # --- Отправка/Редактирование сообщения ---
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=gs['message_id'], text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        if "not found" in str(e).lower():
            logger.warning(f"Msg {gs['message_id']} not found.");
            if gs['state'] != 'game_over':
                try: n_msg = await context.bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN); gs['message_id'] = n_msg.message_id
                except Exception as e2: logger.error(f"Failed resend: {e2}")
        else: logger.error(f"Update error {gs['message_id']}: {e}")


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
    needs_reshuffle = len(deck) < (NUM_DECKS * 52 * (1.0 - RESHUFFLE_PENETRATION))
    if needs_reshuffle: logger.info(f"Reshuffle {chat_id}"); gs['deck'] = create_deck(NUM_DECKS); gs['_deck_state_index'] = 0.0; deck = gs['deck']

    # --- Логика действий ---
    if action == 'hit':
        card, adj = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, target="player")
        if card:
            h.append(card); gs['_deck_state_index'] += adj; gs['cards_dealt'] += 1
            hd['can_double'] = False; hd['can_split'] = False
            hv = get_hand_value(h)
            if hv > 21: hd['status'] = 'bust'
            elif hv == 21: hd['status'] = 'stand'
            if hd['status'] != 'active': await next_player_action_or_dealer(context, chat_id)
            else: await show_game_state(context, chat_id)
        else: hd['status'] = 'stand'; await next_player_action_or_dealer(context, chat_id)

    elif action == 'stand':
        hd['status'] = 'stand'; await next_player_action_or_dealer(context, chat_id)

    elif action == 'double':
        if hd.get('can_double', False) and bal >= bet and len(h) == 2:
            if update_balance(user_id, -bet) is not None: # Проверяем успех списания
                hd['bet'] += bet
                card, adj = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, target="player")
                if card: h.append(card); gs['_deck_state_index'] += adj; gs['cards_dealt'] += 1; hd['status'] = 'bust' if get_hand_value(h) > 21 else 'stand'
                else: hd['status'] = 'stand'
                await next_player_action_or_dealer(context, chat_id)
            else: await query.answer("Ошибка списания баланса!", show_alert=True) # Уведомляем об ошибке

    elif action == 'split':
        if len(h) == 2 and h[0][0] == h[1][0] and bal >= bet and gs['split_count'] < MAX_SPLITS:
             if update_balance(user_id, -bet) is not None: # Проверяем успех списания
                 gs['split_count'] += 1; card_m = h.pop()
                 n_hd = {'hand': [card_m], 'bet': bet, 'status': 'active', 'can_double': True, 'can_split': False}
                 gs['player_hands'].insert(hand_index + 1, n_hd)
                 card1, adj1 = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, target="player"); gs['_deck_state_index'] += adj1; gs['cards_dealt'] += 1
                 card2, adj2 = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, target="player"); gs['_deck_state_index'] += adj2; gs['cards_dealt'] += 1
                 if card1: h.append(card1)
                 if card2: n_hd['hand'].append(card2)
                 is_ace = h[0][0] == 'A'
                 if is_ace: hd['status']='stand'; hd['can_double']=False; n_hd['status']='stand'; n_hd['can_double']=False; await next_player_action_or_dealer(context, chat_id)
                 else:
                     hd['can_double']=bool(card1); hd['can_split']=bool(card1 and len(h)==2 and h[0][0]==card1[0])
                     n_hd['can_double']=bool(card2); n_hd['can_split']=bool(card2 and len(n_hd['hand'])==2 and n_hd['hand'][0][0]==card2[0])
                     if get_hand_value(h)==21: hd['status']='stand'
                     await show_game_state(context, chat_id)
             else: await query.answer("Ошибка списания баланса!", show_alert=True)


async def next_player_action_or_dealer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs['state'] != 'player_turn': return
    phs = gs['player_hands']; cur_idx = gs['current_hand_index']; next_idx = cur_idx + 1
    while next_idx < len(phs):
        if phs[next_idx]['status'] == 'active': gs['current_hand_index'] = next_idx; await show_game_state(context, chat_id); return
        next_idx += 1
    gs['state'] = 'dealer_turn'; await show_game_state(context, chat_id)
    context.job_queue.run_once(dealer_turn_job, DEALER_TURN_DELAY, chat_id=chat_id, data=chat_id, name=f"dealer_{chat_id}")


async def dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs['state'] != 'dealer_turn': return
    deck = gs['deck']; dh = gs['dealer_hand']
    can_win = any(p['status'] not in ['bust', 'blackjack'] for p in gs['player_hands'])
    if not can_win:
        if len(dh) == 2 and not (get_hand_value(dh) == 21): gs['_deck_state_index'] += _get_rank_weight(dh[1])
        await determine_outcome(context, chat_id, False); return
    d_bj = get_hand_value(dh) == 21 and len(dh) == 2
    if not d_bj and len(dh) == 2: gs['_deck_state_index'] += _get_rank_weight(dh[1])
    while True:
        dv = get_hand_value(dh); ac = sum(1 for c in dh if c[0] == 'A'); is_s = ac > 0 and (dv - 11 * ac < 11)
        hit = (dv < 17) or (dv == 17 and is_s and DEALER_HITS_SOFT_17)
        if not hit: break
        if len(deck) < (NUM_DECKS * 52 * (1.0 - RESHUFFLE_PENETRATION)): gs['deck'] = create_deck(NUM_DECKS); gs['_deck_state_index'] = 0.0; deck = gs['deck']
        card, adj = _draw_card_from_shoe(deck, gs['_deck_state_index'], gs['cards_dealt'], NUM_DECKS, target="dealer")
        if card: dh.append(card); gs['_deck_state_index'] += adj; gs['cards_dealt'] += 1
        else: break
    await determine_outcome(context, chat_id, d_bj)


async def determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, dealer_had_blackjack: bool):
    if chat_id not in context.bot_data.get('games', {}): return
    gs = context.bot_data['games'][chat_id]
    if gs['state'] == 'game_over' and gs.get('outcome_text'): await show_game_state(context, chat_id); return

    pid = gs['player_id']; phs = gs['player_hands']; dh = gs['dealer_hand']; dv = get_hand_value(dh); d_bust = dv > 21
    bal_before = get_balance(pid); outcomes = []; total_payout = 0

    for i, hd in enumerate(phs):
        h = hd['hand']; bet = hd['bet']; st = hd['status']; pv = get_hand_value(h); p_bj = st == 'blackjack'
        mult = 0; outc = ""; pfx = f"Р{i+1}: " if len(phs) > 1 else ""
        if st == 'bust': outc = f"{pfx}Перебор({pv}). {-bet} F"; mult = 0
        elif p_bj: w = bet * BLACKJACK_PAYOUT; outc = f"{pfx}БЖ! +{w:.2f} F"; mult = 1 + BLACKJACK_PAYOUT
        elif dealer_had_blackjack: outc = f"{pfx}{pv} vs БЖ. {-bet} F"; mult = 0
        elif d_bust: outc = f"{pfx}{pv} vs Перебор({dv})! +{bet} F"; mult = 2
        elif pv > dv: outc = f"{pfx}{pv} > {dv}. +{bet} F"; mult = 2
        elif pv == dv: outc = f"{pfx}{pv} = {dv}. Пуш."; mult = 1
        else: outc = f"{pfx}{pv} < {dv}. {-bet} F"; mult = 0
        total_payout += bet * mult; outcomes.append(outc)

    if total_payout > 0: update_balance(pid, total_payout)
    final_bal = get_balance(pid); total_bet = sum(h['bet'] for h in phs); net_change = total_payout - total_bet
    gs['state'] = 'game_over'; gs['outcome_text'] = "\n".join(outcomes) + f"\n\n**Итог: {net_change:+.2f} F**"
    await show_game_state(context, chat_id)


# <<< --- Internal Dealing Logic (Moved Down, Unchanged) --- >>>
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
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data; user_id = query.from_user.id
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
    # Запускаем keep-alive веб-сервер
    keep_alive()

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    if 'games' not in application.bot_data: application.bot_data['games'] = {}

    # Регистрация обработчиков
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
    if not BOT_TOKEN or not DATABASE_URL:
        print("Ошибка: Не заданы переменные окружения BOT_TOKEN и/или DATABASE_URL")
        exit(1)
    main()