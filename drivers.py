import logging
import random

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
import finance
from util import esc, money, now

FIRST = ["Александр", "Сергей", "Андрей", "Дмитрий", "Алексей", "Михаил", "Иван", "Николай", "Владимир",
         "Евгений", "Олег", "Виктор", "Игорь", "Роман", "Павел", "Артём", "Денис", "Максим", "Юрий",
         "Геннадий", "Валерий", "Фёдор", "Руслан", "Тимур"]
LAST = ["Иванов", "Петров", "Сидоров", "Кузнецов", "Смирнов", "Волков", "Морозов", "Новиков", "Фролов",
        "Соколов", "Лебедев", "Орлов", "Захаров", "Беляев", "Комаров", "Тихонов", "Гусев", "Ершов",
        "Мартынов", "Зуев", "Рябов", "Данилов", "Копылов", "Шаров"]

# стаж (рейсов), с которого водитель получает следующий рейтинг
PROMOTE = {1: 15, 2: 50, 3: 130, 4: 300}
RATING_WEIGHTS = [15, 30, 30, 18, 7]
CANDIDATES_PER_DAY = 5
MAX_DRIVERS = 20


def stars(r):
    return "⭐" * int(r)


def market(rating, stazh):
    """Рыночная ставка, ₽/км: новичок ~12, опытный ~27."""
    return 12 + 3 * (rating - 1) + min(stazh, 300) / 100


def hire_cost(rating, stazh):
    return 8_000 + 10_000 * (rating - 1) + stazh * 40


def complaints(d):
    out = []
    if d["salary"] < 0.85 * market(d["rating"], d["stazh"]):
        out.append("😠 недоволен зарплатой")
    if d["grievance"] >= 3:
        out.append("😟 устал от поломок")
    return out


def card_line(d):
    st = "🔴 в рейсе" if d["busy"] else "🟢 свободен"
    return f"{esc(d['name'])} {stars(d['rating'])} · {d['city']} · {st}"


# ---------- кандидаты ----------
def _gen():
    rating = random.choices([1, 2, 3, 4, 5], RATING_WEIGHTS)[0]
    lo = PROMOTE.get(rating - 1, 0)
    hi = PROMOTE[rating] - 1 if rating in PROMOTE else 450
    stazh = random.randint(lo, hi)
    salary = max(10, round(market(rating, stazh) * random.uniform(0.95, 1.1)))
    return f"{random.choice(FIRST)} {random.choice(LAST)}", rating, stazh, salary


async def ensure_candidates(uid):
    have = await db.pool.fetchval(
        "SELECT count(*) FROM candidates WHERE owner=$1 AND day=current_date", uid)
    if have:
        return
    await db.pool.execute("DELETE FROM candidates WHERE owner=$1", uid)
    for _ in range(CANDIDATES_PER_DAY):
        n, r, s, sal = _gen()
        await db.pool.execute(
            "INSERT INTO candidates (owner, day, name, rating, stazh, salary) VALUES ($1,current_date,$2,$3,$4,$5)",
            uid, n, r, s, sal)


async def hire(uid, cand_id, city):
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            cd = await c.fetchrow("SELECT * FROM candidates WHERE id=$1 AND owner=$2 FOR UPDATE", cand_id, uid)
            if not p or not cd:
                return "Кандидат уже нанят или ушёл."
            cnt = await c.fetchval("SELECT count(*) FROM drivers WHERE owner=$1", uid)
            if cnt >= MAX_DRIVERS:
                return f"Максимум водителей: {MAX_DRIVERS}."
            cost = hire_cost(cd["rating"], cd["stazh"])
            if p["money"] < cost:
                return f"Не хватает денег: найм стоит {money(cost)}."
            if cnt == 0:
                await c.execute("UPDATE players SET last_payroll=now() WHERE id=$1", uid)
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, cost)
            await finance.expense(c, uid, "hire", cost, False, f"Найм: {cd['name']}")
            await c.execute(
                "INSERT INTO drivers (owner, name, rating, stazh, salary, city) VALUES ($1,$2,$3,$4,$5,$6)",
                uid, cd["name"], cd["rating"], cd["stazh"], cd["salary"], city)
            await c.execute("DELETE FROM candidates WHERE id=$1", cand_id)
    return None


# ---------- управление ----------
async def free_in_city(uid, city):
    return await db.pool.fetch(
        "SELECT * FROM drivers WHERE owner=$1 AND city=$2 AND busy=FALSE ORDER BY rating DESC, id", uid, city)


async def raise_salary(uid, did):
    row = await db.pool.fetchrow(
        "UPDATE drivers SET salary = salary + 1, grievance = GREATEST(0, grievance - 1) "
        "WHERE id=$1 AND owner=$2 AND salary < 45 RETURNING salary", did, uid)
    return row["salary"] if row else None


async def fire(uid, did):
    async with db.pool.acquire() as c:
        async with c.transaction():
            d = await c.fetchrow("SELECT * FROM drivers WHERE id=$1 AND owner=$2 FOR UPDATE", did, uid)
            if not d:
                return "Водитель не найден."
            if d["busy"]:
                return "Нельзя уволить водителя, пока он в рейсе."
            if d["owed"]:
                cash = d["owed"] * (100 - finance.NDFL) // 100
                await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, cash)
                await finance.post(c, uid, "salary", -cash, d["owed"], 0, f"Расчёт: {d['name']}")
            await c.execute("DELETE FROM drivers WHERE id=$1", did)
    return None


# ---------- после рейса (вызывается внутри транзакции settle) ----------
async def after_trip(c, row):
    """Обновляет водителя после рейса. Возвращает текст-приписку для игрока или ''."""
    if not row["driver_id"]:
        return ""
    d = await c.fetchrow("SELECT * FROM drivers WHERE id=$1 FOR UPDATE", row["driver_id"])
    if not d:
        return ""
    had_breakdown = row["inc_done"] and row["inc_kind"] == "breakdown"
    stazh = d["stazh"] + (0 if row["empty"] else 1)
    rating = d["rating"]
    promoted = False
    if rating < 5 and stazh >= PROMOTE[rating]:
        rating += 1
        promoted = True
    grievance = d["grievance"] if had_breakdown else max(0, d["grievance"] - 1)
    wage = row["km"] * d["salary"]
    await c.execute(
        """UPDATE drivers SET busy=FALSE, city=$2, stazh=$3, rating=$4, grievance=$5,
           owed = owed + $6, total_km = total_km + $7 WHERE id=$1""",
        d["id"], row["to_city"], stazh, rating, grievance, wage, row["km"])
    note = f"\n👤 Водитель {esc(d['name'])}: начислено {money(wage)} (выплата раз в сутки)"
    if promoted:
        note += f"\n🎉 {esc(d['name'])} повысил рейтинг до {stars(rating)}. Проверьте его ставку!"
    return note


# ---------- раз в сутки: зарплата и увольнения ----------
async def process_daily(bot):
    rows = await db.pool.fetch(
        """SELECT id FROM players p WHERE last_payroll <= now() - interval '1 day'
           AND EXISTS (SELECT 1 FROM drivers d WHERE d.owner = p.id)""")
    for r in rows:
        try:
            await _daily_for(bot, r["id"])
        except Exception:
            logging.exception("daily payroll error")


async def _daily_for(bot, uid):
    got = await db.pool.fetchval(
        "UPDATE players SET last_payroll=now() WHERE id=$1 AND last_payroll <= now() - interval '1 day' RETURNING id",
        uid)
    if not got:
        return
    drivers = await db.pool.fetch("SELECT * FROM drivers WHERE owner=$1", uid)
    total = sum(d["owed"] for d in drivers)
    quit_names = []
    async with db.pool.acquire() as c:
        async with c.transaction():
            cash = total * (100 - finance.NDFL) // 100
            if total:
                await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, cash)
                await finance.post(c, uid, "salary", -cash, total, 0, "Зарплата водителям")
                await c.execute("UPDATE drivers SET owed=0 WHERE owner=$1", uid)
            for d in drivers:
                if d["busy"] or (now() - d["hired_at"]).total_seconds() < 86400:
                    continue
                chance = 0.0
                if d["salary"] < 0.85 * market(d["rating"], d["stazh"]):
                    chance += 0.3
                if d["grievance"] >= 3:
                    chance += 0.1 * (d["grievance"] - 2)
                if chance and random.random() < min(0.6, chance):
                    await c.execute("DELETE FROM drivers WHERE id=$1", d["id"])
                    quit_names.append(d["name"])
    lines = []
    if total:
        lines.append(f"💸 <b>Зарплата водителям:</b> начислено {money(total)}, на руки {money(cash)}. "
                     f"НДФЛ {finance.NDFL}% и взносы {finance.CONTRIB}% пойдут в налоги периода.")
    for n in quit_names:
        lines.append(f"🚪 Водитель {esc(n)} уволился (низкая ставка или частые поломки).")
    if lines:
        try:
            await bot.send_message(
                uid, "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="👥 Водители", callback_data="drivers")]]))
        except Exception:
            logging.exception("payroll notify failed")
