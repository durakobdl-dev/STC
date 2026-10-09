"""RPG-элементы: навыки владельца (очки за уровень) и тюнинг фур."""
import db
import finance
from util import money, now

# ключ -> (название, колонка в players, макс. уровень, описание эффекта за 1 уровень)
BRANCHES = {
    "eco":  ("🌿 Эко-вождение", "sk_eco", 5, "−2% к расходу топлива за уровень"),
    "mech": ("🔧 Механик", "sk_mech", 5, "−5% к цене ТО и ремонта за уровень"),
    "neg":  ("🤝 Переговорщик", "sk_neg", 5, "+4% к авансу за уровень (максимум 50%)"),
}
ECO_PCT = 2
MECH_PCT = 5
NEG_PCT = 4

# ключ -> (название, цена, колонка в trucks, описание)
TUNING = {
    "tank":  ("⛽ Увеличенный бак", 150_000, "tune_tank",
              "−3% к стоимости топлива (реже заправляетесь по дорогим ценам трассы)"),
    "aero":  ("💨 Аэродинамические обвесы", 200_000, "tune_aero", "−2% к расходу топлива"),
    "tires": ("🛞 Хорошая резина", 120_000, "tune_tires", "−25% к шансу ДТП, зимой −40%"),
}


# ---------- очки навыков ----------
def spent(p):
    return p["sk_eco"] + p["sk_mech"] + p["sk_neg"]


def free_points(p, level):
    """Очко даётся за каждый уровень выше первого; считается из опыта, поэтому отдельно начислять не нужно."""
    return max(0, level - 1 - spent(p))


def fuel_factor(p):
    return 1 - ECO_PCT * p["sk_eco"] / 100


def service_factor(p):
    """Множитель цены ТО и ремонта."""
    return 1 - MECH_PCT * p["sk_mech"] / 100


def advance_pct(p, base):
    return base + NEG_PCT * p["sk_neg"]


async def upgrade(uid, branch):
    """Вкладывает очко в ветку. Возвращает текст ошибки или None."""
    if branch not in BRANCHES:
        return "Неизвестный навык."
    from game import level      # здесь, чтобы не было циклического импорта
    _, col, cap, _ = BRANCHES[branch]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            if not p:
                return "Игрок не найден."
            if p[col] >= cap:
                return "Навык уже на максимуме."
            if free_points(p, level(p["xp"])) <= 0:
                return "Нет свободных очков навыков: они даются за каждый новый уровень."
            await c.execute(f"UPDATE players SET {col} = {col} + 1 WHERE id=$1", uid)
    return None


def render(p, level):
    free = free_points(p, level)
    lines = [f"🧠 <b>Навыки</b>\n⭐ Уровень {level} · свободных очков: <b>{free}</b>",
             "Очко даётся за каждый новый уровень.\n"]
    for key, (title, col, cap, desc) in BRANCHES.items():
        lines.append(f"{title}: <b>{p[col]}/{cap}</b>\n   {desc}")
    return "\n".join(lines)


# ---------- тюнинг ----------
def truck_fuel_factor(t):
    f = 1.0
    if t["tune_tank"]:
        f *= 0.97
    if t["tune_aero"]:
        f *= 0.98
    return f


def is_winter():
    return now().month in (12, 1, 2)


def accident_factor(t):
    if not t["tune_tires"]:
        return 1.0
    return 0.6 if is_winter() else 0.75


def tuning_lines(t):
    lines = []
    for key, (title, price, col, desc) in TUNING.items():
        mark = "✅ установлено" if t[col] else f"{money(price)}"
        lines.append(f"{title} — {mark}\n   {desc}")
    return "\n".join(lines)


async def buy_tuning(uid, truck_id, kind):
    """Возвращает текст ошибки или None."""
    if kind not in TUNING:
        return "Неизвестный тюнинг."
    title, price, col, _ = TUNING[kind]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT * FROM players WHERE id=$1 FOR UPDATE", uid)
            t = await c.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2 FOR UPDATE", truck_id, uid)
            if not p or not t:
                return "Фура не найдена."
            if t["busy"]:
                return "Фура в рейсе."
            if t[col]:
                return "Это улучшение уже установлено."
            if p["money"] < price:
                return f"Не хватает денег: {title} стоит {money(price)}."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, price)
            await c.execute(f"UPDATE trucks SET {col}=TRUE WHERE id=$1", truck_id)
            await finance.expense(c, uid, "tuning", price, False, f"{title}: {t['brand']} {t['model']}")
    return None
