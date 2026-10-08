"""Игровая логика: часы, поездки, ордера, триггеры, бланк, гонорар."""

import json
import math
import sqlite3

from .casefile import ORDER, SEARCH, Case, Parsed
from .db import TEAM_TABLES, Database

EMPTY = "empty"
DEFAULTS = {
    "started_at": None,
    "stopped_at": None,
    "duration_min": 120,
    "order_delay_sec": 300,
    "fee_coef": 20,
    "answers_closed": False,
    "results_published": False,
    "debrief_published": False,
    "released_hints": {},
}
MAX_ANSWER_LEN = 5000


class GameError(Exception):
    """Ошибка, которую можно показать игроку как есть."""


class Game:
    def __init__(self, db: Database, case: Case):
        self.db = db
        self.case = case
        self.triggers_by_key = {t.entry.key: t for t in case.triggers}
        self.questions = {q["n"]: q for q in case.raw["questions"]}

    # ---------- настройки ----------

    def get(self, key: str):
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else DEFAULTS[key]

    def put(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(key)
        self.db.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def settings(self) -> dict:
        return {k: self.get(k) for k in DEFAULTS}

    # ---------- часы ----------

    def clock(self, now: float) -> dict:
        """Пауза: stopped_at замораживает часы; resume сдвигает started_at на длину паузы."""
        started, stopped = self.get("started_at"), self.get("stopped_at")
        if started is None:
            return {"status": "waiting", "started_at": None, "ends_at": None, "remaining": None}
        ends = started + self.get("duration_min") * 60
        at = now if stopped is None else stopped
        remaining = max(0, round(ends - at))
        if remaining == 0:
            status = "finished"
        else:
            status = "paused" if stopped is not None else "running"
        return {"status": status, "started_at": started, "ends_at": ends, "remaining": remaining}

    def start(self, now: float) -> None:
        if self.clock(now)["status"] in ("running", "paused"):
            raise GameError("Игра уже идёт. Используйте «Стоп» и «Продолжить» или сбросьте игру.")
        self.put("started_at", now)
        self.put("stopped_at", None)

    def stop(self, now: float) -> None:
        if self.clock(now)["status"] == "running":
            self.put("stopped_at", now)

    def resume(self, now: float) -> None:
        stopped = self.get("stopped_at")
        if stopped is None:
            return
        self.put("started_at", self.get("started_at") + (now - stopped))
        self.put("stopped_at", None)

    def add_minutes(self, minutes: int) -> None:
        self.put("duration_min", max(1, self.get("duration_min") + minutes))

    # ---------- поездки ----------

    def visit(self, team_id: int, district: str, number: str, now: float) -> dict:
        if self.clock(now)["status"] != "running":
            raise GameError("Время остановлено: поездки недоступны.")
        parsed = self.case.parse(district, number)
        with self.db.tx() as c:
            self._deliver_due(c, team_id, now)
            if parsed.command == ORDER:
                result = self._order(c, team_id, parsed, now)
            elif parsed.command == SEARCH:
                result = self._search(c, team_id, parsed)
            else:
                result = self._plain(c, team_id, parsed, now)
            if result.get("new"):
                self._check_triggers(c, team_id, now)
        return result

    def _plain(self, c, team_id: int, parsed: Parsed, now: float) -> dict:
        trigger = self.triggers_by_key.get(parsed.key)
        entry = trigger.entry if trigger else self.case.entries.get(parsed.key)
        if trigger and not self._has_event(c, team_id, entry.id):
            entry = None
        if entry and not set(entry.requires) <= self._visited(c, team_id):
            entry = None
        if entry:
            return self._record(c, team_id, entry.key, parsed.label, entry.id, "trip", now)
        return self._record(c, team_id, parsed.key, parsed.label, EMPTY, "trip", now)

    def _order(self, c, team_id: int, parsed: Parsed, now: float) -> dict:
        search = self.case.searches.get(parsed.base_key)
        if not search:
            return self._record(c, team_id, parsed.key, parsed.label, EMPTY, "trip", now)
        label = f"{parsed.base_label}/{ORDER}"
        result = self._record(c, team_id, parsed.key, label, f"order:{parsed.base_key}", "trip", now)
        if result["new"]:
            c.execute(
                "INSERT OR IGNORE INTO pending (team_id, addr_key, label, entry_id, source, due_at) "
                "VALUES (?, ?, ?, ?, 'search', ?)",
                (team_id, f"{parsed.base_key}/{SEARCH}", f"{parsed.base_label}/{SEARCH}",
                 search.id, now + self.get("order_delay_sec")),
            )
        return result

    def _search(self, c, team_id: int, parsed: Parsed) -> dict:
        search = self.case.searches.get(parsed.base_key)
        texts = self.case.texts
        if search:
            row = c.execute(
                "SELECT * FROM events WHERE team_id = ? AND entry_id = ?", (team_id, search.id)
            ).fetchone()
            if row:
                return {"new": False, "counted": False, "event": self.event_view(row)}
            if c.execute(
                "SELECT 1 FROM pending WHERE team_id = ? AND entry_id = ?", (team_id, search.id)
            ).fetchone():
                return {"new": False, "counted": False,
                        "message": texts["order_pending"].format(address=parsed.base_label)}
        return {"new": False, "counted": False, "message": texts["search_needs_order"]}

    def _record(self, c, team_id, key, label, entry_id, source, now, counted=True) -> dict:
        """Повторный запрос того же ответа бесплатен и возвращает старую запись."""
        if entry_id == EMPTY:
            row = c.execute(
                "SELECT * FROM events WHERE team_id = ? AND addr_key = ? AND entry_id = ?",
                (team_id, key, EMPTY),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM events WHERE team_id = ? AND entry_id = ?", (team_id, entry_id)
            ).fetchone()
        if row:
            return {"new": False, "counted": False, "event": self.event_view(row)}
        event_id = c.execute(
            "INSERT INTO events (team_id, addr_key, label, entry_id, source, counted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (team_id, key, label, entry_id, source, int(counted), now),
        ).lastrowid
        row = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return {"new": True, "counted": counted, "event": self.event_view(row)}

    @staticmethod
    def _has_event(c, team_id: int, entry_id: str) -> bool:
        return c.execute(
            "SELECT 1 FROM events WHERE team_id = ? AND entry_id = ?", (team_id, entry_id)
        ).fetchone() is not None

    @staticmethod
    def _visited(c, team_id: int) -> set[str]:
        rows = c.execute(
            "SELECT addr_key FROM events WHERE team_id = ? AND entry_id != ? "
            "AND entry_id NOT LIKE 'order:%'",
            (team_id, EMPTY),
        ).fetchall()
        return {r["addr_key"] for r in rows}

    def _check_triggers(self, c, team_id: int, now: float) -> None:
        visited = self._visited(c, team_id)
        for t in self.case.triggers:
            if not (set(t.after_all) <= visited and (not t.after_any or visited & set(t.after_any))):
                continue
            c.execute(
                "INSERT OR IGNORE INTO pending (team_id, addr_key, label, entry_id, source, due_at) "
                "SELECT ?, ?, ?, ?, 'trigger', ? WHERE NOT EXISTS "
                "(SELECT 1 FROM events WHERE team_id = ? AND entry_id = ?)",
                (team_id, t.entry.key, t.entry.label, t.entry.id, now + t.delay_sec,
                 team_id, t.entry.id),
            )

    def _deliver_due(self, c, team_id: int, now: float) -> int:
        rows = c.execute(
            "SELECT * FROM pending WHERE team_id = ? AND due_at <= ? ORDER BY due_at",
            (team_id, now),
        ).fetchall()
        for r in rows:
            c.execute(
                "INSERT OR IGNORE INTO events "
                "(team_id, addr_key, label, entry_id, source, counted, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (team_id, r["addr_key"], r["label"], r["entry_id"], r["source"], r["due_at"]),
            )
            c.execute("DELETE FROM pending WHERE id = ?", (r["id"],))
        if rows:
            self._check_triggers(c, team_id, now)
        return len(rows)

    def deliver_due(self, team_id: int, now: float) -> int:
        with self.db.tx() as c:
            return self._deliver_due(c, team_id, now)

    # ---------- журнал ----------

    def event_view(self, row: sqlite3.Row) -> dict:
        entry_id = row["entry_id"]
        texts = self.case.texts
        title, text, appendices = None, texts["empty"], ()
        if entry_id.startswith("order:"):
            title = "Запрос ордера"
            text = texts["order_requested"].format(address=row["label"].rsplit("/", 1)[0])
        elif entry_id != EMPTY and entry_id in self.case.by_id:
            entry = self.case.by_id[entry_id]
            title, text, appendices = entry.title, entry.text, entry.appendices
        return {
            "id": row["id"],
            "label": row["label"],
            "title": title,
            "text": text,
            "empty": entry_id == EMPTY,
            "source": row["source"],
            "counted": bool(row["counted"]),
            "at": row["created_at"],
            "appendices": [self.appendix_view(n) for n in appendices],
        }

    def appendix_view(self, n: int) -> dict:
        a = self.case.raw["appendices"][n]
        return {
            "n": n,
            "title": a["title"],
            "download": f"/files/a/{n}/{a['download']}",
            "previews": [f"/files/a/{n}/{p}" for p in a["previews"]],
        }

    def events(self, team_id: int, since_id: int = 0) -> list[dict]:
        rows = self.db.all(
            "SELECT * FROM events WHERE team_id = ? AND id > ? ORDER BY id", (team_id, since_id)
        )
        return [self.event_view(r) for r in rows]

    def pending(self, team_id: int) -> list[dict]:
        rows = self.db.all(
            "SELECT label, source FROM pending WHERE team_id = ? AND source = 'search'", (team_id,)
        )
        return [{"label": r["label"], "source": r["source"]} for r in rows]

    def trips(self, team_id: int) -> int:
        row = self.db.one("SELECT COALESCE(SUM(counted), 0) AS n FROM events WHERE team_id = ?",
                          (team_id,))
        return row["n"]

    def unlocked(self, team_id: int) -> set[int]:
        rows = self.db.all("SELECT DISTINCT entry_id FROM events WHERE team_id = ?", (team_id,))
        found = set()
        for r in rows:
            entry = self.case.by_id.get(r["entry_id"])
            if entry:
                found.update(entry.appendices)
        return found

    # ---------- сводки ----------

    def hints(self, now: float, everything: bool = False) -> list[dict]:
        started = self.get("started_at")
        released = self.get("released_hints")
        out = []
        for h in self.case.raw.get("hints", []):
            at = released.get(h["id"])
            if at is None and started is not None and h.get("auto_minute") is not None:
                at = started + h["auto_minute"] * 60
            visible = at is not None and at <= now
            if visible or everything:
                item = {"id": h["id"], "text": h["text"], "at": at if visible else None}
                if everything:
                    item.update(auto_minute=h.get("auto_minute"), note=h.get("note", ""),
                                visible=visible)
                out.append(item)
        return out

    def release_hint(self, hint_id: str, now: float) -> None:
        if hint_id not in {h["id"] for h in self.case.raw.get("hints", [])}:
            raise GameError("Нет такой подсказки.")
        released = self.get("released_hints")
        released[hint_id] = now
        self.put("released_hints", released)

    def add_message(self, text: str, now: float) -> None:
        text = text.strip()
        if not text or len(text) > 1000:
            raise GameError("Сообщение пустое или длиннее 1000 символов.")
        self.db.run("INSERT INTO messages (text, created_at) VALUES (?, ?)", (text, now))

    def messages(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM messages ORDER BY id")
        return [{"id": r["id"], "text": r["text"], "at": r["created_at"]} for r in rows]

    # ---------- бланк ответов ----------

    def answers(self, team_id: int) -> dict[int, str]:
        rows = self.db.all("SELECT q, text FROM answers WHERE team_id = ?", (team_id,))
        return {r["q"]: r["text"] for r in rows}

    def submitted_at(self, team_id: int) -> float | None:
        row = self.db.one("SELECT submitted_at FROM submissions WHERE team_id = ?", (team_id,))
        return row["submitted_at"] if row else None

    def answers_locked(self, team_id: int) -> bool:
        return self.get("answers_closed") or self.submitted_at(team_id) is not None

    def save_answer(self, team_id: int, q: int, text: str, now: float) -> None:
        if q not in self.questions:
            raise GameError("Нет такого вопроса.")
        if len(text) > MAX_ANSWER_LEN:
            raise GameError(f"Ответ длиннее {MAX_ANSWER_LEN} символов.")
        if self.answers_locked(team_id):
            raise GameError("Бланк уже отправлен или приём ответов закрыт.")
        self.db.run(
            "INSERT INTO answers (team_id, q, text, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (team_id, q) DO UPDATE SET text = excluded.text, "
            "updated_at = excluded.updated_at",
            (team_id, q, text, now),
        )

    def submit(self, team_id: int, now: float) -> None:
        if self.answers_locked(team_id):
            raise GameError("Бланк уже отправлен или приём ответов закрыт.")
        self.db.run("INSERT INTO submissions (team_id, submitted_at) VALUES (?, ?)", (team_id, now))

    def unsubmit(self, team_id: int) -> None:
        self.db.run("DELETE FROM submissions WHERE team_id = ?", (team_id,))

    # ---------- оценка ----------

    def set_score(self, team_id: int, q: int, points: float | None) -> None:
        if q not in self.questions:
            raise GameError("Нет такого вопроса.")
        if points is None:
            self.db.run("DELETE FROM scores WHERE team_id = ? AND q = ?", (team_id, q))
            return
        if not 0 <= points <= self.questions[q]["max"]:
            raise GameError(f"Баллы за вопрос {q}: от 0 до {self.questions[q]['max']}.")
        self.db.run(
            "INSERT INTO scores (team_id, q, points) VALUES (?, ?, ?) "
            "ON CONFLICT (team_id, q) DO UPDATE SET points = excluded.points",
            (team_id, q, points),
        )

    def scores(self, team_id: int) -> dict[int, float]:
        rows = self.db.all("SELECT q, points FROM scores WHERE team_id = ?", (team_id,))
        return {r["q"]: r["points"] for r in rows}

    def fee(self, points: float, trips: int) -> int:
        return round(self.get("fee_coef") * points * points / math.sqrt(max(trips, 1)))

    def scoreboard(self) -> list[dict]:
        rows = self.db.all("SELECT id, name, login FROM teams ORDER BY id")
        board = []
        for t in rows:
            points = sum(self.scores(t["id"]).values())
            trips = self.trips(t["id"])
            board.append({
                "id": t["id"], "name": t["name"], "login": t["login"],
                "points": points, "trips": trips, "fee": self.fee(points, trips),
                "submitted": self.submitted_at(t["id"]) is not None,
                "scored": len(self.scores(t["id"])),
            })
        board.sort(key=lambda r: (-r["fee"], -r["points"], r["trips"]))
        for place, row in enumerate(board, 1):
            row["place"] = place
        return board

    # ---------- сброс ----------

    def wipe_team(self, team_id: int) -> None:
        with self.db.tx() as c:
            for table in TEAM_TABLES:
                if table != "sessions":
                    c.execute(f"DELETE FROM {table} WHERE team_id = ?", (team_id,))

    def reset_all(self) -> None:
        with self.db.tx() as c:
            for table in TEAM_TABLES:
                if table != "sessions":
                    c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM messages")
            c.execute(
                "DELETE FROM settings WHERE key IN ('started_at', 'stopped_at', "
                "'released_hints', 'answers_closed', 'results_published', 'debrief_published')"
            )
