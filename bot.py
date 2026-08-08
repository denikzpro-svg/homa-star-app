import asyncio
import json
import logging
import os
import random
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

# ================= CONFIGURATION =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"
ADMIN_ID = 7910818906

WEB_APP_URL = "https://homa-star-app.vercel.app"

logging.basicConfig(level=logging.INFO)
router = Router()

# ================= DATABASE SETUP =================
client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"]
queue_col = db["queue"]

MSK_TZ = timezone(timedelta(hours=3))

STAGE_GOALS = {1: 5, 2: 10, 3: 50}


# ================= HELPER FUNCTIONS =================
async def get_user(user_id: int, username: str = None):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": username or "Игрок",
            "balance": 0,
            "active_battle_id": None,
            "stage": 1,
            "status": "idle",
        }
        await users_col.insert_one(user)
    else:
        if username and user.get("username") != username:
            await users_col.update_one(
                {"user_id": user_id}, {"$set": {"username": username}}
            )
            user["username"] = username
    return user


def main_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚔️ Начать Блиц-Турнир", callback_data="find_match"
                )
            ],
        ]
    )


# ================= START & MENU =================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = (
        message.from_user.username
        or message.from_user.first_name
        or f"id{user_id}"
    )
    user = await get_user(user_id, username)

    # Очищаем застрявший поиск при перезапуске
    await queue_col.delete_many({"user_id": user_id})

    active_battle_id = user.get("active_battle_id")
    if active_battle_id:
        from bson.objectid import ObjectId

        battle = await battles_col.find_one({"_id": ObjectId(active_battle_id)})

        if not battle or battle.get("status") != "active":
            await users_col.update_one(
                {"user_id": user_id},
                {"$set": {"status": "idle", "active_battle_id": None}},
            )
        else:
            msg_id = battle.get("msg_id")
            post_link = f"https://t.me/{CHANNEL_ID.replace('@', '')}/{msg_id}"
            await message.answer(
                f"⚠️ **У тебя сейчас идет активный батл!**\n\n"
                f"🔗 [Перейти к голосованию в канале]({post_link})\n\n"
                f"Дождись его завершения, чтобы начать новый.",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return
    else:
        await users_col.update_one(
            {"user_id": user_id}, {"$set": {"status": "idle"}}
        )

    await message.answer(
        "🔥 **Добро пожаловать в Звёздную Арену!**\n\n"
        "Запускай приложение или вступай в Блиц-Турнир Стихий!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


# ================= ПОИСК СОПЕРНИКА И БАТЛЫ =================
@router.callback_query(F.data == "find_match")
async def process_find_match(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    username = (
        callback.from_user.username
        or callback.from_user.first_name
        or f"id{user_id}"
    )
    user = await get_user(user_id, username)

    if user.get("status") == "in_battle":
        await callback.answer()
        return await callback.message.edit_text(
            "❌ Ты уже участвуешь в активном батле! Дождись итогов.",
            reply_markup=main_menu_kb(),
        )

    stage = user.get("stage", 1)

    # Если уже в поиске — обновляем текст сообщения
    if user.get("status") == "searching":
        await callback.answer()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отменить поиск", callback_data="cancel_search"
                    )
                ]
            ]
        )
        return await callback.message.edit_text(
            f"⏳ **Ожидание второго игрока (Раунд {stage})...**\n\n"
            f"Как только соперник нажмет кнопку, батл автоматически опубликуется в {CHANNEL_ID}!",
            reply_markup=kb,
            parse_mode="Markdown",
        )

    # Ищем другого игрока равного Stage в очереди
    opponent = await queue_col.find_one_and_delete(
        {"stage": stage, "user_id": {"$ne": user_id}}
    )

    if not opponent:
        # Становимся в очередь
        await queue_col.insert_one(
            {
                "user_id": user_id,
                "username": username,
                "stage": stage,
                "chat_id": callback.message.chat.id,
                "msg_id": callback.message.message_id,
                "created_at": datetime.utcnow(),
            }
        )
        await users_col.update_one(
            {"user_id": user_id}, {"$set": {"status": "searching"}}
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отменить поиск", callback_data="cancel_search"
                    )
                ]
            ]
        )
        await callback.answer()
        return await callback.message.edit_text(
            f"⏳ **Ожидание второго игрока (Раунд {stage})...**\n\n"
            f"Ты встал в очередь. Как только найдется соперник, батл сразу появится в канале {CHANNEL_ID}!",
            reply_markup=kb,
            parse_mode="Markdown",
        )

    # Соперник найден! Запускаем батл
    await callback.answer()
    p1_id, p1_name = user_id, username
    p2_id, p2_name = opponent["user_id"], opponent["username"]

    if random.choice([True, False]):
        fire_id, fire_name = p1_id, p1_name
        water_id, water_name = p2_id, p2_name
    else:
        fire_id, fire_name = p2_id, p2_name
        water_id, water_name = p1_id, p1_name

    goal = STAGE_GOALS.get(stage, 5)
    stage_title = (
        "РАУНД 1"
        if stage == 1
        else ("ПОЛУФИНАЛ" if stage == 2 else "👑 ГРАНД-ФИНАЛ 👑")
    )

    post_text = (
        f"⚔️ **БИТВА СТИХИЙ | {stage_title}**\n\n"
        f"🔥 **Огонь:** @{fire_name}\n"
        f"💧 **Вода:** @{water_name}\n\n"
        f"🎯 **Цель раунда:** Набрать минимум **{goal} голосов**!\n"
        f"⏰ **Время на голосование:** 1 час!\n"
        f"Поддержите своего фаворита ниже 👇"
    )

    battle_doc = {
        "stage": stage,
        "fire_id": fire_id,
        "fire_name": fire_name,
        "fire_votes": 0,
        "water_id": water_id,
        "water_name": water_name,
        "water_votes": 0,
        "voted_users": [],
        "created_at": datetime.utcnow(),
        "status": "active",
    }
    res = await battles_col.insert_one(battle_doc)
    battle_id = str(res.inserted_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔥 Огонь (@{fire_name}) — 0",
                    callback_data=f"vote_{battle_id}_fire",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💧 Вода (@{water_name}) — 0",
                    callback_data=f"vote_{battle_id}_water",
                )
            ],
        ]
    )

    try:
        msg = await bot.send_message(
            CHANNEL_ID, post_text, reply_markup=kb, parse_mode="Markdown"
        )
        await battles_col.update_one(
            {"_id": res.inserted_id}, {"$set": {"msg_id": msg.message_id}}
        )

        for uid in [fire_id, water_id]:
            await users_col.update_one(
                {"user_id": uid},
                {"$set": {"status": "in_battle", "active_battle_id": battle_id}},
            )

        post_link = f"https://t.me/{CHANNEL_ID.replace('@', '')}/{msg.message_id}"
        notify_text = (
            f"⚔️ **Соперник найден!** Твой батл ({stage_title}) начался!\n\n"
            f"🎯 Цель: **{goal} голосов**.\n"
            f"🔗 [Перейти к голосованию в канале]({post_link})"
        )

        # Редактируем сообщение для первого игрока (который ждал в очереди)
        try:
            await bot.edit_message_text(
                chat_id=opponent["chat_id"],
                message_id=opponent["msg_id"],
                text=notify_text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            await bot.send_message(
                opponent["user_id"],
                notify_text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        # Редактируем сообщение для второго игрока (который только что нажал)
        await callback.message.edit_text(
            notify_text, parse_mode="Markdown", disable_web_page_preview=True
        )

        asyncio.create_task(schedule_battle_end(bot, battle_id, 3600))

    except Exception as e:
        logging.error(f"Ошибка создания батла: {e}")


@router.callback_query(F.data == "cancel_search")
async def process_cancel_search(callback: CallbackQuery):
    user_id = callback.from_user.id
    await queue_col.delete_many({"user_id": user_id})
    await users_col.update_one({"user_id": user_id}, {"$set": {"status": "idle"}})
    await callback.message.edit_text(
        "❌ Поиск отменен.", reply_markup=main_menu_kb()
    )


# ================= ОБРАБОТКА ГОЛОСОВАНИЯ =================
@router.callback_query(F.data.startswith("vote_"))
async def process_vote(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split("_")
    battle_id = parts[1]
    side = parts[2]
    voter_id = callback.from_user.id

    from bson.objectid import ObjectId

    battle = await battles_col.find_one({"_id": ObjectId(battle_id)})

    if not battle or battle.get("status") != "active":
        return await callback.answer("❌ Батл уже завершен!", show_alert=True)

    if voter_id in battle.get("voted_users", []):
        return await callback.answer(
            "❌ Ты уже проголосовал в этом батле!", show_alert=True
        )

    field_to_inc = "fire_votes" if side == "fire" else "water_votes"
    await battles_col.update_one(
        {"_id": ObjectId(battle_id)},
        {"$inc": {field_to_inc: 1}, "$push": {"voted_users": voter_id}},
    )

    updated_b = await battles_col.find_one({"_id": ObjectId(battle_id)})
    f_votes = updated_b["fire_votes"]
    w_votes = updated_b["water_votes"]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔥 Огонь (@{updated_b['fire_name']}) — {f_votes}",
                    callback_data=f"vote_{battle_id}_fire",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"💧 Вода (@{updated_b['water_name']}) — {w_votes}",
                    callback_data=f"vote_{battle_id}_water",
                )
            ],
        ]
    )

    try:
        await bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID, message_id=updated_b["msg_id"], reply_markup=kb
        )
    except Exception:
        pass

    await callback.answer("✅ Твой голос учтен!")


# ================= ЗАВЕРШЕНИЕ БАТЛА И ИТОГИ =================
async def schedule_battle_end(bot: Bot, battle_id: str, delay: int):
    await asyncio.sleep(delay)
    from bson.objectid import ObjectId

    battle = await battles_col.find_one({"_id": ObjectId(battle_id)})
    if not battle or battle.get("status") != "active":
        return

    await battles_col.update_one(
        {"_id": ObjectId(battle_id)}, {"$set": {"status": "finished"}}
    )

    stage = battle["stage"]
    goal = STAGE_GOALS.get(stage, 5)
    f_votes, w_votes = battle["fire_votes"], battle["water_votes"]
    f_id, w_id = battle["fire_id"], battle["water_id"]

    winner_id = None
    loser_id = None

    if f_votes >= goal and f_votes > w_votes:
        winner_id, loser_id = f_id, w_id
        w_name = battle["fire_name"]
    elif w_votes >= goal and w_votes > f_votes:
        winner_id, loser_id = w_id, f_id
        w_name = battle["water_name"]
    elif f_votes >= goal and f_votes == w_votes:
        winner_id = random.choice([f_id, w_id])
        loser_id = w_id if winner_id == f_id else f_id
        w_name = (
            battle["fire_name"]
            if winner_id == f_id
            else battle["water_name"]
        )

    if winner_id:
        end_text = f"🎉 **БАТЛ ЗАВЕРШЕН!**\n\n🏆 Победитель: @{w_name}\n📊 Счёт: 🔥 {f_votes} vs 💧 {w_votes}"
    else:
        end_text = f"❌ **БАТЛ ЗАВЕРШЕН БЕЗ ПОБЕДИТЕЛЯ!**\n\nУчастники не набрали порог в {goal} голосов."

    try:
        await bot.send_message(CHANNEL_ID, end_text)
    except Exception:
        pass

    if winner_id:
        if stage < 3:
            await users_col.update_one(
                {"user_id": winner_id},
                {
                    "$inc": {"stage": 1},
                    "$set": {"status": "idle", "active_battle_id": None},
                },
            )
            await bot.send_message(
                winner_id,
                f"🏆 **ПОБЕДА!** Ты набрал нужные голоса и прошел в **Раунд {stage + 1}**!\nНажимай 'Начать Блиц-Турнир' в меню, чтобы найти соперника!",
            )
        else:
            await users_col.update_one(
                {"user_id": winner_id},
                {
                    "$inc": {"balance": 100},
                    "$set": {"stage": 1, "status": "idle", "active_battle_id": None},
                },
            )
            await bot.send_message(
                winner_id,
                "👑 **ГРАНД-ФИНАЛ ВЫИГРАН!** 👑\n\nТебе зачислено **100 звёзд ⭐**!",
            )

        await users_col.update_one(
            {"user_id": loser_id},
            {"$set": {"stage": 1, "status": "idle", "active_battle_id": None}},
        )
        await bot.send_message(
            loser_id,
            "❌ К сожалению, ты проиграл в этом батле. Твой уровень сброшен. Попробуй заново!",
        )
    else:
        for uid in [f_id, w_id]:
            await users_col.update_one(
                {"user_id": uid},
                {"$set": {"stage": 1, "status": "idle", "active_battle_id": None}},
            )
            await bot.send_message(
                uid,
                f"❌ Порог в {goal} голосов не был достигнут. Вы оба выбываете из турнира.",
            )


# ================= ВЫВОД ЗВЁЗД ИЗ WEB APP =================
@router.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "withdraw":
            amount = data.get("amount")
            user = message.from_user

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Выплачено",
                            callback_data=f"appr_{user.id}_{amount}",
                        ),
                        InlineKeyboardButton(
                            text="❌ Отклонить",
                            callback_data=f"rejl_{user.id}_{amount}",
                        ),
                    ]
                ]
            )

            admin_text = (
                f"💸 **НОВАЯ ЗАЯВКА НА ВЫВОД ЗВЁЗД!**\n\n"
                f"👤 Игрок: {user.first_name} (@{user.username if user.username else 'нет_ника'})\n"
                f"🆔 ID игрока: `{user.id}`\n"
                f"⭐ Сумма: **{amount} ⭐**"
            )

            await message.bot.send_message(
                ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=kb
            )
            await message.answer(
                f"✅ Ваша заявка на вывод **{amount} ⭐** отправлена администратору!"
            )
    except Exception as e:
        print(f"Ошибка WebApp data: {e}")


@router.callback_query(F.data.startswith("appr_"))
async def approve_withdraw(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет прав!", show_alert=True)
    await call.message.edit_text(
        f"{call.message.text}\n\n✅ **СТАТУС: ВЫПЛАЧЕНО!**",
        parse_mode="Markdown",
    )
    await call.answer("Одобрено!")


@router.callback_query(F.data.startswith("rejl_"))
async def reject_withdraw(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("Нет прав!", show_alert=True)
    await call.message.edit_text(
        f"{call.message.text}\n\n❌ **СТАТУС: ОТКЛОНЕНО!**",
        parse_mode="Markdown",
    )
    await call.answer("Отклонено!")


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

    asyncio.create_task(start_web_server())

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот 'Звёздная Арена' запущен 24/7!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
