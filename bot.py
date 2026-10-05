import asyncio
import logging
import os
import random
from datetime import timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

import db
import game
from config import BOT_TOKEN, DATABASE_URL, LICENSE_DAYS, PORT, START_MONEY
from data import BRANDS, CITIES, LICENSE_PRICE, START_TRUCKS, TO_INTERVAL, TRUCK_PHOTOS, dist
import dealer
import drivers
import finance
import maintenance
from game import dur, esc, money

router = Router()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


def photo_for(brand, model):
    """Путь к фото фуры или None, если файла нет (тогда бот просто покажет текст)."""
    name = TRUCK_PHOTOS.get((brand, model))
    path = os.path.join(IMG_DIR, name) if name else None
    return path if path and os.path.exists(path) else None


async def show(call: CallbackQuery, text, markup, photo=None):
    msg = call.message
    has_photo = bool(msg.photo)
    try:
        if photo and has_photo:
            await msg.edit_media(InputMediaPhoto(media=FSInputFile(photo), caption=text), reply_markup=markup)
        elif photo:
            await msg.delete()
            await msg.answer_photo(FSInputFile(photo), caption=text, reply_markup=markup)
        elif has_photo:
            await msg.delete()
            await msg.answer(text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    try:
        await call.answer()
    except Exception:
        pass


async def get_player(uid):
    return await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", uid)


# ---------- старт и обучение ----------
TUTORIAL = [
    "🚛 <b>Симулятор Транспортной компании</b>\n\nВы строите транспортный бизнес в России: от одной "
    "б/у фуры до собственного автопарка. Всё идёт в реальном времени: фура едет столько, сколько ехала бы в жизни.",
    "📦 <b>Заказы</b>\n\nЗаказы берутся только из города, где сейчас стоит фура. Для рейсов между городами нужна "
    "лицензия на город назначения (действует 14 дней). Пригородные рейсы на 2–4 часа лицензии не требуют: с них удобно начинать.",
    "⛽ <b>Деньги и сроки</b>\n\nПеред рейсом вы получаете аванс 30%, из него и ваших денег оплачивается топливо. "
    "У каждого заказа есть срок: за опоздание оплата уменьшается. Водитель отдыхает до 5 часов после каждых 9 часов в пути.",
    "🏙 <b>Выберите стартовый город</b>\n\nОттуда начнётся ваша компания.",
]


@router.message(CommandStart())
async def cmd_start(m: Message):
    if await get_player(m.from_user.id):
        text, markup = await menu_view(m.from_user.id)
        await m.answer(text, reply_markup=markup)
        return
    await m.answer(TUTORIAL[0], reply_markup=kb([[("Далее ▶️", "tut:1")]]))


@router.callback_query(F.data.startswith("tut:"))
async def tutorial(call: CallbackQuery):
    n = int(call.data.split(":")[1])
    if n < 3:
        await show(call, TUTORIAL[n], kb([[("Далее ▶️", f"tut:{n + 1}")]]))
    else:
        rows = [[(c, f"city:{i}")] for i, c in enumerate(CITIES)]
        await show(call, TUTORIAL[3], kb(rows))


@router.callback_query(F.data.startswith("city:"))
async def pick_city(call: CallbackQuery):
    i = int(call.data.split(":")[1])
    rows = [[(f"{b[0]} {b[1]}", f"brand:{i}:{j}")] for j, b in enumerate(BRANDS[:START_TRUCKS])]
    await show(call, f"Город: <b>{CITIES[i]}</b>\n\nВыберите свою первую б/у фуру (дальше увидите фото и характеристики):", kb(rows))


@router.callback_query(F.data.startswith("brand:"))
async def pick_brand(call: CallbackQuery):
    if await get_player(call.from_user.id):
        await call.answer("Компания уже создана")
        return
    _, i, j = call.data.split(":")
    city, b = CITIES[int(i)], BRANDS[int(j)]
    mileage = random.randint(700, 1100) * 1000
    text = (f"🚚 <b>{b[0]} {b[1]}</b>\n\n"
            f"Год выпуска: {b[2]}\nПробег: {mileage:,} км".replace(",", "\u202f") + "\n"
            f"Грузоподъёмность: {b[3]} т\nРасход топлива: ~{b[4]:.0f} л/100 км\nСредняя скорость: ~{b[5]} км/ч\n"
            f"Город: {city}\n\nЭто б/у фура с пробегом, со временем ей понадобится ТО и возможны поломки в пути.\n\n"
            f"Взять эту фуру и начать с ней?")
    rows = [[("✅ Взять эту фуру", f"confirm:{i}:{j}")], [("⬅️ К выбору марки", f"city:{i}")]]
    await show(call, text, kb(rows), photo=photo_for(b[0], b[1]))


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_brand(call: CallbackQuery):
    if await get_player(call.from_user.id):
        await call.answer("Компания уже создана")
        return
    _, i, j = call.data.split(":")
    city, b = CITIES[int(i)], BRANDS[int(j)]
    uid = call.from_user.id
    name = call.from_user.first_name or "Компания"
    await db.pool.execute(
        "INSERT INTO players (id, name, home_city, money) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
        uid, name, city, START_MONEY)
    await db.pool.execute(
        """INSERT INTO trucks (owner, brand, model, year, mileage, capacity, consumption, speed, city, km_since_to)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        uid, b[0], b[1], b[2], random.randint(700, 1100) * 1000, b[3], b[4], b[5], city,
        random.randint(0, int(TO_INTERVAL.get((b[0], b[1]), 60000) * 0.7)))
    text, markup = await menu_view(uid)
    await show(call, "✅ Компания создана! Загляните в «Заказы» и отправьте первый рейс.\n\n" + text, markup)


# ---------- главное меню ----------
async def menu_view(uid):
    p = await get_player(uid)
    n = await db.pool.fetchval("SELECT count(*) FROM trucks WHERE owner=$1", uid)
    busy = await db.pool.fetchval("SELECT count(*) FROM trucks WHERE owner=$1 AND busy", uid)
    text = (f"🚛 <b>Симулятор Транспортной компании</b>\n\n🏢 {esc(p['name'])}\n"
            f"⭐ Уровень {game.level(p['xp'])} · опыт {p['xp']}\n💰 {money(p['money'])}\n"
            f"🚚 Фур: {n} (в рейсе: {busy})")
    markup = kb([[("🚚 Гараж", "garage"), ("🪪 Лицензии", "lic")],
                 [("👥 Водители", "drivers"), ("🏦 Банк", "bank")],
                 [("🏪 Автосалон", "dealer"), ("🏆 Рейтинг", "top:money")]])
    return text, markup


@router.callback_query(F.data == "menu")
async def menu(call: CallbackQuery):
    if not await get_player(call.from_user.id):
        await call.answer("Нажмите /start")
        return
    text, markup = await menu_view(call.from_user.id)
    await show(call, text, markup)


# ---------- гараж ----------
@router.callback_query(F.data == "garage")
async def garage(call: CallbackQuery):
    trucks = await db.pool.fetch("SELECT * FROM trucks WHERE owner=$1 ORDER BY id", call.from_user.id)
    rows = [[(f"{'🔴' if t['busy'] else ('🔧' if maintenance.in_service(t) else '🟢')} {t['brand']} {t['model']} · {t['city']}", f"truck:{t['id']}")]
            for t in trucks]
    rows.append([("⬅️ Меню", "menu")])
    await show(call, "🚚 <b>Ваш гараж</b>\n🟢 свободна · 🔴 в рейсе · 🔧 на ТО", kb(rows))


@router.callback_query(F.data.startswith("truck:"))
async def truck_view(call: CallbackQuery):
    await render_truck(call, int(call.data.split(":")[1]))


async def render_truck(call: CallbackQuery, tid: int):
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t:
        await call.answer("Фура не найдена")
        return
    text = (f"🚚 <b>{t['brand']} {t['model']}</b> ({t['year']})\n"
            f"Пробег: {t['mileage']:,} км".replace(",", "\u202f") + "\n"
            f"Грузоподъёмность: {t['capacity']} т · расход {t['consumption']:.0f} л/100 км · "
            f"скорость ~{t['speed']} км/ч\n📍 {t['city']}\n\n")
    text += maintenance.to_line(t) + "\n" + maintenance.insurance_line(t) + "\n\n"
    if t["busy"]:
        trip = await game.active_trip(tid)
        inc = await maintenance.open_incident(trip["id"]) if trip else None
        if trip and trip["driver_id"]:
            d = await db.pool.fetchrow("SELECT name, rating FROM drivers WHERE id=$1", trip["driver_id"])
            if d:
                text += f"👤 Водитель: {esc(d['name'])} {drivers.stars(d['rating'])}\n"
        elif trip:
            text += "👤 За рулём: вы сами\n"
        text += game.trip_status(trip, inc) if trip else "В рейсе"
        rows = []
        if inc and not inc["tow_called"] and maintenance.tow_state(inc)[0] == "tow":
            rows.append([("🚨 Вызвать эвакуатор", f"tow:{inc['id']}")])
        rows += [[("🔄 Обновить", f"truck:{tid}")], [("⬅️ Гараж", "garage")]]
    elif maintenance.in_service(t):
        left = (t["maint_until"] - game.now()).total_seconds()
        text += f"🔧 Фура на ТО, ещё {dur(left)}"
        rows = [[("🔄 Обновить", f"truck:{tid}")], [("⬅️ Гараж", "garage")]]
    else:
        text += "🟢 Свободна"
        rows = [[("📦 Заказы", f"orders:{tid}")], [("🚚 Перегнать порожняком", f"move:{tid}")],
                [("🔧 ТО", f"to:{tid}"), ("🛡 Страховка", f"ins:{tid}")],
                [("💰 Продать фуру", f"sell:{tid}")],
                [("⬅️ Гараж", "garage")]]
    await show(call, text, kb(rows), photo=photo_for(t["brand"], t["model"]))


# ---------- водители ----------
@router.callback_query(F.data == "drivers")
async def drivers_list(call: CallbackQuery):
    uid = call.from_user.id
    ds = await db.pool.fetch("SELECT * FROM drivers WHERE owner=$1 ORDER BY id", uid)
    lines = ["👥 <b>Ваши водители</b>\n"]
    rows = []
    if not ds:
        lines.append("Пока никого. Сейчас вы водите сами, но только одну фуру за раз. "
                     "Наймите водителей, чтобы ездить на нескольких фурах сразу.")
    for d in ds:
        warn = drivers.complaints(d)
        rows.append([(f"{d['name']} {drivers.stars(d['rating'])} · {d['city']}" +
                      (" 🔴" if d["busy"] else " 🟢") + (" ⚠️" if warn else ""), f"dr:{d['id']}")])
    lines.append("\nЗарплата начисляется за каждый км и выплачивается раз в сутки.")
    rows.append([("➕ Нанять водителя", "cands")])
    rows.append([("⬅️ Меню", "menu")])
    await show(call, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("dr:"))
async def driver_card(call: CallbackQuery):
    await render_driver(call, int(call.data.split(":")[1]))


async def render_driver(call: CallbackQuery, did):
    d = await db.pool.fetchrow("SELECT * FROM drivers WHERE id=$1 AND owner=$2", did, call.from_user.id)
    if not d:
        await call.answer("Водитель не найден")
        return
    nxt = drivers.PROMOTE.get(d["rating"])
    text = (f"👤 <b>{esc(d['name'])}</b> {drivers.stars(d['rating'])}\n"
            f"Стаж: {d['stazh']} рейсов" + (f" (следующий рейтинг с {nxt})" if nxt else " (максимум)") + "\n"
            f"Пробег за вас: {d['total_km']:,} км".replace(",", "\u202f") + "\n"
            f"📍 {d['city']} · {'🔴 в рейсе' if d['busy'] else '🟢 свободен'}\n\n"
            f"💵 Ставка: {d['salary']} ₽/км (рынок ~{drivers.market(d['rating'], d['stazh']):.0f})\n"
            f"🧾 Начислено к выплате: {money(d['owed'])}")
    warn = drivers.complaints(d)
    if warn:
        text += "\n\n" + "\n".join(warn) + "\nНедовольные водители могут уволиться."
    rows = [[("💵 Повысить ставку +1 ₽/км", f"drup:{did}")]]
    if not d["busy"]:
        rows.append([("🚪 Уволить", f"drfire:{did}")])
    rows += [[("🔄 Обновить", f"dr:{did}")], [("⬅️ Водители", "drivers")]]
    await show(call, text, kb(rows))


@router.callback_query(F.data.startswith("drup:"))
async def driver_raise(call: CallbackQuery):
    did = int(call.data.split(":")[1])
    sal = await drivers.raise_salary(call.from_user.id, did)
    if sal is None:
        await call.answer("Не получилось повысить ставку", show_alert=True)
        return
    await render_driver(call, did)


@router.callback_query(F.data.startswith("drfire:"))
async def driver_fire_ask(call: CallbackQuery):
    did = int(call.data.split(":")[1])
    await show(call, "Уволить водителя? Накопленную зарплату вы выплатите сразу.",
               kb([[("✅ Да, уволить", f"drfire_go:{did}")], [("⬅️ Отмена", f"dr:{did}")]]))


@router.callback_query(F.data.startswith("drfire_go:"))
async def driver_fire(call: CallbackQuery):
    err = await drivers.fire(call.from_user.id, int(call.data.split(":")[1]))
    if err:
        await call.answer(err, show_alert=True)
        return
    await drivers_list(call)


@router.callback_query(F.data == "cands")
async def candidates(call: CallbackQuery):
    uid = call.from_user.id
    await drivers.ensure_candidates(uid)
    cs = await db.pool.fetch("SELECT * FROM candidates WHERE owner=$1 ORDER BY id", uid)
    lines = ["➕ <b>Кандидаты на сегодня</b>\nСписок обновляется раз в сутки.\n"]
    btns = []
    for i, c in enumerate(cs, 1):
        lines.append(f"{i}. {esc(c['name'])} {drivers.stars(c['rating'])} · стаж {c['stazh']} · "
                     f"{c['salary']} ₽/км · найм {money(drivers.hire_cost(c['rating'], c['stazh']))}")
        btns.append((str(i), f"cd:{c['id']}"))
    if not cs:
        lines.append("Все кандидаты на сегодня уже наняты. Загляните завтра.")
    rows = [btns] if btns else []
    rows.append([("⬅️ Водители", "drivers")])
    await show(call, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("cd:"))
async def candidate_card(call: CallbackQuery):
    uid = call.from_user.id
    cid = int(call.data.split(":")[1])
    c = await db.pool.fetchrow("SELECT * FROM candidates WHERE id=$1 AND owner=$2", cid, uid)
    if not c:
        await call.answer("Кандидат уже нанят")
        return
    cities = [r["city"] for r in await db.pool.fetch(
        "SELECT DISTINCT city FROM trucks WHERE owner=$1 ORDER BY city", uid)]
    cost = drivers.hire_cost(c["rating"], c["stazh"])
    text = (f"👤 <b>{esc(c['name'])}</b> {drivers.stars(c['rating'])}\n"
            f"Стаж: {c['stazh']} рейсов\n💵 Просит: {c['salary']} ₽/км\n"
            f"💰 Стоимость найма: {money(cost)}\n\n"
            f"Водитель приедет в город, где стоит ваша фура. Выберите город:")
    rows = [[(f"Нанять в городе {city}", f"hire:{cid}:{CITIES.index(city)}")] for city in cities]
    rows.append([("⬅️ К списку", "cands")])
    await show(call, text, kb(rows))


@router.callback_query(F.data.startswith("hire:"))
async def hire_driver(call: CallbackQuery):
    _, cid, ci = call.data.split(":")
    err = await drivers.hire(call.from_user.id, int(cid), CITIES[int(ci)])
    if err:
        await call.answer(err, show_alert=True)
        return
    await call.answer("Водитель нанят!")
    await drivers_list(call)


# ---------- ТО, страховка, эвакуатор ----------
@router.callback_query(F.data.startswith("tow:"))
async def tow(call: CallbackQuery):
    ok, msg = await maintenance.call_tow(call.from_user.id, int(call.data.split(":")[1]))
    await call.answer(msg, show_alert=True)


@router.callback_query(F.data.startswith("to:"))
async def to_card(call: CallbackQuery):
    from config import TO_COST, TO_HOURS
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t or t["busy"]:
        await call.answer("Фура недоступна")
        return
    text = (f"🔧 <b>Техобслуживание</b>\n{t['brand']} {t['model']} · {t['city']}\n\n"
            f"{maintenance.to_line(t)}\n\n"
            f"Стоимость: {money(TO_COST)}\nВремя: {TO_HOURS} ч (фура не сможет ехать)\n"
            f"После ТО риск поломок снижается до следующего интервала.")
    await show(call, text, kb([[("✅ Провести ТО", f"to_go:{tid}")], [("⬅️ Назад", f"truck:{tid}")]]))


@router.callback_query(F.data.startswith("to_go:"))
async def to_go(call: CallbackQuery):
    tid = int(call.data.split(":")[1])
    err = await maintenance.do_service(call.from_user.id, tid)
    if err:
        await call.answer(err, show_alert=True)
        return
    await render_truck(call, tid)


@router.callback_query(F.data.startswith("ins:"))
async def ins_card(call: CallbackQuery):
    from config import INSURANCE_DAYS, INSURANCE_PCT, INSURANCE_PRICE
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t:
        await call.answer("Фура не найдена")
        return
    text = (f"🛡 <b>Страхование</b>\n{t['brand']} {t['model']}\n\n{maintenance.insurance_line(t)}\n\n"
            f"Цена: {money(INSURANCE_PRICE)} на {INSURANCE_DAYS} дней\n"
            f"Покрывает {INSURANCE_PCT}% стоимости ремонта при поломках и ДТП. "
            f"Если страховка уже есть, срок продлится.")
    await show(call, text, kb([[("✅ Купить", f"ins_go:{tid}")], [("⬅️ Назад", f"truck:{tid}")]]))


@router.callback_query(F.data.startswith("ins_go:"))
async def ins_go(call: CallbackQuery):
    tid = int(call.data.split(":")[1])
    err = await maintenance.buy_insurance(call.from_user.id, tid)
    if err:
        await call.answer(err, show_alert=True)
        return
    await render_truck(call, tid)


# ---------- заказы ----------
@router.callback_query(F.data.startswith("orders:"))
async def orders_list(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, uid)
    if not t or t["busy"]:
        await call.answer("Фура недоступна")
        return
    lic = await game.licensed_cities(uid)
    await game.ensure_orders(uid, t["city"], lic)
    rows = await db.pool.fetch(
        "SELECT * FROM orders WHERE owner=$1 AND from_city=$2 AND expires_at>now() ORDER BY km", uid, t["city"])
    lines = [f"📦 <b>Заказы из города {t['city']}</b>\nГрузоподъёмность: {t['capacity']} т\n"]
    btns = []
    for i, o in enumerate(rows, 1):
        flag = "🔥" if o["urgent"] else ("🏘" if o["to_city"] == o["from_city"] else "🛣")
        big = " ❌" if o["tons"] > t["capacity"] else ""
        lines.append(f"{i}. {flag} {esc(o['to_label'])} · {o['cargo']} {o['tons']} т · {o['km']} км · "
                     f"<b>{money(o['price'])}</b>{big}")
        btns.append((str(i), f"order:{tid}:{o['id']}"))
    lines.append("\n🔥 срочный · 🏘 пригород · 🛣 между городами · ❌ слишком тяжёлый")
    if len([c for c in lic if c != t["city"]]) == 0:
        lines.append("💡 Купите лицензию на город, чтобы появились межгородские заказы.")
    markup = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    markup.append([("⬅️ К фуре", f"truck:{tid}")])
    await show(call, "\n".join(lines), kb(markup))


@router.callback_query(F.data.startswith("order:"))
async def order_view(call: CallbackQuery):
    _, tid, oid = call.data.split(":")
    uid = call.from_user.id
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", int(tid), uid)
    o = await db.pool.fetchrow("SELECT * FROM orders WHERE id=$1 AND owner=$2 AND expires_at>now()", int(oid), uid)
    if not t or not o:
        await call.answer("Заказ уже недоступен")
        return
    fuel = game.fuel_cost(o["km"], t["consumption"], o["from_city"], o["to_city"])
    trav = game.travel_seconds(o["km"], t["speed"])
    ld = game.load_seconds(o["tons"])
    text = (f"{'🔥 СРОЧНЫЙ · ' if o['urgent'] else ''}<b>{o['from_city']} → {esc(o['to_label'])}</b>\n"
            f"Заказчик: {esc(o['client'])}\nГруз: {o['cargo']}, {o['tons']} т · {o['km']} км\n\n"
            f"💰 Оплата: <b>{money(o['price'])}</b>\n💵 Аванс {game.ADVANCE_PCT}%: {money(o['price'] * game.ADVANCE_PCT // 100)}\n"
            f"⛽ Топливо ~{money(fuel)}\n📈 Прибыль ~{money(o['price'] - fuel)}\n\n"
            f"📦 Погрузка и разгрузка: {dur(ld)} + {dur(ld)}\n🛣 В пути: {dur(trav)}\n"
            f"⏳ Срок доставки: {dur(o['limit_s'])}")
    await show(call, text, kb([[("✅ Взять заказ", f"take:{tid}:{oid}")], [("⬅️ К заказам", f"orders:{tid}")]]))


@router.callback_query(F.data.startswith("take:"))
async def take_order(call: CallbackQuery):
    _, tid, oid = call.data.split(":")
    await driver_prompt(call, int(tid), f"o{oid}")


async def driver_prompt(call: CallbackQuery, tid, payload):
    """Если в городе фуры есть свободные водители, спрашиваем, кто поедет."""
    uid = call.from_user.id
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, uid)
    if not t:
        await call.answer("Фура не найдена")
        return
    free = await drivers.free_in_city(uid, t["city"])
    if not free:
        await run_trip(call, tid, payload, 0)
        return
    rows = [[(f"👤 {d['name']} {drivers.stars(d['rating'])} · {d['salary']} ₽/км", f"go:{tid}:{payload}:{d['id']}")]
            for d in free]
    rows.append([("🧑‍✈️ Поеду сам (рейтинг 3, без зарплаты)", f"go:{tid}:{payload}:0")])
    rows.append([("⬅️ Назад", f"truck:{tid}")])
    await show(call, "👥 <b>Кто поедет в рейс?</b>\nВодитель получит ставку за каждый км. "
                     "Чем выше рейтинг, тем реже поломки и ДТП.\n"
                     "Сами вы можете вести только одну фуру одновременно.", kb(rows))


@router.callback_query(F.data.startswith("go:"))
async def go_trip(call: CallbackQuery):
    _, tid, payload, did = call.data.split(":")
    await run_trip(call, int(tid), payload, int(did))


async def run_trip(call: CallbackQuery, tid, payload, did):
    uid = call.from_user.id
    if payload[0] == "o":
        err = await game.start_trip(uid, tid, order_id=int(payload[1:]), driver_id=did or None)
    else:
        err = await game.start_trip(uid, tid, dest=CITIES[int(payload[1:])], driver_id=did or None)
    if err:
        await call.answer(err, show_alert=True)
        return
    t = await game.active_trip(tid)
    await show(call, "🚀 <b>Рейс начался!</b>\n\n" + game.trip_status(t) +
               "\n\nКогда фура приедет, я напишу.", kb([[("🔄 Статус", f"truck:{tid}")], [("🏠 Меню", "menu")]]))


# ---------- порожний перегон ----------
@router.callback_query(F.data.startswith("move:"))
async def move_menu(call: CallbackQuery):
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t or t["busy"]:
        await call.answer("Фура недоступна")
        return
    rows = []
    for i, c in enumerate(CITIES):
        if c != t["city"]:
            km = dist(t["city"], c)
            rows.append([(f"{c} · {km} км · ~{dur(game.travel_seconds(km, t['speed']))}", f"moveto:{tid}:{i}")])
    rows.append([("⬅️ Назад", f"truck:{tid}")])
    await show(call, "🚚 <b>Порожний перегон</b>\nВ другой город без груза: за ваш счёт (топливо), без дохода.", kb(rows))


@router.callback_query(F.data.startswith("moveto:"))
async def move_go(call: CallbackQuery):
    _, tid, i = call.data.split(":")
    await driver_prompt(call, int(tid), f"m{i}")


# ---------- лицензии ----------
@router.callback_query(F.data == "lic")
async def licenses(call: CallbackQuery):
    uid = call.from_user.id
    rows = await db.pool.fetch("SELECT city, expires_at FROM licenses WHERE owner=$1", uid)
    exp = {r["city"]: r["expires_at"] for r in rows}
    n = game.now()
    lines = [f"🪪 <b>Лицензии на города</b>\nДействуют {LICENSE_DAYS} дней. Нужны для заказов в этот город.\n"]
    btns = []
    for i, c in enumerate(CITIES):
        e = exp.get(c)
        status = f"✅ ещё {dur((e - n).total_seconds())}" if e and e > n else "❌ нет"
        lines.append(f"{c}: {status} · {money(LICENSE_PRICE[c])}")
        btns.append([(f"{'Продлить' if e and e > n else 'Купить'}: {c}", f"buylic:{i}")])
    btns.append([("⬅️ Меню", "menu")])
    await show(call, "\n".join(lines), kb(btns))


@router.callback_query(F.data.startswith("buylic:"))
async def buy_license(call: CallbackQuery):
    uid = call.from_user.id
    city = CITIES[int(call.data.split(":")[1])]
    price = LICENSE_PRICE[city]
    async with db.pool.acquire() as c:
        async with c.transaction():
            p = await c.fetchrow("SELECT money FROM players WHERE id=$1 FOR UPDATE", uid)
            if p["money"] < price:
                await call.answer(f"Не хватает денег: нужно {money(price)}", show_alert=True)
                return
            cur = await c.fetchval("SELECT expires_at FROM licenses WHERE owner=$1 AND city=$2", uid, city)
            base = max(game.now(), cur) if cur else game.now()
            await c.execute(
                """INSERT INTO licenses (owner, city, expires_at) VALUES ($1,$2,$3)
                   ON CONFLICT (owner, city) DO UPDATE SET expires_at=$3""",
                uid, city, base + timedelta(days=LICENSE_DAYS))
            await c.execute("UPDATE players SET money = money - $2 WHERE id=$1", uid, price)
            await finance.expense(c, uid, "license", price, False, f"Лицензия: {city}")
            await c.execute("DELETE FROM orders WHERE owner=$1", uid)
    await call.answer(f"Лицензия: {city}")
    await licenses(call)


# ---------- банк и рейтинг ----------
@router.callback_query(F.data == "bank")
async def bank(call: CallbackQuery):
    uid = call.from_user.id
    p, est = await finance.estimate(uid)
    bills = await finance.open_bills(uid)
    n = game.now()
    end = p["tax_period_start"] + timedelta(days=finance.PERIOD_DAYS)
    lines = [f"🏦 <b>Банк</b>\n\n💰 Баланс: <b>{money(p['money'])}</b>"]
    if p["arrested"]:
        lines.append("🔒 <b>Счёт арестован:</b> с выручки автоматически списывается долг по налогам.")
    lines.append(f"\n📅 <b>Текущий налоговый период</b> (закрывается через {dur((end - n).total_seconds())})\n"
                 f"Выручка без НДС: {money(est['income'])}\nРасходы: {money(est['expenses'])}\n"
                 f"Ориентировочно к уплате: НДС {money(est['vat'])}, на прибыль {money(est['profit_tax'])}, "
                 f"НДФЛ и взносы {money(est['payroll'])}")
    if bills:
        total = sum(x["due"] for x in bills)
        lines.append(f"\n🧾 <b>К оплате: {money(total)}</b>")
        for x in bills:
            left = (x["deadline"] - n).total_seconds()
            when = f"срок через {dur(left)}" if left > 0 else "⛔ просрочено (+10% штраф)"
            lines.append(f"• {money(x['due'])} · {when}")
    else:
        lines.append("\n✅ Долгов по налогам нет.")
    rows = []
    if bills:
        rows.append([("💳 Оплатить налоги", "taxpay")])
    rows.append([("📜 История", "history"), ("📊 Отчёт", "report")])
    rows.append([("⬅️ Меню", "menu")])
    await show(call, "\n".join(lines), kb(rows))


@router.callback_query(F.data == "taxpay")
async def tax_pay(call: CallbackQuery):
    paid, left, err = await finance.pay_all(call.from_user.id)
    if err:
        await call.answer(err, show_alert=True)
    else:
        await call.answer(f"Оплачено: {money(paid)}" + (f", осталось {money(left)}" if left else ""), show_alert=True)
    await bank(call)


@router.callback_query(F.data == "history")
async def bank_history(call: CallbackQuery):
    rows = await finance.history(call.from_user.id)
    lines = ["📜 <b>Последние операции</b>\n"]
    for r in rows:
        lines.append(f"{finance.LABELS.get(r['kind'], r['kind'])}: <b>{finance.fmt_cash(r['cash'])}</b>"
                     + (f"\n   {esc(r['note'])}" if r["note"] else ""))
    if not rows:
        lines.append("Пока операций нет.")
    await show(call, "\n".join(lines), kb([[("⬅️ Банк", "bank")]]))


@router.callback_query(F.data == "report")
async def bank_report(call: CallbackQuery):
    uid = call.from_user.id
    p, est = await finance.estimate(uid)
    tot = {r["kind"]: r["net"] for r in await finance.totals(uid)}
    income = tot.get("income", 0)
    wages = tot.get("salary", 0)
    contrib = wages * finance.CONTRIB // 100
    exp_lines = []
    for k in ("fuel", "repair", "service", "insurance", "hire", "license"):
        if tot.get(k):
            exp_lines.append(f"{finance.LABELS[k]}: {money(abs(tot[k]))}")
    if wages:
        exp_lines.append(f"{finance.LABELS['salary']} + взносы: {money(wages + contrib)}")
    expenses = sum(abs(tot.get(k, 0)) for k in ("fuel", "repair", "service", "insurance", "hire", "license")) + wages + contrib
    taxes = abs(tot.get("tax", 0))
    text = (f"📊 <b>Отчёт о прибыли</b>\n\n<b>Текущий период (3 дня)</b>\n"
            f"Выручка: {money(est['income'])}\nРасходы: {money(est['expenses'])}\n"
            f"Прибыль до налога: <b>{money(est['profit'])}</b>\n\n"
            f"<b>За всё время</b>\nВыручка (без НДС): {money(income)}\n" + "\n".join(exp_lines) +
            f"\nИтого расходов: {money(expenses)}\nПрибыль до налогов: <b>{money(income - expenses)}</b>\n"
            f"Уплачено налогов: {money(taxes)}")
    await show(call, text, kb([[("⬅️ Банк", "bank")]]))


# ---------- автосалон ----------
@router.callback_query(F.data == "dealer")
async def dealer_cities(call: CallbackQuery):
    rows = [[(f"🏪 {c}", f"dl:{i}")] for i, c in enumerate(dealer.DEALER_CITIES)]
    rows.append([("⬅️ Меню", "menu")])
    await show(call, "🏪 <b>Автосалоны</b>\nДилеры есть только в четырёх крупных городах. "
                     "Купленная фура появится в городе салона. Ассортимент обновляется раз в сутки.\n\n"
                     "Продать свою фуру можно в любом городе: кнопка есть в карточке фуры.", kb(rows))


@router.callback_query(F.data.startswith("dl:"))
async def dealer_lots(call: CallbackQuery):
    ci = int(call.data.split(":")[1])
    city = dealer.DEALER_CITIES[ci]
    await dealer.ensure_stock(city)
    lots = await db.pool.fetch("SELECT * FROM dealer_stock WHERE city=$1 ORDER BY is_new DESC, price", city)
    lines = [f"🏪 <b>Автосалон: {city}</b>\n"]
    btns = []
    for i, l in enumerate(lots, 1):
        tag = "🆕" if l["is_new"] else "б/у"
        lines.append(f"{i}. {tag} {l['brand']} {l['model']} · {l['year']} · "
                     f"{l['mileage']:,} км · <b>{money(l['price'])}</b>".replace(",", "\u202f"))
        btns.append((str(i), f"lot:{l['id']}"))
    if not lots:
        lines.append("Всё раскуплено. Загляните завтра.")
    rows = [btns] if btns else []
    rows.append([("⬅️ Города", "dealer")])
    await show(call, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("lot:"))
async def dealer_lot(call: CallbackQuery):
    lot = await db.pool.fetchrow("SELECT * FROM dealer_stock WHERE id=$1", int(call.data.split(":")[1]))
    if not lot:
        await call.answer("Эту фуру уже купили")
        return
    b = dealer.spec(lot["brand"], lot["model"])
    ci = dealer.DEALER_CITIES.index(lot["city"])
    text = (f"🚚 <b>{lot['brand']} {lot['model']}</b> {'🆕' if lot['is_new'] else '(б/у)'}\n\n"
            f"Год: {lot['year']}\nПробег: {lot['mileage']:,} км".replace(",", "\u202f") + "\n"
            f"Грузоподъёмность: {b[3]} т\nРасход: ~{b[4]:.0f} л/100 км\nСкорость: ~{b[5]} км/ч\n"
            f"Интервал ТО: {drivers_to(b)} км\n\n"
            f"💰 Цена: <b>{money(lot['price'])}</b>" +
            ("\n🧾 НДС в цене нового авто принимается к вычету" if lot["is_new"] else "") +
            f"\n📍 Фура окажется в городе {lot['city']}")
    rows = [[("✅ Купить", f"buy:{lot['id']}")], [("⬅️ К списку", f"dl:{ci}")]]
    await show(call, text, kb(rows), photo=photo_for(lot["brand"], lot["model"]))


def drivers_to(b):
    return f"{TO_INTERVAL.get((b[0], b[1]), 60000):,}".replace(",", "\u202f")


@router.callback_query(F.data.startswith("buy:"))
async def dealer_buy(call: CallbackQuery):
    err = await dealer.buy(call.from_user.id, int(call.data.split(":")[1]))
    if err:
        await call.answer(err, show_alert=True)
        return
    await call.answer("Фура куплена!", show_alert=True)
    await garage(call)


@router.callback_query(F.data.startswith("sell:"))
async def sell_ask(call: CallbackQuery):
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t or t["busy"]:
        await call.answer("Фура недоступна")
        return
    text = (f"💰 <b>Продажа: {t['brand']} {t['model']}</b> ({t['year']})\n"
            f"Пробег: {t['mileage']:,} км".replace(",", "\u202f") + f"\n\nВам дадут: <b>{money(dealer.sell_price(t))}</b>"
            f" ({dealer.SELL_PCT}% от рыночной цены).\nВернуть фуру будет нельзя.")
    await show(call, text, kb([[("✅ Продать", f"sell_go:{tid}")], [("⬅️ Отмена", f"truck:{tid}")]]))


@router.callback_query(F.data.startswith("sell_go:"))
async def sell_go(call: CallbackQuery):
    err, price = await dealer.sell(call.from_user.id, int(call.data.split(":")[1]))
    if err:
        await call.answer(err, show_alert=True)
        return
    await call.answer(f"Продано за {money(price)}", show_alert=True)
    await garage(call)


TOPS = {"money": ("💰 По деньгам", "total_earned", money), "trips": ("🚛 По рейсам", "trips_done", str),
        "km": ("🛣 По пробегу", "total_km", lambda v: f"{v:,} км".replace(",", "\u202f"))}


@router.callback_query(F.data.startswith("top:"))
async def top(call: CallbackQuery):
    kind = call.data.split(":")[1]
    title, col, fmt = TOPS[kind]
    rows = await db.pool.fetch(f"SELECT name, {col} AS v FROM players ORDER BY {col} DESC LIMIT 10")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>Рейтинг: {title}</b>\n"]
    for i, r in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {esc(r['name'])} — {fmt(r['v'])}")
    btns = [[(v[0], f"top:{k}")] for k, v in TOPS.items()] + [[("⬅️ Меню", "menu")]]
    await show(call, "\n".join(lines), kb(btns))


# ---------- запуск ----------
async def health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init(DATABASE_URL)
    await health_server()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(game.watcher(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
