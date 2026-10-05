import html
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)


def esc(s):
    return html.escape(str(s))


def money(n):
    return f"{int(n):,}".replace(",", "\u202f") + " ₽"


def dur(s):
    s = int(max(0, s))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{max(m, 1)} мин"


def bar(p, n=10):
    f = int(max(0.0, min(1.0, p)) * n)
    return "▓" * f + "░" * (n - f)
