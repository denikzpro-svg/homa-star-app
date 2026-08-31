import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= CONFIGURATION =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"
ADMIN_ID = 7910818906

WEB_APP_URL = "https://homa-star-app.vercel.app"

logging.basicConfig(level=logging.INFO)
router = Router()

# ================= DATABASE & MEMORY SETUP =================
client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"] 
withdrawals_col = db["withdrawals"]

MSK_TZ = timezone(timedelta(hours=3))

# Очередь матчмейкинга в RAM
search_queue = []

# ================= HELPER FUNCTIONS =================
async def get_user(user_id: int, username: str = "Unknown"):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        faction = random.choice(["🔴 ОГОНЬ", "🔵 ВОДА"])
        user = {
            "user_id": user_id,
            "username": username,
            "balance": 0,
            "faction": faction
        }
        await users_col.insert_one(user)
    return user

async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "creator", "administrator"]
    except Exception:
        return False

# ================= KEYBOARDS =================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ", web_app=WebAppInfo(url=WEB_APP_URL))],
            [InlineKeyboardButton(text="⚔️ Искать соперника на Арене", callback_data="search_arena")],
        ]
    )

def sub_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url="https://t.me/hamster_arenas")],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
        ]
    )

# ================= HANDLERS: START & VOTING =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    args = message.text.split()
    user_id = message.from_user.id
    user = await get_user(user_id, message.from_user.username)

    is_subbed = await check_subscription(bot, user_id)

    # ЛОГИКА ГОЛОСОВАНИЯ ПО DEEP-LINK (?start=vote_MATCHID_USERID)
    if len(args) > 1 and args[1].startswith("vote_"):
        if not is_subbed:
            await message.answer("⚠️ Чтобы твой голос был засчитан, подпишись на наш канал, а затем **снова перейди по ссылке друга**!", reply_markup=sub_kb())
            return
            
        try:
            _, match_id, target_id_str = args[1].split("_")
            target_id = int(target_id_str)
            
            # Проверка матча
            match = await battles_col.find_one({"match_id": match_id, "status": "active"})
            if not match:
                return await message.answer("❌ Этот матч уже завершен или не существует.")
            
            if user_id in match.get("voted_users", []):
                return await message.answer("⚠️ Ты уже голосовал в этом матче!")
                
            # Инкремент голоса
            player_key = "player_a" if match["player_a"]["id"] == target_id else "player_b"
            await battles_col.update_one(
                {"match_id": match_id},
                {
                    "$inc": {f"{player_key}.votes": 1},
                    "$push": {"voted_users": user_id}
                }
            )
            await message.answer("✅ Твой голос успешно засчитан! Нажми /start чтобы тоже начать играть.")
            await bot.send_message(target_id, "🔥 +1 голос! Кто-то перешел по твоей ссылке.")
            return
        except Exception as e:
            logging.error(f"Vote error: {e}")
            return await message.answer("❌ Ошибка при обработке голоса.")

    # ОБЫЧНЫЙ СТАРТ
    if not is_subbed:
        await message.answer("👋 Привет! Чтобы пользоваться ботом и играть, подпишись на наш канал:", reply_markup=sub_kb())
        return

    await message.answer(
        f"🔥 **Добро пожаловать в Звёздную Арену!**\nТвоя фракция: **{user['faction']}**\n\nНажми кнопку ниже, чтобы начать битву!",
        reply_markup=main_menu_kb(), parse_mode="Markdown",
    )

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Ты ещё не подписался на канал!", show_alert=True)
    await callback.message.edit_text("✅ Подписка подтверждена!\n\nДобро пожаловать:", reply_markup=main_menu_kb())

# ================= ARENA MATCHMAKING =================
async def create_arena_match(bot: Bot, user_a: int, user_b: int, stage: int, threshold: int):
    match_id = uuid.uuid4().hex[:8]
    
    await battles_col.insert_one({
        "match_id": match_id,
        "player_a": {"id": user_a, "votes": 0},
        "player_b": {"id": user_b, "votes": 0},
        "stage": stage,
        "threshold": threshold,
        "voted_users": [],
        "status": "active"
    })
    
    bot_info = await bot.get_me()
    
    for uid in [user_a, user_b]:
        if uid != 0: # Если не NPC
            ref_link = f"https://t.me/{bot_info.username}?start=vote_{match_id}_{uid}"
            msg = (f"⚔️ **Арена: Раунд {stage}!**\n\n"
                   f"🎯 **Цель:** Набери минимум **{threshold} голосов** до конца текущего этапа.\n\n"
                   f"🔗 **Твоя ссылка для сбора голосов:**\n`{ref_link}`\n\n"
                   f"Отправляй её друзьям и в чаты. Соперник уже начал!")
            await bot.send_message(uid, msg, parse_mode="Markdown")

async def npc_waiter(user_id: int, bot: Bot):
    """Ждет 15 сек. Если юзер все еще в поиске - создает матч с ботом."""
    await asyncio.sleep(15)
    if user_id in search_queue:
        search_queue.remove(user_id)
        await create_arena_match(bot, user_id, 0, stage=1, threshold=5)

@router.callback_query(F.data == "search_arena")
async def join_arena(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    now = datetime.now(MSK_TZ)
    
    # 1. QUEUE HOLD (Защита от позднего старта за 30 мин до конца раунда)
    if now.hour in [11, 13, 15, 17] and now.minute >= 30:
        return await callback.answer("⏳ Регистрация закрыта. Жди старта следующего турнира (в 12:00, 14:00, 16:00 или 18:00)!", show_alert=True)
    
    # 2. Проверка текущих матчей
    active = await battles_col.find_one({"$or": [{"player_a.id": user_id}, {"player_b.id": user_id}], "status": "active"})
    if active:
        return await callback.answer("❌ Ты уже участвуешь в битве!", show_alert=True)
    
    # 3. Инстант-поиск
    if search_queue and search_queue[0] != user_id:
        opponent_id = search_queue.pop(0)
        await callback.message.edit_text("🔥 Соперник найден! Матч запущен.")
        await create_arena_match(bot, user_id, opponent_id, stage=1, threshold=5)
    else:
        if user_id not in search_queue:
            search_queue.append(user_id)
            asyncio.create_task(npc_waiter(user_id, bot)) # Запускаем таймер NPC
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ **Ищем соперника...**\nОжидайте, это займет пару секунд.", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in search_queue:
        search_queue.remove(user_id)
    await callback.message.edit_text("❌ Поиск отменен.", reply_markup=main_menu_kb())

# ================= APSCHEDULER CRON LOGIC =================
async def trigger_round_end(bot: Bot, current_stage: int, required_votes: int, next_stage_votes: int):
    """Скрипт перехода раундов (12:00, 14:00, 16:00, 18:00)"""
    survivors = []
    
    async for match in battles_col.find({"stage": current_stage, "status": "active"}):
        for player in [match["player_a"], match["player_b"]]:
            uid = player["id"]
            if uid != 0:
                if player["votes"] >= required_votes:
                    survivors.append(uid)
                else:
                    await bot.send_message(uid, "❌ Время вышло. Ты не пробил порог и выбываешь из турнира!")
                    
        await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "finished"}})

    # Гранд Финал (награды)
    if next_stage_votes == 0:
        for surv in survivors:
            await users_col.update_one({"user_id": surv}, {"$inc": {"balance": 100}})
            await bot.send_message(surv, "🏆 **ПОБЕДА В ДНЕВНОМ ТУРНИРЕ!**\nНачислено 100 ⭐!", parse_mode="Markdown")
        return

    # Перемешивание для следующего раунда
    random.shuffle(survivors)
    if len(survivors) % 2 != 0:
        survivors.append(0) 

    for i in range(0, len(survivors), 2):
        await create_arena_match(bot, survivors[i], survivors[i+1], stage=current_stage+1, threshold=next_stage_votes)

# ================= МИНИ-СЕРВЕР ДЛЯ RENDER =================
async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ================= MAIN FUNCTION =================
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Планировщик турниров
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(trigger_round_end, 'cron', hour=12, minute=0, args=[bot, 1, 5, 10])
    scheduler.add_job(trigger_round_end, 'cron', hour=14, minute=0, args=[bot, 2, 10, 25])
    scheduler.add_job(trigger_round_end, 'cron', hour=16, minute=0, args=[bot, 3, 25, 50])
    scheduler.add_job(trigger_round_end, 'cron', hour=18, minute=0, args=[bot, 4, 50, 0])
    scheduler.start()

    asyncio.create_task(start_web_server())

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен. Турнирная система активна.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
