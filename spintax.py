"""
Рандомизация контента ("спинтакс"): {вариант1|вариант2|вариант3}, с вложенностью
вида {A|{B|C}}. Каждый вызов resolve() выбирает случайный вариант заново —
используется отдельно для каждой отправки, чтобы сообщения не были одинаковыми.
"""

import random
import re

_PATTERN = re.compile(r"\{([^{}]*)\}")


def resolve(template: str) -> str:
    if not template:
        return template
    text = template
    # Резолвим "изнутри наружу": сначала группы без вложенных скобок,
    # затем то, что осталось (внешние скобки), пока скобок не останется совсем.
    for _ in range(50):  # защита от случайных незакрытых/битых скобок
        if "{" not in text:
            break
        new_text, count = _PATTERN.subn(lambda m: random.choice(m.group(1).split("|")), text)
        if count == 0:
            break
        text = new_text
    return text


def validate(template: str) -> bool:
    """Простая проверка на сбалансированность скобок."""
    depth = 0
    for ch in template:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0