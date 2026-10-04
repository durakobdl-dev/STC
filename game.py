import asyncio
import html
import logging
import math
import random
from datetime import datetime, timedelta, timezone

import db
from config import (ADVANCE_PCT, DRIVE_HOURS_PER_DAY, REST_HOURS_PER_DAY)
from data import (CARGO, CLIENTS, FUEL, SUBURBAN, URGENT_CLIENTS, dist, waypoints)


def now():
    return datetime.now(timezone.utc)


def esc(s):
    return html.escape(str(s))


def money(n):
    return f"{int(n):,}".replace(",", "\u202f") + " ₽"


def dur(s):
    s = int(max(0, s))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин"


def bar(p, n=10):
    f = int(max(0.0, min(1.0, p)) * n)
    return "▓" * f + "░" * (n - f)


def level(xp):
    return 1 + int(math.sqrt(xp / 60))


# ---------- расчёты ----------
def travel_seconds(km, speed):
    drive_h = km / speed
    rest_h = int(drive_h // DRIVE_HOURS_PER_DAY) * REST_HOURS_PER_DAY
    return int((drive_h + rest_h) * 3600)


def load_seconds(tons):
    return (20 + 2 * tons) * 60


def fuel_cost(km, cons, a, b):
    liters = km * cons / 100
    price = (FUEL[a] + FUEL[b]) / 2 * (1.08 if a != b else 1.0)
    return int(liters * price)


# ---------- заказы ----------
def make_order(owner, city, licensed):
    inter = [c for c in licensed if c != city and dist(city, c)]
    suburban = (not inter) or random.random() < 0.4
    if suburban:
        label, km = random.choice(SUBURBAN[city])
        to = city
    else:
        to = random.choice(inter)
        km, label = dist(city, to), to
    tons = random.randint(4, 24)
    cargo = random.choice(list(CARGO))
    urgent = random.random() < 0.12
    price = max(3000, int(tons * km * CARGO[cargo] * random.uniform(0.9, 1.1) / 100) * 100)
    if urgent:
        price = int(price * 1.5 / 100) * 100
    base = load_seconds(tons) + travel_seconds(km, 66)
    limit = int(base * (1.08 if urgent else 1.25)) + 3600
    client = random.choice(URGENT_CLIENTS if urgent else CLIENTS)
    expires = now() + timedelta(minutes=random.randint(60, 240))
    return (owner, city, to, label, cargo, tons, km, price, client, urgent, limit, expires)


async def ensure_orders(owner, city, licensed, target=7):
    await db.pool.execute("DELETE FROM orders WHERE owner=$1 AND expires_at<=now()", owner)
    have = await db.pool.fetchval("SELECT count(*) FROM orders WHERE owner=$1 AND from_city=$2", owner, city)
    for _ in range(max(0, target - have)):
        await db.pool.execute(
            """INSERT INTO orders (owner, from_city, to_city, to_label, cargo, tons, km, price,
               client, urgent, limit_s, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            *make_order(owner, city, licensed))


async def licensed_cities(owner):
    rows = await db.pool.fetch("SELECT city FROM licenses WHERE owner=$1 AND expires_at>now()", owner)
    return [r["city"] for r in rows]


# ---------- рейсы ----------
async def start_trip(uid, truck_id, order_id=None, dest=None):
    """Возвращает текст ошибки или None."""
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not p or not t:
                return "Фура не найдена."
            if t["busy"]:
                return "Фура уже в рейсе."
            if order_id is not None:
                o = await c.fetchrow(
                    "SELECT * FROM orders WHERE id=$1 AND owner=$2 AND from_city=$3 AND expires_at>now()",
                    order_id, uid, t["city"])
                if not o:
                    return "Заказ уже недоступен."
                if o["tons"] > t["capacity"]:
                    return f"Груз {o['tons']} т тяжелее, чем грузоподъёмность фуры ({t['capacity']} т)."
                to, label, cargo, tons, km = o["to_city"], o["to_label"], o["cargo"], o["tons"], o["km"]
                price = o["price"]
                limit = o["limit_s"]
                empty = False
            else:
                to, label, cargo, tons = dest, dest, "Порожний перегон", 0
                km, price, empty = dist(t["city"], dest), 0, True
                limit = 10**9
            fuel = fuel_cost(km, t["consumption"], t["city"], to)
            advance = price * ADVANCE_PCT // 100
            if p["money"] + advance < fuel:
                return (f"Не хватает денег на топливо: нужно {money(fuel)}, "
                        f"у вас {money(p['money'])} + аванс {money(advance)}.")
            start = now()
            load_end = start + timedelta(seconds=0 if empty else load_seconds(tons))
            arrive = load_end + timedelta(seconds=travel_seconds(km, t["speed"]))
            finish = arrive + timedelta(seconds=0 if empty else load_seconds(tons))
            deadline = start + timedelta(seconds=limit)
            await c.execute(
                """INSERT INTO trips (owner, truck_id, from_city, to_city, to_label, cargo, tons, km,
                   price, advance, fuel_cost, empty, started_at, load_end, arrive_at, finish_at, deadline)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
                uid, truck_id, t["city"], to, label, cargo, tons, km, price, advance, fuel, empty,
                start, load_end, arrive, finish, deadline)
            await c.execute("UPDATE players SET money = money + $2 - $3 WHERE id=$1", uid, advance, fuel)
            await c.execute("UPDATE trucks SET busy=TRUE WHERE id=$1", truck_id)
            if order_id is not None:
                await c.execute("DELETE FROM orders WHERE id=$1", order_id)
    return None


async def active_trip(truck_id):
    return await db.pool.fetchrow("SELECT * FROM trips WHERE truck_id=$1 AND settled=FALSE", truck_id)


def trip_status(t):
    n = now()
    total = (t["finish_at"] - t["started_at"]).total_seconds() or 1
    p = (n - t["started_at"]).total_seconds() / total
    left = (t["finish_at"] - n).total_seconds()
    if n < t["load_end"]:
        phase = "📦 Погрузка"
    elif n < t["arrive_at"]:
        q = (n - t["load_end"]).total_seconds() / max(1, (t["arrive_at"] - t["load_end"]).total_seconds())
        wp = waypoints(t["from_city"], t["to_city"]) if t["from_city"] != t["to_city"] else []
        if wp and 0.04 < q < 0.96:
            where = f"проезжаете {wp[min(len(wp) - 1, int(q * len(wp)))]}"
        elif q <= 0.04:
            where = f"выезд из города {t['from_city']}"
        else:
            where = f"подъезд к пункту {t['to_label']}"
        phase = f"🛣 В пути: {where}"
    else:
        phase = "📤 Разгрузка"
    kind = "порожний перегон" if t["empty"] else f"{t['cargo']}, {t['tons']} т"
    return (f"{t['from_city']} → {esc(t['to_label'])} · {kind}\n{phase}\n"
            f"{bar(p)} {int(min(1, max(0, p)) * 100)}%\n"
            f"⏱ До завершения: {dur(left)}")


async def settle(bot, t):
    row = await db.pool.fetchrow(
        "UPDATE trips SET settled=TRUE WHERE id=$1 AND settled=FALSE RETURNING *", t["id"])
    if not row:
        return
    late_s = (row["finish_at"] - row["deadline"]).total_seconds()
    late_h = math.ceil(late_s / 3600) if late_s > 0 else 0
    penalty = min(60, 3 * late_h)
    pay = row["price"] * (100 - penalty) // 100
    net = pay - row["advance"]
    xp = 0 if row["empty"] else 10 + row["km"] // 20
    async with db.pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """UPDATE players SET money = money + $2, xp = xp + $3, total_earned = total_earned + $4,
                   trips_done = trips_done + $5, total_km = total_km + $6 WHERE id=$1""",
                row["owner"], net, xp, pay, 0 if row["empty"] else 1, row["km"])
            await c.execute("UPDATE trucks SET city=$2, mileage = mileage + $3, busy=FALSE WHERE id=$1",
                            row["truck_id"], row["to_city"], row["km"])
    if row["empty"]:
        text = f"🏁 Фура прибыла в {esc(row['to_city'])} (порожний перегон)."
    else:
        text = (f"🏁 <b>Рейс завершён!</b>\n{row['from_city']} → {esc(row['to_label'])}\n"
                f"{row['cargo']}, {row['tons']} т · {row['km']} км\n\n"
                f"💰 Оплата: {money(pay)} (аванс {money(row['advance'])} уже получен)\n"
                f"⛽ Топливо в рейсе: {money(row['fuel_cost'])}\n"
                f"📈 Прибыль: {money(pay - row['fuel_cost'])}\n"
                f"✨ Опыт: +{xp}")
        if penalty:
            text += f"\n⚠️ Опоздание: штраф {penalty}%"
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data="menu")]])
        await bot.send_message(row["owner"], text, reply_markup=kb)
    except Exception:
        logging.exception("notify failed")


async def watcher(bot):
    while True:
        try:
            rows = await db.pool.fetch("SELECT * FROM trips WHERE settled=FALSE AND finish_at<=now()")
            for t in rows:
                await settle(bot, t)
        except Exception:
            logging.exception("watcher error")
        await asyncio.sleep(15)
