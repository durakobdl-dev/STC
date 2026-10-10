"""Уровень компании: от него зависят лимит фур, цены, грузы, визы, контракты и кредиты."""
import math

from data import FOREIGN

MAX_LEVEL_TRUCKS = 20
NEW_TRUCKS_LEVEL = 3          # новые фуры из автосалона с этого уровня
CARGO_MIN_LEVEL = {"Электроника": 3, "Автомобили": 4}


def level(xp):
    return 1 + int(math.sqrt(xp / 60))


def xp_for(lvl):
    """Сколько опыта нужно, чтобы достичь уровня lvl."""
    return 60 * (lvl - 1) ** 2


def max_trucks(lvl):
    return min(MAX_LEVEL_TRUCKS, 1 + 2 * lvl)


def price_mult(lvl):
    """Надбавка к оплате заказов за репутацию: +2% за уровень, не больше +30%."""
    return min(1.3, 1 + 0.02 * (lvl - 1))


def loan_limit(lvl):
    return min(8_000_000, 1_000_000 * (lvl + 1))


def cargo_ok(cargo, lvl):
    return lvl >= CARGO_MIN_LEVEL.get(cargo, 1)


def visa_ok(country, lvl):
    return lvl >= FOREIGN[country]["visa_level"]


def help_lines():
    """Текст для справки: что открывает каждый уровень."""
    return [
        f"• Уровень 1: до {max_trucks(1)} фур, города РФ по лицензии",
        f"• Каждый уровень: +2% к оплате заказов (до +30%) и +2 фуры в лимите",
        f"• Уровень {NEW_TRUCKS_LEVEL}: новые фуры в автосалоне, электроника в грузах",
        f"• Уровень 4: грузы «Автомобили», кредит до 5 млн",
        "• Визы: Беларусь с 2 уровня, Казахстан с 4-го",
        "• Контракты: клиенты открываются с 2 по 6 уровень",
    ]
