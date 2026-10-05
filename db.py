import asyncpg

from data import BRANDS

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
ALTER TABLE trucks ADD COLUMN IF NOT EXISTS km_since_to INT NOT NULL DEFAULT 0;
ALTER TABLE trucks ADD COLUMN IF NOT EXISTS insured_until TIMESTAMPTZ;
ALTER TABLE trucks ADD COLUMN IF NOT EXISTS maint_until TIMESTAMPTZ;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS inc_kind TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS inc_sev INT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS inc_at TIMESTAMPTZ;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS inc_done BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE players ADD COLUMN IF NOT EXISTS last_payroll TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE trips ADD COLUMN IF NOT EXISTS driver_id INT;
CREATE TABLE IF NOT EXISTS drivers (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, name TEXT, rating INT NOT NULL, stazh INT NOT NULL DEFAULT 0,
  salary INT NOT NULL, city TEXT, busy BOOLEAN NOT NULL DEFAULT FALSE, owed BIGINT NOT NULL DEFAULT 0,
  grievance INT NOT NULL DEFAULT 0, total_km BIGINT NOT NULL DEFAULT 0,
  hired_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS candidates (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, day DATE NOT NULL, name TEXT,
  rating INT, stazh INT, salary INT);
ALTER TABLE players ADD COLUMN IF NOT EXISTS tax_period_start TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE players ADD COLUMN IF NOT EXISTS vat_credit BIGINT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS tax_loss BIGINT NOT NULL DEFAULT 0;
ALTER TABLE players ADD COLUMN IF NOT EXISTS arrested BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS ledger (
  id BIGSERIAL PRIMARY KEY, owner BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL, cash BIGINT NOT NULL DEFAULT 0, net BIGINT NOT NULL DEFAULT 0,
  vat BIGINT NOT NULL DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS ledger_owner ON ledger (owner, ts);
CREATE TABLE IF NOT EXISTS tax_bills (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
  vat BIGINT NOT NULL DEFAULT 0, profit BIGINT NOT NULL DEFAULT 0, payroll BIGINT NOT NULL DEFAULT 0,
  total BIGINT NOT NULL, due BIGINT NOT NULL, deadline TIMESTAMPTZ NOT NULL,
  penalized BOOLEAN NOT NULL DEFAULT FALSE, paid_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS dealer_stock (
  id SERIAL PRIMARY KEY, city TEXT, day DATE, brand TEXT, model TEXT, year INT, mileage INT,
  price BIGINT, is_new BOOLEAN);
CREATE TABLE IF NOT EXISTS incidents (
  id SERIAL PRIMARY KEY, owner BIGINT NOT NULL, truck_id INT NOT NULL, trip_id INT NOT NULL,
  kind TEXT, sev INT, cause TEXT, cost BIGINT, paid BIGINT,
  started_at TIMESTAMPTZ NOT NULL, tow_at TIMESTAMPTZ NOT NULL, repair_s INT NOT NULL,
  tow_called BOOLEAN NOT NULL DEFAULT FALSE);
"""


async def init(url):
    global pool
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5, statement_cache_size=0)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        # скорости фур обновились: подтягиваем уже купленные фуры до новых значений
        for b in BRANDS:
            await c.execute("UPDATE trucks SET speed=$3 WHERE brand=$1 AND model=$2 AND speed<$3",
                            b[0], b[1], b[5])
