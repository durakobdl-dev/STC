"""Админ-меню. Доступ только у Telegram ID из переменной окружения ADMIN_IDS (через запятую)."""
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import dealer
from config import ADMIN_IDS, LICENSE_DAYS
from data import BRANDS, CITIES
from util import esc, money, now

router = Router()
STATE = {}   # id админа -> {"act": ..., "target": ...}: ждём от него следующее сообщение


def is_admin(uid):
    return uid in ADMIN_IDS


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


async def ashow(call: CallbackQuery, text, markup):
    msg = call.message
    try:
        if msg.photo:
            await msg.delete()
            await msg.answer(text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
    except Exception as e:      # «message is not modified» и т.п.
        if "not modified" not in str(e):
            logging.exception("admin show failed")
    try:
        await call.answer()
    except Exception:
        pass


async def deny(call: CallbackQuery):
    await call.answer("Нет доступа", show_alert=True)


def parse_amount(s):
    """500000, -2000, 500к, 1.5м, 2m."""
    s = s.lower().replace("_", "").replace(" ", "").replace(",", ".")
    mult = 1
    if s.endswith(("к", "k")):
        mult, s = 1_000, s[:-1]
    elif s.endswith(("м", "m")):
        mult, s = 1_000_000, s[:-1]
    return int(float(s) * mult)


async def find_player(token, me):
    token = token.lower()
    uid = me if token in ("me", "я") else int(token)
    return await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", uid)


async def notify(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception:
        logging.warning("admin notify failed for %s", uid)


# ---------- главная страница ----------
async def admin_page():
    n = await db.pool.fetchval("SELECT count(*) FROM players")
    total = await db.pool.fetchval("SELECT coalesce(sum(money),0) FROM players")
    trucks = await db.pool.fetchval("SELECT count(*) FROM trucks")
    text = (f"🛠 <b>Админ-меню</b>\n\n👥 Игроков: {n}\n🚚 Фур в игре: {trucks}\n"
            f"💰 Денег у всех игроков: {money(total)}\n\nЧто сделать?")
    markup = kb([[("💰 Выдать / снять деньги", "adm:money")],
                 [("🚚 Выдать фуру", "adm:truck")],
                 [("🪪 Выдать лицензии на все города", "adm:lic")],
                 [("👥 Список игроков", "adm:players")],
                 [("⬅️ Меню", "menu")]])
    return text, markup


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    STATE.pop(m.from_user.id, None)
    text, markup = await admin_page()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm")
async def adm_home(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    STATE.pop(call.from_user.id, None)
    text, markup = await admin_page()
    await ashow(call, text, markup)


# ---------- деньги ----------
@router.callback_query(F.data == "adm:money")
async def adm_money(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    STATE[call.from_user.id] = {"act": "money"}
    await ashow(call, "💰 <b>Деньги игроку</b>\n\nОтправьте сообщением: <code>ID сумма</code>\n"
                      "Примеры:\n<code>123456789 500000</code>\n<code>123456789 2м</code> (2 млн)\n"
                      "<code>123456789 -50к</code> (списать 50 тыс.)\n<code>me 1м</code> (себе)\n\n"
                      "ID игроков смотрите в «👥 Список игроков».",
                kb([[("⬅️ Админ-меню", "adm")]]))


# ---------- фуры ----------
@router.callback_query(F.data == "adm:truck")
async def adm_truck(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    STATE[call.from_user.id] = {"act": "truck"}
    await ashow(call, "🚚 <b>Выдать фуру</b>\n\nОтправьте сообщением ID игрока (или <code>me</code> для себя). "
                      "Фура будет новая, в его домашнем городе.", kb([[("⬅️ Админ-меню", "adm")]]))


@router.callback_query(F.data.startswith("adm:t:"))
async def adm_truck_give(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    _, _, target, j = call.data.split(":")
    target, b = int(target), BRANDS[int(j)]
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", target)
    if not p:
        return await call.answer("Игрок не найден", show_alert=True)
    if await db.pool.fetchval("SELECT count(*) FROM trucks WHERE owner=$1", target) >= dealer.MAX_TRUCKS:
        return await call.answer(f"У игрока уже максимум фур ({dealer.MAX_TRUCKS})", show_alert=True)
    await db.pool.execute(
        """INSERT INTO trucks (owner, brand, model, year, mileage, capacity, consumption, speed, city, km_since_to)
           VALUES ($1,$2,$3,$4,0,$5,$6,$7,$8,0)""",
        target, b[0], b[1], dealer.NOW_YEAR, b[3], b[4], b[5], p["home_city"])
    await notify(call.bot, target, f"🎁 Администрация выдала вам фуру: {b[0]} {b[1]} (новая) в городе {p['home_city']}.")
    STATE.pop(call.from_user.id, None)
    await ashow(call, f"✅ Выдана {b[0]} {b[1]} игроку {esc(p['name'])} (<code>{target}</code>), город {esc(p['home_city'])}.",
                kb([[("🚚 Ещё фуру", "adm:truck")], [("⬅️ Админ-меню", "adm")]]))


# ---------- лицензии ----------
@router.callback_query(F.data == "adm:lic")
async def adm_lic(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    STATE[call.from_user.id] = {"act": "lic"}
    await ashow(call, f"🪪 <b>Лицензии</b>\n\nОтправьте ID игрока (или <code>me</code>): "
                      f"получит лицензии на все города на {LICENSE_DAYS} дней.", kb([[("⬅️ Админ-меню", "adm")]]))


# ---------- игроки ----------
@router.callback_query(F.data == "adm:players")
async def adm_players(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await deny(call)
    rows = await db.pool.fetch(
        """SELECT p.id, p.name, p.money, p.home_city, (SELECT count(*) FROM trucks t WHERE t.owner=p.id) AS n
           FROM players p ORDER BY p.money DESC LIMIT 25""")
    lines = ["👥 <b>Игроки</b> (по деньгам, до 25)\n"]
    for r in rows:
        lines.append(f"<code>{r['id']}</code> · {esc(r['name'])} · {money(r['money'])} · 🚚 {r['n']} · {esc(r['home_city'])}")
    if not rows:
        lines.append("Пока никого.")
    lines.append("\nНажмите на ID, чтобы скопировать.")
    await ashow(call, "\n".join(lines), kb([[("⬅️ Админ-меню", "adm")]]))


# ---------- ввод текста админом ----------
def waiting(m: Message):
    return bool(m.text) and is_admin(m.from_user.id) and m.from_user.id in STATE


@router.message(waiting)
async def admin_input(m: Message):
    uid = m.from_user.id
    st = STATE[uid]
    parts = m.text.split()
    back = kb([[("⬅️ Админ-меню", "adm")]])
    try:
        if st["act"] == "money":
            if len(parts) != 2:
                raise ValueError("нужно два значения: ID и сумма")
            p = await find_player(parts[0], uid)
            if not p:
                return await m.answer("Игрок с таким ID не найден. Проверьте ID и отправьте ещё раз.", reply_markup=back)
            amount = parse_amount(parts[1])
            new = await db.pool.fetchval(
                "UPDATE players SET money = money + $2 WHERE id=$1 RETURNING money", p["id"], amount)
            word = "начислила" if amount >= 0 else "списала"
            await notify(m.bot, p["id"], f"🏛 Администрация {word} {money(abs(amount))}. Баланс: {money(new)}")
            STATE.pop(uid, None)
            await m.answer(f"✅ {esc(p['name'])} (<code>{p['id']}</code>): {'+' if amount >= 0 else '−'}{money(abs(amount))}\n"
                           f"Баланс теперь: <b>{money(new)}</b>",
                           reply_markup=kb([[("💰 Ещё", "adm:money")], [("⬅️ Админ-меню", "adm")]]))
        elif st["act"] == "truck":
            p = await find_player(parts[0], uid)
            if not p:
                return await m.answer("Игрок с таким ID не найден. Проверьте ID и отправьте ещё раз.", reply_markup=back)
            rows = [[(f"{b[0]} {b[1]} · {b[3]} т · {b[5]} км/ч", f"adm:t:{p['id']}:{j}")] for j, b in enumerate(BRANDS)]
            rows.append([("⬅️ Админ-меню", "adm")])
            STATE.pop(uid, None)
            await m.answer(f"Какую фуру выдать игроку {esc(p['name'])}? (город: {esc(p['home_city'])})",
                           reply_markup=kb(rows))
        elif st["act"] == "lic":
            p = await find_player(parts[0], uid)
            if not p:
                return await m.answer("Игрок с таким ID не найден. Проверьте ID и отправьте ещё раз.", reply_markup=back)
            async with db.pool.acquire() as c:
                async with c.transaction():
                    for city in CITIES:
                        cur = await c.fetchval("SELECT expires_at FROM licenses WHERE owner=$1 AND city=$2", p["id"], city)
                        base = max(now(), cur) if cur else now()
                        await c.execute(
                            """INSERT INTO licenses (owner, city, expires_at) VALUES ($1,$2,$3)
                               ON CONFLICT (owner, city) DO UPDATE SET expires_at=$3""",
                            p["id"], city, base + timedelta(days=LICENSE_DAYS))
                    await c.execute("DELETE FROM orders WHERE owner=$1", p["id"])
            await notify(m.bot, p["id"], f"🎁 Администрация выдала вам лицензии на все города на {LICENSE_DAYS} дней.")
            STATE.pop(uid, None)
            await m.answer(f"✅ {esc(p['name'])} (<code>{p['id']}</code>) получил лицензии на все города.",
                           reply_markup=kb([[("🪪 Ещё", "adm:lic")], [("⬅️ Админ-меню", "adm")]]))
    except ValueError as e:
        await m.answer("⚠️ Не понял формат. Нужно: ID и сумма, например <code>123456789 500000</code> "
                       "(или <code>me 1м</code>). Попробуйте ещё раз или вернитесь в меню.", reply_markup=back)
    except Exception:
        logging.exception("admin input failed")
        await m.answer("⚠️ Что-то пошло не так, подробности в логах.", reply_markup=back)
