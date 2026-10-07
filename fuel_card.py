"""Топливные карты — скидки на топливо."""
import logging
from datetime import timedelta

import db
from util import money, now


CARDS = {
    "Базовая": (5, 50_000, 30),      # скидка %, стоимость, срок дней
    "Стандартная": (8, 120_000, 30),
    "Премиум": (10, 250_000, 30),
}


async def active_card(uid):
    """Активная карта или None."""
    return await db.pool.fetchrow(
        "SELECT * FROM fuel_cards WHERE owner=$1 AND expires_at>now()", uid)


async def discount_pct(uid):
    """Процент скидки на топливо (0 если нет карты)."""
    card = await active_card(uid)
    return card["discount"] if card else 0


async def buy_card(uid, tier: str):
    """Покупает или продлевает карту. Возвращает (ok, текст)."""
    if tier not in CARDS:
        return False, "Неизвестный уровень карты."
    discount, price, days = CARDS[tier]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            if p["money"] < price:
                return False, f"Не хватает денег: карта стоит {money(price)}."
            card = await c.fetchrow("SELECT * FROM fuel_cards WHERE owner=$1 FOR UPDATE", uid)
            # Если карта активна, продлеваем её; иначе берём сегодня как стартовую дату
            base = card["expires_at"] if card and card["expires_at"] > now() else now()
            await c.execute("UPDATE players SET money=money-$2 WHERE id=$1", uid, price)
            await c.execute(
                """INSERT INTO fuel_cards (owner, name, discount, expires_at)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (owner) DO UPDATE SET
                   name=$2, discount=$3, expires_at=$4""",
                uid, tier, discount, base + timedelta(days=days))
    return True, f"✅ Топливная карта '{tier}' активирована! Скидка {discount}% на протяжении {days} дней."
