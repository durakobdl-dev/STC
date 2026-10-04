import asyncio
import logging
import random
from datetime import timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import game
from config import BOT_TOKEN, DATABASE_URL, LICENSE_DAYS, PORT, START_MONEY
from data import BRANDS, CITIES, LICENSE_PRICE, dist
from game import dur, esc, money

router = Router()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


async def show(call: CallbackQuery, text, markup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await call.answer()


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
    rows = [[(f"{b[0]} {b[1]} ({b[2]}), {b[3]} т", f"brand:{i}:{j}")] for j, b in enumerate(BRANDS)]
    await show(call, f"Город: <b>{CITIES[i]}</b>\n\nВыберите свою первую б/у фуру:", kb(rows))


@router.callback_query(F.data.startswith("brand:"))
async def pick_brand(call: CallbackQuery):
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
        """INSERT INTO trucks (owner, brand, model, year, mileage, capacity, consumption, speed, city)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        uid, b[0], b[1], b[2], random.randint(700, 1100) * 1000, b[3], b[4], b[5], city)
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
                 [("🏦 Банк", "bank"), ("🏆 Рейтинг", "top:money")]])
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
    rows = [[(f"{'🔴' if t['busy'] else '🟢'} {t['brand']} {t['model']} · {t['city']}", f"truck:{t['id']}")]
            for t in trucks]
    rows.append([("⬅️ Меню", "menu")])
    await show(call, "🚚 <b>Ваш гараж</b>\n🟢 свободна · 🔴 в рейсе", kb(rows))


@router.callback_query(F.data.startswith("truck:"))
async def truck_view(call: CallbackQuery):
    tid = int(call.data.split(":")[1])
    t = await db.pool.fetchrow("SELECT * FROM trucks WHERE id=$1 AND owner=$2", tid, call.from_user.id)
    if not t:
        await call.answer("Фура не найдена")
        return
    text = (f"🚚 <b>{t['brand']} {t['model']}</b> ({t['year']})\n"
            f"Пробег: {t['mileage']:,} км".replace(",", "\u202f") + "\n"
            f"Грузоподъёмность: {t['capacity']} т · расход {t['consumption']:.0f} л/100 км · "
            f"скорость ~{t['speed']} км/ч\n📍 {t['city']}\n\n")
    if t["busy"]:
        trip = await game.active_trip(tid)
        text += game.trip_status(trip) if trip else "В рейсе"
        rows = [[("🔄 Обновить", f"truck:{tid}")], [("⬅️ Гараж", "garage")]]
    else:
        text += "🟢 Свободна"
        rows = [[("📦 Заказы", f"orders:{tid}")], [("🚚 Перегнать порожняком", f"move:{tid}")],
                [("⬅️ Гараж", "garage")]]
    await show(call, text, kb(rows))


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
    err = await game.start_trip(call.from_user.id, int(tid), order_id=int(oid))
    if err:
        await call.answer(err, show_alert=True)
        return
    await call.answer("Рейс начался!")
    t = await game.active_trip(int(tid))
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
    err = await game.start_trip(call.from_user.id, int(tid), dest=CITIES[int(i)])
    if err:
        await call.answer(err, show_alert=True)
        return
    t = await game.active_trip(int(tid))
    await show(call, "🚚 <b>Фура отправлена</b>\n\n" + game.trip_status(t), kb([[("🏠 Меню", "menu")]]))


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
            await c.execute("DELETE FROM orders WHERE owner=$1", uid)
    await call.answer(f"Лицензия: {city}")
    await licenses(call)


# ---------- банк и рейтинг ----------
@router.callback_query(F.data == "bank")
async def bank(call: CallbackQuery):
    p = await get_player(call.from_user.id)
    text = (f"🏦 <b>Банк</b>\n\n💰 Баланс: {money(p['money'])}\n📈 Всего заработано: {money(p['total_earned'])}\n"
            f"🚛 Рейсов: {p['trips_done']} · {p['total_km']:,} км".replace(",", "\u202f") +
            "\n\nНалоги, отчёты и кредиты появятся в следующих версиях.")
    await show(call, text, kb([[("⬅️ Меню", "menu")]]))


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
