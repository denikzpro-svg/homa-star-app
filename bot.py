import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.exceptions import TelegramForbiddenError
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= CORE CONFIG =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"  # Публичный канал для турнирных постов
WEB_APP_URL = "https://homa-star-app.vercel.app"
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
router = Router()

# ================= DATABASE LAYER =================
client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"]
queue_col = db["queue"]

async def setup_db_indexes():
    await users_col.create_index("user_id", unique=True)
    await battles_col.create_index("match_id", unique=True)
    logger.info("⚡ [DB] Индексы и коллекции синхронизированы.")

async def get_or_create_user(user_id: int, username: str, first_name: str):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username or "Operator",
            "first_name": first_name or "Challenger",
            "rating": 1200,
            "wins": 0,
            "losses": 0
        }
        await users_col.insert_one(user)
    return user

async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "creator", "administrator"]
    except Exception:
        return False

# ================= UI / KITS =================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ НАЧАТЬ БЛИЦ-ТУРНИР", callback_data="start_blitz")],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="my_profile"),
                InlineKeyboardButton(text="🏆 Зал Славы", callback_data="top_players")
            ],
            [InlineKeyboardButton(text="🚀 КИБЕР-ПРИЛОЖЕНИЕ", web_app=WebAppInfo(url=WEB_APP_URL))]
        ]
    )

def sub_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на Арену", url=f"https://t.me/{CHANNEL_ID[1:]}")],
            [InlineKeyboardButton(text="🔄 Верифицировать доступ", callback_data="check_sub")]
        ]
    )

def battle_kb(match_id: str, name_a: str, votes_a: int, name_b: str, votes_b: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🔥 {name_a} [{votes_a}]", callback_data=f"vote_{match_id}_a"),
                InlineKeyboardButton(text=f"🔥 {name_b} [{votes_b}]", callback_data=f"vote_{match_id}_b")
            ]
        ]
    )

# ================= HANDLERS =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    user_id = message.from_user.id
    await get_or_create_user(user_id, message.from_user.username, message.from_user.first_name)
    is_subbed = await check_subscription(bot, user_id)

    if not is_subbed:
        await message.answer("🛡️ **STAR ARENA // SYSTEM GATEWAY**\n\nДля доступа к турнирам подпишитесь на канал:", reply_markup=sub_kb(), parse_mode="Markdown")
        return

    await message.answer("⚡ **STAR ARENA // COMMAND CENTER**\n\nВыберите протокол взаимодействия:", reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Подписка не обнаружена!", show_alert=True)
    await callback.message.edit_text("✅ **Доступ разблокирован.**", reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⚡ **STAR ARENA // COMMAND CENTER**\n\nВыберите протокол взаимодействия:", reply_markup=main_menu_kb(), parse_mode="Markdown")

# ================= MATCHMAKING & CHANNEL POSTS =================
async def create_match(bot: Bot, player_a: dict, player_b: dict):
    match_id = uuid.uuid4().hex[:8]
    
    # Турнир длится ровно 2 часа с момента создания
    ends_at = datetime.now(timezone.utc) + timedelta(hours=2)
    ends_str = ends_at.strftime("%H:%M МСК")

    match_doc = {
        "match_id": match_id,
        "player_a": {"id": player_a["id"], "name": player_a["name"], "votes": 0},
        "player_b": {"id": player_b["id"], "name": player_b["name"], "votes": 0},
        "voted_users": [],
        "status": "active",
        "ends_at": ends_at
    }
    
    # Формируем публичный пост для канала
    post_text = (
        f"⚔️ **ЭПИЧЕСКИЙ БЛИЦ-ТУРНИР НА АРЕНЕ** ⚔️\n"
        f"-----------------------------------------\n"
        f"🔴 Оператор 1: **{player_a['name']}**\n"
        f"🔵 Оператор 2: **{player_b['name']}**\n\n"
        f"⏳ **Турнир длится до:** `{ends_str}`\n"
        f"📢 Голосуйте за своего фаворита прямо в посте ниже!"
    )
    
    markup = battle_kb(match_id, player_a['name'], 0, player_b['name'], 0)
    
    try:
        sent_msg = await bot.send_message(CHANNEL_ID, post_text, reply_markup=markup, parse_mode="Markdown")
        match_doc["channel_message_id"] = sent_msg.message_id
    except Exception as e:
        logger.error(f"Не удалось отправить пост в канал: {e}")

    await battles_col.insert_one(match_doc)
    
    # Уведомляем участников в личку
    for p in [player_a, player_b]:
        try:
            await bot.send_message(p["id"], f"⚔️ **Ваш Блиц-турнир создан и опубликован в канале!** Переживайте за каждый голос.")
        except TelegramForbiddenError:
            pass

@router.callback_query(F.data.startswith("vote_"))
async def handle_channel_vote(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    match_id = parts[1]
    target_side = parts[2] # 'a' или 'b'
    user_id = callback.from_user.id

    match = await battles_col.find_one({"match_id": match_id, "status": "active"})
    if not match:
        return await callback.answer("❌ Этот турнирный раунд уже завершен.", show_alert=True)
    
    if user_id in match.get("voted_users", []):
        return await callback.answer("⚠️ Вы уже отдали свой голос в этом матче!", show_alert=True)

    player_key = "player_a" if target_side == "а" or target_side == "a" else "player_b"
    
    await battles_col.update_one(
        {"match_id": match_id},
        {"$inc": {f"{player_key}.votes": 1}, "$push": {"voted_users": user_id}}
    )

    # Пересчитываем актуальные голоса для обновления кнопок в канале
    updated_match = await battles_col.find_one({"match_id": match_id})
    p_a = updated_match["player_a"]
    p_b = updated_match["player_b"]

    new_markup = battle_kb(match_id, p_a["name"], p_a["votes"], p_b["name"], p_b["votes"])
    
    try:
        await callback.message.edit_reply_markup(reply_markup=new_markup)
    except Exception:
        pass

    await callback.answer("✅ Ваш голос успешно учтен системой!")

@router.callback_query(F.data == "start_blitz")
async def join_blitz(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    user_name = callback.from_user.first_name
    
    active_match = await battles_col.find_one({
        "$or": [{"player_a.id": user_id}, {"player_b.id": user_id}], 
        "status": "active"
    })
    if active_match:
        return await callback.answer("⚠️ У вас уже активен турнирный бой!", show_alert=True)
    
    await queue_col.delete_many({"id": user_id})
    opponent = await queue_col.find_one_and_delete({"id": {"$ne": user_id}})
    
    if opponent:
        await callback.message.edit_text("⚡ **Оппонент найден! Пост с турниром опубликован в канале.**", parse_mode="Markdown")
        await create_match(bot, {"id": opponent["id"], "name": opponent["name"]}, {"id": user_id, "name": user_name})
    else:
        await queue_col.insert_one({"id": user_id, "name": user_name, "timestamp": datetime.now(timezone.utc)})
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ **Поиск Блиц-турнира... Ожидание второго участника.**", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    await callback.answer()
    await queue_col.delete_many({"id": callback.from_user.id})
    await callback.message.edit_text("🛑 Поиск турнира отменен.", reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    await callback.answer()
    user = await users_col.find_one({"user_id": callback.from_user.id})
    rating = user.get("rating", 1200) if user else 1200
    wins = user.get("wins", 0) if user else 0
    losses = user.get("losses", 0) if user else 0
    
    text = (
        f"👤 **ПРОФИЛЬ ОПЕРАТОРА**\n"
        f"-----------------------------------------\n"
        f"🏆 Рейтинг: `{rating} MMR`\n"
        f"⚡ Побед: `{wins}` | Поражений: `{losses}`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в хаб", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "top_players")
async def show_leaderboard(callback: CallbackQuery):
    await callback.answer()
    top_users = await users_col.find().sort("rating", -1).limit(10).to_list(10)
    text = "🏆 **ЗАЛ СЛАВЫ // TOP-10**\n-----------------------------------------\n"
    for i, u in enumerate(top_users):
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"`#{i+1}`"
        text += f"{medal} **{u.get('first_name', 'Operator')}** — `{u.get('rating', 1200)} MMR`\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в хаб", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ================= CRON: ПРОВЕРКА ЗАВЕРШЕНИЯ ТУРНИРОВ =================
async def check_finished_battles(bot: Bot):
    now = datetime.now(timezone.utc)
    active_matches = await battles_col.find({"status": "active", "ends_at": {"$lte": now}}).to_list(None)
    
    for match in active_matches:
        p_a = match["player_a"]
        p_b = match["player_b"]
        
        # Определяем победителя по голосам
        if p_a["votes"] > p_b["votes"]:
            winner, loser = p_a, p_b
        elif p_b["votes"] > p_a["votes"]:
            winner, loser = p_b, p_a
        else:
            winner = loser = None  эпичная ничья

        # Закрываем матч в базе
        await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "finished"}})

        # Обновляем статистику в БД
        if winner:
            await users_col.update_one({"user_id": winner["id"]}, {"$inc": {"wins": 1, "rating": 25}})
            await users_col.update_one({"user_id": loser["id"]}, {"$inc": {"losses": 1, "rating": -20}})
            result_text = f"🏁 **ТУРНИР ЗАВЕРШЕН!**\n\n🏆 Победитель: **{winner['name']}** ({winner['votes']} голосов)\n💀 Проигравший: **{loser['name']}** ({loser['votes']} голосов)"
        else:
            result_text = f"🏁 **ТУРНИР ЗАВЕРШЕН!**\n\n🤝 Ничья! Оба оператора показали равное влияние."

        # Редактируем пост в канале, убирая кнопки и подбивая итоги
        if "channel_message_id" in match:
            try:
                await bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=match["channel_message_id"],
                    text=result_text,
                    parse_mode="Markdown"
                )
            except Exception:
                pass

# ================= SYSTEM BOOT =================
async def handle_ping(request):
    return web.Response(text="STAR_ARENA_NODE_OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    await setup_db_indexes()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Планировщик проверяет завершение турниров каждую минуту
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_finished_battles, 'interval', minutes=1, args=[bot])
    scheduler.start()

    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🚀 [SYSTEM] Система турниров с публикацией в канал активирована.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
