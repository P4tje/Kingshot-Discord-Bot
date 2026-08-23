"""Sub-event template rows: seeding, migration and the reload sync.

A multi-phase event sends one notification per phase, and each phase now owns an
editable template row. The cog re-runs its schema setup on every load, so the
sync must leave those rows alone or every reload wipes the phase wording.
"""
import importlib
import sqlite3

import pytest

templates = importlib.import_module("cogs.notification_templates")
NotificationTemplates = templates.NotificationTemplates
EVENT_CONFIG = templates.EVENT_CONFIG

PHASE_EVENT = "SvS" if "SvS" in EVENT_CONFIG else "KvK"
PHASES = list(EVENT_CONFIG[PHASE_EVENT]["descriptions"])
SUB_EVENT_ROWS = sum(len(c.get("descriptions") or {}) for c in EVENT_CONFIG.values())


def _reload(cog):
    """Re-run everything a cog load does against an existing database."""
    cog._setup_database()


def _row(cog, event_type, instance_id=None):
    return cog.get_event_template(event_type, instance_id)


def _count(cog, sql, params=()):
    return cog.cursor.execute(sql, params).fetchone()[0]


def test_migration_adds_instance_identifier_once(make_templates_cog):
    cog = make_templates_cog()
    cog._migrate_add_instance_identifier()
    cog._migrate_add_instance_identifier()

    columns = [c[1] for c in cog.cursor.execute("PRAGMA table_info(notification_templates)")]

    assert columns.count("instance_identifier") == 1


def test_migration_runs_on_a_table_that_predates_the_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE notification_templates (
        template_id INTEGER PRIMARY KEY AUTOINCREMENT, template_name TEXT, event_type TEXT,
        embed_description TEXT, is_global INTEGER DEFAULT 1)""")
    conn.commit()

    cog = NotificationTemplates.__new__(NotificationTemplates)
    cog.conn = conn
    cog.cursor = conn.cursor()
    cog._migrate_add_instance_identifier()

    columns = [c[1] for c in cog.cursor.execute("PRAGMA table_info(notification_templates)")]

    assert "instance_identifier" in columns


def test_every_sub_event_gets_its_own_row(make_templates_cog):
    cog = make_templates_cog()

    assert _count(cog, "SELECT COUNT(*) FROM notification_templates "
                       "WHERE instance_identifier IS NOT NULL") == SUB_EVENT_ROWS

    for phase in PHASES:
        row = _row(cog, PHASE_EVENT, phase)
        assert row is not None
        assert row["embed_description"] == EVENT_CONFIG[PHASE_EVENT]["descriptions"][phase]
        assert row["event_type"] == PHASE_EVENT
        assert row["is_global"] == 1


def test_sub_event_rows_are_named_after_their_phase(make_templates_cog):
    cog = make_templates_cog()

    names = [_row(cog, PHASE_EVENT, phase)["template_name"] for phase in PHASES]

    assert len(set(names)) == len(PHASES)
    for name in names:
        assert name.startswith(f"{PHASE_EVENT} - ")


def test_seeding_is_idempotent(make_templates_cog):
    cog = make_templates_cog()
    before = _count(cog, "SELECT COUNT(*) FROM notification_templates")

    cog._ensure_instance_templates()
    cog._ensure_instance_templates()

    assert _count(cog, "SELECT COUNT(*) FROM notification_templates") == before


def test_reload_does_not_duplicate_rows(make_templates_cog):
    cog = make_templates_cog()
    before = _count(cog, "SELECT COUNT(*) FROM notification_templates")

    _reload(cog)
    _reload(cog)

    assert _count(cog, "SELECT COUNT(*) FROM notification_templates") == before


def test_reload_keeps_each_phase_on_its_own_default(make_templates_cog):
    """The regression: the sync used to stamp the parent description over every phase."""
    cog = make_templates_cog()

    _reload(cog)
    _reload(cog)

    parent_description = EVENT_CONFIG[PHASE_EVENT]["description"]
    for phase in PHASES:
        row = _row(cog, PHASE_EVENT, phase)
        assert row["embed_description"] == EVENT_CONFIG[PHASE_EVENT]["descriptions"][phase]
        assert row["embed_description"] != parent_description


def test_reload_keeps_an_admin_edit_to_a_phase(make_templates_cog):
    cog = make_templates_cog()
    phase = PHASES[0]
    row = _row(cog, PHASE_EVENT, phase)
    cog.update_template(row["template_id"], row["embed_title"], "Shields up, %t to go!",
                        None, None, None, None, None, 999)

    _reload(cog)
    _reload(cog)

    assert _row(cog, PHASE_EVENT, phase)["embed_description"] == "Shields up, %t to go!"


def test_reload_still_refreshes_an_untouched_parent(make_templates_cog):
    cog = make_templates_cog()
    cog.cursor.execute("UPDATE notification_templates SET embed_description = 'stale' "
                       "WHERE event_type = ? AND instance_identifier IS NULL", (PHASE_EVENT,))
    cog.conn.commit()

    _reload(cog)

    assert _row(cog, PHASE_EVENT)["embed_description"] == EVENT_CONFIG[PHASE_EVENT]["description"]


def test_get_event_template_returns_the_parent_not_a_phase(make_templates_cog):
    cog = make_templates_cog()
    parent = _row(cog, PHASE_EVENT)
    cog.update_template(parent["template_id"], parent["embed_title"], "Custom parent text",
                        None, None, None, None, None, 999)

    again = _row(cog, PHASE_EVENT)

    assert again["instance_identifier"] is None
    assert again["embed_description"] == "Custom parent text"


def test_apply_description_writes_to_every_sub_event(make_templates_cog):
    cog = make_templates_cog()

    updated = cog.apply_description_to_instances(PHASE_EVENT, "One text for all, %t left", 999)

    assert updated == len(PHASES)
    for phase in PHASES:
        assert _row(cog, PHASE_EVENT, phase)["embed_description"] == "One text for all, %t left"


def test_applied_description_survives_a_reload(make_templates_cog):
    cog = make_templates_cog()
    cog.apply_description_to_instances(PHASE_EVENT, "One text for all, %t left", 999)

    _reload(cog)

    for phase in PHASES:
        assert _row(cog, PHASE_EVENT, phase)["embed_description"] == "One text for all, %t left"


def test_apply_description_leaves_other_events_alone(make_templates_cog):
    cog = make_templates_cog()
    other = "Castle Battle"
    before = {p: _row(cog, other, p)["embed_description"]
              for p in EVENT_CONFIG[other]["descriptions"]}

    cog.apply_description_to_instances(PHASE_EVENT, "One text for all", 999)

    for phase, description in before.items():
        assert _row(cog, other, phase)["embed_description"] == description


def test_apply_description_does_not_touch_titles_or_images(make_templates_cog):
    cog = make_templates_cog()
    phase = PHASES[0]
    before = _row(cog, PHASE_EVENT, phase)

    cog.apply_description_to_instances(PHASE_EVENT, "One text for all", 999)
    after = _row(cog, PHASE_EVENT, phase)

    assert after["embed_title"] == before["embed_title"]
    assert after["embed_thumbnail_url"] == before["embed_thumbnail_url"]
    assert after["embed_image_url"] == before["embed_image_url"]


def test_resetting_a_phase_restores_that_phase_default(make_templates_cog):
    cog = make_templates_cog()
    phase = PHASES[-1]
    row = _row(cog, PHASE_EVENT, phase)
    cog.update_template(row["template_id"], row["embed_title"], "typo", None, None,
                        None, None, None, 999)

    assert cog.reset_template_to_default(row["template_id"], PHASE_EVENT) is True

    restored = _row(cog, PHASE_EVENT, phase)
    assert restored["embed_description"] == EVENT_CONFIG[PHASE_EVENT]["descriptions"][phase]
    assert restored["instance_identifier"] == phase
    assert restored["template_name"].startswith(f"{PHASE_EVENT} - ")


def test_browser_pages_stay_within_discord_limits(make_templates_cog):
    cog = make_templates_cog()
    rows = cog.get_templates_by_event_type()
    view = templates.TemplateBrowseView.__new__(templates.TemplateBrowseView)
    view.templates = rows
    view.page_size = 10
    view.total_pages = (len(rows) + view.page_size - 1) // view.page_size

    assert view.total_pages == 2
    assert view.page_size <= 25
    for page in range(view.total_pages):
        page_rows = rows[page * view.page_size:(page + 1) * view.page_size]
        assert 0 < len(page_rows) <= 25


def test_browser_lists_sub_events_under_their_parent(make_templates_cog):
    cog = make_templates_cog()
    names = [t["template_name"] for t in cog.get_templates_by_event_type()]
    phase_positions = [names.index(_row(cog, PHASE_EVENT, p)["template_name"]) for p in PHASES]

    assert names.index(PHASE_EVENT) < min(phase_positions)
    assert max(phase_positions) - min(phase_positions) == len(PHASES) - 1


@pytest.mark.parametrize("event_name", [e for e, c in EVENT_CONFIG.items()
                                        if not (c.get("descriptions") or {})])
def test_single_notification_events_get_no_sub_event_rows(event_name, make_templates_cog):
    cog = make_templates_cog()

    assert templates.get_instance_labels(event_name) == []
    assert _count(cog, "SELECT COUNT(*) FROM notification_templates "
                       "WHERE event_type = ? AND instance_identifier IS NOT NULL",
                  (event_name,)) == 0
