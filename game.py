import asyncio
import html
import logging
import math
import random
from datetime import datetime, timedelta, timezone

import db
import drivers
import finance
import maintenance
import fuel_card
import fines
import loans
import contracts
from util import bar, dur, esc, money, now
from config import (ADVANCE_PCT, DEFAULT_DRIVER_RATING, REAL_KM, FAR_SPEED, REST_PCT)
from data import (CARGO, CLIENTS, FUEL, SUBURBAN, URGENT_CLIENTS, dist, waypoints)


def level(xp):
    return 1 + int(math.sqrt(xp / 60))


def season_factor():
    """Сезонный коэффициент: зима — медленнее и дороже, лето — больше заказов."""
    month = now().month
    if month in (12, 1, 2):    # зима
        return {"speed": 0.85, "orders": 0.8, "fuel": 1.15, "label": "❄️ Зима"}
    elif month in (6, 7, 8):   # лето
        return {"speed": 1.0,  "orders": 1.3, "fuel": 1.0,  "label": "☀️ Лето"}
    elif month in (3, 4, 5):   # весна
        return {"speed": 0.92, "orders": 1.0, "fuel": 1.05, "label": "🌸 Весна"}
    else:                       # осень
        return {"speed": 0.95, "orders": 0.9, "fuel": 1.08, "label": "🍂 Осень"}


# ---------- расчёты ----------
def travel_seconds(km, speed):
    """Время в пути. Близко как в жизни, дальше сжато. Зимой медленнее."""
    sf = season_factor()["speed"]
    eff_speed = speed * sf
    if km <= REAL_KM:
        drive_h = km / eff_speed
    else:
        # Первые REAL_KM с реальной скоростью, остаток со сжатой скоростью FAR_SPEED
        drive_h = REAL_KM / eff_speed + (km - REAL_KM) / FAR_SPEED
        # Отдых водителя в дальних рейсах (после 3 часов в пути)
        extra_h = max(0, drive_h - 3)
        drive_h += extra_h * REST_PCT / 100
    return int(drive_h * 3600)


def load_seconds(tons):
    return (20 + 2 * tons) * 60


def fuel_cost(km, cons, a, b, discount_pct=0):
    liters = km * cons / 100
    price = (FUEL[a] + FUEL[b]) / 2 * (1.08 if a != b else 1.0)
    return int(liters * price * (100 - discount_pct) / 100)


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
    tons = random.randint(10, 24) if km < 300 else random.randint(4, 24)
    cargo = random.choice(list(CARGO))
    urgent = random.random() < 0.12
    # Короткие маршруты платят больше за км — погрузка/разгрузка съедает время
    if km < 150:
        short_bonus = 3.5
    elif km < 300:
        short_bonus = max(1.5, 3.0 - km / 200)
    elif km < 400:
        short_bonus = 1.2
    else:
        short_bonus = 1.0
    min_price = 30_000 if km < 150 else (25_000 if km < 300 else 20_000)
    max_price = 70_000 if km < 150 else (90_000 if km < 300 else 350_000)  # макс 350k для дальних
    price = max(min_price, int(tons * km * CARGO[cargo] * short_bonus * random.uniform(0.9, 1.1) / 100) * 100)
    if max_price:
        price = min(price, max_price)
    if urgent:
        price = int(price * 1.5 / 100) * 100
    base = load_seconds(tons) + travel_seconds(km, 88)
    limit = int(base * (1.05 if urgent else 1.15))
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
async def start_trip(uid, truck_id, order_id=None, dest=None, driver_id=None, contract_id=None):
    """Возвращает текст ошибки или None."""
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not p or not t:
                return "Фура не найдена."
            if t["busy"]:
                return "Фура уже в рейсе."
            if maintenance.in_service(t):
                return "Фура на ТО, дождитесь окончания."
            rating = DEFAULT_DRIVER_RATING
            if driver_id:
                d = await c.fetchrow("SELECT * FROM drivers WHERE id=$1 AND owner=$2 FOR UPDATE", driver_id, uid)
                if not d:
                    return "Водитель не найден."
                if d["busy"]:
                    return "Этот водитель уже в рейсе."
                if d["city"] != t["city"]:
                    return f"Водитель сейчас в городе {d['city']}, а фура в {t['city']}."
                rating = d["rating"]
            else:
                own = await c.fetchval(
                    "SELECT count(*) FROM trips WHERE owner=$1 AND settled=FALSE AND driver_id IS NULL", uid)
                if own:
                    return "Вы уже за рулём другой фуры. Наймите водителя во вкладке «Водители»."
            if contract_id is not None:
                # Контракт: берём данные из contracts таблицы
                ct = await c.fetchrow(
                    "SELECT * FROM contracts WHERE id=$1 AND owner=$2 AND trips_done<trips_total AND expires_at>now() FOR UPDATE",
                    contract_id, uid)
                if not ct:
                    return "Контракт недоступен."
                if ct["tons"] > t["capacity"]:
                    return f"Груз {ct['tons']} т тяжелее, чем грузоподъёмность фуры ({t['capacity']} т)."
                to, label, cargo, tons, km = ct["to_city"], ct["to_city"], ct["cargo"], ct["tons"], ct["km"]
                price = ct["price_per_trip"]
                limit = 10**9
                empty = False
            elif order_id is not None:
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
            disc = await fuel_card.discount_pct(uid)
            fuel = fuel_cost(km, t["consumption"], t["city"], to, disc)
            advance = price * ADVANCE_PCT // 100
            if p["money"] + advance < fuel:
                return (f"Не хватает денег на топливо: нужно {money(fuel)}, "
                        f"у вас {money(p['money'])} + аванс {money(advance)}.")
            start = now()
            load_end = start + timedelta(seconds=0 if empty else load_seconds(tons))
            arrive = load_end + timedelta(seconds=travel_seconds(km, t["speed"]))
            finish = arrive + timedelta(seconds=0 if empty else load_seconds(tons))
            deadline = start + timedelta(seconds=limit)
            ik, isev, iat = maintenance.roll_incident(t, km, rating, load_end, arrive)
            await c.execute(
                """INSERT INTO trips (owner, truck_id, from_city, to_city, to_label, cargo, tons, km,
                   price, advance, fuel_cost, empty, started_at, load_end, arrive_at, finish_at, deadline,
                   inc_kind, inc_sev, inc_at, driver_id, contract_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)""",
                uid, truck_id, t["city"], to, label, cargo, tons, km, price, advance, fuel, empty,
                start, load_end, arrive, finish, deadline, ik, isev, iat, driver_id or None, contract_id or None)
            if driver_id:
                await c.execute("UPDATE drivers SET busy=TRUE WHERE id=$1", driver_id)
            await c.execute("UPDATE players SET money = money + $2 - $3 WHERE id=$1", uid, advance, fuel)
            if advance:
                await finance.post(c, uid, "advance", advance, 0, 0, f"{t['city']} → {label}")
            await finance.expense(c, uid, "fuel", fuel, True, f"{t['city']} → {label}")
            await c.execute("UPDATE trucks SET busy=TRUE WHERE id=$1", truck_id)
            if order_id is not None:
                await c.execute("DELETE FROM orders WHERE id=$1", order_id)
    return None


async def active_trip(truck_id):
    return await db.pool.fetchrow("SELECT * FROM trips WHERE truck_id=$1 AND settled=FALSE", truck_id)


def trip_status(t, inc=None):
    n = now()
    total = (t["finish_at"] - t["started_at"]).total_seconds() or 1
    p = (n - t["started_at"]).total_seconds() / total
    left = (t["finish_at"] - n).total_seconds()
    if inc:
        phase = maintenance.incident_text(inc)
    elif n < t["load_end"]:
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
    vat = 0 if row["empty"] else finance.vat_of(pay)
    net = pay - row["advance"] + vat          # деньги, которые придут на счёт (с НДС сверху)
    xp = 0 if row["empty"] else 10 + row["km"] // 20
    note = ""
    seized = 0
    async with db.pool.acquire() as c:
        async with c.transaction():
            seized = await finance.seize(c, row["owner"], net)
            if not row["empty"]:
                await finance.post(c, row["owner"], "income", net, pay, vat,
                                   f"{row['from_city']} → {row['to_label']}")
            net -= seized
            await c.execute(
                """UPDATE players SET money = money + $2, xp = xp + $3, total_earned = total_earned + $4,
                   trips_done = trips_done + $5, total_km = total_km + $6 WHERE id=$1""",
                row["owner"], net, xp, pay, 0 if row["empty"] else 1, row["km"])
            await c.execute("UPDATE trucks SET city=$2, mileage = mileage + $3, km_since_to = km_since_to + $3, busy=FALSE WHERE id=$1",
                            row["truck_id"], row["to_city"], row["km"])
            note = await drivers.after_trip(c, row)
            # Завершаем рейс по контракту, если есть
            if row["contract_id"]:
                await c.execute("UPDATE contracts SET trips_done=trips_done+1 WHERE id=$1", row["contract_id"])
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
        text += f"\n🧾 НДС {finance.VAT}% сверху: {money(vat)} (заказчик заплатил, потом отдадите государству)"
    if seized:
        text += f"\n🔒 Счёт арестован за долг по налогам: списано {money(seized)}"
    text += note
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
            # Штрафы ГИБДД — выдаются во время рейса (после погрузки, до финиша)
            fine_rows = await db.pool.fetch(
                """SELECT * FROM trips WHERE settled=FALSE AND load_end<=now() AND finish_at>now()
                   AND id NOT IN (SELECT DISTINCT trip_id FROM fines)""")
            for t in fine_rows:
                await fines.issue_fine(bot, t)
            await maintenance.process(bot)
            await drivers.process_daily(bot)
            await finance.process(bot)
            await loans.process_payments(bot)
        except Exception:
            logging.exception("watcher error")
        await asyncio.sleep(15)
