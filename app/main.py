"""HTTP-слой: страницы, API команд и ведущего, выдача файлов дела."""

import hmac
import json
import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (ADMIN_LOGIN, LoginLimiter, Sessions, burn_password_check, check_password,
                   generate_password, hash_password)
from .casefile import AddressError, load_case
from .db import Database
from .game import Game, GameError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
COOKIE = "st_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ADMIN_SETTINGS = {
    "duration_min": int, "order_delay_sec": int, "fee_coef": int,
    "answers_closed": bool, "results_published": bool, "debrief_published": bool,
}


class LoginIn(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=128)


class VisitIn(BaseModel):
    district: str = Field(max_length=4)
    number: str = Field(max_length=80)


class AnswerIn(BaseModel):
    text: str = Field(max_length=6000)


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    login: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_-]*$")


class ScoreIn(BaseModel):
    points: float | None = None


class ClockIn(BaseModel):
    action: str = Field(pattern="^(start|stop|resume|add)$")
    minutes: int = Field(default=10, ge=-120, le=240)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ResetIn(BaseModel):
    confirm: str


def create_app(data_dir: Path, case_path: Path, admin_password: str) -> FastAPI:
    if len(admin_password) < 8:
        raise RuntimeError("ADMIN_PASSWORD должен быть не короче 8 символов")
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "game.db")
    case = load_case(case_path)
    files_dir = case_path.parent / "files"
    addressbook = json.loads((case_path.parent / "addressbook.json").read_text(encoding="utf-8"))
    materials = {m["id"]: m for m in case.raw["materials"]}
    game = Game(db, case)
    sessions = Sessions(db)
    sessions.drop_admins()  # пароль ведущего мог смениться вместе с перезапуском
    limiter = LoginLimiter()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.game = game

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Кросс-сайтовая форма не может прислать свой заголовок без CORS — этого достаточно.
        if request.method not in SAFE_METHODS and request.headers.get("x-requested-with") != "fetch":
            return JSONResponse({"detail": "Запрос отклонён."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def static_revalidate(request: Request, call_next):
        # Без заголовка браузер берёт скрипты из кэша и после выкатки смешивает старый JS с новой страницей.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(GameError)
    async def game_error(_: Request, exc: GameError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(AddressError)
    async def address_error(_: Request, exc: AddressError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ---------- сессии ----------

    def current(request: Request) -> dict:
        session = sessions.get(request.cookies.get(COOKIE))
        if not session:
            raise HTTPException(401, "Нужно войти.")
        return session

    def team_session(session: dict = Depends(current)) -> int:
        if session["is_admin"] or session["team_id"] is None:
            raise HTTPException(403, "Только для команд.")
        return session["team_id"]

    def viewed_team(view: int | None = None, session: dict = Depends(current)) -> int:
        """Команда смотрит свой экран; ведущий — экран команды `view` (только чтение)."""
        if not session["is_admin"]:
            return team_session(session)
        if view is None:
            raise HTTPException(403, "Только для команд.")
        return team_row(view)["id"]

    def admin_session(session: dict = Depends(current)) -> dict:
        if not session["is_admin"]:
            raise HTTPException(403, "Только для ведущего.")
        return session

    def require_started(session: dict) -> None:
        if not session["is_admin"] and game.clock(time.time())["status"] == "waiting":
            raise HTTPException(403, "Игра ещё не началась.")

    def client_ip(request: Request) -> str:
        return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")

    @app.post("/api/login")
    def login(body: LoginIn, request: Request, response: Response):
        now, ip = time.time(), client_ip(request)
        name = body.login.strip().lower()
        if limiter.blocked(ip, name, now):
            raise HTTPException(429, "Слишком много попыток для этого логина. Подождите 10 минут.")
        if name == ADMIN_LOGIN:
            ok = hmac.compare_digest(body.password.encode(), admin_password.encode())
            team_id, role = None, "admin"
        else:
            row = db.one("SELECT id, pass_hash FROM teams WHERE login = ?", (name,))
            if row:
                ok = check_password(body.password, row["pass_hash"])
            else:
                burn_password_check(body.password)
                ok = False
            team_id, role = (row["id"] if row else None), "team"
        if not ok:
            limiter.fail(ip, name, now)
            raise HTTPException(401, "Неверный логин или пароль.")
        token = sessions.create(team_id, role == "admin")
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                            max_age=7 * 24 * 3600, path="/")
        return {"role": role}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.drop(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        session = sessions.get(request.cookies.get(COOKIE))
        if not session:
            return {"role": None}
        if session["is_admin"]:
            return {"role": "admin"}
        team = db.one("SELECT name FROM teams WHERE id = ?", (session["team_id"],))
        return {"role": "team", "team": team["name"] if team else ""}

    # ---------- команда ----------

    @app.get("/api/case")
    def case_info(session: dict = Depends(current)):
        raw = case.raw
        info = {"id": raw["id"], "title": raw["title"]}
        if not session["is_admin"] and game.clock(time.time())["status"] == "waiting":
            return info
        info.update(
            case_time=raw["case_time"], brief=raw["brief"], contacts=raw["contacts"],
            districts=raw["districts"], questions=raw["questions"],
            materials=[{k: m[k] for k in ("id", "group", "title")}
                       | {"url": f"/files/m/{m['id']}",
                          "kind": "pdf" if m["file"].endswith(".pdf") else "image"}
                       for m in raw["materials"]],
        )
        return info

    @app.get("/api/addressbook")
    def get_addressbook(session: dict = Depends(current)):
        require_started(session)
        return addressbook

    @app.get("/api/state")
    def state(since: int = 0, team_id: int = Depends(viewed_team)):
        now = time.time()
        game.deliver_due(team_id, now)
        team = db.one("SELECT name FROM teams WHERE id = ?", (team_id,))
        data = {
            "now": now,
            "team": team["name"],
            "clock": game.clock(now),
            "trips": game.trips(team_id),
            "events": game.events(team_id, since) if game.clock(now)["status"] != "waiting" else [],
            "pending": game.pending(team_id),
            "hints": game.hints(now),
            "messages": game.messages(),
            "answers": game.answers(team_id),
            "submitted_at": game.submitted_at(team_id),
            "answers_locked": game.answers_locked(team_id),
            "results": None,
            "debrief": None,
        }
        if game.get("results_published"):
            data["results"] = [{k: r[k] for k in ("place", "name", "points", "trips", "fee")}
                               | {"me": r["id"] == team_id} for r in game.scoreboard()]
            data["scores"] = game.scores(team_id)
        if game.get("debrief_published"):
            data["debrief"] = {"text": case.raw["debrief"],
                               "rubric": [{"n": r["n"], "answer": r["answer"]}
                                          for r in case.raw["rubric"]]}
        return data

    @app.post("/api/visit")
    def visit(body: VisitIn, team_id: int = Depends(team_session)):
        result = game.visit(team_id, body.district, body.number, time.time())
        result["trips"] = game.trips(team_id)
        return result

    @app.put("/api/answers/{q}")
    def save_answer(q: int, body: AnswerIn, team_id: int = Depends(team_session)):
        require_started({"is_admin": False})
        game.save_answer(team_id, q, body.text, time.time())
        return {"ok": True}

    @app.post("/api/answers/submit")
    def submit(team_id: int = Depends(team_session)):
        require_started({"is_admin": False})
        game.submit(team_id, time.time())
        return {"ok": True}

    # ---------- файлы ----------

    def send(path: Path, download: bool, name: str, cache: str) -> FileResponse:
        return FileResponse(
            path, filename=name if download else None,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/files/m/{material_id}")
    def material_file(material_id: str, dl: int = 0, session: dict = Depends(current)):
        require_started(session)
        m = materials.get(material_id)
        if not m:
            raise HTTPException(404)
        path = files_dir / m["file"]
        return send(path, bool(dl), f"{m['title']}{path.suffix}", "private, max-age=3600")

    @app.get("/files/a/{n}/{name}")
    def appendix_file(n: int, name: str, dl: int = 0, session: dict = Depends(current)):
        a = case.raw["appendices"].get(n)
        if not a or name not in [a["download"], *a["previews"]]:
            raise HTTPException(404)
        if not session["is_admin"] and n not in game.unlocked(session["team_id"]):
            raise HTTPException(403, "Это приложение вам ещё не выдавали.")
        path = files_dir / "appendix" / name
        # no-cache: после обнуления команды браузер не должен показывать приложение из кэша
        return send(path, bool(dl), f"Приложение {n}. {a['title']}{path.suffix}", "private, no-cache")

    portraits = {e.portrait for e in case.by_id.values() if e.portrait}

    @app.get("/files/p/{name}")
    def portrait_file(name: str, session: dict = Depends(current)):
        if name not in portraits:
            raise HTTPException(404)
        if not session["is_admin"] and name not in game.portraits(session["team_id"]):
            raise HTTPException(403, "Этого человека вы ещё не встречали.")
        return send(files_dir / "portraits" / name, False, name, "private, no-cache")

    # ---------- ведущий ----------

    @app.get("/api/admin/state")
    def admin_state(_: dict = Depends(admin_session)):
        now = time.time()
        return {
            "now": now,
            "clock": game.clock(now),
            "settings": game.settings(),
            "board": game.scoreboard(),
            "hints": game.hints(now, everything=True),
            "messages": game.messages(),
            "questions": case.raw["questions"],
        }

    @app.get("/api/admin/case")
    def admin_case(_: dict = Depends(admin_session)):
        return {"rubric": case.raw["rubric"], "debrief": case.raw["debrief"],
                "questions": case.raw["questions"]}

    @app.post("/api/admin/teams")
    def create_team(body: TeamIn, _: dict = Depends(admin_session)):
        name = body.name.strip()
        login = (body.login or "").strip()
        if not login:
            n = db.one("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM teams")["n"]
            login = f"team{n}"
        if login == ADMIN_LOGIN:
            raise GameError("Логин admin занят ведущим.")
        if db.one("SELECT 1 FROM teams WHERE login = ? OR name = ?", (login, name)):
            raise GameError("Команда с таким названием или логином уже есть.")
        password = generate_password()
        db.run("INSERT INTO teams (name, login, pass_hash, created_at) VALUES (?, ?, ?, ?)",
               (name, login, hash_password(password), time.time()))
        return {"name": name, "login": login, "password": password}

    def team_row(team_id: int):
        row = db.one("SELECT id, name, login FROM teams WHERE id = ?", (team_id,))
        if not row:
            raise HTTPException(404, "Нет такой команды.")
        return row

    @app.post("/api/admin/teams/{team_id}/password")
    def reset_password(team_id: int, _: dict = Depends(admin_session)):
        row = team_row(team_id)
        password = generate_password()
        db.run("UPDATE teams SET pass_hash = ? WHERE id = ?", (hash_password(password), team_id))
        sessions.drop_team(team_id)
        return {"name": row["name"], "login": row["login"], "password": password}

    @app.delete("/api/admin/teams/{team_id}")
    def delete_team(team_id: int, _: dict = Depends(admin_session)):
        team_row(team_id)
        game.wipe_team(team_id)
        sessions.drop_team(team_id)
        db.run("DELETE FROM teams WHERE id = ?", (team_id,))
        return {"ok": True}

    @app.post("/api/admin/teams/{team_id}/wipe")
    def wipe_team(team_id: int, _: dict = Depends(admin_session)):
        team_row(team_id)
        game.wipe_team(team_id)
        return {"ok": True}

    @app.get("/api/admin/teams/{team_id}")
    def team_detail(team_id: int, _: dict = Depends(admin_session)):
        row = team_row(team_id)
        game.deliver_due(team_id, time.time())
        return {
            "id": row["id"], "name": row["name"], "login": row["login"],
            "trips": game.trips(team_id), "events": game.events(team_id),
            "pending": game.pending(team_id), "answers": game.answers(team_id),
            "scores": game.scores(team_id), "submitted_at": game.submitted_at(team_id),
        }

    @app.put("/api/admin/teams/{team_id}/scores/{q}")
    def set_score(team_id: int, q: int, body: ScoreIn, _: dict = Depends(admin_session)):
        team_row(team_id)
        game.set_score(team_id, q, body.points)
        return {"ok": True}

    @app.post("/api/admin/teams/{team_id}/unsubmit")
    def unsubmit(team_id: int, _: dict = Depends(admin_session)):
        team_row(team_id)
        game.unsubmit(team_id)
        return {"ok": True}

    @app.post("/api/admin/clock")
    def clock(body: ClockIn, _: dict = Depends(admin_session)):
        now = time.time()
        if body.action == "start":
            game.start(now)
        elif body.action == "stop":
            game.stop(now)
        elif body.action == "resume":
            game.resume(now)
        else:
            game.add_minutes(body.minutes)
        return game.clock(now)

    @app.post("/api/admin/settings")
    def update_settings(body: dict, _: dict = Depends(admin_session)):
        for key, value in body.items():
            kind = ADMIN_SETTINGS.get(key)
            if kind is None or not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                raise GameError(f"Нельзя изменить настройку {key}.")
            if kind is int and not 0 <= value <= 100000:
                raise GameError(f"Недопустимое значение {key}.")
            game.put(key, value)
        return game.settings()

    @app.post("/api/admin/hints/{hint_id}/release")
    def release_hint(hint_id: str, _: dict = Depends(admin_session)):
        game.release_hint(hint_id, time.time())
        return {"ok": True}

    @app.post("/api/admin/messages")
    def add_message(body: MessageIn, _: dict = Depends(admin_session)):
        game.add_message(body.text, time.time())
        return {"ok": True}

    @app.post("/api/admin/reset")
    def reset(body: ResetIn, _: dict = Depends(admin_session)):
        if body.confirm != "СБРОС":
            raise GameError("Для сброса введите слово СБРОС.")
        game.reset_all()
        return {"ok": True}

    # ---------- страницы ----------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def build_app() -> FastAPI:
    """Точка входа uvicorn: `uvicorn app.main:build_app --factory`."""
    return create_app(
        data_dir=Path(os.environ.get("DATA_DIR", ROOT / "data")),
        case_path=Path(os.environ.get("CASE_FILE", ROOT / "case" / "case24.yaml")),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
    )
