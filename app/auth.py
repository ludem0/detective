"""Пароли (scrypt), сессии в SQLite, ограничение попыток входа."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from .db import Database

SESSION_TTL = 7 * 24 * 3600
LOGIN_WINDOW = 600
LOGIN_MAX_FAILS = 10  # на пару (IP, логин): чужие опечатки за общим NAT не блокируют остальных
ADMIN_LOGIN = "admin"
_WORDS = ("kolt", "fedora", "noir", "jazz", "rider", "bell", "stone", "clue",
          "alibi", "motive", "smoke", "badge", "lamp", "raven", "docks", "radio",
          "pier", "cigar", "trench", "gin", "rail", "owl", "saber", "cameo")
_DUMMY_HASH = None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_password() -> str:
    """Два слова и четыре цифры: ~5 млн вариантов при 10 попытках на логин за 10 минут."""
    return f"{secrets.choice(_WORDS)}-{secrets.choice(_WORDS)}-{secrets.randbelow(9000) + 1000}"


def burn_password_check(password: str) -> None:
    """Для несуществующего логина тратим то же время, что на проверку пароля."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy")
    check_password(password, _DUMMY_HASH)


class Sessions:
    def __init__(self, db: Database):
        self.db = db

    def create(self, team_id: int | None, is_admin: bool) -> str:
        token = secrets.token_urlsafe(32)
        self.db.run("DELETE FROM sessions WHERE created_at < ?", (time.time() - SESSION_TTL,))
        self.db.run(
            "INSERT INTO sessions (token, team_id, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (token, team_id, int(is_admin), time.time()),
        )
        return token

    def get(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one("SELECT * FROM sessions WHERE token = ?", (token,))
        if not row or row["created_at"] + SESSION_TTL < time.time():
            return None
        return {"team_id": row["team_id"], "is_admin": bool(row["is_admin"])}

    def drop(self, token: str | None) -> None:
        if token:
            self.db.run("DELETE FROM sessions WHERE token = ?", (token,))

    def drop_team(self, team_id: int) -> None:
        self.db.run("DELETE FROM sessions WHERE team_id = ?", (team_id,))

    def drop_admins(self) -> None:
        self.db.run("DELETE FROM sessions WHERE is_admin = 1")


class LoginLimiter:
    """Не больше LOGIN_MAX_FAILS неудачных входов в один логин с одного IP за LOGIN_WINDOW секунд.

    Успешный вход счётчик не сбрасывает: иначе можно перебирать чужой пароль,
    перемежая попытки входом в свою команду.
    """

    def __init__(self):
        self.fails: dict[tuple[str, str], deque] = defaultdict(deque)

    def _trim(self, key: tuple[str, str], now: float) -> deque:
        q = self.fails[key]
        while q and q[0] < now - LOGIN_WINDOW:
            q.popleft()
        return q

    def blocked(self, ip: str, login: str, now: float) -> bool:
        return len(self._trim((ip, login), now)) >= LOGIN_MAX_FAILS

    def fail(self, ip: str, login: str, now: float) -> None:
        self._trim((ip, login), now).append(now)
