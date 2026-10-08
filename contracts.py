"""Контракты — долгосрочные заказы на 7 рейсов от крупных клиентов."""
import random
from datetime import timedelta

import db
from data import CARGO, CITIES, SUBURBAN, dist
from util import money, now

CONTRACT_CLIENTS = [
    ("Ozon",        1.30),   # бонус к цене
    ("Wildberries", 1.25),
    ("Магнит",      1.20),
    ("X5 Group",    1.22),
    ("Лента",       1.18),
]
CONTRACT_TRIPS = 7           # рейсов по контракту
CONTRACT_DAYS  = 14          # срок действия контракта


def _make_contract_order(city, licensed):
    """Генерирует маршрут для контракта."""
    inter = [c for c in licensed if c != city and dist(city, c)]
    if inter and random.random() > 0.3:
        to = random.choice(inter)
        km = dist(city, to)
        label = to
    else:
        # Пригородные маршруты - если города нет в SUBURBAN, делаем перегон
        if city in SUBURBAN:
            label, km = random.choice(SUBURBAN[city])
            to = city
        else:
            # Нет пригородных маршрутов для этого города, ищем межгородский
            if inter:
                to = random.choice(inter)
                km = dist(city, to)
                label = to
            else:
                # Нет доступных маршрутов вообще - возвращаем None
                return None
    cargo = random.choice(list(CARGO))
    tons = random.randint(12, 24)      # крупные клиенты грузят много
    return to, label, cargo, tons, km


async def available_contracts(uid, city, licensed):
    """Контракты, доступные для взятия (не взятые ещё)."""
    taken = await db.pool.fetch(
        "SELECT client FROM contracts WHERE owner=$1 AND trips_done<trips_total AND expires_at>now()", uid)
    taken_clients = {r["client"] for r in taken}
    result = []
    for client, bonus in CONTRACT_CLIENTS:
        if client in taken_clients:
            continue
        order_data = _make_contract_order(city, licensed)
        if order_data is None:
            continue
        to, label, cargo, tons, km = order_data
        base_rate = CARGO[cargo]
        price_per_trip = max(30_000, int(km * base_rate * (0.8 + tons / 40) * bonus / 100) * 100)
        result.append({
            "client": client,
            "cargo": cargo,
            "from_city": city,
            "to_city": to,
            "to_label": label,
            "km": km,
            "tons": tons,
            "price_per_trip": price_per_trip,
            "bonus": bonus,
        })
    return result


async def take_contract(uid, city, licensed, client_name: str):
    """Берёт контракт. Возвращает текст ошибки или None."""
    contracts = await available_contracts(uid, city, licensed)
    c_data = next((c for c in contracts if c["client"] == client_name), None)
    if not c_data:
        return "Контракт недоступен."
    expires = now() + timedelta(days=CONTRACT_DAYS)
    await db.pool.execute(
        """INSERT INTO contracts
           (owner, client, cargo, from_city, to_city, km, tons, price_per_trip,
            trips_total, trips_done, expires_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,0,$10)""",
        uid, c_data["client"], c_data["cargo"], c_data["from_city"], c_data["to_city"],
        c_data["km"], c_data["tons"], c_data["price_per_trip"],
        CONTRACT_TRIPS, expires)
    return None


async def active_contracts(uid):
    return await db.pool.fetch(
        "SELECT * FROM contracts WHERE owner=$1 AND trips_done<trips_total AND expires_at>now() ORDER BY id", uid)


async def as_order(contract, uid):
    """Превращает контракт в структуру, совместимую с обычным заказом."""
    return {
        "id": None,
        "owner": uid,
        "from_city": contract["from_city"],
        "to_city": contract["to_city"],
        "to_label": contract["to_city"] if contract["to_city"] != contract["from_city"] else contract["from_city"],
        "cargo": contract["cargo"],
        "tons": contract["tons"],
        "km": contract["km"],
        "price": contract["price_per_trip"],
        "client": contract["client"],
        "urgent": False,
        "limit_s": 10**9,
        "contract_id": contract["id"],
    }


async def complete_trip(contract_id: int):
    """Засчитывает выполненный рейс по контракту."""
    await db.pool.execute(
        "UPDATE contracts SET trips_done=trips_done+1 WHERE id=$1", contract_id)
