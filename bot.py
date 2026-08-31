import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, StateFilter
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

MSK_TZ = timezone(timedelta(hours=3))
search_queue = []  # ОЗУ: Очередь матчмейкинга

async def setup_db_indexes():
    """Архитектура БД: Создаем индексы для сверхбыстрого поиска при 10k+ юзерах"""
    await users_col.create_index("user_id", unique=True)
    await users_col.create_index("balance")
    await battles_col.create_index("match_id", unique=True)
    await battles_col.create_index("status")
    logger.info("БД: Индексы успешно инициализированы.")

# ================= СТРУКТУРЫ ДАННЫХ И СТЕЙТЫ =================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()

async def get_or_create_user(user_id: int, username: str, first_name: str):
    """Геймдизайн: Выдача фракции навсегда при первой регистрации"""
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

# ================= UI/UX: КЛАВИАТУРЫ =================
def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Начать Блиц-турнир", callback_data="start_blitz")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile"),
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

def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика сервера", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="◀️ Выйти", callback_data="back_to_main")]
        ]
    )

# ================= СИСТЕМА ГОЛОСОВАНИЯ (АНТИ-ЧИТ) =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    args = message.text.split()
    user_id = message.from_user.id
    
    # 1. Регистрация юзера
    user = await get_or_create_user(user_id, message.from_user.username, message.from_user.first_name)
    is_subbed = await check_subscription(bot, user_id)

    # 2. Обработка Deep-Link (Голосование)
    if len(args) > 1 and args[1].startswith("vote_"):
        if not is_subbed:
            await message.answer(
                "⚠️ **Стоп!**\nГолосовать могут только зрители Арены. Подпишись на канал, а затем **снова нажми на ссылку друга**, чтобы голос засчитался!", 
                reply_markup=sub_kb(), parse_mode="Markdown"
            )
            return
            
        try:
            parts = args[1].split("_")
            if len(parts) != 3:
                raise ValueError("Invalid deep link format")
            _, match_id, target_id_str = parts
            target_id = int(target_id_str)
            
            # Анти-чит 1: Запрет на голосование за самого себя
            if user_id == target_id:
                return await message.answer("❌ Жульничать нельзя! Голосовать за самого себя запрещено.")

            match = await battles_col.find_one({"match_id": match_id, "status": "active"})
            if not match:
                return await message.answer("❌ Битва уже завершена, либо ссылка недействительна.")
            
            # Анти-чит 2: Один голос на один матч
            if user_id in match.get("voted_users", []):
                return await message.answer("⚠️ Ты уже отдал свой голос в этом противостоянии!")
                
            is_player_a = match["player_a"]["id"] == target_id
            is_player_b = match["player_b"]["id"] == target_id
            
            if not is_player_a and not is_player_b:
                return await message.answer("❌ Этот игрок не участвует в данном матче.")

            player_key = "player_a" if is_player_a else "player_b"
            role_name = match[player_key]["role"]
            
            # Транзакция голоса
            await battles_col.update_one(
                {"match_id": match_id},
                {"$inc": {f"{player_key}.votes": 1}, "$push": {"voted_users": user_id}}
            )
            
            await message.answer(f"✅ Твой голос за **{role_name}** ({match[player_key]['name']}) успешно засчитан!\n\nХочешь сам выйти на Арену?", reply_markup=main_menu_kb(), parse_mode="Markdown")
            
            # Уведомление бойца
            try:
                await bot.send_message(target_id, f"🔥 **+1 голос!** Кто-то поддержал твою стихию ({role_name}).", parse_mode="Markdown")
            except TelegramForbiddenError:
                pass # Юзер блокнул бота, игнорируем
            return
            
        except Exception as e:
            logger.error(f"Vote Error by {user_id}: {e}")
            return await message.answer("❌ Ошибка при обработке ссылки.")

    # 3. Обычный старт (Без ссылки)
    if not is_subbed:
        await message.answer("👋 Привет, боец! Доступ на Звёздную Арену открыт только для подписчиков. Вступай в ряды:", reply_markup=sub_kb())
        return

    text = (f"🔥 **ГЛАВНОЕ МЕНЮ** 🔥\n\n"
            f"👤 Боец: {user['first_name']}\n"
            f"🛡 Фракция: **{user['faction']}**\n"
            f"⭐ Баланс: {user['balance']} звёзд\n\n"
            f"Выбирай действие ниже:")
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    if not await check_subscription(bot, callback.from_user.id):
        return await callback.answer("❌ Подписка не найдена! Проверь еще раз.", show_alert=True)
    await callback.message.edit_text("✅ Доступ разрешен!", reply_markup=main_menu_kb())
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await users_col.find_one({"user_id": callback.from_user.id})
    text = (f"🔥 **ГЛАВНОЕ МЕНЮ** 🔥\n\n"
            f"👤 Боец: {user['first_name']}\n"
            f"🛡 Фракция: **{user['faction']}**\n"
            f"⭐ Баланс: {user['balance']} звёзд\n\n"
            f"Выбирай действие ниже:")
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

# ================= МАТЧМЕЙКИНГ И БОИ =================
async def create_pvp_match(bot: Bot, player_a: dict, player_b: dict):
    """Геймплей: Инициализация битвы 1v1 с распределением ролей"""
    match_id = uuid.uuid4().hex[:8]
    
    await battles_col.insert_one({
        "match_id": match_id,
        "player_a": {"id": player_a["id"], "name": player_a["name"], "role": "🔴 ОГОНЬ", "votes": 0},
        "player_b": {"id": player_b["id"], "name": player_b["name"], "role": "🔵 ВОДА", "votes": 0},
        "voted_users": [],
        "start_time": datetime.now(MSK_TZ),
        "status": "active"
    })
    
    bot_info = await bot.get_me()
    
    for p, opponent, role in [(player_a, player_b, "🔴 ОГОНЬ"), (player_b, player_a, "🔵 ВОДА")]:
        ref_link = f"https://t.me/{bot_info.username}?start=vote_{match_id}_{p['id']}"
        msg = (f"⚔️ **БЛИЦ-ТУРНИР НАЧАЛСЯ!** ⚔️\n\n"
               f"Твоя стихия на этот бой: **{role}**\n"
               f"Твой противник: **{opponent['name']}**\n\n"
               f"🔗 **Твоя ссылка для голосования:**\n`{ref_link}`\n\n"
               f"⚠️ *Отправляй эту ссылку друзьям! У кого больше голосов к концу раунда — тот забирает награду.*")
        try:
            await bot.send_message(p["id"], msg, parse_mode="Markdown")
        except TelegramForbiddenError:
            pass

@router.callback_query(F.data == "start_blitz")
async def join_blitz(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    user_name = callback.from_user.first_name
    
    # 1. Проверка на активный бой
    active_match = await battles_col.find_one({
        "$or": [{"player_a.id": user_id}, {"player_b.id": user_id}], 
        "status": "active"
    })
    if active_match:
        return await callback.answer("❌ Ты уже сражаешься! Ищи голоса по своей ссылке.", show_alert=True)
    
    # 2. Логика очереди (Queue)
    if search_queue and search_queue[0]["id"] != user_id:
        opponent = search_queue.pop(0)
        await callback.message.edit_text("🔥 **Противник найден!** Битва генерируется...", parse_mode="Markdown")
        await create_pvp_match(bot, opponent, {"id": user_id, "name": user_name})
    else:
        if not any(u["id"] == user_id for u in search_queue):
            search_queue.append({"id": user_id, "name": user_name})
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="cancel_search")]])
        await callback.message.edit_text("⏳ **Ищем достойного соперника...**\nОжидание в очереди.", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery):
    global search_queue
    search_queue = [u for u in search_queue if u["id"] != callback.from_user.id]
    await callback.message.edit_text("❌ Поиск отменен.", reply_markup=main_menu_kb())

# ================= ПРОФИЛИ И ЛИДЕРБОРД =================
@router.callback_query(F.data == "my_profile")
async def show_profile(callback: CallbackQuery):
    user = await users_col.find_one({"user_id": callback.from_user.id})
    if not user:
        return await callback.answer("Ошибка профиля.", show_alert=True)
        
    winrate = 0
    if user['matches_played'] > 0:
        winrate = round((user['wins'] / user['matches_played']) * 100, 1)

    text = (f"👤 **ПРОФИЛЬ БОЙЦА**\n\n"
            f"Имя: {user['first_name']}\n"
            f"Фракция: {user['faction']}\n"
            f"Баланс: **{user['balance']} ⭐**\n\n"
            f"📊 **Статистика Арены:**\n"
            f"Боев: {user['matches_played']}\n"
            f"Побед: {user['wins']} 🏆\n"
            f"Поражений: {user['losses']} 💀\n"
            f"Винрейт: {winrate}%")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "top_players")
async def show_leaderboard(callback: CallbackQuery):
    top_users = await users_col.find().sort("wins", -1).limit(10).to_list(10)
    
    text = "🏆 **ТОП-10 ЛУЧШИХ БОЙЦОВ АРЕНЫ** 🏆\n\n"
    medals = ["🥇", "🥈", "🥉"]
    
    for i, u in enumerate(top_users):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} {u['first_name']} | {u['faction']} | Побед: {u.get('wins', 0)}\n"
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ================= АДМИН-ПАНЕЛЬ (SERVER ARCHITECTURE) =================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 **Панель Администратора**", reply_markup=admin_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    total_users = await users_col.count_documents({})
    active_battles = await battles_col.count_documents({"status": "active"})
    total_battles = await battles_col.count_documents({})
    
    text = (f"📊 **СТАТИСТИКА СЕРВЕРА**\n\n"
            f"👥 Всего игроков: {total_users}\n"
            f"⚔️ Активных боев сейчас: {active_battles}\n"
            f"🗃 Всего боев за историю: {total_battles}\n"
            f"⏳ В поиске (ОЗУ): {len(search_queue)}")
    await callback.message.edit_text(text, reply_markup=admin_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="back_to_main")]])
    await callback.message.edit_text("📢 Отправь сообщение для рассылки всем юзерам:", reply_markup=kb)

@router.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await users_col.find({}, {"user_id": 1}).to_list(None)
    success, fail = 0, 0
    await message.answer("⏳ Рассылка запущена...")
    
    for u in users:
        try:
            await bot.copy_message(chat_id=u["user_id"], from_chat_id=message.chat.id, message_id=message.message_id)
            success += 1
            await asyncio.sleep(0.05) # Лимиты Telegram (20 msg/sec)
        except Exception:
            fail += 1
            
    await message.answer(f"✅ Рассылка завершена!\nУспешно: {success}\nЗаблокировали бота: {fail}", reply_markup=admin_kb())

# ================= СИСТЕМА ТУРНИРОВ (APSCHEDULER) =================
async def process_round_results(bot: Bot):
    """Архитектура: Фоновый скрипт закрытия матчей и выдачи наград"""
    logger.info("CRON: Подведение итогов раунда...")
    active_matches = await battles_col.find({"status": "active"}).to_list(None)
    
    for match in active_matches:
        p_a = match["player_a"]
        p_b = match["player_b"]
        
        winner_id, loser_id = None, None
        
        # Логика определения победителя
        if p_a["votes"] > p_b["votes"]:
            winner_id, loser_id = p_a["id"], p_b["id"]
        elif p_b["votes"] > p_a["votes"]:
            winner_id, loser_id = p_b["id"], p_a["id"]
        else:
            # Ничья - рандом
            winner_id = random.choice([p_a["id"], p_b["id"]])
            loser_id = p_a["id"] if winner_id == p_b["id"] else p_b["id"]

        # Обновление статы в БД
        await users_col.update_one({"user_id": winner_id}, {"$inc": {"balance": 10, "wins": 1, "matches_played": 1}})
        await users_col.update_one({"user_id": loser_id}, {"$inc": {"losses": 1, "matches_played": 1}})
        await battles_col.update_one({"_id": match["_id"]}, {"$set": {"status": "finished", "winner": winner_id}})

        # Уведомления игрокам
        try:
            await bot.send_message(winner_id, "🏆 **БЛИЦ-ТУРНИР ЗАВЕРШЕН!**\nТы одержал победу и заработал **10 ⭐**!", parse_mode="Markdown")
        except: pass
        try:
            await bot.send_message(loser_id, "💀 **БЛИЦ-ТУРНИР ЗАВЕРШЕН!**\nК сожалению, у противника было больше голосов. Попробуй еще раз!", parse_mode="Markdown")
        except: pass

# ================= WEB APP И ВЫВОД СРЕДСТВ =================
@router.message(F.web_app_data)
async def handle_web_app_data(message: Message, bot: Bot):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "withdraw":
            amount = int(data.get("amount", 0))
            user = await users_col.find_one({"user_id": message.from_user.id})
            
            if user['balance'] < amount:
                return await message.answer("❌ Недостаточно звёзд на балансе.")
                
            # Списываем баланс до аппрува (защита от двойного вывода)
            await users_col.update_one({"user_id": message.from_user.id}, {"$inc": {"balance": -amount}})
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выплачено", callback_data=f"appr_{message.from_user.id}_{amount}"),
                 InlineKeyboardButton(text="❌ Отклонить (Вернуть ⭐)", callback_data=f"rejl_{message.from_user.id}_{amount}")]
            ])
            text = f"💸 **ЗАЯВКА НА ВЫВОД**\nИгрок: {message.from_user.first_name} (`{message.from_user.id}`)\nСумма: **{amount} ⭐**"
            await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
            await message.answer(f"✅ Заявка на {amount} ⭐ отправлена админу. Баланс заморожен.")
    except Exception as e:
        logger.error(f"WebApp Error: {e}")

@router.callback_query(F.data.startswith("appr_"))
async def approve_withdraw(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID: return
    _, uid, amount = call.data.split("_")
    await call.message.edit_text(f"{call.message.text}\n\n✅ **ВЫПЛАЧЕНО**", parse_mode="Markdown")
    try: await bot.send_message(int(uid), f"💳 Твоя заявка на вывод **{amount} ⭐** успешно выполнена!", parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("rejl_"))
async def reject_withdraw(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID: return
    _, uid, amount = call.data.split("_")
    # Возвращаем звезды
    await users_col.update_one({"user_id": int(uid)}, {"$inc": {"balance": int(amount)}})
    await call.message.edit_text(f"{call.message.text}\n\n❌ **ОТКЛОНЕНО (Средства возвращены)**", parse_mode="Markdown")
    try: await bot.send_message(int(uid), f"❌ Заявка на вывод **{amount} ⭐** отклонена. Средства возвращены на баланс.", parse_mode="Markdown")
    except: pass

# ================= ВЕБ-СЕРВЕР (ДЛЯ ОБЛАКА RENDER) =================
async def handle_ping(request):
    return web.Response(text="Homa Arena Backend is fully operational 24/7.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер Aiohttp запущен на порту {PORT}")

# ================= ТОЧКА ВХОДА (MAIN) =================
async def main():
    logger.info("Инициализация систем ядра...")
    await setup_db_indexes()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Инициализация Планировщика Задач (Расписание турниров)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(process_round_results, 'cron', hour=12, minute=0, args=[bot])
    scheduler.add_job(process_round_results, 'cron', hour=14, minute=0, args=[bot])
    scheduler.add_job(process_round_results, 'cron', hour=16, minute=0, args=[bot])
    scheduler.add_job(process_round_results, 'cron', hour=18, minute=0, args=[bot])
    scheduler.start()
    logger.info("CRON-расписание раундов активировано (12:00, 14:00, 16:00, 18:00 МСК).")

    # Запуск сервера для поддержания жизни на хостинге
    asyncio.create_task(start_web_server())

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот 'Звёздная Арена' перешел в режим Polling. Готов к нагрузкам.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Сервер бота остановлен вручную.")
