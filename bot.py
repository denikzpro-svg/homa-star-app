import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= КОНФИГУРАЦИЯ ПРОЕКТА =================
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

MSK_TZ = timezone(timedelta(hours=3))

async def setup_db_indexes():
    await users_col.create_index("user_id", unique=True)
    await battles_col.create_index("match_id", unique=True)
    await battles_col.create_index("status")
    logger.info("БД: Индексы успешно инициализированы.")

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()

async def get_or_create_user(user_id: int, username: str, first_name: str):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        faction = random.choice(["🔴 ОГОНЬ", "🔵 ВОДА"])
        user = {
            "user_id": user_id,
            "username": username or "Unknown",
            "first_name": first_name or "Player",
            "balance": 0,
            "faction": faction,
            "wins": 0,
            "losses": 0,
            "matches_played": 0,
            "registration_date": datetime.now(MSK_TZ)
        }
        await users_col.insert_one(user)
    return user

async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "creator", "administrator"]
    except Exception:
        return False

# ================= UI/UX =================
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Начать Блиц-турнир", callback_data="start_blitz")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile"),
         InlineKeyboardButton(text="🏆 Топ игроков", callback_data="top_players")],
        [InlineKeyboardButton(text="🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ", web_app=WebAppInfo(url=WEB_APP_URL))]
    ])

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на Арену", url=f"https://t.me/{CHANNEL_ID[1:]}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика сервера", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="◀️ Выйти", callback_data="back_to_main")]
    ])

# ================= СТАРТ И МЕНЮ =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    user_id = message.from_user.id
    user = await get_or_create_user(user_id, message.from_user.username, message.from_user.first_name)
    
    if not await check_subscription(bot, user_id):
        await message.answer("👋 Доступ на Звёздную Арену открыт только для подписчиков. Вступай в ряды:", reply_markup=sub_kb())
        return

    text = (f"🔥 **ГЛАВНОЕ МЕНЮ** 🔥\n\n"
            f"👤 Боец: {user.get('first_name', 'Боец')}\n"
            f"🛡 Фракция: **{user.get('faction', '🔴 ОГОНЬ')}**\n"
            f"⭐ Баланс: {user.get('balance', 0)} звёзд\n\n"
            f"Выбирай действие ниже:")
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Подписка не найдена!", show_alert=True)
    await callback.message.edit_text("✅ Доступ разрешен!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await users_col.find_one({"user_id": callback.from_user.id})
    text = (f"🔥 **ГЛАВНОЕ МЕНЮ** 🔥\n\n"
            f"👤 Боец: {user['first_name']}\n"
            f"🛡 Фракция: **{user['faction']}**\n"
            f"⭐ Баланс: {user['balance']} звёзд")
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

# ================= МАТЧМЕЙКИНГ =================
async def create_pending_match(bot: Bot, player_a: dict, player_b: dict):
    match_id = uuid.uuid4().hex[:8]
    await battles_col.insert_one({
        "match_id": match_id,
        "round": 1,
        "player_a": {"id": player_a["id"], "name": player_a["first_name"], "votes": 0},
        "player_b": {"id": player_b["id"], "name": player_b["first_name"], "votes": 0},
        "voted_users": [],
        "status": "pending",
        "created_at": datetime.now(timezone.utc)
    })
    
    for p in [player_a, player_b]:
        try:
            await bot.send_message(p["id"], "⚡ **Соперник найден!**\nВы в сетке 1-го раунда. Баттл появится в канале в 10:00 МСК.", parse_mode="Markdown")
        except TelegramForbiddenError:
            pass

@router.callback_query(F.data == "start_blitz")
async def join_blitz(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    first_name = callback.from_user.first_name
    
    active_match = await battles_col.find_one({
        "$or": [{"player_a.id": user_id}, {"player_b.id": user_id}], 
        "status": {"$in": ["active", "pending"]}
    })
    if active_match:
        return await callback.answer("❌ Вы уже находитесь в турнирной сетке!", show_alert=True)
    
    await queue_col.delete_many({"id": user_id})
    opponent = await queue_col.find_one_and_delete({"id": {"$ne": user_id}, "round": {"$exists": False}})
    
    if opponent:
        await callback.message.edit_text("🔥 **Противник найден!** Подготовка арены...", parse_mode="Markdown")
        await create_pending_match(bot, 
            {"id": opponent["id"], "first_name": opponent["first_name"]}, 
            {"id": user_id, "first_name": first_name}
        )
    else:
        await queue_col.insert_one({"id": user_id, "first_name": first_name, "timestamp": datetime.now(timezone.utc)})
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ **Регистрация...** Ожидаем второго бойца.", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    await callback.answer()
    await queue_col.delete_many({"id": callback.from_user.id})
    await callback.message.edit_text("❌ Поиск отменен.", reply_markup=main_menu_kb())

# ================= РЕЛИЗ И ГОЛОСОВАНИЕ В КАНАЛЕ =================
async def publish_morning_battles(bot: Bot):
    pending_matches = await battles_col.find({"status": "pending", "round": 1}).to_list(None)
    for match in pending_matches:
        p_a, p_b = match["player_a"], match["player_b"]
        match_id = match["match_id"]
        post_text = (f"⚔️ **РАУНД 1 | БЛИЦ-ТУРНИР** ⚔️\n\n"
                     f"🔥 **{p_a['name']}**  VS  💧 **{p_b['name']}**\n\n"
                     f"⚠️ **Условие:** минимум 2 голоса для прохода дальше.")
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔥 ОГОНЬ [0]", callback_data=f"vote_{match_id}_a"),
            InlineKeyboardButton(text="💧 ВОДА [0]", callback_data=f"vote_{match_id}_b")
        ]])
        try:
            sent_msg = await bot.send_message(CHANNEL_ID, post_text, reply_markup=markup)
            await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "active", "channel_message_id": sent_msg.message_id}})
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Ошибка выгрузки баттла {match_id}: {e}")

@router.callback_query(F.data.startswith("vote_"))
async def handle_channel_vote(callback: CallbackQuery):
    parts = callback.data.split("_")
    _, match_id, target_side = parts
    user_id = callback.from_user.id

    match = await battles_col.find_one({"match_id": match_id})
    if not match or match.get("status") != "active":
        return await callback.answer("❌ Раунд закрыт.", show_alert=True)
    if user_id in match.get("voted_users", []):
        return await callback.answer("⚠️ Ты уже голосовал!", show_alert=True)

    player_key = "player_a" if target_side == "a" else "player_b"
    await battles_col.update_one({"match_id": match_id}, {"$inc": {f"{player_key}.votes": 1}, "$push": {"voted_users": user_id}})
    
    updated = await battles_col.find_one({"match_id": match_id})
    p_a, p_b = updated["player_a"], updated["player_b"]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔥 ОГОНЬ [{p_a['votes']}]", callback_data=f"vote_{match_id}_a"),
        InlineKeyboardButton(text=f"💧 ВОДА [{p_b['votes']}]", callback_data=f"vote_{match_id}_b")
    ]])
    
    try: await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest: pass
    await callback.answer("✅ Голос принят!")

# ================= ДВИЖОК ТУРНИРНОЙ СЕТКИ =================
async def process_tournament_round(bot: Bot, current_round: int, vote_threshold: int):
    active_matches = await battles_col.find({"status": "active", "round": current_round}).to_list(None)
    winners_list = []
    
    for match in active_matches:
        p_a, p_b = match["player_a"], match["player_b"]
        winner = None
        
        if p_a["votes"] >= vote_threshold and p_a["votes"] > p_b["votes"]: winner = p_a
        elif p_b["votes"] >= vote_threshold and p_b["votes"] > p_a["votes"]: winner = p_b

        await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "finished"}})
        
        if winner:
            await users_col.update_one({"user_id": winner["id"]}, {"$inc": {"wins": 1, "balance": 10, "matches_played": 1}})
            result_text = f"🏁 **РАУНД {current_round} ЗАВЕРШЕН!**\n\n🏆 Победитель: **{winner['name']}** ({winner['votes']} голосов)"
            winners_list.append(winner)
        else:
            result_text = f"🏁 **РАУНД {current_round} ЗАВЕРШЕН!**\n\n💀 Никто не набрал {vote_threshold} голосов или ничья."

        if "channel_message_id" in match:
            try:
                await bot.edit_message_text(chat_id=CHANNEL_ID, message_id=match["channel_message_id"], text=result_text)
                await asyncio.sleep(2)
            except Exception: pass

    next_round = current_round + 1
    waiting_players = await queue_col.find({"round": next_round}).to_list(None)
    for w in waiting_players:
        winners_list.append({"id": w["id"], "name": w["first_name"]})
        await queue_col.delete_one({"_id": w["_id"]})

    random.shuffle(winners_list)
    for i in range(0, len(winners_list) - 1, 2):
        player_1, player_2 = winners_list[i], winners_list[i+1]
        new_match_id = uuid.uuid4().hex[:8]
        post_text = (f"⚔️ **РАУНД {next_round} | БЛИЦ-ТУРНИР** ⚔️\n\n"
                     f"🔥 **{player_1['name']}**  VS  💧 **{player_2['name']}**")
        
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔥 ОГОНЬ [0]", callback_data=f"vote_{new_match_id}_a"),
            InlineKeyboardButton(text="💧 ВОДА [0]", callback_data=f"vote_{new_match_id}_b")
        ]])
        
        try:
            sent_msg = await bot.send_message(CHANNEL_ID, post_text, reply_markup=markup)
            await battles_col.insert_one({
                "match_id": new_match_id, "round": next_round,
                "player_a": {"id": player_1["id"], "name": player_1["name"], "votes": 0},
                "player_b": {"id": player_2["id"], "name": player_2["name"], "votes": 0},
                "voted_users": [], "status": "active",
                "channel_message_id": sent_msg.message_id, "created_at": datetime.now(timezone.utc)
            })
            for p in [player_1, player_2]:
                try: await bot.send_message(p["id"], f"🏆 **Ты прошел в Раунд {next_round}!**\nТвой новый баттл опубликован.")
                except: pass
            await asyncio.sleep(2)
        except: pass

    if len(winners_list) % 2 != 0:
        lucky = winners_list[-1]
        await queue_col.insert_one({"id": lucky["id"], "first_name": lucky["name"], "round": next_round + 1})
        try: await bot.send_message(lucky["id"], f"🎁 **Авто-проход!** Тебе не хватило пары. Жди Раунд {next_round + 1}.")
        except: pass

# ================= ПРОФИЛИ И АДМИНКА =================
@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    user = await users_col.find_one({"user_id": callback.from_user.id})
    winrate = round((user['wins'] / user['matches_played']) * 100, 1) if user['matches_played'] > 0 else 0
    text = (f"👤 **ПРОФИЛЬ БОЙЦА**\n\n"
            f"Имя: {user.get('first_name', 'Боец')}\n"
            f"Фракция: {user.get('faction', '🔴 ОГОНЬ')}\n"
            f"Баланс: **{user.get('balance', 0)} ⭐**\n\n"
            f"📊 Боев: {user.get('matches_played', 0)} | Побед: {user.get('wins', 0)} | Винрейт: {winrate}%")    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]), parse_mode="Markdown")

@router.callback_query(F.data == "top_players")
async def show_leaderboard(callback: CallbackQuery):
    top_users = await users_col.find().sort("wins", -1).limit(10).to_list(10)
    text = "🏆 **ТОП-10 БОЙЦОВ** 🏆\n\n"
    for i, u in enumerate(top_users):
        text += f"{i+1}. {u['first_name']} | Побед: {u.get('wins', 0)}\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]), parse_mode="Markdown")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id == ADMIN_ID: await message.answer("🛠 **Панель Администратора**", reply_markup=admin_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        text = (f"📊 Игроков: {await users_col.count_documents({})}\n"
                f"⚔️ Активных боев: {await battles_col.count_documents({'status': 'active'})}")
        await callback.message.edit_text(text, reply_markup=admin_kb())

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id == ADMIN_ID:
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.edit_text("📢 Отправь сообщение для рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="back_to_main")]]))

@router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await users_col.find({}, {"user_id": 1}).to_list(None)
    success = 0
    for u in users:
        try:
            await bot.copy_message(chat_id=u["user_id"], from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Успешно доставлено: {success}", reply_markup=admin_kb())

# ================= WEB APP И ВЫВОД СРЕДСТВ =================
@router.message(F.web_app_data)
async def handle_web_app_data(message: Message, bot: Bot):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "withdraw":
            amount = int(data.get("amount", 0))
            user = await users_col.find_one({"user_id": message.from_user.id})
            if user['balance'] < amount: return await message.answer("❌ Недостаточно звёзд.")
            
            await users_col.update_one({"user_id": message.from_user.id}, {"$inc": {"balance": -amount}})
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"appr_{message.from_user.id}_{amount}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rejl_{message.from_user.id}_{amount}")]
            ])
            await bot.send_message(ADMIN_ID, f"💸 **ЗАЯВКА НА ВЫВОД**\nИгрок: {message.from_user.first_name}\nСумма: **{amount} ⭐**", reply_markup=kb, parse_mode="Markdown")
            await message.answer(f"✅ Заявка на {amount} ⭐ отправлена. Баланс заморожен.")
    except Exception as e:
        logger.error(f"WebApp Error: {e}")

@router.callback_query(F.data.startswith("appr_"))
async def approve_withdraw(call: CallbackQuery, bot: Bot):
    if call.from_user.id == ADMIN_ID:
        _, uid, amount = call.data.split("_")
        await call.message.edit_text(f"{call.message.text}\n\n✅ **ВЫПЛАЧЕНО**", parse_mode="Markdown")
        try: await bot.send_message(int(uid), f"💳 Твоя заявка на вывод **{amount} ⭐** выполнена!")
        except: pass

@router.callback_query(F.data.startswith("rejl_"))
async def reject_withdraw(call: CallbackQuery, bot: Bot):
    if call.from_user.id == ADMIN_ID:
        _, uid, amount = call.data.split("_")
        await users_col.update_one({"user_id": int(uid)}, {"$inc": {"balance": int(amount)}})
        await call.message.edit_text(f"{call.message.text}\n\n❌ **ОТКЛОНЕНО**", parse_mode="Markdown")
        try: await bot.send_message(int(uid), f"❌ Заявка на {amount} ⭐ отклонена. Средства возвращены.")
        except: pass

# ================= ВЕБ-СЕРВЕР И ЗАПУСК =================
async def handle_ping(request):
    return web.Response(text="Homa Arena Backend is fully operational.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

async def main():
    await setup_db_indexes()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(publish_morning_battles, 'cron', hour=10, minute=0, args=[bot])
    scheduler.add_job(process_tournament_round, 'cron', hour=12, minute=0, args=[bot, 1, 2])
    scheduler.add_job(process_tournament_round, 'cron', hour=14, minute=0, args=[bot, 2, 5])
    scheduler.add_job(process_tournament_round, 'cron', hour=16, minute=0, args=[bot, 3, 10])
    scheduler.add_job(process_tournament_round, 'cron', hour=18, minute=0, args=[bot, 4, 20])
    scheduler.add_job(process_tournament_round, 'cron', hour=20, minute=0, args=[bot, 5, 50])
    scheduler.start()

    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
