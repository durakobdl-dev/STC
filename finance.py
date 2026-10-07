import logging
from datetime import timedelta

import db
from util import money, now

VAT = 22            # НДС, %
PROFIT_TAX = 25     # налог на прибыль, %
NDFL = 13           # НДФЛ с зарплаты водителей, %
CONTRIB = 30        # страховые взносы работодателя, %
PERIOD_DAYS = 3     # налоговый период
DEADLINE_DAYS = 3   # срок оплаты после конца периода
PENALTY_PCT = 10    # штраф за просрочку, %

# расходы, уменьшающие прибыль
EXPENSE_KINDS = ("fuel", "repair", "service", "insurance", "hire", "license")
# расходы с НДС, который можно принять к вычету
VAT_KINDS = ("fuel", "repair", "service", "purchase")

LABELS = {
    "income": "📦 Оплата рейса", "advance": "💵 Аванс", "fuel": "⛽ Топливо", "repair": "🔧 Ремонт",
    "service": "🛠 ТО", "insurance": "🛡 Страховка", "hire": "👤 Найм водителя", "license": "🪪 Лицензия",
    "salary": "💸 Зарплата водителям", "tax": "🏛 Налоги", "purchase": "🚚 Покупка фуры",
    "sale": "💰 Продажа фуры", "fine": "🚔 Штраф ГИБДД", "loan_in": "💳 Кредит", "loan_out": "💸 Погашение кредита",
}


def vat_of(net):
    """НДС сверху на цену без НДС."""
    return net * VAT // 100


def split_vat(gross):
    vat = gross * VAT // (100 + VAT)
    return gross - vat, vat


async def post(conn, owner, kind, cash, net=0, vat=0, note=""):
    await conn.execute(
        "INSERT INTO ledger (owner, kind, cash, net, vat, note) VALUES ($1,$2,$3,$4,$5,$6)",
        owner, kind, int(cash), int(net), int(vat), note)


async def expense(conn, owner, kind, gross, with_vat, note=""):
    """Записывает расход в журнал (деньги списываются отдельно)."""
    net, vat = split_vat(gross) if with_vat else (gross, 0)
    await post(conn, owner, kind, -gross, net, vat, note)


# ---------- расчёт налогов ----------
_AGG = """
SELECT
  coalesce(sum(net) FILTER (WHERE kind='income'),0)::bigint AS inc,
  coalesce(sum(vat) FILTER (WHERE kind='income'),0)::bigint AS vat_out,
  coalesce(sum(vat) FILTER (WHERE kind IN ('fuel','repair','service','purchase')),0)::bigint AS vat_in,
  coalesce(sum(net) FILTER (WHERE kind IN ('fuel','repair','service','insurance','hire','license')),0)::bigint AS exp,
  coalesce(sum(net) FILTER (WHERE kind='salary'),0)::bigint AS wages
FROM ledger WHERE owner=$1 AND ts >= $2 AND ts < $3
"""


def compute(a, credit, loss):
    vat_net = a["vat_out"] - a["vat_in"] - credit
    vat_due = max(0, vat_net)
    new_credit = max(0, -vat_net)
    contrib = a["wages"] * CONTRIB // 100
    profit = a["inc"] - a["exp"] - a["wages"] - contrib
    base = profit - loss
    profit_tax = max(0, base) * PROFIT_TAX // 100
    new_loss = max(0, -base)
    payroll = a["wages"] * (NDFL + CONTRIB) // 100
    return {"vat": vat_due, "profit_tax": profit_tax, "payroll": payroll, "profit": profit,
            "new_credit": new_credit, "new_loss": new_loss, "total": vat_due + profit_tax + payroll,
            "income": a["inc"], "expenses": a["exp"] + a["wages"] + contrib}


async def estimate(uid):
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", uid)
    a = await db.pool.fetchrow(_AGG, uid, p["tax_period_start"], now() + timedelta(days=1))
    return p, compute(a, p["vat_credit"], p["tax_loss"])


async def close_period(uid):
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow(
                """UPDATE players SET tax_period_start = tax_period_start + make_interval(days => $2)
                   WHERE id=$1 AND tax_period_start <= now() - make_interval(days => $2)
                   RETURNING tax_period_start, vat_credit, tax_loss""", uid, PERIOD_DAYS)
            if not p:
                return
            end = p["tax_period_start"]
            start = end - timedelta(days=PERIOD_DAYS)
            a = await c.fetchrow(_AGG, uid, start, end)
            r = compute(a, p["vat_credit"], p["tax_loss"])
            await c.execute("UPDATE players SET vat_credit=$2, tax_loss=$3 WHERE id=$1",
                            uid, r["new_credit"], r["new_loss"])
            if r["total"] > 0:
                await c.execute(
                    """INSERT INTO tax_bills (owner, period_start, period_end, vat, profit, payroll, total, due, deadline)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$7, $3 + make_interval(days => $8))""",
                    uid, start, end, r["vat"], r["profit_tax"], r["payroll"], r["total"], DEADLINE_DAYS)


async def refresh_arrest(c, uid):
    await c.execute(
        """UPDATE players SET arrested = EXISTS(
           SELECT 1 FROM tax_bills WHERE owner=$1 AND paid_at IS NULL AND penalized) WHERE id=$1""", uid)


async def process(bot):
    rows = await db.pool.fetch(
        "SELECT id FROM players WHERE tax_period_start <= now() - make_interval(days => $1)", PERIOD_DAYS)
    for r in rows:
        try:
            await close_period(r["id"])
        except Exception:
            logging.exception("close_period error")
    late = await db.pool.fetch(
        """UPDATE tax_bills SET due = due * (100 + $1) / 100, penalized = TRUE
           WHERE paid_at IS NULL AND NOT penalized AND deadline < now() RETURNING owner""", PENALTY_PCT)
    for r in late:
        await db.pool.execute("UPDATE players SET arrested=TRUE WHERE id=$1", r["owner"])


# ---------- оплата и арест счёта ----------
async def open_bills(uid):
    return await db.pool.fetch("SELECT * FROM tax_bills WHERE owner=$1 AND paid_at IS NULL ORDER BY id", uid)


async def pay_all(uid):
    """Оплачивает долги по порядку, пока хватает денег. -> (оплачено, осталось, ошибка)"""
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            bills = await c.fetch(
                "SELECT * FROM tax_bills WHERE owner=$1 AND paid_at IS NULL ORDER BY id FOR UPDATE", uid)
            if not bills:
                return 0, 0, "Налогов к оплате нет."
            have, paid = p["money"], 0
            for b in bills:
                if have - paid >= b["due"]:
                    paid += b["due"]
                    await c.execute("UPDATE tax_bills SET due=0, paid_at=now() WHERE id=$1", b["id"])
            if paid == 0:
                return 0, sum(b["due"] for b in bills), "Не хватает денег даже на самый старый счёт."
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, paid)
            await post(c, uid, "tax", -paid, note="Оплата налогов")
            await refresh_arrest(c, uid)
            left = await c.fetchval("SELECT coalesce(sum(due),0) FROM tax_bills WHERE owner=$1 AND paid_at IS NULL", uid)
            return paid, left, None


async def seize(c, uid, avail):
    """При аресте счёта забирает часть выручки в счёт долга. Возвращает сколько забрали."""
    if avail <= 0:
        return 0
    if not await c.fetchval("SELECT arrested FROM players WHERE id=$1", uid):
        return 0
    bills = await c.fetch(
        "SELECT * FROM tax_bills WHERE owner=$1 AND paid_at IS NULL ORDER BY id FOR UPDATE", uid)
    taken = 0
    for b in bills:
        x = min(avail - taken, b["due"])
        if x <= 0:
            break
        taken += x
        if x == b["due"]:
            await c.execute("UPDATE tax_bills SET due=0, paid_at=now() WHERE id=$1", b["id"])
        else:
            await c.execute("UPDATE tax_bills SET due=due-$2 WHERE id=$1", b["id"], x)
    if taken:
        await post(c, uid, "tax", -taken, note="Списание по аресту счёта")
    await refresh_arrest(c, uid)
    return taken


# ---------- отчёты ----------
async def history(uid, limit=12):
    return await db.pool.fetch(
        "SELECT * FROM ledger WHERE owner=$1 ORDER BY id DESC LIMIT $2", uid, limit)


async def totals(uid):
    return await db.pool.fetch(
        "SELECT kind, sum(net)::bigint AS net, sum(cash)::bigint AS cash FROM ledger WHERE owner=$1 GROUP BY kind", uid)


def fmt_cash(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + money(abs(v))
