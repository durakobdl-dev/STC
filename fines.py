"""Штрафы ГИБДД — случайные, выписываются во время рейса."""
import logging
import random
from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
import finance
from util import dur, money, now

# (причина, сумма штрафа ₽, шанс успешного оспаривания %)
FINE_TYPES = [
    ("превышение скорости",              5_000,  40),
    ("нарушение режима труда и отдыха",  15_000, 25),
    ("перегруз (превышение массы)",      20_000, 30),
    ("нарушение правил обгона",          10_000, 35),
    ("неисправные тормоза",              25_000, 20),
    ("нарушение знаков на дороге",        4_000, 45),
]

# Базовый шанс получить штраф за рейс
BASE_CHANCE = 0.10    # 10% на рейс


def roll_fine(km: int):
    """Решает, будет ли штраф в рейсе. Возвращает (reason, amount, contest_chance) или None."""
    chance = min(0.35, BASE_CHANCE * (1 + km / 3000))
    if random.random() > chance:
        return None
    return random.choice(FINE_TYPES)


async def issue_fine(bot, trip):
    """Выписывает штраф в ходе рейса."""
    result = roll_fine(trip["km"])
    if not result:
        return
    reason, amount, contest_chance = result
    async with db.pool.acquire() as c:
        fine_id = await c.fetchval(
            """INSERT INTO fines (owner, trip_id, amount, reason, contest_chance)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            trip["owner"], trip["id"], amount, reason, contest_chance)
    text = (f"🚔 <b>Штраф ГИБДД!</b>\n\n"
            f"Причина: {reason}\n"
            f"Сумма: <b>{money(amount)}</b>\n\n"
            f"Можно оплатить сразу или попытаться оспорить (шанс {contest_chance}%).")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {money(amount)}", callback_data=f"fine_pay:{fine_id}")],
        [InlineKeyboardButton(text=f"⚖️ Оспорить (шанс {contest_chance}%)", callback_data=f"fine_contest:{fine_id}")],
    ])
    try:
        await bot.send_message(trip["owner"], text, reply_markup=markup)
    except Exception:
        logging.exception("fine notify failed")


async def pay_fine(uid, fine_id: int):
    """Оплатить штраф. Возвращает (ok, текст)."""
    async with db.pool.acquire() as c:
        async with c.transaction():
            fine = await c.fetchrow(
                "SELECT * FROM fines WHERE id=$1 AND owner=$2 AND status='pending' FOR UPDATE",
                fine_id, uid)
            if not fine:
                return False, "Штраф не найден или уже обработан."
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            if p["money"] < fine["amount"]:
                return False, f"Не хватает денег: штраф {money(fine['amount'])}."
            await c.execute("UPDATE players SET money=money-$2 WHERE id=$1", uid, fine["amount"])
            await c.execute("UPDATE fines SET status='paid' WHERE id=$1", fine_id)
            await finance.expense(c, uid, "fine", fine["amount"], False, f"Штраф ГИБДД: {fine['reason']}")
    return True, f"✅ Штраф оплачен: {money(fine['amount'])}."


async def contest_fine(uid, fine_id: int):
    """Оспорить штраф. Возвращает (ok, текст)."""
    async with db.pool.acquire() as c:
        async with c.transaction():
            fine = await c.fetchrow(
                "SELECT * FROM fines WHERE id=$1 AND owner=$2 AND status='pending' FOR UPDATE",
                fine_id, uid)
            if not fine:
                return False, "Штраф не найден или уже обработан."
            if random.randint(1, 100) <= fine["contest_chance"]:
                await c.execute("UPDATE fines SET status='dismissed' WHERE id=$1", fine_id)
                return True, f"✅ Штраф оспорен и отменён! Вы сэкономили {money(fine['amount'])}."
            else:
                # Не получилось — оплачиваем принудительно
                p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
                pay = min(p["money"], fine["amount"])
                await c.execute("UPDATE players SET money=money-$2 WHERE id=$1", uid, pay)
                await c.execute("UPDATE fines SET status='paid' WHERE id=$1", fine_id)
                await finance.expense(c, uid, "fine", pay, False,
                                      f"Штраф ГИБДД (оспорить не удалось): {fine['reason']}")
                return False, f"❌ Оспорить не удалось. Штраф оплачен: {money(pay)}."
