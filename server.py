from gevent import monkey
monkey.patch_all()

import random
from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

game_state = {
    "status": "WAITING", # WAITING (ставки), PLAYING (полет шарика), PAUSE (ожидание)
    "time_left": 20,
    "bank": 0,
    "players": [],
    "winner": None
}

BOT_PROFILES = [
    {"name": "Alexander", "avatar": "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Maxim", "avatar": "https://images.unsplash.com/photo-1570295999919-56ceb5ecca61?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Dmitriy", "avatar": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Sergey", "avatar": "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Andrey", "avatar": "https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Vladislav", "avatar": "https://images.unsplash.com/photo-1522075469751-3a6694fb2f61?w=100&h=100&fit=crop&crop=faces"},
    {"name": "Artem", "avatar": "https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?w=100&h=100&fit=crop&crop=faces"}
]

ARENA_COLORS = ['#00F3FF', '#FF2E93', '#A855F7', '#00FF88', '#FF6600']
loop_started = False

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

def add_bot():
    if len(game_state["players"]) >= 5:
        return
    profile = random.choice(BOT_PROFILES)
    if any(p["name"] == profile["name"] for p in game_state["players"]):
        return
    bet = random.randint(30, 200)
    color = ARENA_COLORS[len(game_state["players"]) % len(ARENA_COLORS)]
    
    bot = {
        "id": f"bot_{random.randint(1000,9999)}",
        "name": profile["name"],
        "avatar": profile["avatar"],
        "bet": bet,
        "color": color,
        "isUser": False
    }
    game_state["players"].append(bot)
    game_state["bank"] += bet

def run_game_loop():
    while True:
        # 1. СТАТУС ПАУЗЫ (Ожидание игроков: 5-7 секунд после прошлого раунда)
        game_state["status"] = "PAUSE"
        game_state["bank"] = 0
        game_state["players"] = []
        game_state["winner"] = None
        socketio.emit('game_tick', game_state)
        
        pause_duration = random.randint(5, 7)
        socketio.sleep(pause_duration)

        # 2. ФАЗА СТАВОК (20 секунд)
        game_state["status"] = "WAITING"
        game_state["time_left"] = 20
        
        # Первого бота подкидываем не сразу, а с небольшой задержкой
        socketio.sleep(1)
        add_bot()

        while game_state["time_left"] > 0:
            socketio.emit('game_tick', game_state)
            
            # Боты делают ставки постепенно во время таймера
            if len(game_state["players"]) < 5 and game_state["time_left"] > 3 and random.random() > 0.4:
                add_bot()

            socketio.sleep(1)
            game_state["time_left"] -= 1

        # 3. ФАЗА ИГРЫ (Полет шарика 7 секунд)
        game_state["status"] = "PLAYING"
        
        if not game_state["players"]:
            add_bot()

        winning_rand = random.uniform(0, game_state["bank"])
        current_sum = 0
        winner = game_state["players"][-1]
        for p in game_state["players"]:
            current_sum += p["bet"]
            if winning_rand <= current_sum:
                winner = p
                break
        
        game_state["winner"] = winner
        socketio.emit('game_over', game_state)
        
        # Ждем завершения анимации шарика на клиенте (7 секунд) + показ результата (3 секунды)
        socketio.sleep(10)

@socketio.on('connect')
def handle_connect():
    global loop_started
    if not loop_started:
        loop_started = True
        socketio.start_background_task(run_game_loop)
    emit('game_tick', game_state)

@socketio.on('place_bet')
def handle_bet(data):
    if game_state["status"] != "WAITING":
        return
    
    user_id = data.get("id")
    user_name = data.get("name", "Игрок")
    try:
        bet_amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return

    if bet_amount <= 0:
        return

    existing = next((p for p in game_state["players"] if p["id"] == user_id), None)
    if existing:
        return

    user_player = {
        "id": user_id,
        "name": user_name,
        "avatar": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=100&h=100&fit=crop&crop=faces",
        "bet": bet_amount,
        "color": "#FFD700",
        "isUser": True
    }
    
    game_state["players"].append(user_player)
    game_state["bank"] += bet_amount
    socketio.emit('game_tick', game_state)

@socketio.on('cancel_bet')
def handle_cancel_bet(data):
    if game_state["status"] != "WAITING":
        return
    user_id = data.get("id")
    player = next((p for p in game_state["players"] if p["id"] == user_id and p.get("isUser")), None)
    if player:
        game_state["bank"] -= player["bet"]
        game_state["players"].remove(player)
        socketio.emit('game_tick', game_state)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
