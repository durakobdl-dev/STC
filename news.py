"""Новости региона: события, которые на время меняют цены заказов и скорость фур."""
import logging
import random
from datetime import timedelta

import db
from data import CARGO, CITIES
from util import dur, esc, now

NEWS_PERIOD_H = 20      # как часто появляется новая сводка, часов
MAX_PER_CITY = 2        # не больше стольких активных событий в одном городе
WARM_CITIES = {"Сочи", "Севастополь"}   # там не бывает снегопадов


def _pct(mult):
    return int(round(abs(mult - 1) * 100))


# kind -> (вес, функция, строящая событие для города)
def _boom(city):
    cargo = random.choice(["Металл", "Товары", "Автомобили"])
    m = round(random.uniform(1.25, 1.45), 2)
    return dict(cargo=cargo, price_mult=m, speed_mult=1.0, hours=random.randint(36, 60),
                title=f"🏗 В городе {city} стройка века! Доставка груза «{cargo}» туда и оттуда "
                      f"подорожала на {_pct(m)}%.")


def _snow(city):
    s = round(random.uniform(0.7, 0.8), 2)
    m = round(random.uniform(1.15, 1.3), 2)
    return dict(cargo=None, price_mult=m, speed_mult=s, hours=random.randint(24, 40),
                title=f"❄️ Снегопад на трассах вокруг {city}. Скорость фур падает на {_pct(s)}%, "
                      f"но заказчики доплачивают за срочность: +{_pct(m)}% к оплате.")


def _crash(city):
    cargo = random.choice(["Электроника", "Еда", "Продукты", "Товары"])
    m = round(random.uniform(0.7, 0.85), 2)
    return dict(cargo=cargo, price_mult=m, speed_mult=1.0, hours=random.randint(30, 50),
                title=f"📉 Обвал цен на «{cargo}» в городе {city}. "
                      f"Возить это сейчас невыгодно: оплата −{_pct(m)}%.")


def _sale(city):
    cargo = random.choice(list(CARGO))
    m = round(random.uniform(1.18, 1.3), 2)
    return dict(cargo=cargo, price_mult=m, speed_mult=1.0, hours=random.randint(24, 48),
                title=f"🛍 В городе {city} ажиотажный спрос на «{cargo}»: оплата +{_pct(m)}%.")


def _roadwork(city):
    s = round(random.uniform(0.82, 0.9), 2)
    return dict(cargo=None, price_mult=1.0, speed_mult=s, hours=random.randint(24, 48),
                title=f"🚧 Ремонт трассы у города {city}: скорость фур на маршрутах через него −{_pct(s)}%.")


KINDS = [(3, _boom), (3, _snow), (3, _crash), (3, _sale), (2, _roadwork)]


def build_event(city):
    """Случайное событие для города (снег не бывает в тёплых городах)."""
    pool = [(w, f) for w, f in KINDS if not (f is _snow and city in WARM_CITIES)]
    f = random.choices([f for _, f in pool], weights=[w for w, _ in pool])[0]
    return f(city)


async def generate(count=None):
    """Создаёт 2-4 новых события в разных городах и чистит просроченные."""
    await db.pool.execute("DELETE FROM news_events WHERE expires_at <= now()")
    count = count or random.randint(2, 4)
    created = []
    for city in random.sample(CITIES, min(count, len(CITIES))):
        busy = await db.pool.fetchval(
            "SELECT count(*) FROM news_events WHERE city=$1 AND expires_at > now()", city)
        if busy >= MAX_PER_CITY:
            continue
        ev = build_event(city)
        await db.pool.execute(
            """INSERT INTO news_events (city, cargo, price_mult, speed_mult, title, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            city, ev["cargo"], ev["price_mult"], ev["speed_mult"], ev["title"],
            now() + timedelta(hours=ev["hours"]))
        created.append(city)
    return created


async def process():
    """Вызывается из watcher: раз в NEWS_PERIOD_H часов выпускает свежую сводку."""
    last = await db.pool.fetchval("SELECT max(created_at) FROM news_events")
    if last is None or now() - last >= timedelta(hours=NEWS_PERIOD_H):
        created = await generate()
        logging.info("news: generated events for %s", created)


async def active():
    return await db.pool.fetch(
        "SELECT * FROM news_events WHERE expires_at > now() ORDER BY expires_at")


# ---------- множители ----------
def _hit(ev, from_city, to_city):
    return ev["city"] in (from_city, to_city)


def price_mult(events, from_city, to_city, cargo):
    m = 1.0
    for ev in events:
        if _hit(ev, from_city, to_city) and (ev["cargo"] is None or ev["cargo"] == cargo):
            m *= ev["price_mult"]
    return min(1.8, max(0.5, m))


def speed_mult(events, from_city, to_city):
    m = 1.0
    for ev in events:
        if _hit(ev, from_city, to_city):
            m *= ev["speed_mult"]
    return min(1.0, max(0.6, m))


async def route_speed(from_city, to_city):
    return speed_mult(await active(), from_city, to_city)


def render(events):
    if not events:
        return "📰 <b>Новости регионов</b>\n\nПока спокойно: особых событий нет. Свежая сводка выходит раз в сутки."
    lines = ["📰 <b>Новости регионов</b>\nСобытия меняют оплату заказов и скорость фур в этих городах.\n"]
    for ev in events:
        left = (ev["expires_at"] - now()).total_seconds()
        lines.append(f"• {esc(ev['title'])}\n  ⏳ ещё {dur(left)}")
    return "\n".join(lines)
