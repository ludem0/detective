from pathlib import Path

import pytest

from app.casefile import AddressError, compact, load_case
from app.db import Database
from app.game import Game, GameError

CASE = Path(__file__).resolve().parent.parent / "case" / "case24.yaml"
T0 = 1_000_000.0


@pytest.fixture(scope="module")
def case():
    return load_case(CASE)


@pytest.fixture
def game(case, tmp_path):
    g = Game(Database(tmp_path / "g.db"), case)
    g.db.run("INSERT INTO teams (name, login, pass_hash, created_at) VALUES ('A', 'a', 'x', 0)")
    g.start(T0)
    return g


def go(game, address, at=T0 + 10, team=1):
    district, _, rest = address.partition("-")
    return game.visit(team, district, rest, at)


def test_compact_ignores_case_spaces_and_dashes_but_keeps_slash():
    assert compact(" Маэстро Маззарри ") == "маэстромаззарри"
    assert compact("50-01") == "5001"
    assert compact("1/1") != compact("11")


def test_case_loads_every_appendix_and_entry(case):
    used = {n for e in case.by_id.values() for n in e.appendices}
    assert used == set(range(1, 12))
    assert case.key_of("П-00/ЖД") in case.entries


@pytest.mark.parametrize("district,number", [("П", "96"), ("Ю", "П-96"), ("П", " 96 ")])
def test_parse_accepts_full_address_in_number_field(case, district, number):
    assert case.parse(district, number).key == "П-96"


@pytest.mark.parametrize("district,number", [("П", ""), ("П", "абв"), ("Х", "12"), ("П", "1" * 70)])
def test_parse_rejects_garbage(case, district, number):
    with pytest.raises(AddressError):
        case.parse(district, number)


def test_known_address_gives_text_and_counts_trip(game):
    r = go(game, "П-96")
    assert r["counted"] and "колос в щите" in r["event"]["text"]
    assert game.trips(1) == 1


def test_unknown_address_is_empty_but_counted(game):
    r = go(game, "Ц-27")
    assert r["event"]["empty"] and r["counted"]
    assert game.trips(1) == 1


def test_repeat_visit_is_free(game):
    go(game, "Ю-38")
    r = go(game, "Ю-38")
    assert not r["counted"] and not r["new"]
    go(game, "Ц-27")
    go(game, "Ц-27")
    assert game.trips(1) == 2


def test_parametrized_requests_are_normalized(game):
    assert "Майкл Джералд" in go(game, "В-89/Маэстро Маззарри")["event"]["text"]
    assert "Хардман" in go(game, "В-48/П-00/ЖД")["event"]["title"]
    assert "густой шевелюрой" in go(game, "Ц-48/50-01")["event"]["text"]


def test_alias_is_same_trip_as_canonical(game):
    go(game, "В-89/маззарри")
    r = go(game, "В-89/маэстромаззарри")
    assert not r["counted"]
    assert game.trips(1) == 1


def test_portrait_comes_with_visit(game):
    assert not game.portraits(1)
    r = go(game, "Ц-46/3")
    assert r["event"]["portrait"] == "/files/p/bell.jpg"
    assert game.portraits(1) == {"bell.jpg"}
    assert go(game, "П-96/12")["event"]["portrait"] is None


def test_every_portrait_file_exists(case):
    portraits = {e.portrait for e in case.by_id.values() if e.portrait}
    assert len(portraits) == 23
    assert all((CASE.parent / "files" / "portraits" / p).is_file() for p in portraits)


def test_appendices_unlock_only_after_visit(game):
    assert 1 not in game.unlocked(1)
    r = go(game, "П-48")
    assert [a["n"] for a in r["event"]["appendices"]] == [1]
    assert 1 in game.unlocked(1)


def test_requires_hides_answer_until_prerequisite(game):
    assert go(game, "Ц-13")["event"]["empty"]
    go(game, "П-51/87")
    r = go(game, "Ц-13")
    assert r["counted"] and not r["event"]["empty"]
    assert 9 in game.unlocked(1)


def test_order_ready_notice_after_delay_then_search_is_a_trip(game):
    r = go(game, "Ю-35/ордер", at=T0 + 10)
    assert r["counted"] and "ордер" in r["event"]["text"]
    assert game.pending(1)
    assert game.deliver_due(1, T0 + 100) == 0
    assert game.deliver_due(1, T0 + 10 + game.get("order_delay_sec")) == 1
    ready = game.events(1)[-1]
    assert ready["title"] == "Ордер готов" and "Ю-35/обыск" in ready["text"] and not ready["counted"]
    assert 8 not in game.unlocked(1) and game.trips(1) == 1
    r = go(game, "Ю-35/обыск", at=T0 + 400)
    assert r["counted"] and r["event"]["label"] == "Ю-35/обыск" and "Журналисту" in r["event"]["text"]
    assert 8 in game.unlocked(1) and game.trips(1) == 2


def test_second_order_request_is_free(game):
    go(game, "СЗ-37/402/ордер")
    assert not go(game, "СЗ-37/402/ордер")["counted"]
    assert game.trips(1) == 1


def test_order_on_address_without_search_is_empty(game):
    r = go(game, "П-96/ордер")
    assert r["event"]["empty"] and r["counted"]
    assert not game.pending(1)


def test_search_is_free_only_before_order_is_ready(game):
    assert "ордер" in go(game, "Ю-35/обыск")["message"]
    go(game, "Ю-35/ордер")
    assert "оформляется" in go(game, "Ю-35/обыск", at=T0 + 20)["message"]
    assert game.trips(1) == 1
    r = go(game, "Ю-35/обыск", at=T0 + 401)  # ордер готов: доставка при самой поездке
    assert r["counted"] and "Журналисту" in r["event"]["text"]
    assert not go(game, "Ю-35/обыск", at=T0 + 402)["counted"]
    assert game.trips(1) == 2
    assert not any(e["source"] == "search" and not e["counted"] for e in game.events(1))


def test_criminalist_is_plain_address(game):
    assert go(game, "П-00/ЖД/криминалист")["event"]["appendices"][0]["n"] == 7
    assert go(game, "П-96/криминалист")["event"]["empty"]


def test_trigger_fires_after_theatre_and_hotel(game):
    go(game, "ЮВ-44", at=T0 + 10)
    game.deliver_due(1, T0 + 1000)
    assert not any(e["source"] == "trigger" for e in game.events(1))
    go(game, "СЗ-37", at=T0 + 20)
    game.deliver_due(1, T0 + 79)
    assert not any(e["source"] == "trigger" for e in game.events(1))
    game.deliver_due(1, T0 + 81)
    calls = [e for e in game.events(1) if e["source"] == "trigger"]
    assert len(calls) == 1 and "Маззарри" in calls[0]["text"] and not calls[0]["counted"]


def test_trigger_address_typed_early_is_empty_then_free_after_delivery(game):
    assert go(game, "ЮВ-44/звонок")["event"]["empty"]
    go(game, "ЮВ-44")
    go(game, "СЗ-37/216")
    game.deliver_due(1, T0 + 1000)
    r = go(game, "ЮВ-44/звонок", at=T0 + 1001)
    assert not r["counted"] and "Маззарри" in r["event"]["text"]


def test_trigger_fires_once(game):
    for a in ("ЮВ-44", "СЗ-37", "СЗ-37/504"):
        go(game, a)
    game.deliver_due(1, T0 + 1000)
    go(game, "СЗ-37/216", at=T0 + 1001)
    game.deliver_due(1, T0 + 5000)
    assert sum(e["source"] == "trigger" for e in game.events(1)) == 1


def test_visits_blocked_before_start_and_after_end(case, tmp_path):
    g = Game(Database(tmp_path / "b.db"), case)
    with pytest.raises(GameError):
        g.visit(1, "П", "96", T0)
    g.start(T0)
    with pytest.raises(GameError):
        g.visit(1, "П", "96", T0 + 121 * 60)
    g.add_minutes(10)
    assert g.visit(1, "П", "96", T0 + 121 * 60)["counted"]
    g.stop(T0 + 122 * 60)
    with pytest.raises(GameError):
        g.visit(1, "П", "48", T0 + 123 * 60)


def test_hints_auto_release_and_manual(game):
    assert game.hints(T0 + 29 * 60) == []
    assert [h["id"] for h in game.hints(T0 + 31 * 60)] == ["horse"]
    game.release_hint("names", T0 + 5)
    assert {h["id"] for h in game.hints(T0 + 31 * 60)} == {"horse", "names"}


def test_answers_lock_after_submit(game):
    game.save_answer(1, 1, "Джонатан Джун", T0)
    game.submit(1, T0)
    with pytest.raises(GameError):
        game.save_answer(1, 1, "другое", T0)
    game.unsubmit(1)
    game.save_answer(1, 1, "другое", T0)
    assert game.answers(1) == {1: "другое"}


def test_score_bounds_and_fee(game):
    with pytest.raises(GameError):
        game.set_score(1, 1, 3)
    for q in game.questions.values():
        game.set_score(1, q["n"], q["max"])
    for i in range(16):
        go(game, f"СЗ-{i + 1}")
    row = game.scoreboard()[0]
    assert row["points"] == 26 and row["trips"] == 16
    assert row["fee"] == round(20 * 26 * 26 / 4)


def test_scoreboard_ties_go_to_fewer_trips(game):
    game.db.run("INSERT INTO teams (name, login, pass_hash, created_at) VALUES ('B', 'b', 'x', 0)")
    for team, trips in ((1, 9), (2, 4)):
        game.set_score(team, 1, 2)
        for i in range(trips):
            go(game, f"С-{i + 1}", team=team)
    assert [r["name"] for r in game.scoreboard()] == ["B", "A"]


def test_wipe_team_clears_progress(game):
    go(game, "П-96")
    game.save_answer(1, 1, "x", T0)
    game.wipe_team(1)
    assert game.trips(1) == 0 and game.answers(1) == {}


def test_pause_freezes_clock_and_resume_keeps_remaining(game):
    assert game.clock(T0 + 600)["remaining"] == 6600
    game.stop(T0 + 600)
    paused = game.clock(T0 + 3000)
    assert paused["status"] == "paused" and paused["remaining"] == 6600
    with pytest.raises(GameError):
        go(game, "П-96", at=T0 + 3000)
    game.add_minutes(5)
    assert game.clock(T0 + 3000)["remaining"] == 6900
    game.resume(T0 + 3000)
    assert game.clock(T0 + 3100)["remaining"] == 6800
    assert go(game, "П-96", at=T0 + 3100)["counted"]


def test_pause_holds_orders_and_triggers(game):
    delay = game.get("order_delay_sec")
    go(game, "Ю-35/ордер", at=T0 + 10)
    go(game, "ЮВ-44", at=T0 + 10)
    go(game, "СЗ-37", at=T0 + 20)  # звонок через 60 с: T0 + 80
    game.stop(T0 + 30)
    assert game.deliver_due(1, T0 + 10 + delay + 50) == 0
    game.resume(T0 + 30 + 600)
    assert game.deliver_due(1, T0 + 80 + 599) == 0
    assert game.deliver_due(1, T0 + 80 + 600) == 1
    assert game.deliver_due(1, T0 + 10 + delay + 599) == 0
    assert game.deliver_due(1, T0 + 10 + delay + 600) == 1


def test_order_ready_before_pause_arrives_during_it(game):
    delay = game.get("order_delay_sec")
    go(game, "Ю-35/ордер", at=T0 + 10)
    game.stop(T0 + 10 + delay + 5)  # команда не опрашивала сервер, ордер уже готов
    assert game.deliver_due(1, T0 + 10 + delay + 100) == 1
    assert game.events(1)[-1]["at"] == T0 + 10 + delay


def test_second_stop_does_not_move_pause(game):
    game.stop(T0 + 600)
    game.stop(T0 + 900)
    game.resume(T0 + 1000)
    assert game.clock(T0 + 1000)["remaining"] == 6600


def test_start_refused_while_game_running(game):
    with pytest.raises(GameError):
        game.start(T0 + 60)
    game.stop(T0 + 60)
    with pytest.raises(GameError):
        game.start(T0 + 70)
    game.resume(T0 + 80)
    game.start(T0 + 200 * 60)  # время вышло — можно начать заново
    assert game.clock(T0 + 200 * 60)["status"] == "running"


def test_trigger_without_after_any_fires(case, tmp_path):
    from dataclasses import replace
    t = case.triggers[0]
    custom = replace(case, triggers=(replace(t, after_any=()),))
    g = Game(Database(tmp_path / "t.db"), custom)
    g.start(T0)
    go(g, "ЮВ-44")
    g.deliver_due(1, T0 + 1000)
    assert any(e["source"] == "trigger" for e in g.events(1))
