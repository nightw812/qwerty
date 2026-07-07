from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ старт", callback_data="broadcast_start")
    b.button(text="⏹ стоп", callback_data="broadcast_stop")
    b.button(text="👤 аккаунт", callback_data="menu_account")
    b.button(text="👥 группы", callback_data="menu_groups")
    b.button(text="📝 контент", callback_data="menu_content")
    b.button(text="⚙️ настройка", callback_data="menu_settings")
    b.adjust(2, 2, 2)
    return b.as_markup()


def back_button(target="menu_main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ назад", callback_data=target)
    return b.as_markup()


def account_menu(is_authorized: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if is_authorized:
        b.button(text="🚪 выйти из аккаунта", callback_data="account_logout")
    else:
        b.button(text="➕ добавить аккаунт", callback_data="account_add")
    b.button(text="⬅️ назад", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()


def cancel_button() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ отмена", callback_data="menu_main")
    return b.as_markup()


def groups_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 загрузить группы", callback_data="groups_load")
    b.button(text="☑️ выбрать группы", callback_data="groups_select")
    b.button(text="⬅️ назад", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()


def groups_select_kb(groups: list[dict], selected: list[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups:
        mark = "✅" if g["id"] in selected else "◽️"
        b.button(text=f"{mark} {g['name']}", callback_data=f"toggle_{g['id']}")
    b.button(text="⬅️ назад", callback_data="menu_groups")
    b.adjust(1)
    return b.as_markup()


def content_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ задать текст", callback_data="content_set_text")
    b.button(text="⬅️ назад", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()


def settings_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏱ интервал", callback_data="settings_interval")
    b.button(text="⬅️ назад", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()
