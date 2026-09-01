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

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"
ADMIN_ID = 7910818906
WEB_APP_URL = "https://homa-star-app.vercel.app"
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
router = Router()

# ================= БАЗА ДАННЫХ =================
client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"]
queue_col = db["queue"]

async def setup_db_indexes():
    await users_col.create_index("user_id", unique=True)
    await battles_col.create_index("match_id", unique=True)
    logger.info("БД: Индексы успешно инициализированы.")

# ================= ПОЛЬЗОВАТЕЛИ =================
async def get_or_create_user(user_id: int, username: str, first_name: str):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username or "Unknown",
            "first_name": first_name or "Player",
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

# ================= UI =================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Начать Блиц-турнир", callback_data="start_blitz")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="my_profile"),
             InlineKeyboardButton(text="🏆 Топ игроков", callback_data="top_players")],
            [InlineKeyboardButton(text="🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ", web_app=WebAppInfo(url=WEB_APP_URL))]
        ]
    )

def sub_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на Арену", url=f"https://t.me/{CHANNEL_ID[1:]}")],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
        ]
    )

# ================= ОБРАБОТЧИКИ =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    args = message.text.split()
    user_id = message.from_user.id
    
    await get_or_create_user(user_id, message.from_user.username, message.from_user.first_name)
    is_subbed = await check_subscription(bot, user_id)

    if len(args) > 1 and args[1].startswith("vote_"):
        if not is_subbed:
            await message.answer("⚠️ Подпишись на канал, чтобы голос засчитался!", reply_markup=sub_kb())
            return
        try:
            _, match_id, target_id_str = args[1].split("_")
            target_id = int(target_id_str)
            
            if user_id == target_id:
                return await message.answer("❌ За себя голосовать нельзя!")

            match = await battles_col.find_one({"match_id": match_id, "status": "active"})
            if not match:
                return await message.answer("❌ Матч завершен.")
            
            if user_id in match.get("voted_users", []):
                return await message.answer("⚠️ Ты уже голосовал!")
                
            player_key = "player_a" if match["player_a"]["id"] == target_id else "player_b"
            
            await battles_col.update_one(
                {"match_id": match_id},
                {"$inc": {f"{player_key}.votes": 1}, "$push": {"voted_users": user_id}}
            )
            await message.answer("✅ Голос успешно засчитан!", reply_markup=main_menu_kb())
            return
        except Exception:
            return await message.answer("❌ Ошибка обработки.")

    if not is_subbed:
        await message.answer("👋 Подпишись на канал для доступа к арене:", reply_markup=sub_kb())
        return

    await message.answer("🔥 **ГЛАВНОЕ МЕНЮ АРЕНЫ**", reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Подписка не найдена!", show_alert=True)
    await callback.message.edit_text("✅ Подписка подтверждена!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("🔥 **ГЛАВНОЕ МЕНЮ АРЕНЫ**", reply_markup=main_menu_kb(), parse_mode="Markdown")

# ================= МАТЧМЕЙКИНГ =================
async def create_match(bot: Bot, player_a: dict, player_b: dict):
    match_id = uuid.uuid4().hex[:8]
    
    await battles_col.insert_one({
        "match_id": match_id,
        "player_a": {"id": player_a["id"], "name": player_a["name"], "votes": 0},
        "player_b": {"id": player_b["id"], "name": player_b["name"], "votes": 0},
        "voted_users": [],
        "status": "active"
    })
    
    bot_info = await bot.get_me()
    
    for p in [player_a, player_b]:
        ref_link = f"https://t.me/{bot_info.username}?start=vote_{match_id}_{p['id']}"
        msg = f"⚔️ **БИТВА НАЧАЛАСЬ!**\n\n🔗 Твоя ссылка для сбора голосов:\n`{ref_link}`"
        try:
            await bot.send_message(p["id"], msg, parse_mode="Markdown")
        except TelegramForbiddenError:
            pass

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
        return await callback.answer("❌ Ты уже в активном бою!", show_alert=True)
    
    await queue_col.delete_many({"id": user_id})
    opponent = await queue_col.find_one()
    
    if opponent and opponent["id"] != user_id:
        await queue_col.delete_one({"_id": opponent["_id"]})
        await callback.message.edit_text("🔥 Соперник найден! Бой создан.")
        await create_match(bot, {"id": opponent["id"], "name": opponent["name"]}, {"id": user_id, "name": user_name})
    else:
        await queue_col.insert_one({"id": user_id, "name": user_name})
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ Ищем соперника...", reply_markup=kb)

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    await callback.answer()
    await queue_col.delete_many({"id": callback.from_user.id})
    await callback.message.edit_text("❌ Поиск отменен.", reply_markup=main_menu_kb())

@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    await callback.answer()
    user = await users_col.find_one({"user_id": callback.from_user.id})
    wins = user.get("wins", 0) if user else 0
    losses = user.get("losses", 0) if user else 0
    text = f"👤 **ПРОФИЛЬ**\n\nПобед: {wins}\nПоражений: {losses}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "top_players")
async def show_leaderboard(callback: CallbackQuery):
    await callback.answer()
    top_users = await users_col.find().sort("wins", -1).limit(10).to_list(10)
    text = "🏆 **ТОП ИГРОКОВ**\n\n"
    for i, u in enumerate(top_users):
        text += f"{i+1}. {u.get('first_name', 'Player')} — {u.get('wins', 0)} побед\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ================= ЗАПУСК =================
async def handle_ping(request):
    return web.Response(text="OK")

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

    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
