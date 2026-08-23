"""Multi-slot bear traps (1-4) + optional linked farm alliance.

Covers the slot-range validator, the distinct-slot helpers used to drive the
data-driven trap buttons, the farm-link storage helpers, and the two-pass
matcher (main roster priority, linked farm roster as a fallback pool).
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import MagicMock

import pytest
from harness import bt


# ---------------------------------------------------------------------------
# Slot-range validator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trap", [1, 2, 3, 4])
def test_validate_accepts_slots_1_to_4(trap):
    errors = bt.validate_bear_submission("2026-08-21", trap, 40, 1000)
    assert not any("trap" in e.lower() for e in errors)


@pytest.mark.parametrize("trap", [0, 5, -1])
def test_validate_rejects_out_of_range_slots(trap):
    errors = bt.validate_bear_submission("2026-08-21", trap, 40, 1000)
    assert any("trap" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Distinct-slot helpers (drive the data-driven trap buttons)
# ---------------------------------------------------------------------------

def _seed_db(monkeypatch, tmp_path):
    bear_db = tmp_path / "bear_data.sqlite"
    monkeypatch.setattr(bt, "BEAR_DB_PATH", str(bear_db))
    bt.init_bear_database()
    return bear_db


def test_alliance_trap_slots_lists_only_slots_with_data(tmp_path, monkeypatch):
    bear_db = _seed_db(monkeypatch, tmp_path)
    with sqlite3.connect(str(bear_db)) as conn:
        for trap in (1, 3):  # alliance 7 ran main trap 1 and a farm slot 3
            conn.execute(
                "INSERT INTO bear_hunts (alliance_id, date, hunting_trap, total_damage) "
                "VALUES (?, ?, ?, ?)", (7, "2026-08-21", trap, 100))
        conn.execute(
            "INSERT INTO bear_hunts (alliance_id, date, hunting_trap, total_damage) "
            "VALUES (?, ?, ?, ?)", (8, "2026-08-21", 2, 100))  # other alliance
        conn.commit()
    assert bt.alliance_trap_slots(7) == [1, 3]
    assert bt.alliance_trap_slots(999) == []


def test_player_trap_slots_is_per_player(tmp_path, monkeypatch):
    bear_db = _seed_db(monkeypatch, tmp_path)
    with sqlite3.connect(str(bear_db)) as conn:
        hunts = {}
        for trap in (1, 2, 4):
            cur = conn.execute(
                "INSERT INTO bear_hunts (alliance_id, date, hunting_trap, total_damage) "
                "VALUES (?, ?, ?, ?)", (7, "2026-08-21", trap, 100))
            hunts[trap] = cur.lastrowid
        # player 500 appears in traps 1 and 4; player 600 only in trap 2
        conn.execute("INSERT INTO bear_player_damage (hunt_id, fid, damage) VALUES (?, ?, ?)",
                     (hunts[1], 500, 10))
        conn.execute("INSERT INTO bear_player_damage (hunt_id, fid, damage) VALUES (?, ?, ?)",
                     (hunts[4], 500, 10))
        conn.execute("INSERT INTO bear_player_damage (hunt_id, fid, damage) VALUES (?, ?, ?)",
                     (hunts[2], 600, 10))
        conn.commit()
    assert bt.player_trap_slots(7, 500) == [1, 4]
    assert bt.player_trap_slots(7, 600) == [2]


# ---------------------------------------------------------------------------
# Farm-link storage
# ---------------------------------------------------------------------------

def test_farm_link_roundtrip(tmp_path, monkeypatch):
    _seed_db(monkeypatch, tmp_path)
    assert bt.get_farm_link(7) is None
    bt.set_farm_link(7, 999)
    assert bt.get_farm_link(7) == 999
    bt.set_farm_link(7, 1001)  # one farm per main - upsert replaces
    assert bt.get_farm_link(7) == 1001
    bt.clear_farm_link(7)
    assert bt.get_farm_link(7) is None


# ---------------------------------------------------------------------------
# Two-pass matching (main priority, farm fallback)
# ---------------------------------------------------------------------------

def _make_review(monkeypatch, tmp_path, *, main_roster, farm_roster, rows, farm_id=999):
    _seed_db(monkeypatch, tmp_path)
    if farm_roster is not None:
        bt.set_farm_link(7, farm_id)
    cog = MagicMock()
    cog.get_match_roster = MagicMock(return_value=farm_roster or [])

    # discord.py 2.6.x builds the View's stopped-future from the running loop, so
    # construct inside one (harmless on 2.7.x). Matching runs synchronously in init.
    async def _build():
        return bt.BearHuntReviewView(
            cog, MagicMock(),
            hunt_meta={"date": "2026-08-21", "hunting_trap": 3,
                       "rallies": 10, "total_damage": 900},
            rows=rows, roster=main_roster, alliance_id=7, alliance_name="Main",
            original_user_id=1,
        )
    return asyncio.run(_build())


def test_farm_fallback_matches_only_main_misses(tmp_path, monkeypatch):
    view = _make_review(
        monkeypatch, tmp_path,
        main_roster=[(1, "Alice"), (2, "Bob")],
        farm_roster=[(100, "FarmGuy")],
        rows=[
            {"name": "Alice", "damage": 500, "rank": 1},
            {"name": "FarmGuy", "damage": 300, "rank": 2},
            {"name": "Zzzznobody", "damage": 100, "rank": 3},
        ],
    )
    by_name = {r["name"]: r for r in view.rows}
    assert by_name["Alice"]["fid"] == 1
    assert by_name["Alice"]["matched_source"] is None
    assert by_name["FarmGuy"]["fid"] == 100
    assert by_name["FarmGuy"]["matched_source"] == "farm"
    assert by_name["Zzzznobody"]["fid"] is None


def test_main_roster_wins_over_farm(tmp_path, monkeypatch):
    # Same name in both rosters must resolve to the main member, not the farm alt.
    view = _make_review(
        monkeypatch, tmp_path,
        main_roster=[(1, "Alice")],
        farm_roster=[(100, "Alice")],
        rows=[{"name": "Alice", "damage": 500, "rank": 1}],
    )
    assert view.rows[0]["fid"] == 1
    assert view.rows[0]["matched_source"] is None


def test_no_farm_link_leaves_misses_unmatched(tmp_path, monkeypatch):
    view = _make_review(
        monkeypatch, tmp_path,
        main_roster=[(1, "Alice")],
        farm_roster=None,  # no farm linked
        rows=[{"name": "FarmGuy", "damage": 300, "rank": 1}],
    )
    assert view.rows[0]["fid"] is None
    assert view.rows[0]["matched_source"] is None
