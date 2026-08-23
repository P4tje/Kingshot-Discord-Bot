"""A bad API run must not empty a roster.

STATE_MISMATCH for most of an alliance means the Gift Redemption API is
misreporting, not that everyone transferred, so apply_removals holds the
deletions back and flags the members wrong-kingdom instead.
"""
import asyncio
import logging
import sqlite3
import types

import cogs.gift_redemption as gr


def _harness(monkeypatch, roster_size, settings_rows=((5, 1),)):
    users = sqlite3.connect(":memory:", check_same_thread=False)
    users.execute("CREATE TABLE users (fid INTEGER, nickname TEXT, alliance TEXT)")
    for fid in range(1, roster_size + 1):
        users.execute("INSERT INTO users VALUES (?,?,?)", (fid, f"p{fid}", "5"))
    users.commit()

    alliance = sqlite3.connect(":memory:", check_same_thread=False)
    alliance.execute("CREATE TABLE alliancesettings (alliance_id INTEGER, auto_remove_on_transfer INTEGER)")
    for aid, flag in settings_rows:
        alliance.execute("INSERT INTO alliancesettings VALUES (?,?)", (aid, flag))
    alliance.commit()

    settings = sqlite3.connect(":memory:", check_same_thread=False)
    settings.execute("CREATE TABLE alliance_logs (alliance_id INTEGER, channel_id INTEGER)")
    settings.commit()

    real_connect = sqlite3.connect
    routes = {"db/users.sqlite": users, "db/alliance.sqlite": alliance, "db/settings.sqlite": settings}

    def fake_connect(path, *a, **k):
        conn = routes.get(str(path))
        return conn if conn is not None else real_connect(path, *a, **k)

    monkeypatch.setattr(gr.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(gr.gift_state_resolver, "is_multistate", lambda aid: False)

    flagged = []
    monkeypatch.setattr(gr.gift_state_resolver, "flag_state_mismatch_many", flagged.extend)

    cog = types.SimpleNamespace(
        logger=logging.getLogger("test"),
        bot=types.SimpleNamespace(get_channel=lambda cid: None),
    )
    return cog, users, flagged


def _candidates(n, alliance_id=5):
    return [(alliance_id, fid, f"p{fid}") for fid in range(1, n + 1)]


def test_mass_removal_is_held_back(monkeypatch):
    """The reported incident: the API flags a whole alliance, nobody may be deleted."""
    cog, users, flagged = _harness(monkeypatch, roster_size=100)

    asyncio.run(gr.apply_removals(cog, _candidates(100)))

    assert users.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 100, "roster was wiped"
    assert sorted(flagged) == list(range(1, 101)), "held-back members must be flagged instead"


def test_ordinary_transfer_still_removes(monkeypatch):
    """A couple of genuine transfers must still be removed."""
    cog, users, flagged = _harness(monkeypatch, roster_size=100)

    asyncio.run(gr.apply_removals(cog, _candidates(2)))

    remaining = {r[0] for r in users.execute("SELECT fid FROM users").fetchall()}
    assert remaining == set(range(3, 101))
    assert flagged == []


def test_small_alliance_can_still_remove_one(monkeypatch):
    """One member of four is over the share but under the floor, so it goes through."""
    cog, users, flagged = _harness(monkeypatch, roster_size=4)

    asyncio.run(gr.apply_removals(cog, _candidates(1)))

    assert users.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    assert flagged == []


def test_guard_is_per_alliance(monkeypatch):
    """One alliance being held back must not stop a healthy one."""
    cog, users, flagged = _harness(
        monkeypatch, roster_size=100, settings_rows=((5, 1), (6, 1)))
    users.execute("INSERT INTO users VALUES (?,?,?)", (201, "x", "6"))
    users.execute("INSERT INTO users VALUES (?,?,?)", (202, "y", "6"))
    users.commit()

    asyncio.run(gr.apply_removals(cog, _candidates(50) + [(6, 201, "x")]))

    assert users.execute("SELECT COUNT(*) FROM users WHERE alliance='5'").fetchone()[0] == 100
    assert users.execute("SELECT COUNT(*) FROM users WHERE fid=201").fetchone()[0] == 0
    assert sorted(flagged) == list(range(1, 51))
