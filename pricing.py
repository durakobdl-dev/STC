"""Оплата рейсов: общая формула для обычных заказов и контрактов."""
import random

from data import CARGO, country_of
from progression import price_mult

INTL_MULT = 1.2          # международный рейс платят на 20% больше
BORDER_H = 2             # таможня и граница: столько часов добавляется к рейсу
CONTRACT_MULT = {        # множитель к цене обычного рейса, по клиентам
    "Магнит": 1.5, "Лента": 1.55, "X5 Group": 1.6, "Wildberries": 1.7, "Ozon": 1.8,
}


def short_bonus(km):
    """Короткие маршруты платят больше за км: погрузка съедает время."""
    if km < 150:
        return 3.5
    if km < 300:
        return max(1.5, 3.0 - km / 200)
    if km < 400:
        return 1.2
    return 1.0


def limits(km):
    """(минимум, максимум) оплаты рейса."""
    lo = 30_000 if km < 150 else (25_000 if km < 300 else 20_000)
    hi = 70_000 if km < 150 else (90_000 if km < 300 else 350_000)
    return lo, hi


def border_s(a, b):
    return BORDER_H * 3600 if country_of(a) != country_of(b) else 0


def order_price(tons, km, cargo, lvl, intl, rng=random):
    """Цена обычного заказа: с учётом веса, км, груза, уровня, границы и случайного разброса."""
    lo, hi = limits(km)
    raw = tons * km * CARGO[cargo] * short_bonus(km) * rng.uniform(0.9, 1.1)
    raw *= price_mult(lvl) * (INTL_MULT if intl else 1.0)
    return min(max(lo, int(raw / 100) * 100), hi)


def contract_price(client, tons, km, cargo, lvl, intl):
    """Цена рейса по контракту: всегда выше обычного заказа того же маршрута (без разброса)."""
    base = order_price(tons, km, cargo, lvl, intl, rng=_Fixed)
    return int(base * CONTRACT_MULT[client]) // 100 * 100


class _Fixed:
    @staticmethod
    def uniform(a, b):
        return 1.0
