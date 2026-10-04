import asyncpg

pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  id BIGINT PRIMARY KEY, name TEXT, home_city TEXT,
  money BIGINT NOT NULL DEFAULT 0, xp INT NOT NULL DEFAULT 0,
  total_earned BIGINT NOT NULL DEFAULT 0, trips_done INT NOT NULL DEFAULT 0,
  total_km BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS trucks (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, brand TEXT, model TEXT, year INT,
  mileage INT, capacity INT, consumption REAL, speed INT, city TEXT,
  busy BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE IF NOT EXISTS licenses (
  owner BIGINT, city TEXT, expires_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (owner, city));
CREATE TABLE IF NOT EXISTS orders (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, from_city TEXT, to_city TEXT, to_label TEXT,
  cargo TEXT, tons INT, km INT, price BIGINT, client TEXT,
  urgent BOOLEAN DEFAULT FALSE, limit_s INT, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS trips (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, truck_id INT NOT NULL,
  from_city TEXT, to_city TEXT, to_label TEXT, cargo TEXT, tons INT, km INT,
  price BIGINT, advance BIGINT, fuel_cost BIGINT, empty BOOLEAN DEFAULT FALSE,
  started_at TIMESTAMPTZ, load_end TIMESTAMPTZ, arrive_at TIMESTAMPTZ,
  finish_at TIMESTAMPTZ, deadline TIMESTAMPTZ, settled BOOLEAN DEFAULT FALSE);
CREATE INDEX IF NOT EXISTS trips_open ON trips (settled, finish_at);
"""


async def init(url):
    global pool
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5, statement_cache_size=0)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
