"""
Главный файл. Меню в стиле:
 👤 аккаунт | 👥 группы | 📝 контент | 🚀 запустить рассылку

Доступ только для ID из config.ALLOWED_USER_IDS.
Каждый пользователь работает ТОЛЬКО со своим собственным Telegram-аккаунтом
и рассылает только в группы, где сам состоит.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
)

import config
import storage
import userbot_manager as ub
import keyboards as kb

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


class AccountStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class ContentStates(StatesGroup):
    waiting_text = State()


class SettingsStates(StatesGroup):
    waiting_interval = State()


def parse_interval(text: str) -> int | None:
    """Парсит строку вида H:MM:SS в количество секунд. Возвращает None, если формат неверный."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    if hours < 0 or not (0 <= minutes <= 59) or not (0 <= seconds <= 59):
        return None
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        return None
    return total


def format_interval(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# user_id -> asyncio.Task с циклом рассылки
running_tasks: dict[int, asyncio.Task] = {}


def allowed(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


# ---------- Доступ ----------

@router.message.middleware()
async def access_middleware(handler, event: Message, data):
    if not allowed(event.from_user.id):
        await event.answer(
            "У вас нет доступа к этому боту. Обратитесь к администратору."
        )
        return
    return await handler(event, data)


@router.callback_query.middleware()
async def access_middleware_cb(handler, event: CallbackQuery, data):
    if not allowed(event.from_user.id):
        await event.answer("Нет доступа", show_alert=True)
        return
    return await handler(event, data)


# ---------- Главное меню ----------

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Меню:",
        reply_markup=kb.main_menu(),
    )


@router.callback_query(F.data == "menu_main")
async def cb_menu_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Меню:", reply_markup=kb.main_menu())


# ---------- Аккаунт ----------

@router.callback_query(F.data == "menu_account")
async def cb_menu_account(call: CallbackQuery):
    is_auth = await ub.is_authorized(call.from_user.id)
    status = "✅ подключён" if is_auth else "❌ не подключён"
    await call.message.edit_text(
        f"Аккаунт: {status}",
        reply_markup=kb.account_menu(is_auth),
    )


@router.callback_query(F.data == "account_add")
async def cb_account_add(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "Введите номер телефона своего аккаунта в формате +79991234567",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(AccountStates.waiting_phone)


@router.message(AccountStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    try:
        phone_code_hash = await ub.request_code(message.from_user.id, phone)
    except PhoneNumberInvalidError:
        await message.answer("Неверный формат номера. Попробуйте ещё раз.")
        return
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    await state.update_data(phone=phone, phone_code_hash=phone_code_hash)
    await message.answer(
        "Код отправлен в Telegram на этот номер. Введите его сюда.",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(AccountStates.waiting_code)


@router.message(AccountStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    data = await state.get_data()
    code = message.text.strip()
    try:
        await ub.sign_in_code(
            message.from_user.id, data["phone"], code, data["phone_code_hash"]
        )
    except SessionPasswordNeededError:
        await message.answer(
            "На аккаунте включена двухфакторная аутентификация. Введите пароль:",
            reply_markup=kb.cancel_button(),
        )
        await state.set_state(AccountStates.waiting_password)
        return
    except PhoneCodeInvalidError:
        await message.answer("Неверный код, попробуйте ещё раз.")
        return
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    storage.update_user_data(message.from_user.id, phone=data["phone"])
    await state.clear()
    await message.answer("Аккаунт подключён ✅", reply_markup=kb.main_menu())


@router.message(AccountStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    try:
        await ub.sign_in_password(message.from_user.id, message.text.strip())
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    data = await state.get_data()
    storage.update_user_data(message.from_user.id, phone=data.get("phone"))
    await state.clear()
    await message.answer("Аккаунт подключён ✅", reply_markup=kb.main_menu())


@router.callback_query(F.data == "account_logout")
async def cb_account_logout(call: CallbackQuery):
    await ub.logout(call.from_user.id)
    storage.update_user_data(call.from_user.id, phone=None, groups=[], selected=[])
    await call.message.edit_text("Аккаунт отключён.", reply_markup=kb.main_menu())


# ---------- Группы ----------

@router.callback_query(F.data == "menu_groups")
async def cb_menu_groups(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    await call.message.edit_text(
        f"Загружено групп: {len(user['groups'])}\nВыбрано: {len(user['selected'])}",
        reply_markup=kb.groups_menu(),
    )


@router.callback_query(F.data == "groups_load")
async def cb_groups_load(call: CallbackQuery):
    if not await ub.is_authorized(call.from_user.id):
        await call.answer("Сначала подключите аккаунт", show_alert=True)
        return
    await call.answer("Загружаю список групп...")
    groups = await ub.fetch_groups(call.from_user.id)
    storage.update_user_data(call.from_user.id, groups=groups)
    await call.message.edit_text(
        f"Готово. Загружено: {len(groups)} групп(ы).",
        reply_markup=kb.groups_menu(),
    )


@router.callback_query(F.data == "groups_select")
async def cb_groups_select(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    if not user["groups"]:
        await call.answer("Сначала загрузите список групп", show_alert=True)
        return
    await call.message.edit_text(
        "Отметьте группы для рассылки:",
        reply_markup=kb.groups_select_kb(user["groups"], user["selected"]),
    )


@router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_group(call: CallbackQuery):
    group_id = int(call.data.split("_", 1)[1])
    user = storage.get_user_data(call.from_user.id)
    selected = set(user["selected"])
    if group_id in selected:
        selected.discard(group_id)
    else:
        selected.add(group_id)
    user["selected"] = list(selected)
    storage.set_user_data(call.from_user.id, user)
    await call.message.edit_reply_markup(
        reply_markup=kb.groups_select_kb(user["groups"], user["selected"])
    )


# ---------- Контент ----------

@router.callback_query(F.data == "menu_content")
async def cb_menu_content(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    preview = user["content"] or "(не задан)"
    await call.message.edit_text(
        f"Текущий текст рассылки:\n\n{preview}",
        reply_markup=kb.content_menu(),
    )


@router.callback_query(F.data == "content_set_text")
async def cb_content_set_text(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "Отправьте текст, который нужно разослать:",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(ContentStates.waiting_text)


@router.message(ContentStates.waiting_text)
async def process_content_text(message: Message, state: FSMContext):
    storage.update_user_data(message.from_user.id, content=message.text)
    await state.clear()
    await message.answer("Текст сохранён ✅", reply_markup=kb.main_menu())


# ---------- Настройки ----------

@router.callback_query(F.data == "menu_settings")
async def cb_menu_settings(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    interval = user.get("interval")
    text = f"Интервал рассылки: {format_interval(interval)}" if interval else "Интервал не задан (рассылка выполнится один раз при старте)"
    await call.message.edit_text(
        f"Выберите настройку:\n\n{text}",
        reply_markup=kb.settings_menu(),
    )


@router.callback_query(F.data == "settings_interval")
async def cb_settings_interval(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "Отправьте интервал в формате часы:минуты:секунды\nНапример: 1:15:10 "
        "(раз в 1 час 15 минут 10 секунд)",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(SettingsStates.waiting_interval)


@router.message(SettingsStates.waiting_interval)
async def process_interval(message: Message, state: FSMContext):
    seconds = parse_interval(message.text)
    if seconds is None:
        await message.answer(
            "Неверный формат. Пришлите интервал как часы:минуты:секунды, например 1:15:10"
        )
        return
    storage.update_user_data(message.from_user.id, interval=seconds)
    await state.clear()
    await message.answer(
        f"Интервал сохранён: {format_interval(seconds)} ✅",
        reply_markup=kb.main_menu(),
    )


# ---------- Рассылка ----------

async def broadcast_loop(user_id: int, bot: Bot):
    while True:
        user = storage.get_user_data(user_id)
        if user["selected"] and user["content"]:
            try:
                sent, failed = await ub.broadcast(
                    user_id, user["selected"], user["content"], config.DELAY_BETWEEN_SENDS
                )
                await bot.send_message(
                    user_id, f"Рассылка выполнена. Успешно: {sent}, ошибок: {failed}."
                )
            except Exception as e:
                await bot.send_message(user_id, f"Ошибка рассылки: {e}")

        interval = storage.get_user_data(user_id).get("interval")
        if not interval:
            # Интервал не задан — рассылка разовая, дальше не повторяем
            running_tasks.pop(user_id, None)
            return
        await asyncio.sleep(interval)


@router.callback_query(F.data == "broadcast_start")
async def cb_broadcast_start(call: CallbackQuery):
    user_id = call.from_user.id
    user = storage.get_user_data(user_id)

    if not await ub.is_authorized(user_id):
        await call.answer("Сначала подключите аккаунт", show_alert=True)
        return
    if not user["selected"]:
        await call.answer("Сначала выберите группы", show_alert=True)
        return
    if not user["content"]:
        await call.answer("Сначала задайте текст рассылки", show_alert=True)
        return

    existing = running_tasks.get(user_id)
    if existing and not existing.done():
        await call.answer("Рассылка уже запущена", show_alert=True)
        return

    interval = user.get("interval")
    if interval:
        await call.message.edit_text(
            f"Рассылка запущена, интервал: {format_interval(interval)}. "
            f"Групп: {len(user['selected'])}.",
            reply_markup=kb.main_menu(),
        )
    else:
        await call.message.edit_text(
            f"Рассылаю в {len(user['selected'])} групп(ы) один раз "
            f"(интервал не задан — настройте его в ⚙️ настройка, если нужны повторы)...",
        )

    task = asyncio.create_task(broadcast_loop(user_id, call.bot))
    running_tasks[user_id] = task


@router.callback_query(F.data == "broadcast_stop")
async def cb_broadcast_stop(call: CallbackQuery):
    task = running_tasks.pop(call.from_user.id, None)
    if task and not task.done():
        task.cancel()
        await call.message.edit_text("Рассылка остановлена.", reply_markup=kb.main_menu())
    else:
        await call.answer("Рассылка не запущена", show_alert=True)


async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
