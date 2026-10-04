import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.environ.get("PORT", "10000"))

START_MONEY = 400_000        # стартовый капитал, ₽
LICENSE_DAYS = 14            # срок действия лицензии на город
ADVANCE_PCT = 30             # аванс от цены заказа, %
DRIVE_HOURS_PER_DAY = 9      # сколько часов водитель едет в сутки
REST_HOURS_PER_DAY = 5       # отдых после каждых 9 часов в пути
