"""Контракты: долгосрочные заказы на 7 рейсов от крупных клиентов.
Появляются редко, платят на 50–80% больше обычного заказа того же маршрута, открываются с ростом уровня."""
import random
from datetime import timedelta

import db
from data import CARGO, CLIENT_CARGO, SUBURBAN, country_of, dist, HOME
from pricing import contract_price
from progression import cargo_ok
from util import now

# (клиент, мин. уровень компании)
CONTRACT_CLIENTS = [
    ("Магнит", 2),
    ("Лента", 3),
    ("X5 Group", 4),
    ("Wildberries", 5),
    ("Ozon", 6),
]
CONTRACT_TRIPS = 7           # рейсов по контракту
CONTRACT_DAYS = 14           # срок действия контракта
APPEAR_CHANCE = 0.3          # шанс, что клиент предложит контракт в городе за сутки (редкость)
MAX_ACTIVE = 2               # одновременно у игрока может быть не больше стольких контрактов


def _route(rng, city, licensed):
    """Маршрут контракта: (куда, название, км) или None, если ехать некуда."""
    inter = [c for c in licensed if c != city and dist(city, c)]
    if inter and rng.random() > 0.3:
        to = rng.choice(inter)
        return to, to, dist(city, to)
    if city in SUBURBAN:
        label, km = rng.choice(SUBURBAN[city])
        return city, label, km
    if inter:
        to = rng.choice(inter)
        return to, to, dist(city, to)
    return None


def _cargo(rng, client, lvl):
    options = [c for c in CLIENT_CARGO[client] if cargo_ok(c, lvl)] or list(CARGO)
    return rng.choice(options)


async def available_contracts(uid, city, licensed, lvl):
    """Контракты, предложенные сегодня в городе. Набор стабилен в течение дня."""
    if country_of(city) != HOME and city not in licensed:
        return []          # зарубежный город без визы
    taken = await db.pool.fetch(
        "SELECT client FROM contracts WHERE owner=$1 AND trips_done<trips_total AND expires_at>now()", uid)
    taken_clients = {r["client"] for r in taken}
    day = now().date().isoformat()
    result = []
    for client, min_level in CONTRACT_CLIENTS:
        if lvl < min_level or client in taken_clients:
            continue
        rng = random.Random(f"{uid}|{city}|{day}|{client}")
        if rng.random() > APPEAR_CHANCE:
            continue
        route = _route(rng, city, licensed)
        if route is None:
            continue
        to, label, km = route
        cargo = _cargo(rng, client, lvl)
        tons = rng.randint(14, 24)
        price = contract_price(client, tons, km, cargo, lvl, country_of(city) != country_of(to))
        result.append({
            "client": client, "cargo": cargo, "from_city": city, "to_city": to, "to_label": label,
            "km": km, "tons": tons, "price_per_trip": price,
        })
    return result


async def take_contract(uid, city, licensed, client_name: str, lvl):
    """Берёт контракт. Возвращает текст ошибки или None."""
    active = await db.pool.fetchval(
        "SELECT count(*) FROM contracts WHERE owner=$1 AND trips_done<trips_total AND expires_at>now()", uid)
    if active >= MAX_ACTIVE:
        return f"Одновременно можно вести не больше {MAX_ACTIVE} контрактов."
    offers = await available_contracts(uid, city, licensed, lvl)
    c_data = next((c for c in offers if c["client"] == client_name), None)
    if not c_data:
        return "Контракт недоступен."
    expires = now() + timedelta(days=CONTRACT_DAYS)
    await db.pool.execute(
        """INSERT INTO contracts
           (owner, client, cargo, from_city, to_city, to_label, km, tons, price_per_trip,
            trips_total, trips_done, expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,0,$11)""",
        uid, c_data["client"], c_data["cargo"], c_data["from_city"], c_data["to_city"], c_data["to_label"],
        c_data["km"], c_data["tons"], c_data["price_per_trip"], CONTRACT_TRIPS, expires)
    return None


async def active_contracts(uid):
    return await db.pool.fetch(
        "SELECT * FROM contracts WHERE owner=$1 AND trips_done<trips_total AND expires_at>now() ORDER BY id", uid)
