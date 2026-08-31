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

# ================= НАСТРОЙКИ СЕРВЕРА И БАЗЫ =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"
ADMIN_ID = 7910818906
WEB_APP_URL = "https://homa-star-app.vercel.app"

logging.basicConfig(level=logging.INFO)
router = Router()

client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"] 

MSK_TZ = timezone(timedelta(hours=3))
search_queue = [] # Оперативная память матчмейкинга

# ================= БАЗОВЫЕ ФУНКЦИИ =================
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

# ================= ИНТЕРФЕЙСЫ (UI/UX) =================
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

# ================= СТАРТ И ГОЛОСОВАНИЕ (DEEP-LINKS) =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    args = message.text.split()
    user_id = message.from_user.id
    user = await get_user(user_id, message.from_user.username)
    is_subbed = await check_subscription(bot, user_id)

    # Логика голосования по ссылке друга
    if len(args) > 1 and args[1].startswith("vote_"):
        if not is_subbed:
            await message.answer("⚠️ Чтобы голос засчитался, подпишись на канал, а затем **снова нажми на ссылку друга**!", reply_markup=sub_kb())
            return
            
        try:
            _, match_id, target_id_str = args[1].split("_")
            target_id = int(target_id_str)
            
            match = await battles_col.find_one({"match_id": match_id, "status": "active"})
            if not match:
                return await message.answer("❌ Этот матч уже завершен или не существует.")
            
            if user_id in match.get("voted_users", []):
                return await message.answer("⚠️ Ты уже отдал голос в этой битве!")
                
            player_key = "player_a" if match["player_a"]["id"] == target_id else "player_b"
            await battles_col.update_one(
                {"match_id": match_id},
                {"$inc": {f"{player_key}.votes": 1}, "$push": {"voted_users": user_id}}
            )
            await message.answer("✅ Голос успешно засчитан! Нажми /start, чтобы самому выйти на Арену.")
            await bot.send_message(target_id, "🔥 +1 голос! Кто-то поддержал тебя по ссылке.")
            return
        except Exception:
            return await message.answer("❌ Ошибка при обработке голоса.")

    # Логика обычного входа
    if not is_subbed:
        await message.answer("👋 Привет! Добро пожаловать в Звёздную Арену. Подпишись на канал для старта:", reply_markup=sub_kb())
        return

    await message.answer(
        f"🔥 **ГЛАВНОЕ МЕНЮ**\nТвоя фракция: **{user['faction']}**\n\nВыбирай действие ниже:",
        reply_markup=main_menu_kb(), parse_mode="Markdown"
    )

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Сначала подпишись на канал!", show_alert=True)
    await callback.message.edit_text("✅ Подписка подтверждена!", reply_markup=main_menu_kb())

# ================= МАТЧМЕЙКИНГ И БОИ =================
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
        if uid != 0: # 0 = Системный бот (заглушка)
            ref_link = f"https://t.me/{bot_info.username}?start=vote_{match_id}_{uid}"
            msg = (f"⚔️ **РАУНД {stage} НАЧАЛСЯ!**\n\n"
                   f"🎯 **Цель:** Собрать **{threshold} голосов**.\n\n"
                   f"🔗 **Твоя боевая ссылка:**\n`{ref_link}`\n\n"
                   f"Копируй её и отправляй друзьям. Удачи!")
            await bot.send_message(uid, msg, parse_mode="Markdown")

async def npc_waiter(user_id: int, bot: Bot):
    await asyncio.sleep(15)
    if user_id in search_queue:
        search_queue.remove(user_id)
        await create_arena_match(bot, user_id, 0, stage=1, threshold=5)

@router.callback_query(F.data == "search_arena")
async def join_arena(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    now = datetime.now(MSK_TZ)
    
    # Блокировка входа за 30 минут до конца раунда
    if now.hour in [11, 13, 15, 17] and now.minute >= 30:
        return await callback.answer("⏳ Регистрация закрыта. Жди старта следующего турнира!", show_alert=True)
    
    if await battles_col.find_one({"$or": [{"player_a.id": user_id}, {"player_b.id": user_id}], "status": "active"}):
        return await callback.answer("❌ Ты уже в бою! Копируй ссылку и собирай голоса.", show_alert=True)
    
    if search_queue and search_queue[0] != user_id:
        opponent_id = search_queue.pop(0)
        await callback.message.edit_text("🔥 Соперник найден! Арена открыта.")
        await create_arena_match(bot, user_id, opponent_id, stage=1, threshold=5)
    else:
        if user_id not in search_queue:
            search_queue.append(user_id)
            asyncio.create_task(npc_waiter(user_id, bot))
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ **Ищем достойного противника...**", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in search_queue:
        search_queue.remove(user_id)
    await callback.message.edit_text("❌ Поиск отменен.", reply_markup=main_menu_kb())

# ================= ТАЙМЕРЫ И ПЕРЕХОДЫ РАУНДОВ =================
async def trigger_round_end(bot: Bot, current_stage: int, required_votes: int, next_stage_votes: int):
    survivors = []
    
    async for match in battles_col.find({"stage": current_stage, "status": "active"}):
        for player in [match["player_a"], match["player_b"]]:
            uid = player["id"]
            if uid != 0:
                if player["votes"] >= required_votes:
                    survivors.append(uid)
                else:
                    await bot.send_message(uid, "❌ Время вышло. Ты не пробил порог голосов и выбываешь!")
        await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "finished"}})

    if next_stage_votes == 0:
        for surv in survivors:
            await users_col.update_one({"user_id": surv}, {"$inc": {"balance": 100}})
            await bot.send_message(surv, "🏆 **ПОБЕДА В ГРАНД-ФИНАЛЕ!**\nНачислено 100 ⭐!", parse_mode="Markdown")
        return

    random.shuffle(survivors)
    if len(survivors) % 2 != 0:
        survivors.append(0) 

    for i in range(0, len(survivors), 2):
        await create_arena_match(bot, survivors[i], survivors[i+1], stage=current_stage+1, threshold=next_stage_votes)

# ================= ВЕБ-СЕРВЕР И ЗАПУСК =================
@router.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "withdraw":
            amount = data.get("amount")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"appr_{message.from_user.id}_{amount}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rejl_{message.from_user.id}_{amount}")]
            ])
            text = f"💸 **ЗАЯВКА НА ВЫВОД**\nИгрок: {message.from_user.id}\nСумма: {amount} ⭐"
            await message.bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
            await message.answer(f"✅ Заявка на {amount} ⭐ отправлена админу.")
    except Exception:
        pass

@router.callback_query(F.data.startswith("appr_"))
async def approve_withdraw(call: CallbackQuery):
    if call.from_user.id == ADMIN_ID:
        await call.message.edit_text(f"{call.message.text}\n\n✅ ВЫПЛАЧЕНО")

@router.callback_query(F.data.startswith("rejl_"))
async def reject_withdraw(call: CallbackQuery):
    if call.from_user.id == ADMIN_ID:
        await call.message.edit_text(f"{call.message.text}\n\n❌ ОТКЛОНЕНО")

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(trigger_round_end, 'cron', hour=12, minute=0, args=[bot, 1, 5, 10])
    scheduler.add_job(trigger_round_end, 'cron', hour=14, minute=0, args=[bot, 2, 10, 25])
    scheduler.add_job(trigger_round_end, 'cron', hour=16, minute=0, args=[bot, 3, 25, 50])
    scheduler.add_job(trigger_round_end, 'cron', hour=18, minute=0, args=[bot, 4, 50, 0])
    scheduler.start()

    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
