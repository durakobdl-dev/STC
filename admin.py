"""Админ-панель"""
import db
from util import money, now


async def log_action(admin_id, action, target_id=None, details=None):
    """Логирует админ действие"""
    await db.pool.execute(
        "INSERT INTO admin_logs (admin_id, action, target_id, details) VALUES ($1,$2,$3,$4)",
        admin_id, action, target_id, details)


async def get_stats():
    """Получить общую статистику"""
    total_players = await db.pool.fetchval("SELECT count(*) FROM players")
    total_money = await db.pool.fetchval("SELECT coalesce(sum(money),0) FROM players")
    total_trips = await db.pool.fetchval("SELECT coalesce(sum(trips_done),0) FROM players")
    total_km = await db.pool.fetchval("SELECT coalesce(sum(total_km),0) FROM players")
    total_trucks = await db.pool.fetchval("SELECT count(*) FROM trucks")
    active_trips = await db.pool.fetchval("SELECT count(*) FROM trips WHERE settled=FALSE")
    return {
        "players": total_players,
        "total_money": total_money,
        "total_trips": total_trips,
        "total_km": total_km,
        "trucks": total_trucks,
        "active_trips": active_trips,
    }


async def get_logs(limit=10):
    """Получить логи админ действий"""
    return await db.pool.fetch(
        """SELECT id, admin_id, action, target_id, details, ts 
           FROM admin_logs ORDER BY ts DESC LIMIT $1""", limit)


async def find_player(query: str):
    """Найти игрока по ID или имени"""
    try:
        uid = int(query)
        return await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", uid)
    except ValueError:
        return await db.pool.fetchrow("SELECT * FROM players WHERE lower(name) LIKE lower($1)", f"%{query}%")


async def give_money(admin_id, player_id, amount):
    """Выдать деньги игроку"""
    err = await db.pool.fetchval("SELECT id FROM player_bans WHERE player_id=$1", player_id)
    if err:
        return "Игрок забанен"
    
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", player_id)
    if not p:
        return "Игрок не найден"
    
    await db.pool.execute("UPDATE players SET money = money + $2 WHERE id=$1", player_id, amount)
    await log_action(admin_id, "give_money", player_id, f"{money(amount)}")
    return None


async def give_truck(admin_id, player_id, brand, model):
    """Выдать фуру игроку"""
    err = await db.pool.fetchval("SELECT id FROM player_bans WHERE player_id=$1", player_id)
    if err:
        return "Игрок забанен"
    
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", player_id)
    if not p:
        return "Игрок не найден"
    
    # Находим спецификацию фуры
    from data import BRANDS, TO_INTERVAL
    b = next((x for x in BRANDS if x[0] == brand and x[1] == model), None)
    if not b:
        return "Неизвестная модель фуры"
    
    await db.pool.execute(
        """INSERT INTO trucks (owner, brand, model, year, mileage, capacity, consumption, speed, city)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        player_id, b[0], b[1], b[2], 0, b[3], b[4], b[5], p["home_city"])
    await log_action(admin_id, "give_truck", player_id, f"{brand} {model}")
    return None


async def ban_player(admin_id, player_id, reason):
    """Забанить игрока"""
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", player_id)
    if not p:
        return "Игрок не найден"
    
    existing = await db.pool.fetchval("SELECT id FROM player_bans WHERE player_id=$1", player_id)
    if existing:
        return "Игрок уже забанен"
    
    await db.pool.execute(
        "INSERT INTO player_bans (player_id, reason, banned_by) VALUES ($1,$2,$3)",
        player_id, reason, admin_id)
    await log_action(admin_id, "ban_player", player_id, reason)
    return None


async def unban_player(admin_id, player_id):
    """Разбанить игрока"""
    result = await db.pool.fetchval("DELETE FROM player_bans WHERE player_id=$1 RETURNING id", player_id)
    if not result:
        return "Игрок не забанен"
    
    await log_action(admin_id, "unban_player", player_id)
    return None


async def is_banned(player_id):
    """Проверить забанен ли игрок"""
    return await db.pool.fetchval("SELECT id FROM player_bans WHERE player_id=$1", player_id) is not None


async def clear_debt(admin_id, player_id):
    """Очистить налоговый долг"""
    p = await db.pool.fetchrow("SELECT * FROM players WHERE id=$1", player_id)
    if not p:
        return "Игрок не найден"
    
    result = await db.pool.fetchval(
        "SELECT coalesce(sum(due),0) FROM tax_bills WHERE owner=$1 AND paid_at IS NULL", player_id)
    
    if result > 0:
        await db.pool.execute(
            "UPDATE tax_bills SET paid_at=now() WHERE owner=$1 AND paid_at IS NULL", player_id)
        await db.pool.execute("UPDATE players SET arrested=FALSE WHERE id=$1", player_id)
        await log_action(admin_id, "clear_debt", player_id, f"{money(result)}")
        return f"Долг очищен: {money(result)}"
    return "Нет задолженности"


async def clear_loans(admin_id, player_id):
    """Очистить кредиты"""
    result = await db.pool.fetchval(
        "SELECT coalesce(sum(remaining),0) FROM loans WHERE owner=$1", player_id)

    if result > 0:
        await db.pool.execute("DELETE FROM loans WHERE owner=$1", player_id)
        await log_action(admin_id, "clear_loans", player_id, f"{money(result)}")
        return f"Кредиты очищены: {money(result)}"
    return "Нет активных кредитов"


async def get_all_players(limit=50):
    """Получить список всех игроков"""
    return await db.pool.fetch(
        """SELECT id, name, money, xp, trips_done, total_km, created_at
           FROM players ORDER BY created_at DESC LIMIT $1""", limit)


async def wipe_all(admin_id, confirm_code):
    """Вайп всей игры (сброс всех данных)"""
    # Требуется код подтверждения для безопасности
    if confirm_code != "WIPE_ALL_CONFIRM":
        return "Неверный код подтверждения"

    async with db.pool.acquire() as c:
        async with c.transaction():
            # Удаляем все данные в правильном порядке
            await c.execute("DELETE FROM admin_logs")
            await c.execute("DELETE FROM player_bans")
            await c.execute("DELETE FROM fines")
            await c.execute("DELETE FROM incidents")
            await c.execute("DELETE FROM contracts")
            await c.execute("DELETE FROM fuel_cards")
            await c.execute("DELETE FROM loans")
            await c.execute("DELETE FROM tax_bills")
            await c.execute("DELETE FROM ledger")
            await c.execute("DELETE FROM candidates")
            await c.execute("DELETE FROM drivers")
            await c.execute("DELETE FROM dealer_stock")
            await c.execute("DELETE FROM fines")
            await c.execute("DELETE FROM trips")
            await c.execute("DELETE FROM licenses")
            await c.execute("DELETE FROM orders")
            await c.execute("DELETE FROM bases")
            await c.execute("DELETE FROM visas")
            await c.execute("DELETE FROM news_events")
            await c.execute("DELETE FROM trucks")
            await c.execute("DELETE FROM players")

    await log_action(admin_id, "wipe_all", None, "Вайп всей игры")
    return "✅ ВАЙ ВЫПОЛНЕН - ВСЕ ДАННЫЕ УДАЛЕНЫ"


async def find_player_by_name(name: str):
    """Найти игрока по юзернейму (частичное совпадение)"""
    return await db.pool.fetch(
        "SELECT id, name, money, xp, trips_done FROM players WHERE lower(name) LIKE lower($1) LIMIT 10",
        f"%{name}%")
