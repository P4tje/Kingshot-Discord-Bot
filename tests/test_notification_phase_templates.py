"""An edited phase template must reach the notification the wizard creates.

Each phase resolves its own description: the template row when one is stored,
otherwise the phase default in EVENT_CONFIG, never the generic event text.
"""
import importlib
import types

templates = importlib.import_module("cogs.notification_templates")
wizard = importlib.import_module("cogs.notification_wizard")

EVENT_CONFIG = templates.EVENT_CONFIG
PHASE_EVENT = "SvS" if "SvS" in EVENT_CONFIG else "KvK"
PHASES = list(EVENT_CONFIG[PHASE_EVENT]["descriptions"])


def _mk_preview_view(templates_cog):
    view = wizard.WizardPreviewView.__new__(wizard.WizardPreviewView)
    bot = types.SimpleNamespace(get_cog=lambda name: templates_cog if name == "NotificationTemplates" else None)
    view.cog = types.SimpleNamespace(bot=bot)
    return view


def _embed_for(view, phase, base_description="parent text"):
    return view._phase_embed(PHASE_EVENT,
                             {"description": base_description, "title": "%i %n"}, phase)


def test_phase_falls_back_to_the_event_config_default(make_templates_cog):
    view = _mk_preview_view(make_templates_cog())

    for phase in PHASES:
        assert _embed_for(view, phase)["description"] == \
            EVENT_CONFIG[PHASE_EVENT]["descriptions"][phase]


def test_edited_phase_template_wins_over_the_event_config_default(make_templates_cog):
    cog = make_templates_cog()
    phase = PHASES[0]
    row = cog.get_event_template(PHASE_EVENT, phase)
    cog.update_template(row["template_id"], row["embed_title"], "Admin wording, %t to go",
                        None, None, None, None, None, 7)
    view = _mk_preview_view(cog)

    assert _embed_for(view, phase)["description"] == "Admin wording, %t to go"
    for other in PHASES[1:]:
        assert _embed_for(view, other)["description"] == \
            EVENT_CONFIG[PHASE_EVENT]["descriptions"][other]


def test_batch_applied_description_reaches_every_phase(make_templates_cog):
    cog = make_templates_cog()
    cog.apply_description_to_instances(PHASE_EVENT, "Same text everywhere, %t left", 7)
    view = _mk_preview_view(cog)

    for phase in PHASES:
        assert _embed_for(view, phase)["description"] == "Same text everywhere, %t left"


def test_phase_embed_does_not_mutate_the_parent_embed_data(make_templates_cog):
    cog = make_templates_cog()
    view = _mk_preview_view(cog)
    embed_data = {"description": "parent text", "title": "%i %n"}

    view._phase_embed(PHASE_EVENT, embed_data, PHASES[0])

    assert embed_data["description"] == "parent text"


def test_unknown_phase_keeps_the_parent_description(make_templates_cog):
    view = _mk_preview_view(make_templates_cog())

    assert _embed_for(view, "time_0")["description"] == "parent text"
    assert _embed_for(view, None)["description"] == "parent text"


def test_missing_templates_cog_falls_back_to_the_event_config():
    view = wizard.WizardPreviewView.__new__(wizard.WizardPreviewView)
    view.cog = types.SimpleNamespace(bot=types.SimpleNamespace(get_cog=lambda name: None))

    assert _embed_for(view, PHASES[0])["description"] == \
        EVENT_CONFIG[PHASE_EVENT]["descriptions"][PHASES[0]]
