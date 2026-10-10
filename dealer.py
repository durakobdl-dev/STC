import random

import db
import finance
import progression
from data import BRANDS
from util import money, now

NOW_YEAR = 2026
DEALER_CITIES = ["Москва", "Санкт-Петербург", "Екатеринбург", "Новосибирск"]
NEW_LOTS = 2
USED_LOTS = 4
SELL_PCT = 85       # при продаже фуры дают столько % от справедливой цены

# цена новой фуры, ₽
NEW_PRICE = {
    ("Volvo", "FH 460"): 4_200_000, ("Volvo", "FH 500"): 5_000_000,
    ("MAN", "TGX 18.440"): 3_800_000, ("MAN", "TGX 18.480"): 4_400_000,
    ("Scania", "R 450"): 4_100_000, ("Scania", "R 500"): 4_800_000,
}


def spec(brand, model):
    return next(b for b in BRANDS if b[0] == brand and b[1] == model)


def fair_price(brand, model, year, mileage):
    age = max(0, NOW_YEAR - year)
    base = NEW_PRICE[(brand, model)]
    price = base * (0.87 ** age) * max(0.45, 1 - mileage / 1_800_000)
    return int(price // 1000 * 1000)


def sell_price(t):
    return int(fair_price(t["brand"], t["model"], t["year"], t["mileage"]) * SELL_PCT / 100 // 1000 * 1000)


async def ensure_stock(city):
    have = await db.pool.fetchval(
        "SELECT count(*) FROM dealer_stock WHERE city=$1 AND day=current_date", city)
    if have:
        return
    await db.pool.execute("DELETE FROM dealer_stock WHERE city=$1", city)
    models = [(b[0], b[1]) for b in BRANDS]
    for i in range(NEW_LOTS + USED_LOTS):
        brand, model = random.choice(models)
        if i < NEW_LOTS:
            year, mileage, price, is_new = NOW_YEAR, 0, NEW_PRICE[(brand, model)], True
        else:
            year = random.randint(2012, 2023)
            mileage = min(950_000, int((NOW_YEAR - year) * random.randint(50_000, 100_000)) + random.randint(0, 30_000))
            price, is_new = fair_price(brand, model, year, mileage), False
        await db.pool.execute(
            "INSERT INTO dealer_stock (city, day, brand, model, year, mileage, price, is_new) "
            "VALUES ($1,current_date,$2,$3,$4,$5,$6,$7)", city, brand, model, year, mileage, price, is_new)


async def buy(uid, lot_id):
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            lot = await c.fetchrow("SELECT * FROM dealer_stock WHERE id=$1 FOR UPDATE", lot_id)
            if not p or not lot:
                return "Эту фуру уже купили."
            lvl = progression.level(p["xp"])
            if lot["is_new"] and lvl < progression.NEW_TRUCKS_LEVEL:
                return f"Новые фуры продаются с {progression.NEW_TRUCKS_LEVEL} уровня компании."
            limit = progression.max_trucks(lvl)
            if await c.fetchval("SELECT count(*) FROM trucks WHERE owner=$1", uid) >= limit:
                return f"Лимит фур на вашем уровне: {limit}. Повышайте уровень компании, чтобы расширять парк."
            if p["money"] < lot["price"]:
                return f"Не хватает денег: фура стоит {money(lot['price'])}."
            b = spec(lot["brand"], lot["model"])
            await c.execute(
                """INSERT INTO trucks (owner, brand, model, year, mileage, capacity, consumption, speed, city, km_since_to)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,0)""",
                uid, lot["brand"], lot["model"], lot["year"], lot["mileage"], b[3], b[4], b[5], lot["city"])
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, lot["price"])
            vat = finance.split_vat(lot["price"])[1] if lot["is_new"] else 0
            await finance.post(c, uid, "purchase", -lot["price"], lot["price"] - vat, vat,
                               f"{lot['brand']} {lot['model']} ({lot['city']})")
            await c.execute("DELETE FROM dealer_stock WHERE id=$1", lot_id)
    return None


async def sell(uid, truck_id):
    async with db.pool.acquire() as c:
        async with c.transaction():
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not t:
                return "Фура не найдена.", 0
            if t["busy"]:
                return "Фура в рейсе.", 0
            if t["maint_until"] and t["maint_until"] > now():
                return "Фура на ТО.", 0
            if await c.fetchval("SELECT count(*) FROM trucks WHERE owner=$1", uid) <= 1:
                return "Это ваша единственная фура, продавать нельзя.", 0
            price = sell_price(t)
            await c.execute("DELETE FROM trucks WHERE id=$1", truck_id)
            await c.execute("UPDATE players SET money = money + $2 WHERE id=$1", uid, price)
            await finance.post(c, uid, "sale", price, 0, 0, f"{t['brand']} {t['model']}")
    return None, price
