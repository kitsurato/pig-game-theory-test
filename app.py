import eventlet
eventlet.monkey_patch()

import time, random, uuid, io, os
from flask import Flask, render_template, request, send_file, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from PIL import Image 
from game_logic import validate_defense, calculate_grade

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_key_haff_arena_v2'

# 配置 SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet', ping_timeout=60, ping_interval=25)

rooms = {}
online_users = {}
reset_timers = {}

def get_lobby_data():
    active_rooms = []
    for rid, r in rooms.items():
        active_rooms.append({
            "id": rid, "owner": r['owner'], "count": len(r['players']), "state": r['state']
        })
    return active_rooms

@app.route('/')
def index():
    return render_template('index.html')

# === 缩略图生成路由 ===
@app.route('/thumbnail/<path:filename>')
def serve_thumbnail(filename):
    try:
        file_path = os.path.join(app.root_path, 'static', 'cards', filename)
        if not os.path.exists(file_path): return "File not found", 404
        
        img = Image.open(file_path)
        if img.width <= 250: return send_from_directory(os.path.join(app.root_path, 'static', 'cards'), filename)

        base_width = 250
        w_percent = (base_width / float(img.size[0]))
        h_size = int((float(img.size[1]) * float(w_percent)))
        img = img.resize((base_width, h_size), Image.Resampling.LANCZOS)
        
        img_io = io.BytesIO()
        save_format = img.format if img.format else 'PNG'
        img.save(img_io, save_format, quality=85)
        img_io.seek(0)
        return send_file(img_io, mimetype=f'image/{save_format.lower()}')
    except Exception as e:
        print(f"Thumbnail error: {e}")
        return send_from_directory(os.path.join(app.root_path, 'static', 'cards'), filename)
# ===========================

@socketio.on('connect')
def on_connect(): pass

@socketio.on('disconnect')
def on_disconnect():
    if request.sid in online_users: del online_users[request.sid]

@socketio.on('enter_lobby')
def on_enter(data):
    uid = data.get('userId')
    if uid:
        online_users[request.sid] = uid
        emit('lobby_update', get_lobby_data())

@socketio.on('reconnect_user')
def on_reconnect(data):
    uid = data.get('userId')
    if not uid: return
    online_users[request.sid] = uid
    found_room = None
    for rid, room in rooms.items():
        if uid in room["players"]: found_room = room; break
    if found_room:
        rid = found_room["id"]
        join_room(rid)
        is_spectator = (found_room["state"] == "GAME" and uid not in found_room["players"])
        emit('join_success', {**found_room, "is_spectator": is_spectator, "is_reconnect": True})
        if found_room["state"] == "GAME":
            socketio.sleep(0.5)
            broadcast_game_state(rid, target_uid=uid)
            emit('reconnect_result', {'success': True, 'msg': f'已重连至房间 {rid}'})
        else:
            emit('room_sync', found_room)
            emit('reconnect_result', {'success': True, 'msg': '已回到准备大厅'})
    else:
        emit('reconnect_result', {'success': True, 'msg': '欢迎回来'})
        emit('lobby_update', get_lobby_data())

@socketio.on('create_room')
def on_create(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if not rid or not uid: return
    if rid in rooms: return emit('error', {'msg': '房间已存在'})
    rooms[rid] = {
        "id": rid, "players": [uid], "ready": {uid: False}, "owner": uid,
        "state": "LOBBY", "scores": {uid: 10000}, "matches": {}, "bye_player": None,
        "chat_history": [], "history": [], "summary_confirms": []
    }
    join_room(rid)
    emit('join_success', rooms[rid])
    socketio.emit('lobby_update', get_lobby_data())

@socketio.on('join_room')
def on_join(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid not in rooms: return emit('error', {'msg': '房间不存在'})
    room = rooms[rid]
    join_room(rid)
    if uid in room["players"]: pass
    elif room["state"] == "GAME":
        emit('join_success', {**room, "is_spectator": True})
        broadcast_game_state(rid, target_uid=uid)
        return
    else:
        if len(room["players"]) >= 10: return emit('error', {'msg': '房间满员'})
        room["players"].append(uid)
        room["ready"][uid] = False
        room["scores"][uid] = 10000
    emit('join_success', {**room, "is_spectator": False})
    socketio.emit('lobby_update', get_lobby_data())
    emit('room_sync', room, room=rid)

@socketio.on('leave_room')
def on_leave(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid in rooms and uid in rooms[rid]["players"]:
        room = rooms[rid]
        leave_room(rid)
        if room["state"] == "LOBBY":
            room["players"].remove(uid)
            if uid in room["ready"]: del room["ready"][uid]
            if uid in room["scores"]: del room["scores"][uid]
            if len(room["players"]) == 0: del rooms[rid]
            elif uid == room["owner"]: room["owner"] = room["players"][0]
            socketio.emit('lobby_update', get_lobby_data())
            if rid in rooms: emit('room_sync', room, room=rid)
            emit('leave_success')
        else: emit('leave_success')

@socketio.on('set_ready')
def on_ready(data):
    rid, uid = data.get('roomId'), data.get('userId')
    if rid in rooms and uid in rooms[rid]["ready"]:
        rooms[rid]["ready"][uid] = not rooms[rid]["ready"][uid]
        emit('room_sync', rooms[rid], room=rid)

@socketio.on('send_chat')
def on_chat(data):
    rid, uid, msg = data.get('roomId'), data.get('userId'), data.get('msg')
    if rid in rooms and msg:
        payload = {'user': uid, 'msg': msg, 'type': 'chat'}
        rooms[rid]['chat_history'].append(payload)
        socketio.emit('chat_message', payload, room=rid)

# --- Game Logic ---

def game_timer_task(rid, match_id, round_num, duration, timer_stamp):
    socketio.sleep(duration)
    room = rooms.get(rid)
    if not room: return
    match = room["matches"].get(match_id)
    if not match: return
    if match.get("timer_stamp") != timer_stamp: return 

    gd = match["game_data"]
    if match["round"] == round_num and gd["step"] == "SETUP":
        defender, attacker = match["defender"], match["attacker"]
        penalty = 3000
        room["scores"][defender] -= penalty
        room["scores"][attacker] += penalty
        socketio.emit('chat_message', {'user': '裁判', 'msg': f'⏱️ 防守方超时！扣除 {penalty}。', 'type': 'info'}, room=rid)
        finish_round(rid, match, reason="DEF_TIMEOUT", penalty_data={'def_delta': -penalty, 'atk_delta': penalty})
    elif match["round"] == round_num and gd["step"] in ["ATTACK_SELECT", "ATTACKING"]:
        socketio.emit('chat_message', {'user': '裁判', 'msg': '⏳ 攻方思考超时！本轮结束。', 'type': 'info'}, room=rid)
        finish_round(rid, match, reason="ATK_TIMEOUT")

@socketio.on('start_game')
def on_start(data):
    rid = data.get('roomId')
    room = rooms.get(rid)
    if not room or len(room["players"]) < 2: return emit('error', {'msg': '人数不足'})
    room["state"] = "GAME"
    players = room["players"][:]
    random.shuffle(players)
    room["matches"] = {}
    room["history"] = []
    room["bye_player"] = players.pop() if len(players) % 2 != 0 else None
    
    for i in range(0, len(players), 2):
        match_id = str(uuid.uuid4())[:8]
        t_stamp = str(uuid.uuid4())
        room["matches"][match_id] = {
            "id": match_id, "p1": players[i], "p2": players[i+1],
            "defender": players[i], "attacker": players[i+1], "round": 1,
            "timer_stamp": t_stamp,
            "game_data": { "step": "SETUP", "boxes": [], "rule": 0, "strategy": 0, "deadline": time.time() + 300 }
        }
        socketio.start_background_task(game_timer_task, rid, match_id, 1, 300, t_stamp)
    socketio.emit('lobby_update', get_lobby_data())
    broadcast_game_state(rid)

def broadcast_game_state(rid, target_uid=None):
    room = rooms.get(rid)
    if not room: return
    match_list = [{"id": m["id"], "p1": m["p1"], "p2": m["p2"], "round": m["round"], "step": m["game_data"]["step"]} for m in room["matches"].values()]
    common_data = {"match_list": match_list, "scores": room["scores"]}
    target_users = [target_uid] if target_uid else room["players"] + list(online_users.values())
    target_users = list(set(target_users))

    for p in target_users:
        sid = next((k for k,v in online_users.items() if v == p), None)
        if not sid: continue
        my_match = None
        role_info = {"role": "spectator", "match_id": None}
        if room["state"] == "GAME":
            if p == room["bye_player"]: role_info["is_bye"] = True
            else:
                for m in room["matches"].values():
                    if p == m["p1"] or p == m["p2"]:
                        my_match = m
                        role_info = {"role": "defender" if p == m["defender"] else "attacker", "match_id": m["id"]}
                        break
        if role_info["role"] == "spectator" and not my_match and len(room["matches"]) > 0:
            my_match = list(room["matches"].values())[0]
        deadline = my_match["game_data"].get("deadline", 0) if my_match else 0
        socketio.emit('game_update', {**common_data, "role_info": role_info, "match_data": my_match, "server_time": time.time(), "round_deadline": deadline}, room=sid)

@socketio.on('lock_rule')
def on_lock_rule(data):
    match = get_match(data['roomId'], data['userId'])
    if match and match["defender"] == data['userId']:
        match["game_data"]["rule"] = int(data['rule'])
        broadcast_game_state(data['roomId'])

@socketio.on('submit_defense')
def on_submit_def(data):
    rid, uid = data['roomId'], data['userId']
    room = rooms.get(rid)
    match = get_match(rid, uid)
    if not match or match["defender"] != uid: return
    boxes = data['boxes']
    rule = match["game_data"]["rule"]
    if rule == 0: return emit('error', {'msg': '请先选择规则'})
    valid, msg = validate_defense(rule, boxes, room["scores"][uid])
    if not valid: return emit('error', {'msg': msg})
    
    room["scores"][uid] -= sum([b['c10']*10 + b['c100']*100 for b in boxes])
    match["game_data"].update({"boxes": boxes, "step": "ATTACK_SELECT", "deadline": time.time() + 180})
    new_t = str(uuid.uuid4()); match["timer_stamp"] = new_t
    socketio.start_background_task(game_timer_task, rid, match["id"], match["round"], 180, new_t)
    
    public_boxes = [{"id": i, "grade": calculate_grade(b['c10']+b['c100']), "revealed": False, "real_c10": 0, "real_c100": 0, "taken": False} for i, b in enumerate(boxes)]
    match["game_data"]["public_boxes"] = public_boxes
    broadcast_game_state(rid)

@socketio.on('sync_selection_req')
def on_sync_selection(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if match and match["attacker"] == uid:
        socketio.emit('sync_selection_ui', {'match_id': match['id'], 'indices': data['indices']}, room=rid)

@socketio.on('select_strategy')
def on_select_strat(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    strat = int(data['strategy'])
    rule = match["game_data"]["rule"]
    if (strat==2 and rule!=1) or (strat==3 and rule!=2) or (strat==4 and rule!=3): return emit('error', {'msg': '策略不匹配'})
    gd = match["game_data"]
    # 策略2 8次，策略1 8次
    gd.update({"strategy": strat, "step": "ATTACKING", "attempts": 8})
    if strat == 3: gd["guesses"] = 0
    elif strat == 4: gd["s4"] = {"stage": 0, "target_x": 0, "target_y": 0, "revealed_phase1": [], "revealed_phase2": [], "wins": 0}
    broadcast_game_state(rid)

# --- S4 Logic ---
@socketio.on('s4_submit_target')
def on_s4_submit_target(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match: return
    s4 = match["game_data"].get("s4")
    target = int(data['targetNum'])
    if not s4 or not (1 <= target <= 22): return
    if s4["stage"] == 0 and match["attacker"] == uid:
        s4.update({"target_x": target, "stage": 1})
        socketio.emit('chat_message', {'user': '系统', 'msg': f'攻方寻找 {target}。', 'type': 'info'}, room=rid)
        broadcast_game_state(rid)
    elif s4["stage"] == 2 and match["defender"] == uid:
        s4.update({"target_y": target, "stage": 3})
        for pb in match["game_data"]["public_boxes"]: pb["revealed"]=False; pb["real_c10"]=0; pb["real_c100"]=0
        socketio.emit('chat_message', {'user': '系统', 'msg': f'守方反击寻找 {target}。', 'type': 'info'}, room=rid)
        broadcast_game_state(rid)

@socketio.on('s4_reveal')
def on_s4_reveal(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    gd = match["game_data"]
    s4 = gd.get("s4")
    box_idx = int(data['boxId'])
    
    # 防止在 sleep 期间的多余点击
    if (s4["stage"] == 1 and len(s4["revealed_phase1"]) >= 7) or (s4["stage"] == 3 and len(s4["revealed_phase2"]) >= 7):
        return

    def reveal(idx_list):
        if box_idx in idx_list: return False
        idx_list.append(box_idx)
        real = gd["boxes"][box_idx]
        gd["public_boxes"][box_idx].update({"revealed": True, "real_c10": real['c10'], "real_c100": real['c100']})
        return True

    # 1. 执行揭示并立即广播
    updated = False
    if s4["stage"] == 1: updated = reveal(s4["revealed_phase1"])
    elif s4["stage"] == 3: updated = reveal(s4["revealed_phase2"])
    
    if updated:
        broadcast_game_state(rid)
        
        # 2. 如果达到了7个，暂停3秒展示结果，然后清空桌面进入下一阶段
        if s4["stage"] == 1 and len(s4["revealed_phase1"]) >= 7:
            socketio.sleep(3) # 暂停等待用户看清
            
            found = any(gd["boxes"][i]['c10'] == s4["target_x"] for i in s4["revealed_phase1"])
            if found: s4["wins"] += 1
            socketio.emit('chat_message', {'user': '系统', 'msg': f"{'✅' if found else '❌'} 目标[{s4['target_x']}] {'找到' if found else '未找到'}", 'type': 'info'}, room=rid)
            
            # 隐藏所有盒子进入下一阶段
            for pb in gd["public_boxes"]: pb["revealed"] = False
            s4["stage"] = 2
            broadcast_game_state(rid)
            
        elif s4["stage"] == 3 and len(s4["revealed_phase2"]) >= 7:
            socketio.sleep(3) # 暂停等待用户看清
            
            found = any(gd["boxes"][i]['c10'] == s4["target_y"] for i in s4["revealed_phase2"])
            if found: s4["wins"] += 1
            socketio.emit('chat_message', {'user': '系统', 'msg': f"{'✅' if found else '❌'} 目标[{s4['target_y']}] {'找到' if found else '未找到'}", 'type': 'info'}, room=rid)
            
            # 隐藏所有盒子进入结算阶段
            for pb in gd["public_boxes"]: pb["revealed"] = False
            s4["stage"] = 4
            broadcast_game_state(rid)

@socketio.on('s4_execute_pick')
def on_s4_execute_pick(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    s4 = match["game_data"].get("s4")
    if not s4 or s4["stage"] != 4: return
    indices = data['pickIndices']
    limit = 22 if s4["wins"] == 2 else (7 if s4["wins"] == 1 else 5)
    if limit != 22 and len(indices) != limit: return emit('error', {'msg': f'请选择 {limit} 个盒子'})
    
    profit = 0
    for idx in indices:
        if idx < 0 or idx >= 22: continue
        raw = match["game_data"]["boxes"][idx]
        if raw.get('taken', False): continue
        profit += raw['c10']*10 + raw['c100']*100
        match["game_data"]["public_boxes"][idx].update({"revealed": True, "taken": True, "real_c10": raw['c10'], "real_c100": raw['c100']})
        raw.update({'c10': 0, 'c100': 0, 'taken': True})
    
    rooms[rid]["scores"][match["attacker"]] += profit
    socketio.emit('chat_message', {'user': '系统', 'msg': f'方案D结算：掠夺获得 {profit}', 'type': 'info'}, room=rid)
    finish_round(rid, match, reason="NORMAL", penalty_data={'atk_delta': profit})

@socketio.on('execute_attack')
def on_attack(data):
    rid, uid = data['roomId'], data['userId']
    match = get_match(rid, uid)
    if not match or match["attacker"] != uid: return
    gd = match["game_data"]; boxes = gd["boxes"]; strat = gd["strategy"]
    profit = 0; done = False
    
    if strat == 1:
        idx = data['boxId']
        raw = boxes[idx]
        profit = raw['c10']*10 + raw['c100']*100
        gd["public_boxes"][idx].update({"revealed":True, "taken":True, "real_c10":raw['c10'], "real_c100":raw['c100']})
        raw.update({'c10':0, 'c100':0, 'taken':True})
        gd["attempts"] -= 1
        if gd["attempts"] <= 0: done = True
        
    elif strat == 2:
        iA, iB, guess = data['boxA'], data['boxB'], data['guess']
        valA, valB = boxes[iA]['c10']*10+boxes[iA]['c100']*100, boxes[iB]['c10']*10+boxes[iB]['c100']*100
        win = (guess == 'more' and valB >= valA) or (guess == 'less' and valB < valA)
        
        # 优化：明确反馈比大小结果
        res_text = "成功" if win else "失败"
        res_icon = "✅" if win else "❌"
        socketio.emit('chat_message', {'user': '系统', 'msg': f'{res_icon} 猜测{res_text} ({valA} vs {valB})', 'type': 'info'}, room=rid)
        
        if win:
            # 猜对：掠夺资金
            for i in [iA, iB]:
                gd["public_boxes"][i].update({"revealed":True, "taken":True, "real_c10":boxes[i]['c10'], "real_c100":boxes[i]['c100']})
                boxes[i].update({'c10':0, 'c100':0, 'taken':True})
            profit = valA + valB
        else:
            # 猜错：仅展示，不销毁，资金保留用于回合退款
            for i in [iA, iB]:
                gd["public_boxes"][i].update({"revealed":True, "real_c10":boxes[i]['c10'], "real_c100":boxes[i]['c100']})
        
        gd["attempts"] -= 1
        if gd["attempts"] <= 0: done = True
        
    elif strat == 3:
        g = int(data['guessIdx']); gd["guesses"] = gd.get("guesses", 0) + 1
        spec = next((i for i, b in enumerate(boxes) if b['c100'] > 0), 0)
        if g == spec:
            if gd["guesses"] in [1,2,3,7,8]:
                for i in range(22):
                    profit += boxes[i]['c10']*10 + boxes[i]['c100']*100
                    gd["public_boxes"][i].update({"revealed":True, "taken":True, "real_c10":boxes[i]['c10'], "real_c100":boxes[i]['c100']})
                    boxes[i].update({'c10':0, 'c100':0, 'taken':True})
            else:
                gd["public_boxes"][spec].update({"revealed":True, "taken":True, "real_c10":boxes[spec]['c10'], "real_c100":boxes[spec]['c100']})
                boxes[spec].update({'c10':0, 'c100':0, 'taken':True})
            done = True
        else: socketio.emit('strat3_hint', {'hint': "大了" if g > spec else "小了", 'count': gd["guesses"]}, room=rid)
    
    if profit > 0: rooms[rid]["scores"][match["attacker"]] += profit
    
    if done:
        broadcast_game_state(rid)
        socketio.sleep(1) # 短暂延迟确保前端渲染完最后一帧
        finish_round(rid, match, reason="NORMAL", penalty_data={'atk_delta': profit})
    else:
        broadcast_game_state(rid)

def finish_round(rid, match, reason="NORMAL", penalty_data=None):
    room = rooms.get(rid)
    if not room: return
    
    # 增加状态锁：防止Race Condition导致双重退款
    gd = match["game_data"]
    if gd.get("step") == "FINISHING": return
    gd["step"] = "FINISHING"

    refund = sum([b['c10']*10 + b['c100']*100 for b in match["game_data"]["boxes"] if not b.get('taken', False)])
    room["scores"][match["defender"]] += refund
    room["history"].append({
        "round": match["round"], "defender": match["defender"], "attacker": match["attacker"],
        "rule": match["game_data"]["rule"], "strat": match["game_data"]["strategy"],
        "result": reason, "pnl_atk": penalty_data['atk_delta'] if penalty_data else 0, "pnl_def": penalty_data['def_delta'] if penalty_data else 0
    })
    socketio.emit('round_summary', {"round": match["round"], "refund": refund, "reason": reason}, room=rid)
    socketio.sleep(4)
    if match["round"] >= 6: handle_game_over(rid)
    else:
        match["defender"], match["attacker"] = match["attacker"], match["defender"]
        match["round"] += 1
        new_t = str(uuid.uuid4()); match["timer_stamp"] = new_t
        match["game_data"] = { "step": "SETUP", "boxes": [], "rule": 0, "strategy": 0, "deadline": time.time() + 300 }
        socketio.start_background_task(game_timer_task, rid, match["id"], match["round"], 300, new_t)
        broadcast_game_state(rid)

def handle_game_over(rid):
    room = rooms.get(rid)
    if not room: return
    winner = max(room["scores"], key=room["scores"].get)
    socketio.emit('show_game_summary', {"history": room["history"], "scores": room["scores"], "winner": winner}, room=rid)
    tid = str(uuid.uuid4()); reset_timers[rid] = tid
    socketio.start_background_task(auto_reset_task, rid, tid)

def auto_reset_task(rid, tid):
    socketio.sleep(180)
    if rid in reset_timers and reset_timers[rid] == tid: reset_room_logic(rid)

@socketio.on('confirm_summary')
def on_confirm_summary(data):
    rid, uid = data.get('roomId'), data.get('userId')
    room = rooms.get(rid)
    if room:
        if uid not in room['summary_confirms']: room['summary_confirms'].append(uid)
        count = len(room['summary_confirms'])
        socketio.emit('update_confirm_count', count, room=rid)
        if count >= len(room['players']): reset_room_logic(rid)

def get_match(rid, uid):
    room = rooms.get(rid)
    if not room: return None
    for m in room["matches"].values():
        if uid in [m["p1"], m["p2"]]: return m
    return None

def reset_room_logic(rid):
    room = rooms.get(rid)
    if not room: return
    if rid in reset_timers: del reset_timers[rid]
    room['state'] = "LOBBY"; room['matches'] = {}; room['history'] = []
    room['summary_confirms'] = []; room['scores'] = {p: 10000 for p in room['players']}
    room['ready'] = {p: False for p in room['players']}
    socketio.emit('reset_to_lobby', room=rid)
    socketio.emit('lobby_update', get_lobby_data())
    socketio.emit('room_sync', room, room=rid)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)