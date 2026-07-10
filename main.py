"""
Главный файл. Меню:
 ▶️ старт | ⏹ стоп
 👤 аккаунт | 👥 группы
 📝 контент | ⚙️ настройка
 🗓 расписание рассылок

Доступ только для ID из config.ALLOWED_USER_IDS.
Каждый пользователь работает ТОЛЬКО со своими собственными Telegram-аккаунтами
(можно добавить несколько). Если аккаунт один — рассылка всегда идёт через него.
Если аккаунтов два и больше — какие из них реально рассылают, отмечается прямо
в разделе «👤 аккаунт» галочками.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from io import BytesIO

import qrcode
from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, BufferedInputFile
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
    waiting_photo = State()          # mode в FSM-данных: "photo" | "photo_text"
    waiting_photo_caption = State()


class SettingsStates(StatesGroup):
    waiting_interval = State()               # data: account_index
    waiting_delay = State()                  # data: account_index
    waiting_delay_between_accounts = State()


class ScheduleStates(StatesGroup):
    picking_days = State()                   # data: days=[int,...]
    waiting_time = State()                   # data: days=[int,...]


# ---------- безопасные edit_text/edit_reply_markup ----------

async def safe_edit_text(message: Message, text: str, reply_markup=None):
    """
    Обёртка над message.edit_text, которая не падает, если:
    - новое содержимое совпадает со старым (Telegram даёт ошибку "not modified");
    - у сообщения нет текста для правки (например, это было фото) — тогда
      сообщение удаляется и отправляется новое.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer(text, reply_markup=reply_markup)


async def safe_edit_reply_markup(message: Message, reply_markup=None):
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise


# ---------- парсинг/форматирование ----------

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


def format_interval(total_seconds) -> str:
    if not total_seconds:
        return "не задан (отправка один раз)"
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def parse_delay_spec(text: str) -> dict | None:
    """
    Парсит паузу между отправками в группы.
    "5"     -> фиксированная пауза 5 сек.
    "5-6"   -> случайная пауза от 5 до 6 сек. (с точностью до сотых)
    Дробные значения тоже допускаются: "5.5-6.2".
    """
    text = text.strip().replace(",", ".")
    if "-" in text[1:]:  # пропускаем возможный минус в самом начале (на случай опечатки)
        parts = text.split("-")
        if len(parts) != 2:
            return None
        try:
            lo, hi = float(parts[0]), float(parts[1])
        except ValueError:
            return None
    else:
        try:
            lo = hi = float(text)
        except ValueError:
            return None
    if lo < 0 or hi < 0:
        return None
    if hi < lo:
        lo, hi = hi, lo
    return {"min": lo, "max": hi}


def format_delay_spec(spec: dict) -> str:
    lo, hi = spec.get("min", 0), spec.get("max", 0)
    if lo == hi:
        return f"{lo} сек."
    return f"{lo}–{hi} сек. (случайно)"


def parse_single_delay(text: str) -> float | None:
    text = text.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_time_hhmm(text: str) -> str | None:
    text = text.strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23) or not (0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def content_preview(user: dict) -> str:
    ctype = user.get("content_type")
    if ctype == "text":
        return f"текст: {user['content_text']}"
    if ctype == "photo":
        return "фото (без текста)"
    if ctype == "photo_text":
        return f"фото + текст: {user['content_text']}"
    return "(не задан)"


# user_id -> список asyncio.Task (по одной на каждый рассылающий аккаунт)
running_tasks: dict[int, list[asyncio.Task]] = {}

# user_id -> {"client":..., "index":..., "task": asyncio.Task, "awaiting_password": bool}
pending_qr_logins: dict[int, dict] = {}


# def allowed(user_id: int) -> bool:
#     return user_id in config.ALLOWED_USER_IDS


async def _canonical_phone(client) -> str:
    me = await client.get_me()
    return f"+{me.phone}" if me and me.phone else "неизвестный номер"


# ---------- Доступ ----------

# @router.message.middleware()
# async def access_middleware(handler, event: Message, data):
#     if not allowed(event.from_user.id):
#         await event.answer("У вас нет доступа к этому боту. Обратитесь к администратору.")
#         return
#     return await handler(event, data)


# @router.callback_query.middleware()
# async def access_middleware_cb(handler, event: CallbackQuery, data):
#     if not allowed(event.from_user.id):
#         await event.answer("Нет доступа", show_alert=True)
#         return
#     return await handler(event, data)


# ---------- Главное меню ----------

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Меню:", reply_markup=kb.main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    pending = pending_qr_logins.pop(message.from_user.id, None)
    if pending and not pending["task"].done():
        pending["task"].cancel()
    await message.answer("Отменено.", reply_markup=kb.main_menu())


@router.callback_query(F.data == "menu_main")
async def cb_menu_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    pending = pending_qr_logins.pop(call.from_user.id, None)
    if pending and not pending["task"].done():
        pending["task"].cancel()
    await safe_edit_text(call.message, "Меню:", reply_markup=kb.main_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ---------- Аккаунт (несколько на пользователя) ----------

@router.callback_query(F.data == "menu_account")
async def cb_menu_account(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    if user["accounts"]:
        if len(user["accounts"]) == 1:
            text = f"Аккаунт: {user['accounts'][0]['phone']}\nРассылка ведётся через него."
        else:
            text = (
                f"Аккаунтов подключено: {len(user['accounts'])}\n"
                f"Отметьте галочками, какие будут рассылать при ▶️ старт."
            )
    else:
        text = "Аккаунтов пока нет. Добавь свой первый аккаунт."
    await safe_edit_text(
        call.message, text, reply_markup=kb.account_menu(user["accounts"], user["broadcast_accounts"])
    )


@router.callback_query(F.data == "account_add")
async def cb_account_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id

    old = pending_qr_logins.pop(user_id, None)
    if old and not old["task"].done():
        old["task"].cancel()

    account_index = storage.next_account_index(user_id)
    key = storage.session_key(user_id, account_index)
    client = await ub.get_client(key)

    await safe_edit_text(call.message, "Готовлю QR-код...")
    task = asyncio.create_task(
        _qr_login_flow(user_id, account_index, key, client, call.bot, call.message.chat.id)
    )
    pending_qr_logins[user_id] = {"client": client, "index": account_index, "task": task}


async def _send_qr(bot: Bot, chat_id: int, url: str, caption: str):
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    photo = BufferedInputFile(buf.read(), filename="login_qr.png")
    return await bot.send_photo(chat_id, photo, caption=caption, reply_markup=kb.cancel_button())


async def _finish_login(user_id: int, account_index: int, key: str, client, bot: Bot, chat_id: int):
    """Общая логика после успешной авторизации: проверка реестра номеров,
    добавление аккаунта либо отказ + автоматический выход, если номер уже был в базе."""
    phone = await _canonical_phone(client)
    if storage.is_phone_registered(phone):
        try:
            await ub.logout(key)
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            f"⚠️ Номер {phone} уже есть в базе данных бота. Аккаунт НЕ добавлен, "
            f"бот вышел из этого аккаунта.",
            reply_markup=kb.main_menu(),
        )
        return
    storage.add_account(user_id, account_index, phone)
    await bot.send_message(chat_id, f"Аккаунт {phone} подключён ✅", reply_markup=kb.main_menu())


async def _qr_login_flow(user_id: int, account_index: int, key: str, client, bot: Bot, chat_id: int):
    caption = (
        "Отсканируйте этот QR-код в приложении Telegram того аккаунта, который "
        "добавляете:\n\nНастройки → Устройства → Привязать устройство\n\n"
        "У QR-кода ограниченное время действия (около 2 минут). Если не успели — "
        "нажмите «добавить аккаунт (QR)» ещё раз."
    )
    msg = None
    try:
        if user_id not in pending_qr_logins:
            return  # отменено пользователем
        qr = await client.qr_login()
        msg = await _send_qr(bot, chat_id, qr.url, caption)
        try:
            await qr.wait(120)
        except asyncio.TimeoutError:
            pending_qr_logins.pop(user_id, None)
            try:
                await bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
            await bot.send_message(
                chat_id,
                "Время сканирования истекло. Нажмите «➕ добавить аккаунт (QR)» ещё раз.",
                reply_markup=kb.main_menu(),
            )
            return
        except SessionPasswordNeededError:
            try:
                await bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
            await bot.send_message(
                chat_id,
                "На этом аккаунте включён пароль (2FA). Отправьте его сообщением "
                "(или /cancel чтобы отменить):",
            )
            pending_qr_logins[user_id]["awaiting_password"] = True
            return
    except Exception as e:
        pending_qr_logins.pop(user_id, None)
        await bot.send_message(chat_id, f"Ошибка входа: {e}")
        return

    pending_qr_logins.pop(user_id, None)
    await _finish_login(user_id, account_index, key, client, bot, chat_id)


@router.message(F.text, lambda m: pending_qr_logins.get(m.from_user.id, {}).get("awaiting_password"))
async def process_qr_password(message: Message):
    user_id = message.from_user.id
    entry = pending_qr_logins.get(user_id)
    if not entry:
        return
    client = entry["client"]
    try:
        await client.sign_in(password=message.text.strip())
    except Exception as e:
        await message.answer(f"Не подошло: {e}. Попробуйте ещё раз или /cancel.")
        return

    pending_qr_logins.pop(user_id, None)
    key = storage.session_key(user_id, entry["index"])
    await _finish_login(user_id, entry["index"], key, client, message.bot, message.chat.id)


@router.callback_query(F.data == "account_add_phone")
async def cb_account_add_phone(call: CallbackQuery, state: FSMContext):
    old = pending_qr_logins.pop(call.from_user.id, None)
    if old and not old["task"].done():
        old["task"].cancel()
    await safe_edit_text(
        call.message,
        "Введите номер телефона аккаунта, который хотите добавить, "
        "в формате +79991234567\n\n"
        "⚠️ Если код придёт в самом Telegram (не по SMS), Telegram иногда блокирует "
        "вход, когда код вводится не в официальном приложении — это защита от кражи "
        "аккаунтов. Если столкнётесь с ошибкой — используйте вход через QR.",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(AccountStates.waiting_phone)


@router.message(AccountStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    account_index = storage.next_account_index(message.from_user.id)
    key = storage.session_key(message.from_user.id, account_index)
    try:
        phone_code_hash = await ub.request_code(key, phone)
    except PhoneNumberInvalidError:
        await message.answer("Неверный формат номера. Попробуйте ещё раз.")
        return
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    await state.update_data(phone=phone, phone_code_hash=phone_code_hash, account_index=account_index)
    await message.answer("Код отправлен. Введите его сюда так, как он пришёл.", reply_markup=kb.cancel_button())
    await state.set_state(AccountStates.waiting_code)


@router.message(AccountStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    data = await state.get_data()
    code = message.text.strip()
    key = storage.session_key(message.from_user.id, data["account_index"])
    try:
        await ub.sign_in_code(key, data["phone"], code, data["phone_code_hash"])
    except SessionPasswordNeededError:
        await message.answer(
            "На аккаунте включена двухфакторная аутентификация. Введите пароль:",
            reply_markup=kb.cancel_button(),
        )
        await state.set_state(AccountStates.waiting_password)
        return
    except PhoneCodeInvalidError:
        await message.answer(
            "Код не подошёл (неверный, либо Telegram заблокировал вход как "
            "подозрительный). Попробуйте снова или используйте вход через QR."
        )
        return
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    client = await ub.get_client(key)
    await _finish_login(message.from_user.id, data["account_index"], key, client, message.bot, message.chat.id)


@router.message(AccountStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    key = storage.session_key(message.from_user.id, data["account_index"])
    try:
        await ub.sign_in_password(key, message.text.strip())
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return

    await state.clear()
    client = await ub.get_client(key)
    await _finish_login(message.from_user.id, data["account_index"], key, client, message.bot, message.chat.id)


@router.callback_query(F.data.startswith("bcacc_toggle_"))
async def cb_toggle_broadcast_account(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    user = storage.toggle_broadcast_account(call.from_user.id, index)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.account_menu(user["accounts"], user["broadcast_accounts"])
    )


@router.callback_query(F.data == "account_remove_list")
async def cb_account_remove_list(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    if not user["accounts"]:
        await call.answer("Аккаунтов нет", show_alert=True)
        return
    await safe_edit_text(
        call.message, "Какой аккаунт удалить?", reply_markup=kb.account_remove_list_kb(user["accounts"])
    )


@router.callback_query(F.data.startswith("account_remove_"))
async def cb_account_remove(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    key = storage.session_key(call.from_user.id, index)
    await ub.logout(key)
    user = storage.remove_account(call.from_user.id, index)
    await safe_edit_text(
        call.message, "Аккаунт удалён.", reply_markup=kb.account_menu(user["accounts"], user["broadcast_accounts"])
    )


# ---------- Группы (у каждого аккаунта — свои, с постраничным списком) ----------

async def _show_groups_menu(call: CallbackQuery, index: int):
    acc = storage.get_account(call.from_user.id, index)
    await safe_edit_text(
        call.message,
        f"Аккаунт: {acc['phone']}\n"
        f"Загружено групп: {len(acc['groups'])}\nВыбрано: {len(acc['selected'])}",
        reply_markup=kb.groups_menu(index),
    )


@router.callback_query(F.data == "menu_groups")
async def cb_menu_groups(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    accounts = user["accounts"]
    if not accounts:
        await safe_edit_text(
            call.message,
            "Сначала подключите аккаунт (раздел «👤 аккаунт»).",
            reply_markup=kb.back_button("menu_main"),
        )
        return
    if len(accounts) == 1:
        idx = accounts[0]["index"]
        storage.update_user_data(call.from_user.id, groups_account=idx)
        await _show_groups_menu(call, idx)
        return
    await safe_edit_text(
        call.message,
        "Выберите аккаунт, чьи группы настраиваем:",
        reply_markup=kb.account_picker_kb(accounts, "groups_menu", "menu_main"),
    )


@router.callback_query(F.data.startswith("groups_menu_"))
async def cb_groups_menu_for_account(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    storage.update_user_data(call.from_user.id, groups_account=index)
    await _show_groups_menu(call, index)


@router.callback_query(F.data.startswith("groups_load_"))
async def cb_groups_load(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    key = storage.session_key(call.from_user.id, index)
    if not await ub.is_authorized(key):
        await call.answer("Аккаунт не авторизован", show_alert=True)
        return
    await call.answer("Загружаю список групп...")
    groups = await ub.fetch_groups(key)
    storage.update_account(call.from_user.id, index, groups=groups)
    await safe_edit_text(
        call.message, f"Готово. Загружено: {len(groups)} групп(ы).", reply_markup=kb.groups_menu(index)
    )


@router.callback_query(F.data.startswith("groups_select_"))
async def cb_groups_select(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    acc = storage.get_account(call.from_user.id, index)
    if not acc or not acc["groups"]:
        await call.answer("Сначала загрузите список групп", show_alert=True)
        return
    await safe_edit_text(
        call.message,
        f"Отметьте группы для рассылки (аккаунт {acc['phone']}):",
        reply_markup=kb.groups_select_kb(index, acc["groups"], acc["selected"], page=0),
    )


@router.callback_query(F.data.startswith("groups_page_"))
async def cb_groups_page(call: CallbackQuery):
    parts = call.data.split("_")
    index, page = int(parts[-2]), int(parts[-1])
    acc = storage.get_account(call.from_user.id, index)
    if not acc:
        await call.answer("Аккаунт не найден", show_alert=True)
        return
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.groups_select_kb(index, acc["groups"], acc["selected"], page)
    )


@router.callback_query(F.data.startswith("toggle_"))
async def cb_toggle_group(call: CallbackQuery):
    _, index_str, page_str, gid_str = call.data.split("_", 3)
    index, page, group_id = int(index_str), int(page_str), int(gid_str)
    acc = storage.get_account(call.from_user.id, index)
    selected = set(acc["selected"])
    if group_id in selected:
        selected.discard(group_id)
    else:
        selected.add(group_id)
    storage.update_account(call.from_user.id, index, selected=list(selected))
    acc = storage.get_account(call.from_user.id, index)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.groups_select_kb(index, acc["groups"], acc["selected"], page)
    )


@router.callback_query(F.data.startswith("groups_all_"))
async def cb_groups_select_all(call: CallbackQuery):
    parts = call.data.split("_")
    index, page = int(parts[-2]), int(parts[-1])
    acc = storage.get_account(call.from_user.id, index)
    all_ids = [g["id"] for g in acc["groups"]]
    storage.update_account(call.from_user.id, index, selected=all_ids)
    acc = storage.get_account(call.from_user.id, index)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.groups_select_kb(index, acc["groups"], acc["selected"], page)
    )


@router.callback_query(F.data.startswith("groups_reset_"))
async def cb_groups_reset(call: CallbackQuery):
    parts = call.data.split("_")
    index, page = int(parts[-2]), int(parts[-1])
    storage.update_account(call.from_user.id, index, selected=[])
    acc = storage.get_account(call.from_user.id, index)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.groups_select_kb(index, acc["groups"], acc["selected"], page)
    )


# ---------- Контент (текст / фото / фото+текст) ----------

@router.callback_query(F.data == "menu_content")
async def cb_menu_content(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    await safe_edit_text(
        call.message, f"Текущий контент рассылки:\n\n{content_preview(user)}", reply_markup=kb.content_menu()
    )


@router.callback_query(F.data == "content_set_text")
async def cb_content_set_text(call: CallbackQuery, state: FSMContext):
    await safe_edit_text(call.message, "Отправьте текст, который нужно разослать:", reply_markup=kb.cancel_button())
    await state.set_state(ContentStates.waiting_text)


@router.message(ContentStates.waiting_text)
async def process_content_text(message: Message, state: FSMContext):
    storage.update_user_data(
        message.from_user.id, content_type="text", content_text=message.text, content_photo=None
    )
    await state.clear()
    await message.answer("Текст сохранён ✅", reply_markup=kb.main_menu())


@router.callback_query(F.data == "content_set_photo")
async def cb_content_set_photo(call: CallbackQuery, state: FSMContext):
    await safe_edit_text(call.message, "Отправьте фото, которое нужно разослать:", reply_markup=kb.cancel_button())
    await state.update_data(mode="photo")
    await state.set_state(ContentStates.waiting_photo)


@router.callback_query(F.data == "content_set_photo_text")
async def cb_content_set_photo_text(call: CallbackQuery, state: FSMContext):
    await safe_edit_text(
        call.message,
        "Отправьте фото. Если сразу добавите подпись к фото — текст возьмётся из неё, "
        "иначе я спрошу текст отдельным сообщением.",
        reply_markup=kb.cancel_button(),
    )
    await state.update_data(mode="photo_text")
    await state.set_state(ContentStates.waiting_photo)


async def _save_photo(message: Message, user_id: int) -> str:
    os.makedirs(config.MEDIA_DIR, exist_ok=True)
    path = os.path.join(config.MEDIA_DIR, f"user_{user_id}.jpg")
    await message.bot.download(message.photo[-1], destination=path)
    return path


@router.message(ContentStates.waiting_photo, F.photo)
async def process_content_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", "photo")
    path = await _save_photo(message, message.from_user.id)

    if mode == "photo":
        storage.update_user_data(
            message.from_user.id, content_type="photo", content_text=None, content_photo=path
        )
        await state.clear()
        await message.answer("Фото сохранено ✅", reply_markup=kb.main_menu())
        return

    if message.caption:
        storage.update_user_data(
            message.from_user.id, content_type="photo_text", content_text=message.caption, content_photo=path
        )
        await state.clear()
        await message.answer("Фото и текст сохранены ✅", reply_markup=kb.main_menu())
        return

    await state.update_data(photo_path=path)
    await message.answer("Теперь отправьте текст (подпись) к этому фото:", reply_markup=kb.cancel_button())
    await state.set_state(ContentStates.waiting_photo_caption)


@router.message(ContentStates.waiting_photo)
async def process_content_photo_wrong_type(message: Message):
    await message.answer("Пришлите именно фото (картинкой, не файлом-документом).")


@router.message(ContentStates.waiting_photo_caption)
async def process_photo_caption(message: Message, state: FSMContext):
    data = await state.get_data()
    storage.update_user_data(
        message.from_user.id, content_type="photo_text", content_text=message.text, content_photo=data["photo_path"]
    )
    await state.clear()
    await message.answer("Фото и текст сохранены ✅", reply_markup=kb.main_menu())


# ---------- Настройки ----------

async def _show_settings_menu(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    accounts = user["accounts"]
    if not accounts:
        await safe_edit_text(
            call.message,
            "Сначала подключите аккаунт (раздел «👤 аккаунт»).",
            reply_markup=kb.back_button("menu_main"),
        )
        return
    selected_index = user["settings_account"]
    if len(accounts) == 1:
        text = "Выберите настройку:"
    else:
        acc = storage.get_account(call.from_user.id, selected_index)
        text = f'Выберите настройку по номеру "{acc["phone"]}":'
    await safe_edit_text(
        call.message, text, reply_markup=kb.settings_menu(accounts, selected_index, len(user["broadcast_accounts"]))
    )


@router.callback_query(F.data == "menu_settings")
async def cb_menu_settings(call: CallbackQuery):
    await _show_settings_menu(call)


@router.callback_query(F.data.startswith("settings_switch_"))
async def cb_settings_switch(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    storage.update_user_data(call.from_user.id, settings_account=index)
    await _show_settings_menu(call)


# --- Интервал (для выбранного в настройках аккаунта) ---

@router.callback_query(F.data == "settings_interval")
async def cb_settings_interval(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    acc = storage.get_account(call.from_user.id, user["settings_account"])
    if not acc:
        await call.answer("Сначала добавьте аккаунт", show_alert=True)
        return
    await safe_edit_text(
        call.message,
        f"Интервал для {acc['phone']}: {format_interval(acc['interval'])}",
        reply_markup=kb.interval_settings_kb(acc["index"]),
    )


@router.callback_query(F.data == "settings_interval_set")
async def cb_settings_interval_set(call: CallbackQuery, state: FSMContext):
    user = storage.get_user_data(call.from_user.id)
    await state.update_data(account_index=user["settings_account"])
    await safe_edit_text(
        call.message,
        "Отправьте интервал в формате часы:минуты:секунды\nНапример: 1:15:10 "
        "(раз в 1 час 15 минут 10 секунд)",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(SettingsStates.waiting_interval)


@router.callback_query(F.data == "settings_interval_once")
async def cb_settings_interval_once(call: CallbackQuery):
    """Разовая отправка прямо сейчас. Сохранённый интервал НЕ трогаем."""
    user = storage.get_user_data(call.from_user.id)
    idx = user["settings_account"]
    acc = storage.get_account(call.from_user.id, idx)
    if not acc:
        await call.answer("Сначала добавьте аккаунт", show_alert=True)
        return
    if not acc["selected"]:
        await call.answer(f"Не настроена группа на номере — {acc['phone']}", show_alert=True)
        return
    if not user["content_type"]:
        await call.answer("Сначала задайте контент рассылки", show_alert=True)
        return
    await call.answer("Отправляю один раз...")
    asyncio.create_task(_send_once_for_account(call.from_user.id, idx, call.bot))


@router.message(SettingsStates.waiting_interval)
async def process_interval(message: Message, state: FSMContext):
    seconds = parse_interval(message.text)
    if seconds is None:
        await message.answer("Неверный формат. Пришлите интервал как часы:минуты:секунды, например 1:15:10")
        return
    data = await state.get_data()
    storage.update_account(message.from_user.id, data["account_index"], interval=seconds)
    await state.clear()
    await message.answer(f"Интервал сохранён: {format_interval(seconds)} ✅", reply_markup=kb.main_menu())


# --- Пауза между отправками в группы (случайная, для выбранного аккаунта) ---

@router.callback_query(F.data == "settings_delay")
async def cb_settings_delay(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    acc = storage.get_account(call.from_user.id, user["settings_account"])
    if not acc:
        await call.answer("Сначала добавьте аккаунт", show_alert=True)
        return
    await safe_edit_text(
        call.message,
        f"Пауза между отправками для {acc['phone']}: {format_delay_spec(acc['delay'])}\n\n"
        f"Формат: одно число — фиксированная пауза (например 5).\n"
        f"Диапазон вида 5-7 — пауза каждый раз случайна между 5 и 7 сек. "
        f"(с точностью до сотых секунды, например 5.10, потом 5.78).",
        reply_markup=kb.delay_settings_kb(),
    )


@router.callback_query(F.data == "settings_delay_set")
async def cb_settings_delay_set(call: CallbackQuery, state: FSMContext):
    user = storage.get_user_data(call.from_user.id)
    await state.update_data(account_index=user["settings_account"])
    await safe_edit_text(
        call.message,
        "Отправьте паузу: число (например 5) или диапазон (например 5-7):",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(SettingsStates.waiting_delay)


@router.message(SettingsStates.waiting_delay)
async def process_delay(message: Message, state: FSMContext):
    spec = parse_delay_spec(message.text)
    if spec is None:
        await message.answer("Неверный формат. Пришлите число (5) или диапазон (5-7).")
        return
    data = await state.get_data()
    storage.update_account(message.from_user.id, data["account_index"], delay=spec)
    await state.clear()
    await message.answer(f"Пауза сохранена: {format_delay_spec(spec)} ✅", reply_markup=kb.main_menu())


# --- Пауза между аккаунтами (нужно 2+ аккаунта, отмеченных для рассылки) ---

@router.callback_query(F.data == "settings_delay_between_accounts")
async def cb_settings_delay_between_accounts(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    if len(user["broadcast_accounts"]) < 2:
        await safe_edit_text(
            call.message,
            "Пауза между аккаунтами доступна, только если для рассылки отмечено "
            "2 и более аккаунта (раздел «👤 аккаунт»).",
            reply_markup=kb.back_button("menu_settings"),
        )
        return
    value = user.get("delay_between_accounts", 0)
    await safe_edit_text(
        call.message,
        f"Пауза между аккаунтами: {value} сек.\n"
        f"0 = все отмеченные аккаунты стартуют одновременно.\n"
        f"Больше 0 = каждый следующий аккаунт стартует с такой задержкой после предыдущего.",
        reply_markup=kb.delay_between_accounts_kb(),
    )


@router.callback_query(F.data == "settings_delay_between_accounts_set")
async def cb_settings_delay_between_accounts_set(call: CallbackQuery, state: FSMContext):
    user = storage.get_user_data(call.from_user.id)
    if len(user["broadcast_accounts"]) < 2:
        await call.answer("Нужно отметить 2+ аккаунта для рассылки", show_alert=True)
        return
    await safe_edit_text(
        call.message,
        "Отправьте паузу между аккаунтами в секундах (0 = одновременно):",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(SettingsStates.waiting_delay_between_accounts)


@router.message(SettingsStates.waiting_delay_between_accounts)
async def process_delay_between_accounts(message: Message, state: FSMContext):
    value = parse_single_delay(message.text)
    if value is None:
        await message.answer("Неверный формат. Пришлите число секунд, например 0 или 5.5")
        return
    user = storage.get_user_data(message.from_user.id)
    if len(user["broadcast_accounts"]) < 2:
        await state.clear()
        await message.answer(
            "Пока вы вводили значение, отмеченных аккаунтов стало меньше 2 — настройка не сохранена.",
            reply_markup=kb.main_menu(),
        )
        return
    storage.update_user_data(message.from_user.id, delay_between_accounts=value)
    await state.clear()
    await message.answer(f"Пауза между аккаунтами сохранена: {value} сек. ✅", reply_markup=kb.main_menu())


# ---------- Расписание рассылок (теперь в главном меню) ----------

@router.callback_query(F.data == "menu_schedule")
async def cb_menu_schedule(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    status = "🟢 включено" if user.get("schedule_enabled") else "🔴 выключено"
    await safe_edit_text(
        call.message,
        f"Расписание рассылок. Статус: {status}\n\n"
        f"Бот будет автоматически запускать рассылку в указанное время (по времени "
        f"сервера, на котором запущен бот).\n\n"
        f"Включается кнопкой ▶️ старт в главном меню (если расписание не пустое), "
        f"выключается кнопкой ⏹ стоп.",
        reply_markup=kb.schedule_menu(user["schedule"], user.get("schedule_enabled", False)),
    )


@router.callback_query(F.data == "schedule_add")
async def cb_schedule_add(call: CallbackQuery, state: FSMContext):
    await state.update_data(days=[])
    await safe_edit_text(
        call.message,
        "Выберите дни недели (можно несколько) или «каждый день», затем нажмите «дальше»:",
        reply_markup=kb.schedule_days_kb([]),
    )
    await state.set_state(ScheduleStates.picking_days)


@router.callback_query(ScheduleStates.picking_days, F.data.startswith("schedule_day_"))
async def cb_schedule_toggle_day(call: CallbackQuery, state: FSMContext):
    suffix = call.data.rsplit("_", 1)[-1]
    data = await state.get_data()
    days = set(data.get("days", []))
    if suffix == "all":
        days = set()  # пусто = "каждый день"
    else:
        day = int(suffix)
        if day in days:
            days.discard(day)
        else:
            days.add(day)
    await state.update_data(days=list(days))
    await safe_edit_reply_markup(call.message, reply_markup=kb.schedule_days_kb(list(days)))


@router.callback_query(ScheduleStates.picking_days, F.data == "schedule_days_done")
async def cb_schedule_days_done(call: CallbackQuery, state: FSMContext):
    await safe_edit_text(
        call.message, "Введите время в формате ЧЧ:ММ (например 09:00):", reply_markup=kb.back_button("menu_schedule")
    )
    await state.set_state(ScheduleStates.waiting_time)


@router.message(ScheduleStates.waiting_time)
async def process_schedule_time(message: Message, state: FSMContext):
    time_str = parse_time_hhmm(message.text)
    if time_str is None:
        await message.answer("Неверный формат. Пришлите время как ЧЧ:ММ, например 09:00")
        return
    data = await state.get_data()
    storage.add_schedule_entry(message.from_user.id, data.get("days", []), time_str)
    await state.clear()
    await message.answer(f"Добавлено в расписание: {time_str} ✅", reply_markup=kb.main_menu())


@router.callback_query(F.data.startswith("schedule_remove_"))
async def cb_schedule_remove(call: CallbackQuery):
    entry_id = int(call.data.rsplit("_", 1)[-1])
    user = storage.remove_schedule_entry(call.from_user.id, entry_id)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.schedule_menu(user["schedule"], user.get("schedule_enabled", False))
    )


# ---------- Рассылка (одновременно/со сдвигом с нескольких аккаунтов) ----------

def _eligible_broadcast_indices(user: dict) -> list[int]:
    result = []
    for idx in user["broadcast_accounts"]:
        acc = next((a for a in user["accounts"] if a["index"] == idx), None)
        if acc and acc["selected"]:
            result.append(idx)
    return result


async def _send_once_for_account(user_id: int, account_index: int, bot: Bot):
    user = storage.get_user_data(user_id)
    acc = storage.get_account(user_id, account_index)
    if not acc or not user["content_type"] or not acc["selected"]:
        return
    content = {
        "type": user["content_type"],
        "text": user["content_text"],
        "photo": user["content_photo"],
    }
    key = storage.session_key(user_id, account_index)
    try:
        if await ub.is_authorized(key):
            await ub.broadcast(key, acc["selected"], content, acc["delay"])
    except Exception as e:
        logger.error(f"Ошибка рассылки для {user_id}, аккаунт {account_index}: {e}")
        try:
            await bot.send_message(user_id, f"Ошибка рассылки (аккаунт {acc['phone']}): {e}")
        except Exception:
            pass


async def _account_broadcast_loop(user_id: int, account_index: int, start_offset: float, bot: Bot):
    if start_offset:
        await asyncio.sleep(start_offset)

    while True:
        await _send_once_for_account(user_id, account_index, bot)

        acc = storage.get_account(user_id, account_index)
        interval = acc["interval"] if acc else None
        if not interval:
            return
        await asyncio.sleep(interval)


@router.callback_query(F.data == "broadcast_start")
async def cb_broadcast_start(call: CallbackQuery):
    user_id = call.from_user.id
    user = storage.get_user_data(user_id)

    # Если настроено расписание — старт включает именно его, а не немедленную рассылку.
    if user["schedule"]:
        storage.update_user_data(user_id, schedule_enabled=True)
        await safe_edit_text(
            call.message,
            "🗓 Расписание запущено. Рассылка будет выполняться автоматически по "
            "заданным дням и времени. Нажмите ⏹ стоп, чтобы остановить.",
            reply_markup=kb.main_menu(),
        )
        return

    broadcast_idx = user["broadcast_accounts"]
    if not broadcast_idx:
        await call.answer(
            "Нет аккаунтов, отмеченных для рассылки (раздел «👤 аккаунт»).", show_alert=True
        )
        return

    missing = []
    for idx in broadcast_idx:
        acc = storage.get_account(user_id, idx)
        if not acc or not acc["selected"]:
            missing.append(acc["phone"] if acc else str(idx))
    if missing:
        await call.answer("Не настроена группа на номере — " + ", ".join(missing), show_alert=True)
        return

    if not user["content_type"]:
        await call.answer("Сначала задайте контент рассылки", show_alert=True)
        return

    existing = running_tasks.get(user_id)
    if existing and any(not t.done() for t in existing):
        await call.answer("Рассылка уже запущена", show_alert=True)
        return

    indices = broadcast_idx
    delay_between_accounts = user.get("delay_between_accounts", 0) if len(indices) >= 2 else 0

    tasks = []
    for i, idx in enumerate(indices):
        offset = i * delay_between_accounts
        tasks.append(asyncio.create_task(_account_broadcast_loop(user_id, idx, offset, call.bot)))
    running_tasks[user_id] = tasks

    accounts_desc = ", ".join(storage.get_account(user_id, idx)["phone"] for idx in indices)
    lines = [f"Рассылка запущена. Аккаунты ({len(indices)}): {accounts_desc}"]
    if len(indices) >= 2 and delay_between_accounts:
        lines.append(f"Пауза между стартом аккаунтов: {delay_between_accounts} сек.")
    elif len(indices) >= 2:
        lines.append("Все аккаунты стартуют одновременно.")
    await safe_edit_text(call.message, "\n".join(lines), reply_markup=kb.main_menu())


@router.callback_query(F.data == "broadcast_stop")
async def cb_broadcast_stop(call: CallbackQuery):
    user_id = call.from_user.id
    user = storage.get_user_data(user_id)
    stopped_anything = False

    if user.get("schedule_enabled"):
        storage.update_user_data(user_id, schedule_enabled=False)
        stopped_anything = True

    tasks = running_tasks.pop(user_id, [])
    active = [t for t in tasks if not t.done()]
    if active:
        for t in active:
            t.cancel()
        stopped_anything = True

    if stopped_anything:
        await safe_edit_text(call.message, "Рассылка/расписание остановлены.", reply_markup=kb.main_menu())
    else:
        await call.answer("Рассылка не запущена", show_alert=True)


# ---------- Планировщик расписания ----------

async def _run_scheduled_broadcast(user_id: int, bot: Bot):
    user = storage.get_user_data(user_id)
    indices = _eligible_broadcast_indices(user)
    if not indices or not user["content_type"]:
        return
    delay_between_accounts = user.get("delay_between_accounts", 0) if len(indices) >= 2 else 0
    for i, idx in enumerate(indices):
        offset = i * delay_between_accounts

        async def _delayed(idx=idx, offset=offset):
            if offset:
                await asyncio.sleep(offset)
            await _send_once_for_account(user_id, idx, bot)

        asyncio.create_task(_delayed())


# async def scheduler_loop(bot: Bot):
#     fired: set[tuple[int, int, str]] = set()
#     while True:
#         now = datetime.now()
#         hhmm = now.strftime("%H:%M")
#         weekday = now.weekday()
#         stamp = now.strftime("%Y-%m-%d %H:%M")
#
#         for user_id in config.ALLOWED_USER_IDS:
#             user = storage.get_user_data(user_id)
#             if not user.get("schedule_enabled"):
#                 continue
#             for entry in user["schedule"]:
#                 if entry["time"] != hhmm:
#                     continue
#                 if entry["days"] and weekday not in entry["days"]:
#                     continue
#                 key = (user_id, entry["id"], stamp)
#                 if key in fired:
#                     continue
#                 fired.add(key)
#                 asyncio.create_task(_run_scheduled_broadcast(user_id, bot))
#
#         if len(fired) > 5000:
#             cutoff = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
#             fired = {k for k in fired if k[2] >= cutoff}
#
#         await asyncio.sleep(config.SCHEDULER_CHECK_INTERVAL)


async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    #asyncio.create_task(scheduler_loop(bot))
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
