import logging
import random
from datetime import timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
import finance
from config import (INSURANCE_DAYS, INSURANCE_PCT, INSURANCE_PRICE, TO_COST, TO_HOURS,
                    TOW_DEFAULT_H, TOW_MIN_H)
from data import TO_INTERVAL
from util import dur, esc, money, now

H = 3600

# тяжесть: (название, диапазон стоимости ₽, диапазон времени ремонта в секундах)
BREAKDOWN = {
    1: ("Мелкая поломка", (8_000, 25_000), (int(0.5 * H), int(1.5 * H))),
    2: ("Серьёзная неисправность", (40_000, 120_000), (int(2 * H), int(5 * H))),
    3: ("Тяжёлая поломка", (150_000, 400_000), (int(8 * H), int(16 * H))),
}
ACCIDENT = {
    1: ("Лёгкое ДТП", (30_000, 90_000), (int(0.25 * H), int(1 * H))),
    2: ("ДТП средней тяжести", (120_000, 300_000), (int(3 * H), int(8 * H))),
    3: ("Тяжёлое ДТП", (350_000, 800_000), (int(12 * H), int(24 * H))),
}
CAUSES = {
    1: ["пробито колесо", "перегрелся двигатель", "отказал генератор", "лопнул патрубок"],
    2: ["вышла из строя турбина", "проблемы с коробкой передач", "отказала топливная система",
        "сломалась пневмоподвеска"],
    3: ["серьёзная поломка двигателя", "вышло из строя сцепление", "поломка рулевого управления"],
}
ACC_CAUSES = {
    1: ["задели отбойник на повороте", "лёгкое столкновение на парковке АЗС"],
    2: ["столкновение с легковым авто на трассе", "занос на мокрой дороге"],
    3: ["тяжёлое столкновение на трассе", "съезд в кювет"],
}


# ---------- ТО ----------
def to_interval(t):
    return TO_INTERVAL.get((t["brand"], t["model"]), 60000)


def to_line(t):
    interval = to_interval(t)
    ratio = t["km_since_to"] / interval
    icon = "✅" if ratio < 0.8 else ("⚠️" if ratio < 1 else "🔴")
    txt = f"{icon} ТО: {t['km_since_to']:,} из {interval:,} км".replace(",", "\u202f")
    if ratio >= 1:
        txt += " — ТО просрочено, риск поломок выше!"
    return txt


def insured(t):
    return bool(t["insured_until"] and t["insured_until"] > now())


def insurance_line(t):
    if insured(t):
        return f"🛡 Страховка до {t['insured_until'].strftime('%d.%m')} (покрывает {INSURANCE_PCT}% ремонта)"
    return "🛡 Страховки нет"


def in_service(t):
    return bool(t["maint_until"] and t["maint_until"] > now())


# ---------- шансы ----------
def breakdown_chance(t, km, rating):
    ratio = t["km_since_to"] / to_interval(t)
    age = 1 + max(0, t["mileage"] - 500_000) / 1_000_000
    wear = 0.8 + 0.2 * ratio if ratio <= 1 else 1 + (ratio - 1) * 2.5
    rate = 1.5 - 0.15 * rating               # рейтинг 1 -> x1.35, рейтинг 5 -> x0.75
    length = min(2.5, max(0.3, km / 600))
    return min(0.6, 0.05 * age * wear * rate * length)


def accident_chance(km, rating):
    rate = {1: 2.0, 2: 1.5, 3: 1.0, 4: 0.7, 5: 0.5}[max(1, min(5, round(rating)))]
    length = min(2.5, max(0.3, km / 600))
    return 0.012 * rate * length


def roll_incident(t, km, rating, load_end, arrive_at):
    """Решаем заранее, будет ли в рейсе происшествие и когда. -> (kind, sev, at) или (None, None, None)"""
    r = random.random()
    pa = accident_chance(km, rating)
    pb = breakdown_chance(t, km, rating)
    if r < pa:
        kind = "accident"
    elif r < pa + pb:
        kind = "breakdown"
    else:
        return None, None, None
    x = random.random()
    sev = 1 if x < 0.6 else (2 if x < 0.9 else 3)
    span = (arrive_at - load_end).total_seconds()
    at = load_end + timedelta(seconds=span * random.uniform(0.15, 0.85))
    return kind, sev, at


# ---------- происшествие ----------
def tow_state(inc):
    """('tow'|'repair'|'done', секунд до конца этапа)"""
    n = now()
    repair_end = inc["tow_at"] + timedelta(seconds=inc["repair_s"])
    if n < inc["tow_at"]:
        return "tow", (inc["tow_at"] - n).total_seconds()
    if n < repair_end:
        return "repair", (repair_end - n).total_seconds()
    return "done", 0


def incident_text(inc):
    title = (ACCIDENT if inc["kind"] == "accident" else BREAKDOWN)[inc["sev"]][0]
    phase, left = tow_state(inc)
    head = f"🚨 <b>{title}</b>: {esc(inc['cause'])}"
    if phase == "tow":
        return f"{head}\n🚛 Ждём эвакуатор: {dur(left)}\n🔧 Потом ремонт: {dur(inc['repair_s'])}"
    return f"{head}\n🔧 Идёт ремонт, осталось {dur(left)}"


async def open_incident(trip_id):
    inc = await db.pool.fetchrow(
        "SELECT * FROM incidents WHERE trip_id=$1 ORDER BY id DESC LIMIT 1", trip_id)
    if inc and tow_state(inc)[0] != "done":
        return inc
    return None


async def create_incident(bot, trip):
    row = await db.pool.fetchrow(
        "UPDATE trips SET inc_done=TRUE WHERE id=$1 AND inc_done=FALSE RETURNING *", trip["id"])
    if not row:
        return
    kind, sev = row["inc_kind"], row["inc_sev"]
    table = ACCIDENT if kind == "accident" else BREAKDOWN
    causes = ACC_CAUSES if kind == "accident" else CAUSES
    title, cost_r, rep_r = table[sev]
    cause = random.choice(causes[sev])
    cost = random.randint(*cost_r) // 100 * 100
    repair_s = random.randint(*rep_r)
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1", row["truck_id"])
    covered = cost * INSURANCE_PCT // 100 if insured(t) else 0
    pay = cost - covered
    delay = timedelta(seconds=TOW_DEFAULT_H * H + repair_s)
    async with db.pool.acquire() as c:
        async with c.transaction():
            inc_id = await c.fetchval(
                """INSERT INTO incidents (owner, truck_id, trip_id, kind, sev, cause, cost, paid,
                   started_at, tow_at, repair_s) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now(),
                   now() + make_interval(hours => $9), $10) RETURNING id""",
                row["owner"], row["truck_id"], row["id"], kind, sev, cause, cost, pay, TOW_DEFAULT_H, repair_s)
            await c.execute("UPDATE trips SET arrive_at = arrive_at + $2, finish_at = finish_at + $2 WHERE id=$1",
                            row["id"], delay)
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", row["owner"], pay)
            await finance.expense(c, row["owner"], "repair", pay, True,
                                  f"{title}: {cause}" + (" (страховка покрыла часть)" if covered else ""))
            if row["driver_id"] and kind == "breakdown":
                await c.execute("UPDATE drivers SET grievance = grievance + 1 WHERE id=$1", row["driver_id"])
    text = (f"🚨 <b>{title}!</b>\n{row['from_city']} → {esc(row['to_label'])}\n\n"
            f"Что случилось: {esc(cause)}.\n"
            f"🔧 Ремонт займёт ~{dur(repair_s)}, стоимость {money(cost)}")
    if covered:
        text += f"\n🛡 Страховка покрыла {money(covered)}, вы платите {money(pay)}"
    text += (f"\n\n🚛 Эвакуатор приедет сам через {TOW_DEFAULT_H} ч. "
             f"Вызвав его кнопкой, можно сократить ожидание до {TOW_MIN_H}-{TOW_DEFAULT_H} ч.\n"
             f"⏱ Рейс задержится, возможен штраф за опоздание.")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 Вызвать эвакуатор", callback_data=f"tow:{inc_id}")],
        [InlineKeyboardButton(text="🚚 К фуре", callback_data=f"truck:{row['truck_id']}")]])
    try:
        await bot.send_message(row["owner"], text, reply_markup=markup)
    except Exception:
        logging.exception("incident notify failed")


async def process(bot):
    rows = await db.pool.fetch(
        """SELECT * FROM trips WHERE settled=FALSE AND inc_kind IS NOT NULL
           AND inc_done=FALSE AND inc_at<=now()""")
    for t in rows:
        await create_incident(bot, t)


async def call_tow(uid, inc_id):
    """Возвращает (ok, текст)."""
    inc = await db.pool.fetchrow("SELECT * FROM incidents WHERE id=$1 AND owner=$2", inc_id, uid)
    if not inc:
        return False, "Происшествие не найдено."
    if inc["tow_called"]:
        return False, "Эвакуатор уже вызван."
    if now() >= inc["tow_at"]:
        return False, "Эвакуатор уже приехал, идёт ремонт."
    new_at = min(inc["tow_at"], now() + timedelta(seconds=random.uniform(TOW_MIN_H * H, TOW_DEFAULT_H * H)))
    saved = inc["tow_at"] - new_at
    async with db.pool.acquire() as c:
        async with c.transaction():
            await c.execute("UPDATE incidents SET tow_at=$2, tow_called=TRUE WHERE id=$1", inc_id, new_at)
            await c.execute("UPDATE trips SET arrive_at = arrive_at - $2, finish_at = finish_at - $2 WHERE id=$1",
                            inc["trip_id"], saved)
    return True, f"🚛 Эвакуатор вызван, будет через {dur((new_at - now()).total_seconds())}."


# ---------- ТО и страховка ----------
async def do_service(uid, truck_id):
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not p or not t:
                return "Фура не найдена."
            if t["busy"]:
                return "Фура в рейсе."
            if in_service(t):
                return "Фура уже на ТО."
            if p["money"] < TO_COST:
                return f"Не хватает денег: ТО стоит {money(TO_COST)}."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, TO_COST)
            await finance.expense(c, uid, "service", TO_COST, True, f"ТО {t['brand']} {t['model']}")
            await c.execute("UPDATE trucks SET km_since_to=0, maint_until = now() + make_interval(hours => $2) "
                            "WHERE id=$1", truck_id, TO_HOURS)
    return None


async def buy_insurance(uid, truck_id):
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not p or not t:
                return "Фура не найдена."
            if p["money"] < INSURANCE_PRICE:
                return f"Не хватает денег: страховка стоит {money(INSURANCE_PRICE)}."
            base = t["insured_until"] if insured(t) else now()
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, INSURANCE_PRICE)
            await finance.expense(c, uid, "insurance", INSURANCE_PRICE, False, f"{t['brand']} {t['model']}")
            await c.execute("UPDATE trucks SET insured_until=$2 WHERE id=$1", truck_id,
                            base + timedelta(days=INSURANCE_DAYS))
    return None
