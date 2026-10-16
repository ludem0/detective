from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import LOGIN_MAX_FAILS
from app.main import create_app

CASE = Path(__file__).resolve().parent.parent / "case" / "case24.yaml"
H = {"X-Requested-With": "fetch"}
ADMIN_PW = "secret-admin-pw"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, CASE, ADMIN_PW)


def login(app, user, password):
    c = TestClient(app)
    r = c.post("/api/login", json={"login": user, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin(app):
    return login(app, "admin", ADMIN_PW)


@pytest.fixture
def team(app, admin):
    creds = admin.post("/api/admin/teams", json={"name": "Сыщики"}, headers=H).json()
    return login(app, creds["login"], creds["password"])


def start(admin):
    assert admin.post("/api/admin/clock", json={"action": "start"}, headers=H).status_code == 200


def test_short_admin_password_refused(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(tmp_path, CASE, "short")


def test_post_without_fetch_header_is_rejected(app):
    r = TestClient(app).post("/api/login", json={"login": "admin", "password": ADMIN_PW})
    assert r.status_code == 403


def test_wrong_password_and_rate_limit_per_login(app, admin):
    creds = admin.post("/api/admin/teams", json={"name": "Бета"}, headers=H).json()
    c = TestClient(app)
    codes = [c.post("/api/login", json={"login": "admin", "password": "nope"}, headers=H).status_code
             for _ in range(LOGIN_MAX_FAILS + 1)]
    assert codes[:-1] == [401] * LOGIN_MAX_FAILS and codes[-1] == 429
    # Другая команда за тем же IP не заблокирована.
    login(app, creds["login"], creds["password"])
    # Успешный вход не сбрасывает счётчик заблокированного логина.
    r = c.post("/api/login", json={"login": "admin", "password": ADMIN_PW}, headers=H)
    assert r.status_code == 429


def test_unknown_login_is_401(app):
    r = TestClient(app).post("/api/login", json={"login": "ghost", "password": "x"}, headers=H)
    assert r.status_code == 401


def test_answers_closed_before_start(team):
    assert team.put("/api/answers/1", json={"text": "рано"}, headers=H).status_code == 403
    assert team.post("/api/answers/submit", headers=H).status_code == 403


def test_generated_password_has_two_words(admin):
    creds = admin.post("/api/admin/teams", json={"name": "Гамма"}, headers=H).json()
    assert len(creds["password"].split("-")) == 3


def test_team_cannot_use_admin_api(team):
    assert team.get("/api/admin/state").status_code == 403
    assert team.post("/api/admin/clock", json={"action": "start"}, headers=H).status_code == 403


def test_anonymous_gets_401(app):
    c = TestClient(app)
    assert c.get("/api/state").status_code == 401
    assert c.get("/files/m/brief").status_code == 401


def test_materials_hidden_until_start(admin, team):
    assert "brief" not in team.get("/api/case").json()
    assert team.get("/files/m/brief").status_code == 403
    assert team.get("/api/addressbook").status_code == 403
    start(admin)
    info = team.get("/api/case").json()
    assert "Стоун" in info["brief"] and len(info["questions"]) == 8
    assert team.get("/files/m/brief").status_code == 200
    assert len(team.get("/api/addressbook").json()) == 1082


def test_visit_flow_and_appendix_access(admin, team):
    start(admin)
    assert team.get("/files/a/1/app01.pdf").status_code == 403
    r = team.post("/api/visit", json={"district": "П", "number": "48"}, headers=H).json()
    assert r["trips"] == 1 and r["event"]["appendices"][0]["n"] == 1
    assert team.get("/files/a/1/app01.pdf").status_code == 200
    assert team.get("/files/a/1/app01_p1.jpg").status_code == 200
    assert team.get("/files/a/1/..%2F..%2Fcase24.yaml").status_code == 404
    assert team.get("/files/a/2/app02.pdf").status_code == 403
    assert admin.get("/files/a/2/app02.pdf").status_code == 200


def test_download_sets_attachment(admin, team):
    start(admin)
    r = team.get("/files/m/map-pdf?dl=1")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")


def test_bad_address_returns_422(admin, team):
    start(admin)
    r = team.post("/api/visit", json={"district": "П", "number": "ферма"}, headers=H)
    assert r.status_code == 422 and "Номер" in r.json()["detail"]


def test_state_returns_new_events_since(admin, team):
    start(admin)
    team.post("/api/visit", json={"district": "П", "number": "96"}, headers=H)
    s = team.get("/api/state").json()
    assert s["trips"] == 1 and len(s["events"]) == 1
    assert team.get(f"/api/state?since={s['events'][0]['id']}").json()["events"] == []


def test_admin_views_team_screen_read_only(admin, team):
    start(admin)
    team.post("/api/visit", json={"district": "П", "number": "96"}, headers=H)
    team.put("/api/answers/1", json={"text": "Джун"}, headers=H)
    tid = admin.get("/api/admin/state").json()["board"][0]["id"]
    s = admin.get(f"/api/state?view={tid}").json()
    assert s["team"] == "Сыщики" and s["trips"] == 1 and s["answers"]["1"] == "Джун"
    assert admin.get("/api/state").status_code == 403
    assert admin.get("/api/state?view=999").status_code == 404
    assert admin.post("/api/visit", json={"district": "П", "number": "48"}, headers=H).status_code == 403
    assert admin.put("/api/answers/1", json={"text": "x"}, headers=H).status_code == 403
    assert team.get("/api/state").json()["trips"] == 1


def test_team_cannot_view_other_team(app, admin, team):
    admin.post("/api/admin/teams", json={"name": "Соперники"}, headers=H)
    other = next(r["id"] for r in admin.get("/api/admin/state").json()["board"] if r["name"] == "Соперники")
    assert team.get(f"/api/state?view={other}").json()["team"] == "Сыщики"


def test_answers_submit_and_scoring(admin, team):
    start(admin)
    assert team.put("/api/answers/1", json={"text": "Джун"}, headers=H).status_code == 200
    assert team.post("/api/answers/submit", headers=H).status_code == 200
    assert team.put("/api/answers/1", json={"text": "x"}, headers=H).status_code == 409
    board = admin.get("/api/admin/state").json()["board"]
    tid = board[0]["id"]
    assert admin.put(f"/api/admin/teams/{tid}/scores/1", json={"points": 2}, headers=H).status_code == 200
    assert admin.put(f"/api/admin/teams/{tid}/scores/1", json={"points": 9}, headers=H).status_code == 409
    assert team.get("/api/state").json()["results"] is None
    admin.post("/api/admin/settings", json={"results_published": True, "debrief_published": True}, headers=H)
    s = team.get("/api/state").json()
    assert s["results"][0]["points"] == 2 and s["results"][0]["me"]
    assert "Кох" in s["debrief"]["text"]


def test_settings_validation(admin):
    assert admin.post("/api/admin/settings", json={"started_at": 1}, headers=H).status_code == 409
    assert admin.post("/api/admin/settings", json={"order_delay_sec": "5"}, headers=H).status_code == 409
    assert admin.post("/api/admin/settings", json={"order_delay_sec": 60}, headers=H).json()["order_delay_sec"] == 60


def test_password_reset_logs_team_out(app, admin, team):
    tid = admin.get("/api/admin/state").json()["board"][0]["id"]
    creds = admin.post(f"/api/admin/teams/{tid}/password", headers=H).json()
    assert team.get("/api/state").status_code == 401
    login(app, creds["login"], creds["password"])


def test_duplicate_team_and_admin_login_refused(admin):
    admin.post("/api/admin/teams", json={"name": "Альфа", "login": "alpha"}, headers=H)
    assert admin.post("/api/admin/teams", json={"name": "Альфа"}, headers=H).status_code == 409
    assert admin.post("/api/admin/teams", json={"name": "X", "login": "admin"}, headers=H).status_code == 409


def test_reset_requires_confirm_word(admin, team):
    start(admin)
    team.post("/api/visit", json={"district": "П", "number": "96"}, headers=H)
    assert admin.post("/api/admin/reset", json={"confirm": "да"}, headers=H).status_code == 409
    assert admin.post("/api/admin/reset", json={"confirm": "СБРОС"}, headers=H).status_code == 200
    assert team.get("/api/state").json()["clock"]["status"] == "waiting"
