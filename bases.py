"""Собственные базы в городах: СТО, АЗС и диспетчер (лейт-гейм)."""
import db
import finance
from util import esc, money

BASE_PRICE = {
    "Москва": 5_000_000, "Санкт-Петербург": 4_000_000, "Сочи": 3_000_000,
    "Екатеринбург": 2_500_000, "Новосибирск": 2_000_000, "Севастополь": 2_000_000,
    "Пермь": 1_500_000,
}
DEFAULT_BASE_PRICE = 2_500_000

STO_FACTOR = 0.7        # −30% к ТО и ремонту
AZS_FACTOR = 0.9        # −10% к топливу (закупка оптом)
VIP_ORDERS = 2          # столько VIP-заказов держит диспетчер
VIP_BONUS = 1.6         # VIP-заказ платит в 1.6 раза больше обычного

# ключ -> (название, цена, колонка, описание)
UPGRADES = {
    "sto":  ("🛠 Своя СТО", 2_000_000, "sto", "−30% на ТО и ремонт в этом городе"),
    "azs":  ("⛽ Своя АЗС", 3_000_000, "azs", "−10% на топливо в рейсах из этого города (закупка оптом)"),
    "disp": ("📞 Диспетчер", 1_500_000, "dispatcher", "открывает скрытые VIP-заказы в этом городе"),
}


def price_of(city):
    return BASE_PRICE.get(city, DEFAULT_BASE_PRICE)


async def owned(uid):
    return await db.pool.fetch("SELECT * FROM bases WHERE owner=$1 ORDER BY bought_at", uid)


async def get(uid, city):
    return await db.pool.fetchrow("SELECT * FROM bases WHERE owner=$1 AND city=$2", uid, city)


# ---------- эффекты ----------
async def sto_factor(uid, *cities):
    """Скидка СТО действует, если СТО есть хотя бы в одном из указанных городов."""
    hit = await db.pool.fetchval(
        "SELECT count(*) FROM bases WHERE owner=$1 AND city = ANY($2::text[]) AND sto", uid, list(cities))
    return STO_FACTOR if hit else 1.0


async def azs_factor(uid, city):
    hit = await db.pool.fetchval("SELECT count(*) FROM bases WHERE owner=$1 AND city=$2 AND azs", uid, city)
    return AZS_FACTOR if hit else 1.0


async def has_dispatcher(uid, city):
    return bool(await db.pool.fetchval(
        "SELECT count(*) FROM bases WHERE owner=$1 AND city=$2 AND dispatcher", uid, city))


# ---------- покупки ----------
async def buy_base(uid, city):
    """Возвращает текст ошибки или None."""
    price = price_of(city)
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            if not p:
                return "Игрок не найден."
            if await c.fetchval("SELECT count(*) FROM bases WHERE owner=$1 AND city=$2", uid, city):
                return "У вас уже есть база в этом городе."
            if p["money"] < price:
                return f"Не хватает денег: участок в городе {city} стоит {money(price)}."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, price)
            await c.execute("INSERT INTO bases (owner, city) VALUES ($1,$2)", uid, city)
            await finance.expense(c, uid, "base", price, False, f"База: {city}")
    return None


async def buy_upgrade(uid, city, kind):
    """Возвращает текст ошибки или None."""
    if kind not in UPGRADES:
        return "Неизвестное улучшение."
    title, price, col, _ = UPGRADES[kind]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            b = await c.fetchrow("SELECT * FROM bases WHERE owner=$1 AND city=$2 FOR UPDATE", uid, city)
            if not p:
                return "Игрок не найден."
            if not b:
                return "Сначала купите базу в этом городе."
            if b[col]:
                return "Это улучшение уже построено."
            if p["money"] < price:
                return f"Не хватает денег: {title} стоит {money(price)}."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, price)
            await c.execute(f"UPDATE bases SET {col}=TRUE WHERE owner=$1 AND city=$2", uid, city)
            await finance.expense(c, uid, "base", price, False, f"{title}: {city}")
    return None


# ---------- тексты ----------
def base_card(b):
    lines = [f"🏗 <b>База: {esc(b['city'])}</b>\n"]
    for key, (title, price, col, desc) in UPGRADES.items():
        status = "✅ построено" if b[col] else money(price)
        lines.append(f"{title} — {status}\n   {desc}")
    return "\n".join(lines)
