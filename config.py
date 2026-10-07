import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PORT = int(os.environ.get("PORT", "10000"))

START_MONEY = 400_000        # стартовый капитал, ₽
LICENSE_DAYS = 14            # срок действия лицензии на город
ADVANCE_PCT = 30             # аванс от цены заказа, %
# --- этап 2: поломки, ТО, страховка ---
DEFAULT_DRIVER_RATING = 3    # рейтинг, когда вы за рулём сами
TO_COST = 35_000             # цена ТО, ₽
TO_HOURS = 6                 # сколько часов фура на ТО
TOW_DEFAULT_H = 0.5          # эвакуатор приезжает сам через столько часов (30 минут)
TOW_MIN_H = 0.25             # при вызове кнопкой - от 15 мин до 30 минут
INSURANCE_PRICE = 20_000     # страховка на фуру, ₽
INSURANCE_DAYS = 30
INSURANCE_PCT = 60           # страховка покрывает столько % ремонта

# --- время в пути ---
# Первые REAL_KM км фура едет с реальной скоростью (110 км ≈ 1 ч 20 мин).
# Всё, что дальше, сжато по времени, чтобы дальние рейсы не тянулись сутками:
# на эталонной фуре (82 км/ч) остаток идёт со скоростью FAR_SPEED км/ч.
# Екатеринбург → Москва (1790 км) получается около 8,5 часа, Екатеринбург → Новосибирск около 8.
REAL_KM = 250
FAR_SPEED = 300
REST_PCT = 8                 # отдых водителя в дальних рейсах, % к времени сверх первых 3 часов
# Админы: юзернеймы через запятую, с @ или без (переменная ADMIN_USERNAMES), например: durakobdl,@ivan
ADMIN_USERNAMES = {x.lstrip("@").lower() for x in os.environ.get("ADMIN_USERNAMES", "").replace(" ", "").split(",") if x}
# необязательно: можно дополнительно указать числовые Telegram ID (переменная ADMIN_IDS)
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
             if x.lstrip("-").isdigit()}


def is_admin(uid, username):
    return uid in ADMIN_IDS or bool(username and username.lower() in ADMIN_USERNAMES)
