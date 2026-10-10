"""Визы в зарубежные страны: оформляются около суток, действуют 30 дней."""
import logging
from datetime import timedelta

import db
import finance
from data import FOREIGN
from progression import level, visa_ok
from util import money, now

VISA_DAYS = 30
PROCESS_H = 24


async def active_countries(uid):
    rows = await db.pool.fetch(
        "SELECT country FROM visas WHERE owner=$1 AND ready_at<=now() AND expires_at>now()", uid)
    return [r["country"] for r in rows]


async def status(uid, country):
    """('none'|'pending'|'active', row)."""
    r = await db.pool.fetchrow("SELECT * FROM visas WHERE owner=$1 AND country=$2", uid, country)
    if not r or r["expires_at"] <= now():
        return "none", r
    return ("active" if r["ready_at"] <= now() else "pending"), r


async def buy(uid, country):
    """Оформляет или продлевает визу. Возвращает (ok, текст)."""
    if country not in FOREIGN:
        return False, "Неизвестная страна."
    price = FOREIGN[country]["visa_price"]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT money, xp FROM players WHERE id=$1 FOR UPDATE", uid)
            if not p:
                return False, "Игрок не найден."
            if not visa_ok(country, level(p["xp"])):
                return False, f"Виза в {country} откроется с {FOREIGN[country]['visa_level']} уровня компании."
            if p["money"] < price:
                return False, f"Не хватает денег: виза стоит {money(price)}."
            row = await c.fetchrow("SELECT * FROM visas WHERE owner=$1 AND country=$2 FOR UPDATE", uid, country)
            n = now()
            if row and row["ready_at"] > n:
                left = row["ready_at"] - n
                return False, f"Виза уже оформляется, осталось около {max(1, int(left.total_seconds() // 3600))} ч."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, price)
            if row and row["expires_at"] > n:
                # продление действующей визы: +30 дней к текущему сроку
                await c.execute("UPDATE visas SET expires_at = expires_at + make_interval(days=>$3) "
                                "WHERE owner=$1 AND country=$2", uid, country, VISA_DAYS)
                text = f"✅ Виза в {country} продлена на {VISA_DAYS} дней."
            else:
                ready = n + timedelta(hours=PROCESS_H)
                await c.execute(
                    """INSERT INTO visas (owner, country, ready_at, expires_at, notified)
                       VALUES ($1,$2,$3,$4,FALSE)
                       ON CONFLICT (owner, country) DO UPDATE SET
                       ready_at=$3, expires_at=$4, notified=FALSE""",
                    uid, country, ready, ready + timedelta(days=VISA_DAYS))
                text = f"📨 Документы на визу в {country} отправлены. Виза будет готова примерно через сутки."
            await finance.expense(c, uid, "license", price, False, f"Виза: {country}")
    return True, text


async def notify(bot):
    """Сообщает игроку, когда виза готова (вызывается из watcher)."""
    rows = await db.pool.fetch(
        "SELECT * FROM visas WHERE notified=FALSE AND ready_at<=now() AND expires_at>now()")
    for r in rows:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🪪 Лицензии", callback_data="lic")]])
            await bot.send_message(r["owner"], f"🛂 Виза в {r['country']} готова! Теперь доступны заказы в её города.",
                                   reply_markup=kb)
            await db.pool.execute("UPDATE visas SET notified=TRUE WHERE owner=$1 AND country=$2",
                                  r["owner"], r["country"])
        except Exception:
            logging.exception("visa notification failed")
