# --- Blackjack Game (Private Chat Only) ---
BJ_GAME_KEY = 'blackjack_game'

async def blackjack_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user=update.effective_user; chat=update.effective_chat; logger.info(f"BJ start {user.id} in {chat.id} ({chat.type})")
    if chat.type != ChatType.PRIVATE: await update.message.reply_text("Играть в БЖ только в <b>личном чате</b>.", parse_mode=ParseMode.HTML); return
    reply_func=update.message.reply_text; delete_prev_msg_id=None
    if update.callback_query: reply_func=update.callback_query.message.reply_text; delete_prev_msg_id=update.callback_query.message.message_id; await update.callback_query.answer()
    user_game=context.user_data.get(BJ_GAME_KEY, {})
    if user_game.get('state') not in [None,'game_over','waiting_bet']: await reply_func("Вы уже в игре."); return
    # Use message_id from game state if available for deletion check
    current_game_msg_id = user_game.get('message_id')
    if current_game_msg_id and delete_prev_msg_id != current_game_msg_id:
        try: await context.bot.delete_message(chat.id, current_game_msg_id)
        except Exception as e: logger.debug(f"Could not delete previous game message {current_game_msg_id}: {e}")
    balance=get_balance(user.id)
    if balance is None or balance <= 0: await reply_func(f"Баланс ({balance:.2f if balance is not None else 'N/A'}) мал."); return
    opts=[1,5,10,25,50,100,250,500]; valid=[b for b in opts if b <= balance]
    if not valid: await reply_func(f"Баланс < мин. ставки ({min(opts)})."); return
    btns=[[InlineKeyboardButton(f"{b} F", callback_data=f"bj_bet_{b}") for b in r] for r in [valid[i:i+4] for i in range(0, len(valid), 4)]]
    markup=InlineKeyboardMarkup(btns); text=f"Баланс: <b>{balance:.2f}</b>. Ставка?"
    try:
        # Delete the callback message if it exists before sending new prompt
        if delete_prev_msg_id:
             try: await context.bot.delete_message(chat.id, delete_prev_msg_id)
             except Exception as e: logger.debug(f"Could not delete 'New Game' message {delete_prev_msg_id}: {e}")
        # Always send a new message for the bet prompt
        sent_message=await context.bot.send_message(chat.id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
        # Store the NEW message ID in the game state
        context.user_data[BJ_GAME_KEY]={'state':'waiting_bet', 'message_id': sent_message.message_id}
        logger.info(f"BJ game started for {user.id}, prompt message ID: {sent_message.message_id}")
    except Exception as e: logger.error(f"BJ start error {user.id}: {e}")

async def blackjack_handle_bet(update: Update, context: ContextTypes.DEFAULT_TYPE, bet: int):
    q=update.callback_query; u=q.from_user; uid=u.id; chat_id = q.message.chat_id # Use chat_id from message
    game=context.user_data.get(BJ_GAME_KEY,{})
    # Check if the callback message matches the stored message_id
    if not game or game.get('state') != 'waiting_bet' or game.get('message_id') != q.message.message_id:
        await q.answer("Неактуальная игра или ставка.", show_alert=False); return
    bal=get_balance(uid)
    if bal is None or bet <= 0 or bet > bal: await q.answer("Неверная ставка/баланс.", show_alert=True); return
    if update_balance(uid, -bet) is None: await q.answer("Ошибка списания.", show_alert=True); return
    deck=create_deck(); ph, dh = [], []; dlt = 0
    try:
        for _ in range(2):
            cp,sp=_get_next_item(deck, dlt, NUM_DECKS); ph.append(cp); dlt+=1
            cd,sd=_get_next_item(deck, dlt, NUM_DECKS); dh.append(cd); dlt+=1
            if sp != 0 or sd != 0: raise ValueError("Draw failed, status non-zero")
            if cp is None or cd is None: raise ValueError("Draw failed, got None card")
    except Exception as e: logger.error(f"BJ deal {uid}: {e}"); update_balance(uid, bet); await q.edit_message_text(f"Ошибка ({e}). Ставка возвр."); context.user_data.pop(BJ_GAME_KEY, None); return
    p_val, dv = get_hand_value(ph), get_hand_value(dh); p_bj = (p_val == 21 and len(ph) == 2); d_bj = (dv == 21 and len(dh) == 2)
    out, st, ps = None, 'player_turn', 'active'
    if p_bj:
        ps = 'blackjack'; st = 'game_over'
        if d_bj: out = f"⚖️ Ничья! У обоих Блекджек."; update_balance(uid, bet)
        else: w = bet * BLACKJACK_PAYOUT; update_balance(uid, bet + w); out = f"✨ БЛЕКДЖЕК! ✨ Выигрыш {w:.2f} F!"
    elif d_bj: out = f"😥 У дилера Блекджек!"; st = 'game_over'
    game.update({'state':st,'deck':deck,'cards_dealt':dlt,'player_hands':[{'hand':ph,'bet':bet,'status':ps,'can_double':(not p_bj and not d_bj),'can_split':False}],'current_hand_index':0,'dealer_hand':dh,'initial_bet':bet,'split_count':0,'outcome_text':out})
    # Use chat_id from query.message.chat_id and the correct message_id from the query
    await blackjack_show_state(context, chat_id, uid, q.message.message_id)
    try: await q.answer(f"Ставка {bet} F!")
    except BadRequest: pass # Ignore if query is too old

# --- ИЗМЕНЕНА СИГНАТУРА ---
async def blackjack_show_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, msg_id: int):
    game = context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    # --- ИСПРАВЛЕНИЕ: Проверяем актуальность message_id ---
    if not game or game.get('message_id') != msg_id:
        logger.warning(f"BJ show_state called for user {user_id} with outdated message ID ({msg_id} != {game.get('message_id')}). Skipping update.")
        return
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    bal=get_balance(user_id); bal_s = f"{bal:.2f}" if bal is not None else "N/A"
    dh, phs = game.get('dealer_hand', []), game.get('player_hands', []); ci, st = game.get('current_hand_index', -1), game.get('state', '?')
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
        can_s = (len(p_h)==2 and p_h[0] and p_h[1] and get_card_value(p_h[0])==get_card_value(p_h[1]) and bal is not None and bal >= p_b and game.get('split_count',0)<MAX_SPLITS)
        act_h['can_split'] = can_s; kbd.append([InlineKeyboardButton("Еще",callback_data=f"bj_hit_{ci}"), InlineKeyboardButton("Хватит",callback_data=f"bj_stand_{ci}")])
        spc=[b for c,b in [(can_d,InlineKeyboardButton("Удв",callback_data=f"bj_double_{ci}")),(can_s,InlineKeyboardButton("Разд",callback_data=f"bj_split_{ci}"))] if c]
        if spc: kbd.append(spc)
    elif st=='game_over':
        txt+=f"\n<b>Игра окончена!</b>🎉\n{game.get('outcome_text','')}\n"; fin_b=get_balance(user_id)
        txt+=f"\nБаланс:<b>{fin_b:.2f}</b> F." if fin_b is not None else ""; kbd.append([InlineKeyboardButton("🔄Новая",callback_data="bj_new_game")])
        # Clean up game state AFTER showing result (but before next potential start)
        # We move the cleanup to determine_outcome or start_command
        # context.application.user_data[user_id].pop(BJ_GAME_KEY, None)
        # logger.info(f"BJ game state cleaned for user {user_id} after showing game over")

    elif st=='dealer_turn': txt += "\n<i>Ход дилера...</i>"
    mrk=InlineKeyboardMarkup(kbd) if kbd else None
    try:
        # --- ИСПОЛЬЗУЕМ chat_id и msg_id ---
        await context.bot.edit_message_text(chat_id, msg_id, txt, reply_markup=mrk, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Edit BJ fail {msg_id} chat {chat_id} (user {user_id}): {e}")
        # Handle 'message to edit not found' potentially by resending, but it complicates state.
        # For now, just log it.
    except Exception as e: logger.error(f"Show BJ error chat {chat_id} user {user_id}: {e}")

async def blackjack_handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list):
    q=update.callback_query; u=q.from_user; uid=u.id; chat_id = q.message.chat_id # Get chat_id
    game=context.user_data.get(BJ_GAME_KEY,{})
    act,h_idx_s = parts[0], parts[1]
    try: h_idx = int(h_idx_s)
    except: return

    # --- ИСПРАВЛЕНИЕ: Проверяем message_id перед действием ---
    if not game or game.get('state') != 'player_turn' or game.get('message_id') != q.message.message_id:
        await q.answer("Неактуальная игра/сообщение.", show_alert=False); return
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    phs=game.get('player_hands',[]);
    if not(0<=h_idx<len(phs)) or h_idx!=game.get('current_hand_index',-1): await q.answer("Ход др. руки.",show_alert=False); return
    hd=phs[h_idx];
    if not isinstance(hd,dict) or hd.get('status')!='active': await q.answer("Неактуально.",show_alert=False); return
    h,dk=hd.get('hand',[]), game.get('deck',[]); bal,b=get_balance(uid),hd.get('bet',0); dlt=game.get('cards_dealt',0); upd=False
    try: # Main action block
        if act=='hit':
            c,sc=_get_next_item(dk,dlt,NUM_DECKS);
            if sc==0 and c:
                h.append(c); game['cards_dealt']+=1; hd['can_double']=hd['can_split']=False
                hv=get_hand_value(h); await q.answer(f"{c[0]}{c[1]}")
                if hv>21: hd['status']='bust'; await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
                elif hv==21: hd['status']='stand'; await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
                else: upd=True
            else: raise IndexError("Draw fail")
        elif act=='stand':
            hd['status']='stand'; await q.answer("Стоп."); await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
        elif act=='double':
            can=(hd.get('can_double',False) and len(h)==2 and bal is not None and bal>=b)
            if can and update_balance(uid, -b) is not None:
                hd['bet']+=b; hd['can_double']=hd['can_split']=False; c,sc=_get_next_item(dk, game['cards_dealt'], NUM_DECKS)
                if sc==0 and c:
                    h.append(c); game['cards_dealt']+=1; hv=get_hand_value(h)
                    hd['status']='bust' if hv>21 else 'stand'; await q.answer(f"Удв!{c[0]}{c[1]}.Ит:{hv}{'!'*(hv>21)}")
                else:
                    hd['status']='stand'; await q.answer("Удв!Не взята карта.",show_alert=True)
                await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
            else: await q.answer("Нельзя удвоить.",show_alert=True)
        elif act=='split':
             can=hd.get('can_split', False) # Check pre-calculated flag
             if can and bal is not None and bal>=b and update_balance(uid, -b) is not None:
                 game['split_count']+=1; cm=h.pop(); nh={'hand':[cm],'bet':b,'status':'active','can_double':False,'can_split':False}; phs.insert(h_idx+1, nh); cs=[];
                 for _ in range(2): c,sc=_get_next_item(dk, game['cards_dealt'], NUM_DECKS); cs.append(c if sc==0 else None); game['cards_dealt']+= (1 if c else 0) # Increment only if card drawn
                 if cs[0]: h.append(cs[0]) # Add card1 to original hand
                 if cs[1]: nh['hand'].append(cs[1]) # Add card2 to new hand
                 is_a=get_card_value(h[0])==11
                 if is_a:
                     hd['status']=nh['status']='stand'; hd['can_double']=nh['can_double']=False; await q.answer("Тузы разд."); await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
                 else:
                     hd['can_double']=(len(h) == 2)
                     nh['can_double']=(len(nh['hand']) == 2)
                     lo=game['split_count']<MAX_SPLITS
                     hd['can_split']=(len(h)==2 and h[0] and h[1] and get_card_value(h[0])==get_card_value(h[1]) and lo)
                     nh['can_split']=(len(nh['hand'])==2 and nh['hand'][0] and nh['hand'][1] and get_card_value(nh['hand'][0])==get_card_value(nh['hand'][1]) and lo)
                     if get_hand_value(h)==21: hd['status']='stand'
                     if get_hand_value(nh['hand'])==21: nh['status']='stand'
                     await q.answer("Разделено!"); upd=True
             else: await q.answer("Нельзя разделить.",show_alert=True)
    # --- Exception Handling for Actions ---
    except IndexError: # Triggered if _get_next_item fails
         hd['status']='stand'; await q.answer("Не взять карту!",show_alert=True); await blackjack_next_action(context, chat_id, uid) # Pass chat_id, uid
    except Exception as e: logger.error(f"BJ act '{act}' u {uid}: {e}", exc_info=True); await q.answer("Ошибка.",show_alert=True)
    # Update game state message if needed
    if upd: await blackjack_show_state(context, chat_id, uid, q.message.message_id) # Pass chat_id, uid, msg_id

# --- ИЗМЕНЕНА СИГНАТУРА ---
async def blackjack_next_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state') != 'player_turn': return
    phs=game.get('player_hands',[]); ci=game.get('current_hand_index',-1)
    ni=next((i for i,h in enumerate(phs[ci+1:],start=ci+1) if isinstance(h,dict) and h.get('status')=='active'),-1)
    # --- ИСПОЛЬЗУЕМ chat_id, user_id, message_id из game ---
    message_id = game.get('message_id')
    if not message_id: logger.error(f"No message_id in game state for user {user_id}"); return

    if ni!=-1:
        game['current_hand_index']=ni
        await blackjack_show_state(context, chat_id, user_id, message_id) # Pass all IDs
    else:
        game['state']='dealer_turn'
        await blackjack_show_state(context, chat_id, user_id, message_id) # Pass all IDs
        # Pass both chat_id and user_id to job
        context.job_queue.run_once(blackjack_dealer_turn_job, DEALER_TURN_DELAY, data={'chat_id': chat_id, 'user_id': user_id}, name=f"dealer_{user_id}")

async def blackjack_dealer_turn_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data.get('user_id')
    chat_id = job_data.get('chat_id') # Get chat_id
    if not user_id or not chat_id: logger.error(f"BJ Dealer job missing IDs: {job_data}"); return

    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game or game.get('state')!='dealer_turn': return
    dk,dh,phs=game.get('deck',[]),game.get('dealer_hand',[]),game.get('player_hands',[]); dlt=game.get('cards_dealt',0)
    can_w=any(isinstance(h,dict) and h.get('status') not in ['bust','blackjack'] for h in phs); d_bj=(get_hand_value(dh)==21 and len(dh)==2)
    if not can_w and not d_bj: await blackjack_determine_outcome(context, chat_id, user_id, d_bj); return # Pass chat_id, user_id
    while True:
        dv=get_hand_value(dh); ac=sum(1 for c in dh if c and c[0]=='A'); soft=ac>0 and (dv-ac*11)<11
        if dv>17 or (dv==17 and not (soft and DEALER_HITS_SOFT_17)): logger.info(f"Дилер стоп {dv} у {user_id}."); break
        try:
            c, sc = _get_next_item(dk, dlt, NUM_DECKS)
            if sc == 0 and c: dh.append(c); game['cards_dealt'] += 1; dlt = game['cards_dealt']
            else: raise IndexError("Dealer draw fail")
        except IndexError: logger.warning(f"BJ Dealer {user_id} deck empty?"); break
    await blackjack_determine_outcome(context, chat_id, user_id, d_bj) # Pass chat_id, user_id

# --- ИЗМЕНЕНА СИГНАТУРА ---
async def blackjack_determine_outcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, d_had_bj: bool):
    game=context.application.user_data.get(user_id, {}).get(BJ_GAME_KEY)
    if not game: return
    # --- ИСПРАВЛЕНИЕ: Используем message_id из game state ---
    message_id = game.get('message_id')
    if not message_id: logger.error(f"BJ outcome: No message_id for user {user_id}"); return
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    if game.get('state')=='game_over' and 'outcome_determined' in game:
        await blackjack_show_state(context, chat_id, user_id, message_id); return # Pass IDs
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
    await blackjack_show_state(context, chat_id, user_id, message_id) # Pass IDs
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