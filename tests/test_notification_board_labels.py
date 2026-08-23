"""Schedule board lines must name the phase, not just the event.

Phases of one event share a single embed title, so each line carries the phase
label; an instance id with no label must never reach the board.
"""
import asyncio
import importlib
import logging
import sqlite3
from datetime import datetime, timedelta

import pytz

sched = importlib.import_module("cogs.notification_schedule")


def _mk_cog():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE bear_notification_embeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER, title TEXT)""")
    conn.commit()

    cog = sched.NotificationSchedule.__new__(sched.NotificationSchedule)
    cog.conn = conn
    cog.cursor = conn.cursor()
    cog.logger = logging.getLogger("test")
    return cog


def _notif(notif_id, event_type, instance_identifier):
    next_at = (datetime.now(pytz.UTC) + timedelta(hours=2)).isoformat()
    return (notif_id, 20, 10, 0, "UTC", "EMBED_MESSAGE:x", 1, next_at, 1, 0, 0,
            event_type, instance_identifier)


def _line(cog, notif):
    return asyncio.run(cog._format_event_line(notif, pytz.UTC, False, 0))


def test_phases_of_one_event_get_distinct_lines():
    cog = _mk_cog()
    for notif_id in (1, 2, 3):
        cog.cursor.execute(
            "INSERT INTO bear_notification_embeds (notification_id, title) VALUES (?, '%i %n')",
            (notif_id,))
    cog.conn.commit()

    lines = [
        _line(cog, _notif(1, "KvK", "borders_open")),
        _line(cog, _notif(2, "KvK", "teleport_window")),
        _line(cog, _notif(3, "KvK", "battle_start")),
    ]

    assert "KvK - Borders Open" in lines[0]
    assert "KvK - Teleport Window" in lines[1]
    assert "KvK - Battle Start" in lines[2]
    assert len(set(lines)) == 3, "each phase must render as its own line"


def test_events_without_named_instances_are_untouched():
    cog = _mk_cog()
    cog.cursor.execute(
        "INSERT INTO bear_notification_embeds (notification_id, title) VALUES (1, '%i %n')")
    cog.conn.commit()

    line = _line(cog, _notif(1, "Fortress Battle", "time_0"))

    assert line.endswith("Fortress Battle"), "instance ids without a label must not leak into the board"


def test_label_is_not_repeated_when_the_title_already_names_the_phase():
    cog = _mk_cog()
    cog.cursor.execute(
        "INSERT INTO bear_notification_embeds (notification_id, title)"
        " VALUES (1, 'KvK Borders Open')")
    cog.conn.commit()

    line = _line(cog, _notif(1, "KvK", "borders_open"))

    assert line.count("Borders Open") == 1
