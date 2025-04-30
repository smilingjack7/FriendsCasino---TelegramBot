# blackjack.py
# -*- coding: utf-8 -*-

import logging
import random
import asyncio
from html import escape as html_escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message # Import Message
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest

# Import shared functions from main.py (assuming main.py is in the same directory)
try:
    # This will work when running main.py
    from main import get_balance, update_balance, get_user_mention, get_job_data
except ImportError:
    # Provide dummy functions or raise an error if this file is run directly
    logging.error("blackjack.py should not be run directly. Import shared functions failed.")
    # Define dummy functions to allow loading, but they won't work
    async def get_balance(user_id: int) -> float | None: return 0.0
    async def update_balance(user_id: int, change: float) -> float | None: return 0.0
    async def get_user_mention(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str: return f"User {user_id}"
    def get_job_data(context: ContextTypes.DEFAULT_TYPE) -> dict: return context.job.data if context.job else {}


# Use a logger specific to this module
logger = logging.getLogger(__name__)

# --- Blackjack Specific Constants ---
NUM_DECKS = 8
DEALER_HITS_SOFT_17 = True
BLACKJACK_PAYOUT = 1.5
MAX_SPLITS = 3
DEALER_TURN_DELAY = 0.7 # Delay for dealer actions

BJ_GAME_KEY = 'blackjack_game' # Key for user_data storage

# --- Card Definitions (Specific to standard deck games) ---
SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VALUES = {"2":2,"3":3,"4":4,"5":5,"6":6,"7":7,"8":8,"9":9,"T":10,"J":10,"Q":10,"K":10,"A":11}

# --- Blackjack Game Utilities ---
def create_deck(num=NUM_DECKS)->list:
    deck = [(r, s) for _ in range(num) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck

def get_card_value(card: tuple | None) -> int:
    return RANK_VALUES.get(card[0], 0) if card else 0

def get_hand_value(hand: list) -> int:
    value = sum(get_card_value(card) for card in hand if card)
    num_aces = sum(1 for card in hand if card and card[0] == 'A')
    # Adjust for Aces
    while value > 21 and num_aces > 0:
        value -= 10
        num_aces -= 1
    return value

def format_hand(hand: list, hide_one: bool = False) -> str:
    if not hand: return "Пусто"
    if hide_one and len(hand) > 1:
        first_card = f"{hand[0][0]}{hand[0][1]}" if hand[0] else "??"
        return f"[{first_card}, ??]"
    return ", ".join([f"{card[0]}{card[1]}" for card in hand if card])

def draw_card(deck: list) -> tuple | None:
    if not deck:
        logger.warning("Attempted to draw from an empty deck.")
        return None
    try:
        return deck.pop(random.randrange(len(deck)))
    except (ValueError, IndexError) as e:
        logger.error(f"Error drawing card: {e}")
        return None

# --- Blackjack Core Logic Functions ---

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    logger.info(f"BJ /blackjack command from user {user.id} in chat {chat.id} (type: {chat.type})")

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Играть в Блекджек можно только в <b>личном чате</b> со мной.", parse_mode=ParseMode.HTML)
        return

    is_callback = update.callback_query is not None
    source_message = update.callback_query.message if is_callback else update.message
    callback_message_id = source_message.message_id if is_callback else None
    effective_chat_id = chat.id

    if is_callback:
        try:
            await update.callback_query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query in blackjack_start_command: {e}")

    # Удаляем старое сообщение игры, если оно есть
    user_game_state_before_clear = context.application.user_data.get(user.id, {}).get(BJ_GAME_KEY, {})
    previous_message_id = user_game_state_before_clear.get('message_id')
    if previous_message_id and previous_message_id != callback_message_id:
        try:
            await context.bot.delete_message(effective_chat_id, previous_message_id)
            logger.debug(f"Deleted previous BJ message {previous_message_id} for user {user.id}")
        except Exception as e:
            logger.debug(f"Failed to delete old BJ message {previous_message_id}: {e}")

    # --- Очистка предыдущего состояния игры (САМЫЙ ПРЯМОЙ ПОДХОД) ---
    try:
        # Пытаемся получить существующий словарь данных для пользователя
        if user.id in context.application.user_data:
            user_specific_data = context.application.user_data[user.id]
            # Проверяем, что это действительно словарь
            if isinstance(user_specific_data, dict):
                # Если это словарь, удаляем ключ игры
                user_specific_data.pop(BJ_GAME_KEY, None)
                logger.debug(f"Cleared previous blackjack state for user {user.id}")
            else:
                # Если по ключу user.id лежит не словарь, это странно.
                # Заменяем его пустым словарем.
                logger.warning(f"Data for user {user.id} was not a dict ({type(user_specific_data)}). Re-initializing.")
                context.application.user_data[user.id] = {}
        else:
            # Если пользователя вообще нет в user_data, создаем для него пустой словарь
            context.application.user_data[user.id] = {}
            logger.debug(f"Initialized user_data dictionary for user {user.id} (was missing).")

    except TypeError as e:
        # Эта ошибка может возникнуть, если сам context.application.user_data не поддерживает item assignment '[user.id] = {}'
        logger.error(f"CRITICAL: TypeError accessing application.user_data for user {user.id}. Is user_data structure correct? Error: {e}", exc_info=True)
        # В этом случае бот, вероятно, не сможет работать с user_data вообще
        try:
             await update.effective_message.reply_text("Критическая ошибка хранения данных. Невозможно начать игру.")
        except Exception as send_err:
            logger.error(f"Failed to send critical error message: {send_err}")
        return # Выход из функции, если структура user_data сломана
    except Exception as e:
        logger.error(f"Error clearing/initializing user_data for user {user.id} during game start: {e}", exc_info=True)
        try:
            await update.effective_message.reply_text("Произошла внутренняя ошибка при подготовке игры. Попробуйте позже.")
        except Exception as send_err:
            logger.error(f"Failed to send internal error message: {send_err}")
        return # Выход из функции при других ошибках очистки
    # --- Конец очистки ---


    # Проверка баланса
    balance = await get_balance(user.id) # Используем await для асинхронной функции
    if balance is None:
         # Сообщение пользователю об ошибке баланса
         try:
             await source_message.reply_text("Не удалось получить ваш баланс. Попробуйте /start.", parse_mode=ParseMode.HTML)
         except Exception as send_err:
             logger.error(f"Failed to send balance error message: {send_err}")
         return
    if balance <= 0:
        # Сообщение пользователю о недостаточном балансе
        try:
            await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) недостаточен для игры. Попробуйте /bonus.", parse_mode=ParseMode.HTML)
        except Exception as send_err:
             logger.error(f"Failed to send insufficient balance message: {send_err}")
        return

    # Опции ставок и кнопки
    bet_options = [1, 5, 10, 25, 50, 100, 250, 500, 1000]
    valid_bets = [b for b in bet_options if b <= balance]

    if not valid_bets:
        min_bet = min(bet_options) if bet_options else 1
        try:
            await source_message.reply_text(f"Ваш баланс (<b>{balance:.2f}</b> F) меньше минимальной ставки (<b>{min_bet}</b> F).", parse_mode=ParseMode.HTML)
        except Exception as send_err:
             logger.error(f"Failed to send minimum bet error message: {send_err}")
        return

    buttons = []
    row = []
    for bet in valid_bets:
        row.append(InlineKeyboardButton(f"{bet} F", callback_data=f"bj_bet_{bet}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    markup = InlineKeyboardMarkup(buttons)
    text = f"Ваш баланс: <b>{balance:.2f}</b> F.\nВыберите вашу ставку:"

    # Отправка сообщения с кнопками ставок
    sent_message = None # Инициализируем переменную перед блоком try
    try:
        # Удаляем сообщение, с которого пришел колбэк (если это был колбэк "Новая игра")
        if callback_message_id:
            try:
                await context.bot.delete_message(effective_chat_id, callback_message_id)
            except Exception as e:
                 logger.warning(f"Failed to delete callback message {callback_message_id} in blackjack_start_command: {e}")

        # Отправляем новое сообщение с кнопками ставок
        sent_message = await context.bot.send_message(
            chat_id=effective_chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logger.error(f"BJ start error sending bet prompt message for user {user.id}: {e}", exc_info=True)
        # Пытаемся уведомить пользователя об ошибке
        try:
            await context.bot.send_message(effective_chat_id, "❌ Произошла ошибка при начале игры.")
        except Exception as send_err:
            logger.error(f"Failed to send game start error message: {send_err}")
        return # Выходим, если не удалось отправить сообщение со ставками

    # --- Сохранение начального состояния ---
    # Убедимся, что сообщение было отправлено успешно перед сохранением состояния
    if sent_message:
        try:
            # Получаем словарь пользователя (он должен существовать и быть словарем после блока очистки)
            # Используем get для безопасности, хотя проверка выше должна была создать словарь
            user_specific_data = context.application.user_data.get(user.id)
            if isinstance(user_specific_data, dict):
                 user_specific_data[BJ_GAME_KEY] = {
                     'state': 'waiting_bet',
                     'message_id': sent_message.message_id
                 }
                 logger.info(f"BJ bet prompt sent (msg {sent_message.message_id}) for user {user.id}. Initial state saved.")
                 # logger.debug(f"Current user_data for {user.id}: {context.application.user_data.get(user.id)}") # Debug log
            else:
                # Если это не словарь после всех проверок - серьезная проблема
                logger.error(f"CRITICAL: user_data for {user.id} is not a dict ({type(user_specific_data)}) when saving game state!")
                raise TypeError("User data storage is corrupted.")

        except Exception as e:
            logger.error(f"BJ start error saving initial state for user {user.id}: {e}", exc_info=True)
            try:
                # Пытаемся удалить сообщение со ставками, так как состояние не сохранилось
                await context.bot.delete_message(chat_id=effective_chat_id, message_id=sent_message.message_id)
                await context.bot.send_message(effective_chat_id, "❌ Произошла ошибка при сохранении состояния игры.")
            except Exception as cleanup_err:
                logger.error(f"Failed to cleanup/notify after save state error: {cleanup_err}")
            return # Выходим, если не удалось сохранить состояние
    else:
        # Эта ветка не должна выполняться, если блок try/except выше отработал корректно,
        # но добавляем для полноты картины
        logger.error(f"BJ start: sent_message object is None after sending bet prompt for user {user.id}. Cannot save state.")
        return
    
async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    game = context.application.user_data.get(uid, {}).get(BJ_GAME_KEY, {})
    bet_prompt_message_id = q.message.message_id

    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != bet_prompt_message_id:
        # Don't answer here, handle_callback in main.py will answer if needed
        # await q.answer("Эта игра больше неактивна.", show_alert=False)
        return

    balance = await get_balance(uid)
    if balance is None:
        await q.answer("Ошибка получения баланса.", show_alert=True)
        return
    if bet <= 0 or bet > balance:
        await q.answer(f"Недопустимая ставка ({bet} F) или недостаточно средств ({balance:.2f} F).", show_alert=True)
        return

    if await update_balance(uid, -bet) is None:
        await q.answer("Ошибка при списании ставки.", show_alert=True)
        return

    # Acknowledge bet acceptance *before* dealing (which might take time)
    await q.answer(f"Ставка принята: {bet} F")

    deck = create_deck()
    player_hand, dealer_hand = [], []
    cards_dealt_count = 0

    try:
        for _ in range(2):
            card_p = draw_card(deck)
            if not card_p: raise IndexError("Deck empty during player deal")
            player_hand.append(card_p)
            cards_dealt_count += 1
            card_d = draw_card(deck)
            if not card_d: raise IndexError("Deck empty during dealer deal")
            dealer_hand.append(card_d)
            cards_dealt_count += 1
    except IndexError as e:
        logger.error(f"BJ dealing error for user {uid}: {e}")
        await update_balance(uid, bet) # Refund bet
        try:
            await q.edit_message_text(f"❌ Ошибка раздачи карт ({e}). Ставка {bet} F возвращена.", reply_markup=None)
        except Exception: pass
        context.application.user_data.get(uid, {}).pop(BJ_GAME_KEY, None)
        return
    except Exception as e:
        logger.error(f"BJ unexpected dealing error for user {uid}: {e}", exc_info=True)
        await update_balance(uid, bet) # Refund bet
        try:
             await q.edit_message_text(f"❌ Непредвиденная ошибка ({e}). Ставка {bet} F возвращена.", reply_markup=None)
        except Exception: pass
        context.application.user_data.get(uid, {}).pop(BJ_GAME_KEY, None)
        return

    player_value = get_hand_value(player_hand)
    dealer_value = get_hand_value(dealer_hand)
    player_has_blackjack = (player_value == 21 and len(player_hand) == 2)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2)

    game_state = 'player_turn'
    hand_status = 'active'
    outcome_text = None
    winnings = 0.0 # Track winnings/payout for initial BJ case

    if player_has_blackjack:
        hand_status = 'blackjack'
        game_state = 'game_over'
        if dealer_has_blackjack:
            outcome_text = "⚖️ Ничья! У обоих Блекджек."
            await update_balance(uid, bet) # Refund bet (push)
            winnings = bet
        else:
            bj_payout_amount = bet * BLACKJACK_PAYOUT
            await update_balance(uid, bet + bj_payout_amount) # Refund bet + BJ payout
            outcome_text = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {bj_payout_amount:.2f} F!"
            winnings = bet + bj_payout_amount
    elif dealer_has_blackjack:
        game_state = 'game_over'
        outcome_text = "😥 У дилера Блекджек! Вы проиграли."
        winnings = 0.0 # Bet already lost

    game.update({
        'state': game_state,
        'deck': deck,
        'cards_dealt': cards_dealt_count,
        'player_hands': [{
            'hand': player_hand, 'bet': bet, 'status': hand_status,
            'can_double': (game_state == 'player_turn' and len(player_hand) == 2),
            'can_split': False
        }],
        'current_hand_index': 0,
        'dealer_hand': dealer_hand,
        'initial_bet': bet,
        'split_count': 0,
        'outcome_text': outcome_text,
        'outcome_determined': (game_state == 'game_over'),
        'total_winnings_paid': winnings if game_state == 'game_over' else 0.0
    })

    try:
        # Edit the original bet prompt message to show the game state
        new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True, message_id_to_edit=bet_prompt_message_id)

        if not new_message_info: # If editing failed, try sending new
             logger.warning(f"Editing bet prompt {bet_prompt_message_id} failed, sending new message.")
             await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id) # Try deleting old one
             new_message_info = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=False)

        if new_message_info:
            new_msg_id = new_message_info if isinstance(new_message_info, int) else new_message_info.message_id
            game['message_id'] = new_msg_id # Update message ID in state
            logger.info(f"BJ initial state sent/edited (msg {new_msg_id}) for user {uid}. State: {game_state}")
            if game['outcome_determined']: # Clean up if game ended due to initial BJ
                context.application.user_data.get(uid, {}).pop(BJ_GAME_KEY, None)
                logger.info(f"BJ game state cleaned for user {uid} after initial Blackjack outcome.")
        else:
             raise Exception("Failed to show initial game state after bet.")

    except Exception as e:
        logger.error(f"Failed to show initial BJ state for user {uid}: {e}")
        await update_balance(uid, bet) # Refund bet
        context.application.user_data.get(uid, {}).pop(BJ_GAME_KEY, None)
        try:
            await context.bot.send_message(chat_id, "❌ Ошибка отображения игры. Ставка возвращена.")
        except Exception: pass
        # Clean up the original bet prompt if possible
        try: await context.bot.delete_message(chat_id=chat_id, message_id=bet_prompt_message_id)
        except Exception: pass


async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, game_state: dict | None = None, edit_existing: bool = True, message_id_to_edit: int | None = None) -> Message | int | None:
    """ Edits or sends a message showing the current Blackjack game state. """
    # Use application.user_data consistently
    if game_state is None:
        game_state = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game_state:
        logger.warning(f"blackjack_show_state called for user {user_id} but no game state found.")
        return None

    message_id_to_process = message_id_to_edit if message_id_to_edit else game_state.get('message_id')

    # --- Fetch Data ---
    balance = await get_balance(user_id)
    balance_str = f"{balance:.2f}" if balance is not None else "N/A"
    dealer_hand = game_state.get('dealer_hand', [])
    player_hands_data = game_state.get('player_hands', [])
    current_hand_idx = game_state.get('current_hand_index', -1)
    state = game_state.get('state', 'unknown')
    dealer_value = get_hand_value(dealer_hand)
    dealer_has_blackjack = (dealer_value == 21 and len(dealer_hand) == 2 and game_state.get('cards_dealt', 0) <= 4)

    all_player_hands_finished = all(
        hdata.get('status') in ['bust', 'stand', 'blackjack'] for hdata in player_hands_data if isinstance(hdata, dict)
    )
    hide_dealer_card = (state == 'player_turn' and not dealer_has_blackjack and not all_player_hands_finished)

    # --- Build Text ---
    text = f"<b>Блекджек</b> | Баланс: <b>{balance_str}</b> F\n"
    total_bet = sum(h.get('bet', 0) for h in player_hands_data if isinstance(h, dict))
    num_hands = len(player_hands_data)
    text += f"Общая ставка: <b>{total_bet}</b> F{' (Рук: ' + str(num_hands) + ')' if num_hands > 1 else ''}\n"
    text += "--------------------\n"

    dealer_value_display = "??" if not dealer_hand else (str(dealer_value) if not hide_dealer_card else f"{get_card_value(dealer_hand[0])}+?")
    text += f"<b>Диллер:</b> {format_hand(dealer_hand, hide_one=hide_dealer_card)} ({dealer_value_display})\n\n"

    text += "<b>Вы:</b>\n"
    active_hand_data = None
    for i, hand_data in enumerate(player_hands_data):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', [])
        hand_value = get_hand_value(hand)
        hand_status = hand_data.get('status', '?')
        hand_bet = hand_data.get('bet', 0)
        is_current_turn = (i == current_hand_idx and hand_status == 'active' and state == 'player_turn')

        indicator = "▶️" if is_current_turn else \
                    "✅" if hand_status == 'stand' else \
                    "❌" if hand_status == 'bust' else \
                    "💰" if hand_status == 'blackjack' else \
                    "▫️"

        text += f"{indicator} Рука {i+1}: {format_hand(hand)} (<b>{hand_value}</b>) [<i>{hand_bet} F</i>]"

        status_label = ""
        if hand_status == 'bust': status_label = " - <b>Перебор!</b>"
        elif hand_status == 'blackjack': status_label = " - <b>Блекджек!</b>"
        elif hand_status == 'stand' and not is_current_turn: status_label = " - <i>Стоп</i>"
        text += status_label + "\n"

        if is_current_turn:
            active_hand_data = hand_data

    # --- Build Keyboard ---
    keyboard = []
    if active_hand_data and state == 'player_turn':
        player_hand = active_hand_data.get('hand', [])
        player_bet = active_hand_data.get('bet', 0)
        current_balance = await get_balance(user_id) # Fetch fresh balance for checks

        can_double = (active_hand_data.get('can_double', False) and
                      len(player_hand) == 2 and
                      current_balance is not None and current_balance >= player_bet)

        can_split = (len(player_hand) == 2 and
                     player_hand[0] and player_hand[1] and
                     get_card_value(player_hand[0]) == get_card_value(player_hand[1]) and
                     current_balance is not None and current_balance >= player_bet and
                     game_state.get('split_count', 0) < MAX_SPLITS)
        active_hand_data['can_split'] = can_split # Update possibility

        action_buttons = [
            InlineKeyboardButton("Еще", callback_data=f"bj_hit_{current_hand_idx}"),
            InlineKeyboardButton("Хватит", callback_data=f"bj_stand_{current_hand_idx}")
        ]
        keyboard.append(action_buttons)

        special_buttons = []
        if can_double:
            special_buttons.append(InlineKeyboardButton("Удвоить", callback_data=f"bj_double_{current_hand_idx}"))
        if can_split:
            special_buttons.append(InlineKeyboardButton("Разделить", callback_data=f"bj_split_{current_hand_idx}"))
        if special_buttons:
            keyboard.append(special_buttons)

    elif state == 'game_over':
        text += f"\n<b>Игра окончена!</b>\n{game_state.get('outcome_text', 'Результат не определен.')}\n"
        final_balance = await get_balance(user_id)
        text += f"\nИтоговый баланс: <b>{final_balance:.2f}</b> F." if final_balance is not None else ""
        # *** IMPORTANT: Use the 'bj_' prefix ***
        keyboard.append([InlineKeyboardButton("🔄 Новая игра", callback_data="bj_new_game")])
    elif state == 'dealer_turn':
        text += "\n<i>⏳ Ход дилера...</i>"

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Send or Edit Message ---
    result: Message | int | None = None
    max_retries = 1
    current_retry = 0
    edit_failed_once = False

    while current_retry <= max_retries:
        try:
            if edit_existing and message_id_to_process and not edit_failed_once:
                logger.debug(f"Attempting edit (try {current_retry+1}) BJ state msg {message_id_to_process} for user {user_id}")
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id_to_process, text=text,
                    reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
                logger.debug(f"Successfully edited BJ state msg {message_id_to_process}")
                result = message_id_to_process
                break
            else:
                # Send new message
                logger.debug(f"Sending NEW BJ state message for user {user_id} (edit_existing={edit_existing}, msg_id={message_id_to_process}, edit_failed={edit_failed_once})")
                if message_id_to_process: # Try deleting old one if sending new
                    try: await context.bot.delete_message(chat_id, message_id_to_process)
                    except Exception: pass

                new_message = await context.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
                # Update message ID in game state *immediately*
                game_state['message_id'] = new_message.message_id
                logger.debug(f"Sent NEW BJ state msg {new_message.message_id} for user {user_id}. Updated game state.")
                result = new_message
                break
        except BadRequest as e:
            error_str = str(e).lower()
            if "message is not modified" in error_str:
                result = message_id_to_process
                logger.debug(f"BJ state msg {message_id_to_process} not modified.")
                break
            elif "message to edit not found" in error_str:
                logger.warning(f"Message {message_id_to_process} to edit not found for user {user_id}. Forcing send new.")
                edit_failed_once = True
                message_id_to_process = None # Clear invalid ID
                game_state['message_id'] = None # Clear from game state too
                current_retry += 1
                if current_retry > max_retries:
                    logger.error(f"Failed to send new message after edit failed for user {user_id}.")
                    result = None
            elif "chat not found" in error_str:
                 logger.error(f"Chat {chat_id} not found for user {user_id} while updating BJ state. Aborting update.")
                 result = None
                 break # Don't retry chat not found
            elif "can't parse entities" in error_str:
                logger.error(f"HTML Parsing Error for user {user_id} (msg {message_id_to_process}): {e}\nText snippet: {text[:200]}...")
                result = None
                break
            else:
                logger.warning(f"Edit/Send BJ state failed for user {user_id} (msg {message_id_to_process}) (try {current_retry+1}): {e}")
                result = None
                current_retry += 1
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Unexpected error in blackjack_show_state for user {user_id} (try {current_retry+1}): {e}", exc_info=True)
            result = None
            current_retry += 1
            await asyncio.sleep(0.5)

    if result is None:
        logger.error(f"Failed to update/send BJ state for user {user_id} after all attempts.")

    return result


async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, hand_index_str: str):
    q = update.callback_query
    u = q.from_user
    uid = u.id
    chat_id = q.message.chat_id
    # Use application.user_data
    game = context.application.user_data.get(uid, {}).get(BJ_GAME_KEY, {})

    try:
        hand_index = int(hand_index_str)
    except (ValueError, TypeError):
        logger.warning(f"Invalid hand index received: {hand_index_str} for user {uid}")
        await q.answer("Ошибка: Неверный индекс руки.", show_alert=True)
        return

    action_message_id = q.message.message_id

    # --- Validations ---
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != action_message_id:
        await q.answer("Эта игра или действие больше неактивны.", show_alert=False)
        # Try removing buttons from the message if it's the correct one
        if game.get('message_id') == action_message_id:
            try:
                await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=action_message_id, reply_markup=None)
            except Exception: pass
        return

    player_hands = game.get('player_hands', [])
    if not (0 <= hand_index < len(player_hands)) or hand_index != game.get('current_hand_index', -1):
        await q.answer("Сейчас ход другой руки.", show_alert=False)
        return

    current_hand_data = player_hands[hand_index]
    if not isinstance(current_hand_data, dict) or current_hand_data.get('status') != 'active':
        await q.answer("Эта рука неактивна для действий.", show_alert=False)
        return

    # --- Get Required Data ---
    hand = current_hand_data.get('hand', [])
    deck = game.get('deck', [])
    balance = await get_balance(uid) # Fetch fresh balance
    bet = current_hand_data.get('bet', 0)

    # --- Execute Action ---
    needs_state_update = False
    move_to_next = False
    action_answer_text = "" # Text for q.answer()

    try:
        if action == 'hit':
            card = draw_card(deck)
            if card:
                hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                current_hand_data['can_double'] = False
                current_hand_data['can_split'] = False
                hand_value = get_hand_value(hand)
                action_answer_text = f"Взяли: {card[0]}{card[1]}"
                needs_state_update = True

                if hand_value > 21:
                    current_hand_data['status'] = 'bust'
                    move_to_next = True
                elif hand_value == 21:
                    current_hand_data['status'] = 'stand'
                    move_to_next = True
            else:
                raise IndexError("Draw fail (deck empty or error)")

        elif action == 'stand':
            current_hand_data['status'] = 'stand'
            action_answer_text = "Стоп."
            needs_state_update = True
            move_to_next = True

        elif action == 'double':
            can_double = (current_hand_data.get('can_double', False) and
                          len(hand) == 2 and
                          balance is not None and balance >= bet)
            if can_double:
                new_balance = await update_balance(uid, -bet)
                if new_balance is not None:
                    current_hand_data['bet'] += bet
                    current_hand_data['can_double'] = False
                    current_hand_data['can_split'] = False
                    balance = new_balance # Update local cache

                    card = draw_card(deck)
                    drawn_card_str = ""
                    if card:
                        hand.append(card)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                        hand_value = get_hand_value(hand)
                        current_hand_data['status'] = 'bust' if hand_value > 21 else 'stand'
                        drawn_card_str = f" Карта: {card[0]}{card[1]}. Итог: {hand_value}{' (Перебор!)' if hand_value > 21 else ''}"
                    else:
                        current_hand_data['status'] = 'stand'
                        drawn_card_str = " Ошибка взятия карты."
                        logger.warning(f"BJ double failed draw for user {uid}")

                    action_answer_text = f"Удвоено!{drawn_card_str}"
                    needs_state_update = True
                    move_to_next = True
                else:
                    await q.answer("Ошибка списания средств для удвоения.", show_alert=True)
                    return # Don't proceed further on balance error
            else:
                await q.answer("Удвоить сейчас нельзя.", show_alert=True)
                return

        elif action == 'split':
            can_split = (len(hand) == 2 and hand[0] and hand[1] and
                         get_card_value(hand[0]) == get_card_value(hand[1]) and
                         balance is not None and balance >= bet and
                         game.get('split_count', 0) < MAX_SPLITS)
            current_hand_data['can_split'] = can_split # Update state

            if can_split:
                new_balance = await update_balance(uid, -bet)
                if new_balance is not None:
                    game['split_count'] += 1
                    balance = new_balance # Update local cache
                    card_to_move = hand.pop()

                    new_hand_data = {'hand': [card_to_move], 'bet': bet, 'status': 'active', 'can_double': False, 'can_split': False}

                    cards_drawn = [draw_card(deck), draw_card(deck)]
                    drawn_count = 0
                    if cards_drawn[0]: hand.append(cards_drawn[0]); drawn_count += 1
                    if cards_drawn[1]: new_hand_data['hand'].append(cards_drawn[1]); drawn_count += 1

                    if drawn_count < 2:
                         logger.warning(f"BJ split failed to draw both cards for user {uid}. Deck empty?")
                         await update_balance(uid, bet) # Refund second bet
                         game['split_count'] -= 1
                         hand.append(card_to_move) # Put card back
                         await q.answer("Ошибка разделения: не хватило карт! Ставка возвращена.", show_alert=True)
                         # Restore original hand state possibilities?
                         current_hand_data['can_split'] = False # Can't split now
                         current_hand_data['can_double'] = (len(hand)==2)
                         needs_state_update = True # Show restored hand
                         # Don't set move_to_next, let player choose again? Or force stand? For now, let them choose.
                    else:
                        player_hands.insert(hand_index + 1, new_hand_data)
                        game['cards_dealt'] = game.get('cards_dealt', 0) + drawn_count

                        is_ace_split = get_card_value(hand[0] if hand else None) == 11
                        if is_ace_split:
                            current_hand_data['status'] = 'stand'
                            new_hand_data['status'] = 'stand'
                            current_hand_data['can_double'] = False
                            new_hand_data['can_double'] = False
                            action_answer_text = "Тузы разделены и стоят."
                            needs_state_update = True
                            # move_to_next will be handled by job check
                        else:
                            if get_hand_value(hand) == 21: current_hand_data['status'] = 'stand'
                            if get_hand_value(new_hand_data['hand']) == 21: new_hand_data['status'] = 'stand'

                            current_hand_data['can_double'] = (len(hand) == 2 and current_hand_data['status'] == 'active')
                            new_hand_data['can_double'] = (len(new_hand_data['hand']) == 2 and new_hand_data['status'] == 'active')

                            limit_ok = game.get('split_count', 0) < MAX_SPLITS
                            current_balance_after_split = balance

                            chd_can_resplit = (current_hand_data['status'] == 'active' and len(hand) == 2 and hand[0] and hand[1] and
                                               get_card_value(hand[0]) == get_card_value(hand[1]) and limit_ok and
                                               current_balance_after_split >= current_hand_data['bet'])
                            nhd_can_resplit = (new_hand_data['status'] == 'active' and len(new_hand_data['hand']) == 2 and new_hand_data['hand'][0] and new_hand_data['hand'][1] and
                                               get_card_value(new_hand_data['hand'][0]) == get_card_value(new_hand_data['hand'][1]) and limit_ok and
                                               current_balance_after_split >= new_hand_data['bet'])
                            current_hand_data['can_split'] = chd_can_resplit
                            new_hand_data['can_split'] = nhd_can_resplit

                            action_answer_text = "Рука разделена!"
                            needs_state_update = True
                            if current_hand_data['status'] == 'stand':
                                move_to_next = True
                else:
                    await q.answer("Ошибка списания средств для разделения.", show_alert=True)
                    return
            else:
                await q.answer("Разделить сейчас нельзя.", show_alert=True)
                return

    except IndexError as e:
        logger.warning(f"BJ action '{action}' user {uid} failed draw: {e}")
        current_hand_data['status'] = 'stand'
        action_answer_text = "Не удалось взять карту! Ход завершен."
        needs_state_update = True
        move_to_next = True
    except Exception as e:
        logger.error(f"BJ action '{action}' user {uid} unexpected error: {e}", exc_info=True)
        await q.answer("Произошла непредвиденная ошибка.", show_alert=True)
        # Force stand on error to avoid broken state?
        current_hand_data['status'] = 'stand'
        needs_state_update = True
        move_to_next = True

    # Answer the callback query *before* potentially long state update/job scheduling
    if action_answer_text:
        show_alert = "Ошибка" in action_answer_text or "Перебор" in action_answer_text
        try:
            await q.answer(action_answer_text, show_alert=show_alert)
        except Exception as e:
             logger.warning(f"Failed to answer callback for action {action}: {e}")


    # --- Update State Message ---
    if needs_state_update:
        update_result = await blackjack_show_state(context, chat_id, uid, game_state=game, edit_existing=True)
        if not update_result:
             logger.error(f"Failed to update state message after action '{action}' for user {uid}. Game might be stuck.")
             try: await context.bot.send_message(chat_id, "⚠️ Ошибка обновления отображения игры.")
             except Exception: pass

    # --- Move to Next Action if Hand Ended ---
    is_ace_split_action = (action == 'split' and get_card_value(hand[0] if hand else None) == 11)

    if move_to_next or is_ace_split_action:
        context.job_queue.run_once(
             blackjack_next_action_job,
             when=0.1, # Short delay
             data={'chat_id': chat_id, 'user_id': uid, 'message_id': action_message_id}, # Pass message_id for validation
             name=f"bj_next_action_{uid}_{action_message_id}"
         )

async def blackjack_next_action_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = get_job_data(context) # Use helper from main
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    expected_message_id = job_data.get('message_id') # Message ID when the action was performed

    if not user_id or not chat_id or not expected_message_id:
         logger.error(f"Missing data in blackjack_next_action_job: {job_data}")
         return

    await blackjack_next_action(context, chat_id, user_id, expected_message_id)

async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, expected_message_id: int):
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game:
        logger.info(f"BJ next_action (job) for user {user_id}: Game state not found. Aborting.")
        return
    # Validate message ID to prevent race conditions if game restarted quickly
    if game.get('message_id') != expected_message_id:
        logger.info(f"BJ next_action (job) for user {user_id}: Message ID mismatch ({game.get('message_id')} != {expected_message_id}). Aborting.")
        return
    if game.get('state') != 'player_turn':
        logger.info(f"BJ next_action (job) for user {user_id}: Game state is not 'player_turn' ({game.get('state')}). Aborting.")
        return

    player_hands = game.get('player_hands', [])
    current_hand_idx = game.get('current_hand_index', -1)

    next_active_idx = -1
    for i in range(current_hand_idx + 1, len(player_hands)):
        hand_data = player_hands[i]
        if isinstance(hand_data, dict) and hand_data.get('status') == 'active':
            next_active_idx = i
            break

    message_id = game.get('message_id') # Should match expected_message_id here

    if next_active_idx != -1:
        game['current_hand_index'] = next_active_idx
        logger.info(f"BJ user {user_id}: Moving to next active hand index {next_active_idx}")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)
    else:
        logger.info(f"BJ user {user_id}: All player hands done, moving to dealer's turn.")
        game['state'] = 'dealer_turn'
        update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

        if not update_success:
            logger.error(f"BJ user {user_id}: Failed to update state to 'dealer_turn'. Aborting dealer job.")
            try: await context.bot.send_message(chat_id, "⚠️ Ошибка! Не удалось перейти к ходу дилера.")
            except Exception: pass
            context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
            return

        context.job_queue.run_once(
            blackjack_dealer_turn_job, DEALER_TURN_DELAY,
            data={'chat_id': chat_id, 'user_id': user_id, 'message_id': message_id},
            name=f"bj_dealer_turn_{user_id}_{message_id}"
        )

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = get_job_data(context)
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id')

    if not user_id or not chat_id or not message_id:
        logger.error(f"BJ Dealer job missing required data: {job_data}")
        return

    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)

    if not game: logger.info(f"BJ Dealer job user {user_id} msg {message_id}: Game state gone."); return
    if game.get('state') != 'dealer_turn': logger.info(f"BJ Dealer job user {user_id} msg {message_id}: State != dealer_turn ({game.get('state')})."); return
    if game.get('message_id') != message_id: logger.info(f"BJ Dealer job user {user_id} msg {message_id}: Message ID mismatch."); return

    deck = game.get('deck', [])
    dealer_hand = game.get('dealer_hand', [])
    player_hands = game.get('player_hands', [])
    dealer_value_initial = get_hand_value(dealer_hand)
    dealer_had_initial_blackjack = (dealer_value_initial == 21 and len(dealer_hand) == 2 and game.get('cards_dealt', 0) <= 4)

    player_can_win = any(isinstance(h, dict) and h.get('status') not in ['bust', 'blackjack'] for h in player_hands)
    dealer_needs_to_hit = player_can_win and get_hand_value(dealer_hand) < 17 # Simplified initial check

    hit_occurred = False
    dealer_stood = False

    if dealer_needs_to_hit:
        logger.info(f"BJ Dealer user {user_id} starts hitting.")
        while not dealer_stood:
            current_dealer_value = get_hand_value(dealer_hand)
            num_aces = sum(1 for c in dealer_hand if c and c[0] == 'A')
            is_soft = num_aces > 0 and (current_dealer_value - (num_aces * 10)) <= 11

            stand_value_met = False
            if current_dealer_value > 17: stand_value_met = True
            elif current_dealer_value == 17:
                if not (is_soft and DEALER_HITS_SOFT_17): stand_value_met = True

            if stand_value_met or current_dealer_value >= 21:
                log_msg = f"stands on {current_dealer_value}"
                if not hit_occurred: log_msg = f"stands initially on {current_dealer_value}"
                logger.info(f"BJ Dealer user {user_id} {log_msg}.")
                dealer_stood = True
                break

            logger.info(f"BJ Dealer user {user_id} hits on {current_dealer_value}{' (soft)' if is_soft else ''}.")
            card = draw_card(deck)
            if card:
                dealer_hand.append(card)
                game['cards_dealt'] = game.get('cards_dealt', 0) + 1
                hit_occurred = True
                await asyncio.sleep(DEALER_TURN_DELAY * 0.6)
            else:
                logger.warning(f"BJ Dealer user {user_id} failed draw. Standing.")
                dealer_stood = True
                break
    else:
         final_dealer_value_no_hit = get_hand_value(dealer_hand)
         logger.info(f"BJ Dealer user {user_id}: No player can win or dealer >= 17 ({final_dealer_value_no_hit}). Dealer stands immediately.")
         dealer_stood = True

    final_dealer_value = get_hand_value(dealer_hand)
    logger.info(f"BJ Dealer user {user_id}: Finished turn with {final_dealer_value}. Updating display.")

    update_success = await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    if not update_success:
        logger.error(f"BJ Dealer user {user_id}: Failed show final dealer hand. Aborting outcome.")
        try: await context.bot.send_message(chat_id, "⚠️ Ошибка отображения хода дилера.")
        except Exception: pass
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        return

    await asyncio.sleep(DEALER_TURN_DELAY * 0.8) # Pause to see dealer hand

    await blackjack_determine_outcome(context, chat_id, user_id, dealer_had_initial_blackjack)


async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game: logger.warning(f"BJ outcome user {user_id}: Game data not found."); return
    message_id = game.get('message_id')
    if not message_id: logger.error(f"BJ outcome user {user_id}: No message_id found."); return

    if game.get('outcome_determined'):
        logger.info(f"BJ outcome user {user_id}: Outcome already determined. Skipping.")
        await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True) # Reshow state
        return

    player_hands = game.get('player_hands', [])
    dealer_hand = game.get('dealer_hand', [])
    dealer_final_value = get_hand_value(dealer_hand)
    dealer_busted = dealer_final_value > 21

    outcome_lines = []
    total_winnings_to_pay = 0.0
    total_initial_bet_sum = 0.0

    for i, hand_data in enumerate(player_hands):
        if not isinstance(hand_data, dict): continue
        hand = hand_data.get('hand', [])
        bet = hand_data.get('bet', 0)
        status = hand_data.get('status')
        player_value = get_hand_value(hand)
        player_had_blackjack = (status == 'blackjack')

        total_initial_bet_sum += bet
        payout_amount = 0.0
        outcome_str = ""
        prefix = f"Рука {i+1}: " if len(player_hands) > 1 else ""

        if status == 'bust':
            outcome_str = f"{prefix}Перебор ({player_value}). Ставка проиграна (-{bet:.2f} F)."
            payout_amount = 0
        elif player_had_blackjack:
            if d_had_bj:
                outcome_str = f"{prefix}Блекджек! Ничья с дилером."
                payout_amount = bet
            else:
                win_amount = bet * BLACKJACK_PAYOUT
                outcome_str = f"{prefix}Блекджек! Выигрыш +{win_amount:.2f} F."
                payout_amount = bet + win_amount
        elif d_had_bj:
            outcome_str = f"{prefix}У дилера Блекджек. Ставка проиграна (-{bet:.2f} F)."
            payout_amount = 0
        elif dealer_busted:
            outcome_str = f"{prefix}У дилера перебор ({dealer_final_value})! Выигрыш +{bet:.2f} F."
            payout_amount = bet * 2
        elif player_value > dealer_final_value:
            outcome_str = f"{prefix}Вы выиграли ({player_value} {html_escape('>')}) {dealer_final_value}. Выигрыш +{bet:.2f} F."
            payout_amount = bet * 2
        elif player_value == dealer_final_value:
            outcome_str = f"{prefix}Ничья ({player_value} = {dealer_final_value}). Ставка возвращена."
            payout_amount = bet
        else: # player_value < dealer_final_value
            outcome_str = f"{prefix}Вы проиграли ({player_value} {html_escape('<')} {dealer_final_value}). Ставка проиграна (-{bet:.2f} F)."
            payout_amount = 0

        outcome_lines.append(outcome_str)
        total_winnings_to_pay += payout_amount

    net_change = total_winnings_to_pay - total_initial_bet_sum
    balance_updated_ok = True

    if total_winnings_to_pay > 0:
        if await update_balance(user_id, total_winnings_to_pay) is None:
            outcome_lines.append("\n<b>❌ ОШИБКА НАЧИСЛЕНИЯ ВЫИГРЫША! ❌</b>")
            net_change = -total_initial_bet_sum
            balance_updated_ok = False
            logger.error(f"BJ outcome user {user_id}: FAILED balance update payout {total_winnings_to_pay:.2f}.")
        else:
            logger.info(f"BJ outcome user {user_id}: Balance updated +{total_winnings_to_pay:.2f}. Net: {net_change:+.2f}")
    else:
        logger.info(f"BJ outcome user {user_id}: No winnings. Net: {net_change:+.2f}")

    game['state'] = 'game_over'
    final_summary = f"\n\n<b>Общий итог раунда: {html_escape(f'{net_change:+.2f}')} F</b>"
    game['outcome_text'] = "\n".join(outcome_lines) + final_summary
    game['outcome_determined'] = True

    await blackjack_show_state(context, chat_id, user_id, game_state=game, edit_existing=True)

    if balance_updated_ok:
        context.application.user_data.get(user_id, {}).pop(BJ_GAME_KEY, None)
        logger.info(f"BJ game state cleaned for user {user_id}")
    else:
        logger.warning(f"BJ game state NOT cleaned for user {user_id} due to balance error.")


# --- Blackjack Callback Router ---
async def handle_blackjack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: str):
    """Handles callbacks starting with 'bj_'."""
    q = update.callback_query
    logger.debug(f"Handling BJ callback: {callback_data}")

    parts = callback_data.split("_", 2) # bj_action_payload -> [bj, action, payload]
    if len(parts) < 2:
        logger.warning(f"Invalid BJ callback format: {callback_data}")
        await q.answer()
        return

    action = parts[1]
    payload = parts[2] if len(parts) > 2 else None

    try:
        if action == "bet" and payload:
            # handle_bet answers the query itself
            await blackjack_handle_bet(update, context, int(payload))
        elif action == "new":
            await q.answer("Новая игра...")
            await blackjack_start_command(update, context) # Re-run start logic using the callback update
        elif action in ["hit", "stand", "double", "split"] and payload is not None:
            # handle_action answers the query itself
            await blackjack_handle_action(update, context, action, payload) # Pass action and payload separately
        elif action == "cancel" and payload == "start": # Example for a potential cancel button
             await q.answer("Ставка отменена.")
             await q.delete_message()
             context.application.user_data.get(q.from_user.id, {}).pop(BJ_GAME_KEY, None) # Clean up state
        else:
            logger.warning(f"Unknown BJ callback action/payload: {action} / {payload}")
            await q.answer()

    except ValueError as e:
         logger.error(f"BJ Callback ValueError (likely int conversion) for '{callback_data}' user {q.from_user.id}: {e}")
         await q.answer("Ошибка: Неверный формат данных.", show_alert=True)
    except Exception as e:
        logger.error(f"BJ Callback general error for '{callback_data}' user {q.from_user.id}: {e}", exc_info=True)
        await q.answer("Произошла внутренняя ошибка.", show_alert=True)


# --- Function to Register Blackjack Handlers ---
def register_handlers(application):
    """Adds Blackjack command and callback handlers to the application."""
    application.add_handler(CommandHandler("blackjack", blackjack_start_command))

    # Note: The main callback handler is in main.py and delegates here.
    # We don't add a separate CallbackQueryHandler here with a pattern like '^bj_'.
    # Instead, the main handler calls handle_blackjack_callback if data starts with 'bj_'.

    logger.info("Blackjack handlers registered.")