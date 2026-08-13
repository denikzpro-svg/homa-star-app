import time
import random
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Состояние арены
game_state = {
    "status": "WAITING",  # WAITING (ставки) или PLAYING (пинг-понг)
    "time_left": 20,
    "bank": 0,
    "players": [],  # Список игроков и ботов с их ставками и цветами
    "winner": None
}

# База ботов для авто-массовки
BOT_NAMES = ['Max_99', 'Илья', 'Vityok', 'Artem', '@crypto_bull', 'Ton_Hunter', '0x_Whale']
ARENA_COLORS = ['#00F3FF', '#FF2E93', '#A855F7', '#00FF88', '#FF6600']

def run_game_loop():
    while True:
        # 1. ФАЗА СТАВОК (20 секунд)
        game_state["status"] = "WAITING"
        game_state["time_left"] = 20
        game_state["bank"] = 0
        game_state["players"] = []
        game_state["winner"] = None
        
        # Добавляем стартового бота
        add_bot()

        while game_state["time_left"] > 0:
            socketio.emit('game_tick', game_state)
            
            # Рандомный долив ботов во время таймера
            if len(game_state["players"]) < 4 and game_state["time_left"] > 3 and random.random() > 0.6:
                add_bot()

            time.sleep(1)
            game_state["time_left"] -= 1

        # 2. ФАЗА ИГРЫ (Пинг-понг / Определение победителя)
        game_state["status"] = "PLAYING"
        
        # Если никто не поставил, добавляем ботов, чтобы банк не был пустым
        if not game_state["players"]:
            add_bot()

        # Честный выбор победителя по весу ставок
        winning_rand = random.uniform(0, game_state["bank"])
        current_sum = 0
        winner = game_state["players"][-1]
        for p in game_state["players"]:
            current_sum += p["bet"]
            if winning_rand <= current_sum:
                winner = p
                break
        
        game_state["winner"] = winner
        
        # Рассылаем сигнал начала анимации шарика и победителя всем
        socketio.emit('game_over', game_state)
        
        # Пауза перед следующим раундом (показ результатов)
        time.sleep(4)

def add_bot():
    if len(game_state["players"]) >= 4:
        return
    name = random.choice(BOT_NAMES)
    bet = random.randint(30, 250)
    color = ARENA_COLORS[len(game_state["players"]) % len(ARENA_COLORS)]
    
    bot = {
        "id": f"bot_{random.randint(1000,9999)}",
        "name": name,
        "bet": bet,
        "color": color,
        "isUser": False
    }
    game_state["players"].append(bot)
    game_state["bank"] += bet

@socketio.on('connect')
def handle_connect():
    # Отправляем текущее состояние новому игроку сразу при входе
    emit('game_tick', game_state)

@socketio.on('place_bet')
def handle_bet(data):
    if game_state["status"] != "WAITING":
        return
    
    user_id = data.get("id")
    user_name = data.get("name", "Игрок")
    bet_amount = float(data.get("amount", 0))

    if bet_amount <= 0:
        return

    # Проверяем, не ставил ли уже этот пользователь в текущем раунде
    existing = next((p for p in game_state["players"] if p["id"] == user_id), None)
    if existing:
        return

    user_player = {
        "id": user_id,
        "name": user_name,
        "bet": bet_amount,
        "color": "#FFD700",  # Золотой цвет для реального игрока
        "isUser": True
    }
    
    game_state["players"].append(user_player)
    game_state["bank"] += bet_amount
    
    # Сразу обновляем всех участников
    socketio.emit('game_tick', game_state)

if __name__ == '__main__':
    # Запускаем фоновый игровой цикл
    socketio.start_background_task(run_game_loop)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
