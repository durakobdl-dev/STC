import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.environ.get("PORT", "10000"))

START_MONEY = 400_000        # стартовый капитал, ₽
LICENSE_DAYS = 14            # срок действия лицензии на город
ADVANCE_PCT = 30             # аванс от цены заказа, %
DRIVE_HOURS_PER_DAY = 9      # сколько часов водитель едет в сутки
REST_HOURS_PER_DAY = 5       # отдых после каждых 9 часов в пути

# --- этап 2: поломки, ТО, страховка ---
DEFAULT_DRIVER_RATING = 3    # пока нет водителей (этап 3), считаем рейтинг 3 из 5
TO_COST = 35_000             # цена ТО, ₽
TO_HOURS = 6                 # сколько часов фура на ТО
TOW_DEFAULT_H = 3            # эвакуатор приезжает сам через столько часов
TOW_MIN_H = 1                # при вызове кнопкой - от 1 до 3 часов
INSURANCE_PRICE = 20_000     # страховка на фуру, ₽
INSURANCE_DAYS = 30
INSURANCE_PCT = 60           # страховка покрывает столько % ремонта
