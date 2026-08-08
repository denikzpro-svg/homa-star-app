import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient

# ================= CONFIGURATION =================
BOT_TOKEN = "8978125889:AAH4WRBVCTUFykQCbxpzucuqp8ySXuKf4G4"
MONGO_URI = "mongodb+srv://denikzpro_db_user:kUTYTo4uyKTgC8uE@cluster0.oome800.mongodb.net/?appName=Cluster0"
CHANNEL_ID = "@hamster_arenas"
ADMIN_ID = 7910818906  # Твой Telegram ID

WEB_APP_URL = "https://homa-star-app.vercel.app"

logging.basicConfig(level=logging.INFO)
router = Router()

# ================= DATABASE SETUP =================
client = AsyncIOMotorClient(MONGO_URI)
db = client["star_arena_bot"]
users_col = db["users"]
battles_col = db["battles"]
withdrawals_col = db["withdrawals"]

MSK_TZ = timezone(timedelta(hours=3))


# ================= HELPER FUNCTIONS =================
async def get_user(user_id: int):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "balance": 0,
            "invited_count": 0,
            "referred_by": None,
            "active_tournament": False,
            "tournament_stage": 0,
            "tournament_invited": 0,
            "round_start_time": None,
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
            [
                InlineKeyboardButton(
                    text="🚀 ОТКРЫТЬ ПРИЛОЖЕНИЕ",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚔️ Начать блиц-турнир", callback_data="start_tournament"
                )
            ],
        ]
    )


# ================= HANDLERS: START & MENU =================
@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    args = message.text.split()
    user_id = message.from_user.id
    user = await get_user(user_id)

    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id and not user.get("referred_by"):
            await users_col.update_one(
                {"user_id": user_id}, {"$set": {"referred_by": referrer_id}}
            )
            await users_col.update_one(
                {"user_id": referrer_id}, {"$inc": {"invited_count": 1}}
            )

            ref_user = await get_user(referrer_id)
            if ref_user.get("active_tournament"):
                await users_col.update_one(
                    {"user_id": referrer_id}, {"$inc": {"tournament_invited": 1}}
                )

    is_subbed = await check_subscription(bot, user_id)
    if not is_subbed:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📢 Подписаться на канал",
                        url="https://t.me/hamster_arenas",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✅ Я подписался", callback_data="check_sub"
                    )
                ],
            ]
        )
        await message.answer(
            "👋 Привет! Чтобы пользоваться ботом и играть, подпишись на наш канал:",
            reply_markup=kb,
        )
        return

    await message.answer(
        "🔥 **Добро пожаловать в Звёздную Арену!**\n\nНажми кнопку ниже, чтобы запустить приложение или участвовать в турнирах!",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "check_sub")
async def process_check_sub(callback: CallbackQuery, bot: Bot):
    is_subbed = await check_subscription(bot, callback.from_user.id)
    if not is_subbed:
        await callback.answer(
            "❌ Ты ещё не подписался на канал!", show_alert=True
        )
        return

    await callback.message.edit_text(
        "✅ Подписка подтверждена!\n\nДобро пожаловать в Звёздную Арену:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "main_menu")
async def process_main_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎮 **Главное меню Звёздной Арены:**",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown",
    )


# ================= TOURNAMENT LOGIC (ПОЧИНЕНО) =================
class TournamentStates(StatesGroup):
    waiting_for_round_1 = State()
    waiting_for_round_2 = State()
    waiting_for_round_3 = State()


@router.callback_query(F.data == "start_tournament")
async def process_start_tournament(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await get_user(user_id)

    # Авто-сброс турнира, если с момента старта прошлого прошло больше 3 часов
    if user.get("round_start_time"):
        if datetime.utcnow() - user.get("round_start_time") > timedelta(
            hours=3
        ):
            await users_col.update_one(
                {"user_id": user_id}, {"$set": {"active_tournament": False}}
            )
            user["active_tournament"] = False

    if user.get("active_tournament"):
        await callback.answer(
            "❌ Ты уже участвуешь в активном турнире!", show_alert=True
        )
        return

    now_msk = datetime.now(MSK_TZ)
    end_time = now_msk + timedelta(hours=1)
    end_time_str = end_time.strftime("%H:%M")

    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "active_tournament": True,
                "tournament_stage": 1,
                "tournament_invited": 0,
                "round_start_time": datetime.utcnow(),
                "round_end_time_msk": end_time_str,
            }
        },
    )
    await state.set_state(TournamentStates.waiting_for_round_1)

    text = (
        "⚔️ **Блиц-турнир: Раунд 1**\n\n"
        "🎯 **Цель:** Пригласи минимум **2 человека**.\n"
        f"⏰ **Дедлайн:** до {end_time_str} МСК (1 час)!\n"
        "⚠️ *Если не успеешь — вылет!*\n\n"
        f"🔗 Твоя реф-ссылка:\n`https://t.me/{(await callback.bot.get_me()).username}?start={user_id}`"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Проверить Раунд 1",
                    callback_data="check_t_round_1",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Сдаться / Покинуть",
                    callback_data="fail_tournament",
                )
            ],
        ]
    )
    await callback.message.edit_text(
        text, reply_markup=kb, parse_mode="Markdown"
    )


@router.callback_query(F.data == "check_t_round_1")
async def process_check_t_round_1(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    start_time = user.get("round_start_time")

    if not start_time or (
        datetime.utcnow() - start_time > timedelta(hours=1)
    ):
        await fail_tournament_logic(
            callback.from_user.id, state, callback.message
        )
        return

    invited = user.get("tournament_invited", 0)
    if invited >= 2:
        now_msk = datetime.now(MSK_TZ)
        end_time_str = (now_msk + timedelta(hours=1)).strftime("%H:%M")

        await users_col.update_one(
            {"user_id": callback.from_user.id},
            {
                "$set": {
                    "tournament_stage": 2,
                    "tournament_invited": 0,
                    "round_start_time": datetime.utcnow(),
                }
            },
        )
        await state.set_state(TournamentStates.waiting_for_round_2)

        text = (
            "✅ **Раунд 1 пройден!**\n\n"
            "⚔️ **Раунд 2: Полуфинал**\n"
            "🎯 **Цель:** Пригласи **15 человек**.\n"
            f"⏰ **Дедлайн:** до {end_time_str} МСК!"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔍 Проверить Раунд 2",
                        callback_data="check_t_round_2",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Сдаться", callback_data="fail_tournament"
                    )
                ],
            ]
        )
        await callback.message.edit_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    else:
        await callback.answer(
            f"❌ Нужно 2 человека! У тебя: {invited}/2", show_alert=True
        )


@router.callback_query(F.data == "check_t_round_2")
async def process_check_t_round_2(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    start_time = user.get("round_start_time")

    if not start_time or (
        datetime.utcnow() - start_time > timedelta(hours=1)
    ):
        await fail_tournament_logic(
            callback.from_user.id, state, callback.message
        )
        return

    invited = user.get("tournament_invited", 0)
    if invited >= 15:
        now_msk = datetime.now(MSK_TZ)
        end_time_str = (now_msk + timedelta(hours=1)).strftime("%H:%M")

        await users_col.update_one(
            {"user_id": callback.from_user.id},
            {
                "$set": {
                    "tournament_stage": 3,
                    "tournament_invited": 0,
                    "round_start_time": datetime.utcnow(),
                }
            },
        )
        await state.set_state(TournamentStates.waiting_for_round_3)

        text = (
            "🏆 **Полуфинал пройден! Ты в Гранд-Финале!**\n\n"
            "⚔️ **Раунд 3: Гранд-Финал**\n"
            "🎯 **Цель:** Пригласи **50 человек**.\n"
            f"⏰ **Дедлайн:** до {end_time_str} МСК!\n"
            "👑 *Победитель забирает 100 звёзд!*"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔍 Проверить Гранд-Финал",
                        callback_data="check_t_round_3",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Сдаться", callback_data="fail_tournament"
                    )
                ],
            ]
        )
        await callback.message.edit_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    else:
        await callback.answer(
            f"❌ Нужно 15 человек! У тебя: {invited}/15", show_alert=True
        )


@router.callback_query(F.data == "check_t_round_3")
async def process_check_t_round_3(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    start_time = user.get("round_start_time")

    if not start_time or (
        datetime.utcnow() - start_time > timedelta(hours=1)
    ):
        await fail_tournament_logic(
            callback.from_user.id, state, callback.message
        )
        return

    invited = user.get("tournament_invited", 0)
    if invited >= 50:
        await users_col.update_one(
            {"user_id": callback.from_user.id},
            {
                "$inc": {"balance": 100},
                "$set": {
                    "active_tournament": False,
                    "tournament_stage": 0,
                    "tournament_invited": 0,
                },
            },
        )
        await state.clear()

        text = (
            "👑 **ПОБЕДА В ГРАНД-ФИНАЛЕ!** 👑\n\n"
            "Ты первым набрал 50 приглашений!\n"
            "🏆 На твой баланс зачислено **100 звёзд**!"
        )
        await callback.message.edit_text(
            text, reply_markup=main_menu_kb(), parse_mode="Markdown"
        )
    else:
        await callback.answer(
            f"❌ Нужно 50 человек! У тебя: {invited}/50", show_alert=True
        )


@router.callback_query(F.data == "fail_tournament")
async def process_fail_t_btn(callback: CallbackQuery, state: FSMContext):
    await fail_tournament_logic(
        callback.from_user.id, state, callback.message
    )


async def fail_tournament_logic(
    user_id: int, state: FSMContext, message: Message
):
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "active_tournament": False,
                "tournament_stage": 0,
                "tournament_invited": 0,
            }
        },
    )
    await state.clear()
    text = "❌ **Турнир завершен или ты сдался.** Статус сброшен, теперь ты можешь зайти в турнир заново!"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ В меню", callback_data="main_menu")]
        ]
    )
    await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


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
                f"✅ Ваша заявка на вывод **{amount} ⭐** отправлена администратору!\nОжидайте выплату."
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

    # Запускаем фоновый веб-сервер для Render
    asyncio.create_task(start_web_server())

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот 'Звёздная Арена' запущен 24/7!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())