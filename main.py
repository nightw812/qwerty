"""
Главный файл. Меню:
 ▶️ старт | ⏹ стоп
 🕐 рассылка по времени | 📊 статистика
 👤 аккаунт | 👥 группы
 📝 контент | ⚙️ настройка
 🗓 расписание рассылок
 ❓ FAQ

Доступ только для ID из config.ALLOWED_USER_IDS.
Каждый пользователь работает ТОЛЬКО со своими собственными Telegram-аккаунтами
(можно добавить несколько). Если аккаунт один — рассылка всегда идёт через него.
Если аккаунтов два и больше — какие из них реально рассылают, отмечается прямо
в разделе «👤 аккаунт» галочками. Контент и часть настроек — на каждый аккаунт
свои.
"""

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(MSK)

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
    FloodWaitError,
)

import config
import storage
import spintext
import userbot_manager as ub
import keyboards as kb  # ← ИМПОРТИРУЕМ из keyboards.py
from emoji_utils import emoji

# ... ОСТАЛЬНОЙ КОД main.py (без дублирования клавиатур!)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


class AccountStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class ContentStates(StatesGroup):
    waiting_text = State()           # data: account_index
    waiting_photo = State()          # data: account_index, mode="photo"|"photo_text"
    waiting_photo_caption = State()  # data: account_index, photo_path
    waiting_forward = State()        # data: account_index


class SettingsStates(StatesGroup):
    waiting_interval = State()               # data: account_index
    waiting_delay = State()                  # data: account_index
    waiting_delay_between_accounts = State()


class ScheduleStates(StatesGroup):
    picking_days = State()                   # data: days=[int,...]
    waiting_start_time = State()             # data: days=[int,...]
    waiting_end_time = State()               # data: days=[int,...], start="HH:MM"


# ---------- безопасные edit_text/edit_reply_markup ----------

async def safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode=None):
    """
    Обёртка над message.edit_text, которая не падает, если:
    - новое содержимое совпадает со старым (Telegram даёт ошибку "not modified");
    - у сообщения нет текста для правки (например, это было фото) — тогда
      сообщение удаляется и отправляется новое.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def safe_edit_reply_markup(message: Message, reply_markup=None):
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise


# ---------- парсинг/форматирование ----------

def parse_interval(text: str) -> dict | None:
    """
    Парсит интервал. Поддерживает два формата:
    - Фиксированный: 0:1:15
    - Рандомный: 0:1:15-0:1:20

    Возвращает dict {"min": seconds, "max": seconds} или None при ошибке.
    """
    text = text.strip()

    # Проверяем, есть ли диапазон
    if "-" in text:
        parts = text.split("-")
        if len(parts) != 2:
            return None

        min_val = _parse_single_interval(parts[0])
        max_val = _parse_single_interval(parts[1])

        if min_val is None or max_val is None:
            return None

        if min_val > max_val:
            min_val, max_val = max_val, min_val

        return {"min": min_val, "max": max_val}
    else:
        val = _parse_single_interval(text)
        if val is None:
            return None
        return {"min": val, "max": val}


def _parse_single_interval(text: str) -> int | None:
    """Парсит один интервал в формате часы:минуты:секунды."""
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


def format_interval(interval_spec: dict) -> str:
    """Форматирует интервал для отображения пользователю."""
    if not interval_spec:
        return "не задан (отправка один раз)"

    min_val = interval_spec.get("min", 0)
    max_val = interval_spec.get("max", 0)

    h, rem = divmod(int(min_val), 3600)
    m, s = divmod(rem, 60)
    min_str = f"{h}:{m:02d}:{s:02d}"

    if min_val == max_val:
        return min_str

    h, rem = divmod(int(max_val), 3600)
    m, s = divmod(rem, 60)
    max_str = f"{h}:{m:02d}:{s:02d}"

    return f"{min_str}–{max_str} (случайно)"


def format_interval(total_seconds) -> str:
    if not total_seconds:
        return "не задан (отправка один раз)"
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def parse_delay_spec(text: str) -> dict | None:
    text = text.strip().replace(",", ".")
    if "-" in text[1:]:
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


def _content_preview(acc: dict) -> str:
    ctype = acc.get("content_type")
    if ctype == "text":
        return f"текст: {acc['content_text']}"
    if ctype == "photo":
        return "фото (без текста)"
    if ctype == "photo_text":
        return f"фото + текст: {acc['content_text']}"
    if ctype == "forward":
        return "пересылаемое сообщение"
    return "(не задан)"


def _groups_back_target(user: dict) -> str:
    return "menu_main" if len(user["accounts"]) == 1 else "menu_groups"


def _content_back_target(user: dict) -> str:
    return "menu_main" if len(user["accounts"]) == 1 else "menu_content"


def _extract_forward_origin(message: Message):
    """
    Возвращает (chat_id, message_id) исходного сообщения.
    Теперь работает даже если пересылка запрещена — пытается найти источник
    любым доступным способом.
    """
    # Способ 1: через forward_origin (новый API)
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        msg_id = getattr(origin, "message_id", None)
        if chat is not None and msg_id is not None:
            return chat.id, msg_id

    # Способ 2: через forward_from_chat (старый API)
    if message.forward_from_chat and message.forward_from_message_id:
        return message.forward_from_chat.id, message.forward_from_message_id

    # Способ 3: если переслано от пользователя
    if message.forward_from and message.forward_from_message_id:
        return message.forward_from.id, message.forward_from_message_id

    # Способ 4: если переслано из канала с подписью
    if hasattr(message, "forward_sender_name") and message.forward_sender_name:
        # В этом случае мы не можем определить chat_id, но можем попробовать
        # использовать медиа-группу или другие поля
        pass

    # Способ 5: пробуем найти через reply_to_message (если есть)
    if message.reply_to_message:
        reply = message.reply_to_message
        if reply.chat and reply.message_id:
            # Если это ответ на сообщение, можно использовать его как источник
            return reply.chat.id, reply.message_id

    return None, None


# user_id -> список asyncio.Task (по одной на каждый рассылающий аккаунт)
running_tasks: dict[int, list] = {}

# user_id -> {"client":..., "index":..., "task": asyncio.Task, "awaiting_password": bool}
pending_qr_logins: dict[int, dict] = {}


def allowed(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


async def _canonical_phone(client) -> str:
    me = await client.get_me()
    return f"+{me.phone}" if me and me.phone else "неизвестный номер"


# ---------- Доступ ----------

@router.message.middleware()
async def access_middleware(handler, event: Message, data):
    if not allowed(event.from_user.id):
        await event.answer("У вас нет доступа к этому боту. Обратитесь к администратору.")
        return
    return await handler(event, data)


@router.callback_query.middleware()
async def access_middleware_cb(handler, event: CallbackQuery, data):
    if not allowed(event.from_user.id):
        await event.answer("Нет доступа", show_alert=True)
        return
    return await handler(event, data)


# ---------- Главное меню ----------

async def _abandon_pending_login(user_id: int, state: FSMContext):
    """
    Прерывает незавершённую попытку входа (QR или по номеру+коду) и сбрасывает
    связанного Telethon-клиента. Без сброса клиента повторная попытка на тот же
    account_index переиспользует "подвешенного" клиента и падает со странной
    ошибкой вида "Two-steps verification is enabled..." — именно так это и
    проявлялось при отмене входа на этапе пароля 2FA.
    """
    pending = pending_qr_logins.pop(user_id, None)
    if pending:
        if not pending["task"].done():
            pending["task"].cancel()
        await ub.discard_client(storage.session_key(user_id, pending["index"]))

    data = await state.get_data()
    account_index = data.get("account_index")
    if account_index is not None:
        await ub.discard_client(storage.session_key(user_id, account_index))


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(f"{emoji('circle')} Меню:", reply_markup=kb.main_menu(), parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await _abandon_pending_login(message.from_user.id, state)
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.main_menu())


@router.callback_query(F.data == "menu_main")
async def cb_menu_main(call: CallbackQuery, state: FSMContext):
    await _abandon_pending_login(call.from_user.id, state)
    await state.clear()
    await safe_edit_text(call.message, f"{emoji('circle')} Меню:", reply_markup=kb.main_menu(), parse_mode="HTML")


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ---------- Статистика ----------

@router.callback_query(F.data == "menu_statistics")
async def cb_menu_statistics(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    accounts = user["accounts"]
    total_groups = sum(len(a["groups"]) for a in accounts)
    total_sent = sum(a.get("stat_sent", 0) for a in accounts)
    total_errors = sum(a.get("stat_errors", 0) for a in accounts)
    tasks = running_tasks.get(call.from_user.id, [])
    running = any(not t.done() for t in tasks) or user.get("schedule_enabled", False)
    status = "включена" if running else "остановлена"
    text = (
        f"{emoji('circle')} статистика\n\n"
        f"акк {len(accounts)} · групп {total_groups}\n\n"
        f"✓ отправлено {total_sent}\n"
        f"✗ ошибки {total_errors}\n\n"
        f"○ ({status})"
    )
    await safe_edit_text(call.message, text, reply_markup=kb.back_button("menu_main"), parse_mode="HTML")

@router.callback_query(F.data == "menu_account")
async def cb_menu_account(call: CallbackQuery):
    if _bot_running(call.from_user.id):
        await call.answer("Сначала выключите бота (⏹ стоп).", show_alert=True)
        return
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
    if old:
        if not old["task"].done():
            old["task"].cancel()
        await ub.discard_client(storage.session_key(user_id, old["index"]))

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
    phone = await _canonical_phone(client)

    # Проверяем, не превышен ли лимит аккаунтов
    user = storage.get_user_data(user_id)
    if len(user["accounts"]) >= config.MAX_ACCOUNTS_PER_USER:
        await bot.send_message(
            chat_id,
            f"⚠️ Достигнут лимит аккаунтов ({config.MAX_ACCOUNTS_PER_USER}). "
            f"Удалите один из существующих аккаунтов, чтобы добавить новый.",
            reply_markup=kb.main_menu(),
        )
        await ub.discard_client(key)
        return

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
            return
        qr = await client.qr_login()
        msg = await _send_qr(bot, chat_id, qr.url, caption)
        try:
            await qr.wait(120)
        except asyncio.TimeoutError:
            pending_qr_logins.pop(user_id, None)
            await ub.discard_client(key)
            # Удаляем QR-код
            if msg:
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
            # Удаляем QR-код
            if msg:
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
        await ub.discard_client(key)
        if msg:
            try:
                await bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass
        await bot.send_message(chat_id, f"Ошибка входа: {e}")
        return

    # QR-код успешно отсканирован — удаляем его
    if msg:
        try:
            await bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass

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
    if old:
        if not old["task"].done():
            old["task"].cancel()
        await ub.discard_client(storage.session_key(call.from_user.id, old["index"]))
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
    user = storage.get_user_data(call.from_user.id)
    acc = storage.get_account(call.from_user.id, index)
    names = [g["name"] for g in acc["groups"] if g["id"] in acc["selected"]]
    if names:
        pairs = [", ".join(names[i:i + 2]) for i in range(0, len(names), 2)]
        list_block = "<blockquote>" + "\n".join(html.escape(p) for p in pairs) + "</blockquote>"
    else:
        list_block = "(группы для отправки не выбраны)"
    text = (
        f"{emoji('account')} Аккаунт: {acc['phone']}\n"
        f"Загружено групп: {len(acc['groups'])}\nВыбрано: {len(acc['selected'])}\n\n"
        f"{list_block}"
    )
    await safe_edit_text(
        call.message, text, reply_markup=kb.groups_menu(index, _groups_back_target(user)), parse_mode="HTML"
    )


@router.callback_query(F.data == "menu_groups")
async def cb_menu_groups(call: CallbackQuery):
    if _bot_running(call.from_user.id):
        await call.answer("Сначала выключите бота (⏹ стоп).", show_alert=True)
        return
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
    await call.answer("Загружаю список групп (без прав на отправку — пропускаю)...")
    groups = await ub.fetch_groups(key)
    storage.update_account(call.from_user.id, index, groups=groups)
    user = storage.get_user_data(call.from_user.id)
    await safe_edit_text(
        call.message,
        f"Готово. Загружено: {len(groups)} групп(ы), в которые есть право писать.",
        reply_markup=kb.groups_menu(index, _groups_back_target(user)),
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
        f"Отметьте группы для рассылки\nаккаунт ({acc['phone']}):",
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


# ---------- Контент (текст / фото / фото+текст / пересылка — на каждый аккаунт свой) ----------

async def _show_content_menu(call: CallbackQuery, index: int):
    user = storage.get_user_data(call.from_user.id)
    acc = storage.get_account(call.from_user.id, index)
    await safe_edit_text(
        call.message,
        f"Контент для {acc['phone']}:\n\n{_content_preview(acc)}",
        reply_markup=kb.content_menu(index, _content_back_target(user)),
    )


@router.callback_query(F.data == "menu_content")
async def cb_menu_content(call: CallbackQuery):
    if _bot_running(call.from_user.id):
        await call.answer("Сначала выключите бота (⏹ стоп).", show_alert=True)
        return
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
        storage.update_user_data(call.from_user.id, content_account=idx)
        await _show_content_menu(call, idx)
        return
    await safe_edit_text(
        call.message,
        "Выберите аккаунт, чей контент настраиваем:",
        reply_markup=kb.account_picker_kb(accounts, "content_menu", "menu_main"),
    )


@router.callback_query(F.data.startswith("content_menu_"))
async def cb_content_menu_for_account(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    storage.update_user_data(call.from_user.id, content_account=index)
    await _show_content_menu(call, index)


@router.callback_query(F.data.startswith("content_set_text_"))
async def cb_content_set_text(call: CallbackQuery, state: FSMContext):
    index = int(call.data.rsplit("_", 1)[-1])
    await state.update_data(account_index=index)
    await safe_edit_text(
        call.message,
        "Отправьте текст, который нужно разослать (форматирование Telegram — жирный, "
        "курсив, цитата и т.д. — сохранится).\n\n"
        "Поддерживается рандомизация: <code>{вариант1|вариант2}</code> — каждая "
        "отправка выберет случайный вариант. Поддерживается вложенность: "
        "<code>{A|{B|C}}</code>.",
        reply_markup=kb.cancel_button(),
        parse_mode="HTML",
    )
    await state.set_state(ContentStates.waiting_text)


def _cleanup_photo_if_unused(user_id: int, account_index: int, new_content_type: str):
    """
    Файл фото для аккаунта хранится под фиксированным именем
    (media/user_<uid>_<idx>.jpg), поэтому при переходе photo/photo_text -> photo
    он просто перезаписывается сам. А вот при переходе на text/forward файл
    остаётся на диске без ссылки на него — эта функция удаляет его, и вызывается
    ровно в момент, когда бот уже подтвердил (сохранил) новый источник контента,
    а не раньше.
    """
    if new_content_type in ("photo", "photo_text"):
        return
    path = os.path.join(config.MEDIA_DIR, f"user_{user_id}_{account_index}.jpg")
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@router.message(ContentStates.waiting_text)
async def process_content_text(message: Message, state: FSMContext):
    text = message.html_text
    if not spintext.validate(text):
        await message.answer("В шаблоне не совпадает количество { и } — проверьте и пришлите ещё раз.")
        return
    data = await state.get_data()
    storage.update_account(
        message.from_user.id, data["account_index"],
        content_type="text", content_text=text, content_photo=None,
        content_forward_chat_id=None, content_forward_message_id=None,
    )
    _cleanup_photo_if_unused(message.from_user.id, data["account_index"], "text")
    await state.clear()
    await message.answer("Текст сохранён ✅", reply_markup=kb.main_menu())


@router.callback_query(F.data.startswith("content_set_photo_"))
async def cb_content_set_photo(call: CallbackQuery, state: FSMContext):
    index = int(call.data.rsplit("_", 1)[-1])
    await state.update_data(account_index=index, mode="photo")
    await safe_edit_text(call.message, "Отправьте фото, которое нужно разослать:", reply_markup=kb.cancel_button())
    await state.set_state(ContentStates.waiting_photo)


@router.callback_query(F.data.startswith("content_set_phototext_"))
async def cb_content_set_phototext(call: CallbackQuery, state: FSMContext):
    index = int(call.data.rsplit("_", 1)[-1])
    await state.update_data(account_index=index, mode="photo_text")
    await safe_edit_text(
        call.message,
        "Отправьте фото. Если сразу добавите подпись к фото — текст возьмётся из неё "
        "(можно с рандомизацией {a|b}), иначе я спрошу текст отдельным сообщением.",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(ContentStates.waiting_photo)


@router.callback_query(F.data.startswith("content_set_forward_"))
async def cb_content_set_forward(call: CallbackQuery, state: FSMContext):
    index = int(call.data.rsplit("_", 1)[-1])
    await state.update_data(account_index=index)
    await safe_edit_text(
        call.message,
        "Перешлите сюда (Forward) сообщение, которое нужно рассылать пересылкой.\n\n"
        "⚠️ Если отправитель ограничил пересылку своих сообщений, источник "
        "определить не получится — тогда используйте текст/фото вместо пересылки.",
        reply_markup=kb.cancel_button(),
    )
    await state.set_state(ContentStates.waiting_forward)


@router.message(ContentStates.waiting_forward)
async def process_content_forward(message: Message, state: FSMContext):
    data = await state.get_data()
    account_index = data["account_index"]

    chat_id = None
    msg_id = None

    # 1. Сначала пробуем определить источник из пересылки
    origin = _extract_forward_origin(message)
    if origin[0] is not None:
        chat_id, msg_id = origin
    else:
        # 2. Если пересылка не определена, проверяем, есть ли reply
        if message.reply_to_message:
            reply = message.reply_to_message
            chat_id = reply.chat.id
            msg_id = reply.message_id
            await message.answer("✅ Источник определён через ответ (Reply).")
        else:
            await message.answer(
                "⚠️ Не удалось определить источник.\n\n"
                "Попробуйте один из способов:\n"
                "1️⃣ Перешлите сообщение заново\n"
                "2️⃣ Ответьте (Reply) на сообщение, которое хотите переслать\n"
                "3️⃣ Используйте текст/фото вместо пересылки",
                reply_markup=kb.cancel_button()
            )
            return

    # Сохраняем источник в аккаунт
    storage.update_account(
        message.from_user.id, account_index,
        content_type="forward",
        content_forward_chat_id=chat_id,
        content_forward_message_id=msg_id,
        content_text=None,
        content_photo=None,
    )
    _cleanup_photo_if_unused(message.from_user.id, account_index, "forward")
    await state.clear()
    await message.answer(
        "✅ Пересылаемое сообщение сохранено (источник определён).",
        reply_markup=kb.main_menu()
    )

async def _save_photo(message: Message, user_id: int, account_index: int) -> str:
    os.makedirs(config.MEDIA_DIR, exist_ok=True)
    path = os.path.join(config.MEDIA_DIR, f"user_{user_id}_{account_index}.jpg")
    await message.bot.download(message.photo[-1], destination=path)
    return path


@router.message(ContentStates.waiting_photo, F.photo)
async def process_content_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", "photo")
    idx = data["account_index"]
    path = await _save_photo(message, message.from_user.id, idx)

    if mode == "photo":
        storage.update_account(
            message.from_user.id, idx,
            content_type="photo", content_text=None, content_photo=path,
            content_forward_chat_id=None, content_forward_message_id=None,
        )
        await state.clear()
        await message.answer("Фото сохранено ✅", reply_markup=kb.main_menu())
        return

    if message.caption:
        caption = message.html_text
        if not spintext.validate(caption):
            await message.answer("В подписи не совпадает количество { и } — пришлите фото с подписью ещё раз.")
            return
        storage.update_account(
            message.from_user.id, idx,
            content_type="photo_text", content_text=caption, content_photo=path,
            content_forward_chat_id=None, content_forward_message_id=None,
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
    text = message.html_text
    if not spintext.validate(text):
        await message.answer("В шаблоне не совпадает количество { и } — пришлите текст ещё раз.")
        return
    data = await state.get_data()
    storage.update_account(
        message.from_user.id, data["account_index"],
        content_type="photo_text", content_text=text, content_photo=data["photo_path"],
        content_forward_chat_id=None, content_forward_message_id=None,
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
    if _bot_running(call.from_user.id):
        await call.answer("Сначала выключите бота (⏹ стоп).", show_alert=True)
        return
    await _show_settings_menu(call)


@router.callback_query(F.data.startswith("settings_switch_"))
async def cb_settings_switch(call: CallbackQuery):
    index = int(call.data.rsplit("_", 1)[-1])
    storage.update_user_data(call.from_user.id, settings_account=index)
    await _show_settings_menu(call)


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
        "Отправьте интервал в формате часы:минуты:секунды\n\n"
        "📌 Фиксированный: 0:1:15 (раз в 1 минуту 15 секунд)\n"
        "📌 Случайный: 0:1:15-0:1:20 (от 1:15 до 1:20 случайно)\n\n"
        "Пример: 1:30:00 (раз в 1.5 часа)",
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
    if not acc["content_type"]:
        await call.answer(f"Не настроен контент на номере — {acc['phone']}", show_alert=True)
        return
    await call.answer("Отправляю один раз...")
    asyncio.create_task(_send_once_for_account(call.from_user.id, idx, call.bot))


@router.message(SettingsStates.waiting_interval)
async def process_interval(message: Message, state: FSMContext):
    interval_spec = parse_interval(message.text)
    if interval_spec is None:
        await message.answer(
            "❌ Неверный формат.\n\n"
            "📌 Фиксированный: 0:1:15\n"
            "📌 Случайный: 0:1:15-0:1:20\n\n"
            "Попробуйте ещё раз:"
        )
        return

    data = await state.get_data()
    storage.update_account(message.from_user.id, data["account_index"], interval=interval_spec)
    await state.clear()

    # Красиво форматируем для ответа
    if interval_spec["min"] == interval_spec["max"]:
        h, rem = divmod(int(interval_spec["min"]), 3600)
        m, s = divmod(rem, 60)
        formatted = f"{h}:{m:02d}:{s:02d}"
        await message.answer(f"✅ Интервал сохранён: {formatted}", reply_markup=kb.main_menu())
    else:
        h, rem = divmod(int(interval_spec["min"]), 3600)
        m, s = divmod(rem, 60)
        min_str = f"{h}:{m:02d}:{s:02d}"
        h, rem = divmod(int(interval_spec["max"]), 3600)
        m, s = divmod(rem, 60)
        max_str = f"{h}:{m:02d}:{s:02d}"
        await message.answer(f"✅ Интервал сохранён: {min_str}–{max_str} (случайный)", reply_markup=kb.main_menu())

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


# ---------- Расписание рассылок (окно начало-конец, теперь в главном меню) ----------

@router.callback_query(F.data == "menu_schedule")
async def cb_menu_schedule(call: CallbackQuery):
    user = storage.get_user_data(call.from_user.id)
    status = "🟢 включено" if user.get("schedule_enabled") else "🔴 выключено"
    await safe_edit_text(
        call.message,
        f"Расписание рассылок. Статус: {status}\n\n"
        f"Для каждой записи задаётся окно времени: начало и конец. Пока текущее "
        f"время внутри окна (и, если указаны дни — сегодня подходящий день), "
        f"рассылка идёт автоматически; вне окна — останавливается сама.\n\n"
        f"Включается кнопкой 🕐 «рассылка по времени» в главном меню, "
        f"выключается кнопкой ⏹ стоп. Время — по МСК (Москва).",
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
        days = set()
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
        call.message,
        "Введите время НАЧАЛА рассылки по МСК в формате ЧЧ:ММ (например 09:00):",
        reply_markup=kb.back_button("menu_schedule"),
    )
    await state.set_state(ScheduleStates.waiting_start_time)


@router.message(ScheduleStates.waiting_start_time)
async def process_schedule_start_time(message: Message, state: FSMContext):
    time_str = parse_time_hhmm(message.text)
    if time_str is None:
        await message.answer("Неверный формат. Пришлите время как ЧЧ:ММ, например 09:00")
        return
    await state.update_data(start=time_str)
    await message.answer("Теперь введите время ОКОНЧАНИЯ рассылки по МСК в формате ЧЧ:ММ (например 18:00):")
    await state.set_state(ScheduleStates.waiting_end_time)


@router.message(ScheduleStates.waiting_end_time)
async def process_schedule_end_time(message: Message, state: FSMContext):
    time_str = parse_time_hhmm(message.text)
    if time_str is None:
        await message.answer("Неверный формат. Пришлите время как ЧЧ:ММ, например 18:00")
        return
    data = await state.get_data()
    storage.add_schedule_entry(message.from_user.id, data.get("days", []), data["start"], time_str)
    await state.clear()
    await message.answer(f"Добавлено в расписание: {data['start']}–{time_str} ✅", reply_markup=kb.main_menu())


@router.callback_query(F.data.startswith("schedule_remove_"))
async def cb_schedule_remove(call: CallbackQuery):
    entry_id = int(call.data.rsplit("_", 1)[-1])
    user = storage.remove_schedule_entry(call.from_user.id, entry_id)
    await safe_edit_reply_markup(
        call.message, reply_markup=kb.schedule_menu(user["schedule"], user.get("schedule_enabled", False))
    )


def _time_in_window(now_hhmm: str, start: str, end: str) -> bool:
    if start <= end:
        return start <= now_hhmm <= end
    return now_hhmm >= start or now_hhmm <= end  # окно "через полночь"


def _schedule_active_now(user: dict, now: datetime, hhmm: str) -> bool:
    weekday = now.weekday()
    for entry in user["schedule"]:
        if entry["days"] and weekday not in entry["days"]:
            continue
        if _time_in_window(hhmm, entry["start"], entry["end"]):
            return True
    return False


def _extract_content_from_forward(message: Message) -> dict | None:
    """
    Извлекает содержимое из пересланного сообщения.
    Возвращает словарь с ключами:
    - type: "text" | "photo" | "photo_text" | None
    - text: текст (если есть)
    - photo_path: путь к сохранённому фото (если есть)
    """
    if message.forward_origin or message.forward_from_chat or message.forward_from:
        # Это пересланное сообщение
        content = {"type": None, "text": None, "photo_path": None}

        # Текст (если есть)
        if message.text or message.caption:
            content["text"] = message.html_text or message.html_caption
            content["type"] = "text"

        # Фото (если есть)
        if message.photo:
            # Сохраняем фото во временную папку
            os.makedirs(config.MEDIA_DIR, exist_ok=True)
            # Используем временное имя
            import tempfile
            temp_path = os.path.join(config.MEDIA_DIR,
                                     f"temp_forward_{message.from_user.id}_{int(datetime.now().timestamp())}.jpg")
            message.bot.download(message.photo[-1], destination=temp_path)
            content["photo_path"] = temp_path
            if content["text"]:
                content["type"] = "photo_text"
            else:
                content["type"] = "photo"

        # Если ничего не найдено (например, документ) — возвращаем None
        if content["type"] is None:
            return None

        return content

    return None


# ---------- Рассылка ----------

def _eligible_broadcast_indices(user: dict) -> list[int]:
    result = []
    for idx in user["broadcast_accounts"]:
        acc = next((a for a in user["accounts"] if a["index"] == idx), None)
        if acc and acc["selected"] and acc["content_type"]:
            result.append(idx)
    return result


async def _handle_dead_session(user_id: int, account_index: int, bot: Bot):
    """
    Аккаунт отключён от Telegram (сессия была завершена вручную через
    приложение Telegram, в разделе "Устройства") — убираем его из профиля
    полностью: чистим сохранённое фото, удаляем аккаунт (это же освобождает
    номер в глобальной базе телефонов бота) и уведомляем владельца.
    """
    acc = storage.get_account(user_id, account_index)
    if not acc:
        return
    phone = acc["phone"]
    photo_path = acc.get("content_photo")
    if photo_path and os.path.exists(photo_path):
        try:
            os.remove(photo_path)
        except OSError:
            pass
    key = storage.session_key(user_id, account_index)
    try:
        await ub.discard_client(key)
    except Exception:
        pass
    storage.remove_account(user_id, account_index)
    try:
        await bot.send_message(
            user_id,
            f"⚠️ Бот отключён от Telegram на номере {phone} — сессия была "
            f"завершена вручную через приложение Telegram (раздел «Устройства»). "
            f"Аккаунт удалён из профиля бота, фото/текст для него очищены. "
            f"При необходимости подключите аккаунт заново.",
        )
    except Exception:
        pass


async def _send_once_for_account(user_id: int, account_index: int, bot: Bot):
    acc = storage.get_account(user_id, account_index)
    if not acc or not acc["content_type"] or not acc["selected"]:
        return
    content = {
        "type": acc["content_type"],
        "text": acc["content_text"],
        "photo": acc["content_photo"],
        "forward_chat_id": acc["content_forward_chat_id"],
        "forward_message_id": acc["content_forward_message_id"],
    }
    key = storage.session_key(user_id, account_index)
    try:
        if not await ub.is_authorized(key):
            await _handle_dead_session(user_id, account_index, bot)
            return
        sent, failed = await ub.broadcast(key, acc["selected"], content, acc["delay"])
        storage.add_stats(user_id, account_index, sent, failed)
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
        interval_spec = acc.get("interval")
        if not interval_spec:
            return
        min_interval = interval_spec.get("min", 0)
        max_interval = interval_spec.get("max", min_interval)

        if not interval_spec:
            return

        # Поддержка старого формата (число) и нового (словарь с min/max)
        if isinstance(interval_spec, (int, float)):
            interval_spec = {"min": interval_spec, "max": interval_spec}

        min_interval = interval_spec.get("min", 0)
        max_interval = interval_spec.get("max", min_interval)

        if max_interval > min_interval:
            interval = random.uniform(min_interval, max_interval)
        else:
            interval = min_interval

        await asyncio.sleep(interval)


def _launch_tasks(user_id: int, indices: list[int], bot: Bot):
    user = storage.get_user_data(user_id)
    delay_between_accounts = user.get("delay_between_accounts", 0) if len(indices) >= 2 else 0
    tasks = []
    for i, idx in enumerate(indices):
        offset = i * delay_between_accounts
        tasks.append(asyncio.create_task(_account_broadcast_loop(user_id, idx, offset, bot)))
    running_tasks[user_id] = tasks
    return tasks


def _is_running(user_id: int) -> bool:
    tasks = running_tasks.get(user_id, [])
    return any(not t.done() for t in tasks)


def _bot_running(user_id: int) -> bool:
    """Активна ли рассылка (обычная или по расписанию) — пока это так,
    настройки/аккаунт/группы/контент менять нельзя."""
    user = storage.get_user_data(user_id)
    return _is_running(user_id) or user.get("schedule_enabled", False)


@router.callback_query(F.data == "broadcast_start")
async def cb_broadcast_start(call: CallbackQuery):
    user_id = call.from_user.id
    user = storage.get_user_data(user_id)

    if user.get("schedule_enabled"):
        await call.answer("Сначала выключите рассылку по расписанию (⏹ стоп).", show_alert=True)
        return

    broadcast_idx = user["broadcast_accounts"]
    if not broadcast_idx:
        await call.answer("Нет аккаунтов, отмеченных для рассылки (раздел «👤 аккаунт»).", show_alert=True)
        return

    missing_groups, missing_content = [], []
    for idx in broadcast_idx:
        acc = storage.get_account(user_id, idx)
        if not acc:
            continue
        if not acc["selected"]:
            missing_groups.append(acc["phone"])
        if not acc["content_type"]:
            missing_content.append(acc["phone"])
    if missing_groups:
        await call.answer("Не настроена группа на номере — " + ", ".join(missing_groups), show_alert=True)
        return
    if missing_content:
        await call.answer("Не настроен контент на номере — " + ", ".join(missing_content), show_alert=True)
        return

    if _is_running(user_id):
        await call.answer("Рассылка уже запущена", show_alert=True)
        return

    _launch_tasks(user_id, broadcast_idx, call.bot)

    accounts_desc = ", ".join(storage.get_account(user_id, idx)["phone"] for idx in broadcast_idx)
    lines = [f"Рассылка запущена. Аккаунты ({len(broadcast_idx)}): {accounts_desc}"]
    if len(broadcast_idx) >= 2:
        delay_between_accounts = user.get("delay_between_accounts", 0)
        if delay_between_accounts:
            lines.append(f"Пауза между стартом аккаунтов: {delay_between_accounts} сек.")
        else:
            lines.append("Все аккаунты стартуют одновременно.")
    await safe_edit_text(call.message, "\n".join(lines), reply_markup=kb.main_menu())


@router.callback_query(F.data == "schedule_start")
async def cb_schedule_start(call: CallbackQuery):
    user_id = call.from_user.id
    user = storage.get_user_data(user_id)

    if not user.get("schedule_enabled") and _is_running(user_id):
        await call.answer("Сначала выключите обычную рассылку (⏹ стоп).", show_alert=True)
        return

    if not user["schedule"]:
        await call.answer('Сначала добавьте хотя бы одно время в "🗓 расписание".', show_alert=True)
        return

    broadcast_idx = user["broadcast_accounts"]
    if not broadcast_idx:
        await call.answer("Нет аккаунтов, отмеченных для рассылки (раздел «👤 аккаунт»).", show_alert=True)
        return

    missing_groups, missing_content = [], []
    for idx in broadcast_idx:
        acc = storage.get_account(user_id, idx)
        if not acc:
            continue
        if not acc["selected"]:
            missing_groups.append(acc["phone"])
        if not acc["content_type"]:
            missing_content.append(acc["phone"])
    if missing_groups:
        await call.answer("Не настроена группа на номере — " + ", ".join(missing_groups), show_alert=True)
        return
    if missing_content:
        await call.answer("Не настроен контент на номере — " + ", ".join(missing_content), show_alert=True)
        return

    storage.update_user_data(user_id, schedule_enabled=True)

    now = now_msk()
    hhmm = now.strftime("%H:%M")
    active_now = _schedule_active_now(user, now, hhmm)
    if active_now and not _is_running(user_id):
        indices = _eligible_broadcast_indices(user)
        if indices:
            _launch_tasks(user_id, indices, call.bot)

    if active_now:
        status_line = "Сейчас как раз внутри окна — рассылка уже идёт."
    else:
        status_line = f"Сейчас ({hhmm} МСК) вне заданных окон — запустится автоматически, когда время подойдёт."

    await safe_edit_text(
        call.message,
        f"🕐 Рассылка по расписанию включена. {status_line}\n"
        f"Время сверяется по МСК. Нажмите ⏹ стоп, чтобы выключить.",
        reply_markup=kb.main_menu(),
    )


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
        await safe_edit_text(call.message, "Рассылка остановлена.", reply_markup=kb.main_menu())
    else:
        await call.answer("Рассылка не запущена", show_alert=True)


# ---------- Планировщик расписания (окна времени) ----------

async def scheduler_loop(bot: Bot):
    while True:
        now = now_msk()
        hhmm = now.strftime("%H:%M")

        for user_id in config.ALLOWED_USER_IDS:
            user = storage.get_user_data(user_id)
            if not user.get("schedule_enabled") or not user["schedule"]:
                continue

            should_run = _schedule_active_now(user, now, hhmm)
            currently_running = _is_running(user_id)

            if should_run and not currently_running:
                indices = _eligible_broadcast_indices(user)
                if indices:
                    _launch_tasks(user_id, indices, bot)
            elif not should_run and currently_running:
                for t in running_tasks.get(user_id, []):
                    if not t.done():
                        t.cancel()
                running_tasks.pop(user_id, None)

        await asyncio.sleep(config.SCHEDULER_CHECK_INTERVAL)


async def _startup_session_check(bot: Bot):
    """При запуске бота проверяем все сохранённые аккаунты: если сессия была
    отозвана вручную (через Устройства в Telegram), пока бот не работал —
    сразу же чистим её, а не ждём первой попытки рассылки."""
    for user_id in config.ALLOWED_USER_IDS:
        user = storage.get_user_data(user_id)
        for acc in list(user["accounts"]):
            key = storage.session_key(user_id, acc["index"])
            try:
                ok = await ub.is_authorized(key)
            except Exception:
                ok = False
            if not ok:
                await _handle_dead_session(user_id, acc["index"], bot)


async def main():
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await _startup_session_check(bot)
    asyncio.create_task(scheduler_loop(bot))
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
