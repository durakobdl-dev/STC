"""Кредиты — займы для покупки фур."""
import logging
from datetime import timedelta

import db
import finance
from util import money, now


RATE_PCT = 15              # годовая ставка, %
MONTHS = 12                # срок кредита, месяцев
MAX_AMOUNT = 8_000_000     # макс сумма займа
PENALTY_PCT = 5            # штраф за просрочку, %


def payment_amount(principal, months=MONTHS):
    """Аннуитетный платёж: (месячный платёж, переплата за период)."""
    monthly_rate = (RATE_PCT / 100) / 12
    if monthly_rate == 0:
        return principal // months, 0
    # Аннуитет: A = P * (r * (1+r)^n) / ((1+r)^n - 1)
    numerator = monthly_rate * ((1 + monthly_rate) ** months)
    denominator = ((1 + monthly_rate) ** months) - 1
    payment = int(principal * numerator / denominator)
    return payment, payment * months - principal


async def active_loan(uid):
    """Активный кредит или None."""
    return await db.pool.fetchrow(
        "SELECT * FROM loans WHERE owner=$1 AND remaining>0 ORDER BY id DESC LIMIT 1", uid)


async def take_loan(uid, amount: int):
    """Берёт кредит. Возвращает (ok, текст)."""
    if amount > MAX_AMOUNT:
        return False, f"Максимум кредита: {money(MAX_AMOUNT)}."
    if amount < 100_000:
        return False, "Минимум кредита: 100 000 ₽."
    async with db.pool.acquire() as c:
        async with c.transaction():
            active = await c.fetchval("SELECT count(*) FROM loans WHERE owner=$1 AND remaining>0", uid)
            if active:
                return False, "У вас уже есть активный кредит. Погасите его сначала."
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            monthly, overpay = payment_amount(amount)
            next_payment = now() + timedelta(days=30)
            await c.execute("UPDATE players SET money=money+$2 WHERE id=$1", uid, amount)
            await c.execute(
                """INSERT INTO loans (owner, amount, remaining, rate_pct, monthly_payment, next_payment)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                uid, amount, amount, RATE_PCT, monthly, next_payment)
            await finance.post(c, uid, "loan_in", amount, note=f"Кредит на {MONTHS} месяцев")
    return True, (f"✅ Кредит на {money(amount)} одобрен!\n"
                  f"Ежемесячный платёж: {money(monthly)}\n"
                  f"Переплата за {MONTHS} месяцев: {money(overpay)}\n"
                  f"Первый платёж: {next_payment.strftime('%d.%m.%Y')}")


async def pay_loan(uid, loan_id: int):
    """Оплачивает платёж по кредиту. Возвращает (ok, текст)."""
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            loan = await c.fetchrow("SELECT * FROM loans WHERE id=$1 AND owner=$2 FOR UPDATE", loan_id, uid)
            if not loan:
                return False, "Кредит не найден."
            if loan["remaining"] <= 0:
                return False, "Кредит уже погашен."
            due = loan["monthly_payment"]
            if p["money"] < due:
                # Платим что можем, добавляем штраф
                pay = p["money"]
                await c.execute("UPDATE players SET money=0 WHERE id=$1", uid)
                await c.execute(
                    "UPDATE loans SET remaining=remaining-$2, next_payment=next_payment+make_interval(days=>30) WHERE id=$1",
                    loan_id, pay)
                penalty = due * PENALTY_PCT // 100
                await c.execute("UPDATE loans SET remaining=remaining+$2 WHERE id=$1", loan_id, penalty)
                await finance.expense(c, uid, "fine", penalty, False, "Штраф за просрочку кредита")
                return True, f"⚠️ Платёж неполный: {money(pay)} из {money(due)}. Добавлен штраф {money(penalty)}."
            else:
                await c.execute("UPDATE players SET money=money-$2 WHERE id=$1", uid, due)
                await c.execute(
                    "UPDATE loans SET remaining=remaining-$2, next_payment=next_payment+make_interval(days=>30) WHERE id=$1",
                    loan_id, due)
                await finance.expense(c, uid, "loan_out", due, note="Погашение кредита")
                remaining = loan["remaining"] - due
                if remaining > 0:
                    return True, f"✅ Платёж {money(due)} принят. Осталось: {money(remaining)}."
                else:
                    return True, f"✅ Кредит полностью погашен! Переплачено: {money(due * MONTHS - loan['amount'])}."


async def process_payments(bot):
    """Напоминает об очередном платеже по кредитам (вызывается из watcher)."""
    rows = await db.pool.fetch("SELECT * FROM loans WHERE remaining>0 AND next_payment<=now()")
    for loan in rows:
        try:
            p = await db.pool.fetchrow("SELECT money FROM players WHERE id=$1", loan["owner"])
            text = (f"💳 <b>Пора платить по кредиту!</b>\n\n"
                    f"Сумма: {money(loan['monthly_payment'])}\n"
                    f"Осталось погасить: {money(loan['remaining'])}\n"
                    f"На счёте: {money(p['money'])}")
            if p["money"] < loan["monthly_payment"]:
                text += f"\n\n⚠️ Не хватает денег. За просрочку будет штраф {PENALTY_PCT}%."
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💸 Оплатить", callback_data=f"loan_pay:{loan['id']}")]])
            await bot.send_message(loan["owner"], text, reply_markup=kb)
        except Exception:
            logging.exception("loan notification failed")
