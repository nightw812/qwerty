"""
Простое JSON-хранилище на пользователя: список групп, что выбрано,
какой контент рассылать.
"""

import json
import os
from threading import Lock

import config

_lock = Lock()

_DEFAULT = {
    "phone": None,
    "groups": [],       # все группы, загруженные с аккаунта: [{"id":.., "name":..}]
    "selected": [],      # id групп, отмеченных для рассылки
    "content": None,     # текст, который будем рассылать
    "interval": None,    # интервал повторной рассылки в секундах
}


def _load():
    if not os.path.exists(config.DATA_FILE):
        return {}
    with open(config.DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(config.DATA_FILE), exist_ok=True)
    with open(config.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_data(user_id: int) -> dict:
    data = _load()
    user = data.get(str(user_id))
    if user is None:
        user = dict(_DEFAULT)
    return user


def set_user_data(user_id: int, user_data: dict):
    with _lock:
        data = _load()
        data[str(user_id)] = user_data
        _save(data)


def update_user_data(user_id: int, **fields):
    user = get_user_data(user_id)
    user.update(fields)
    set_user_data(user_id, user)
    return user
