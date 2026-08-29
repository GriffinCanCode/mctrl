"""Chords, and what a gesture means in front of which application.

All of it runs with no Quartz, no camera and no window, which is the point of
:mod:`mindcontrol.control.keys` and :mod:`mindcontrol.control.bindings` being the
half of the keyboard path that imports nothing: a binding has to be checkable
before there is anything to send it to.

The failure worth most of the attention here is a binding that is accepted and
does nothing. It is only discovered at the moment the gesture is performed, by
which time the user is looking at their hand rather than at a log, and it is
indistinguishable from the tracking having failed.
"""

from __future__ import annotations

import pytest

from mindcontrol.config import Config, load
from mindcontrol.control.bindings import BINDABLE, MUTED, App, BindingTable, Focus
from mindcontrol.control.keys import KEY_CODES, format_chord, parse_chord, resolve_key
from mindcontrol.gestures.engine import Action

# Actions the pipeline acts on itself, in `_dispatch`. Everything else the engine
# can emit has to be bindable, or it is an intent with nowhere to go.
DISPATCHED = {
    Action.ENGAGE_TOGGLE,
    Action.POINTER_MOVE,
    Action.DRAG_MOVE,
    Action.CLICK,
    Action.DRAG_START,
    Action.DRAG_END,
    Action.SCROLL,
}


# --------------------------------------------------------------------- the chords


def test_a_chord_reads_the_way_a_menu_writes_one():
    binding = parse_chord("cmd+shift+p")
    assert (binding.key, binding.mods) == ("p", ["cmd", "shift"])
    assert parse_chord("f5").mods == []
    assert parse_chord("  CTRL + Left  ").key == "left"


def test_the_spellings_of_one_modifier_mean_one_modifier():
    """Because macOS itself calls them both, and a user will type either."""
    for spelling in ("cmd", "command", "super", "meta"):
        assert parse_chord(f"{spelling}+a").mods == ["cmd"]
    for spelling in ("alt", "option", "opt"):
        assert parse_chord(f"{spelling}+a").mods == ["alt"]
    assert parse_chord("cmd+cmd+a").mods == ["cmd"], "repeats collapse"


def test_a_key_may_be_named_or_typed():
    assert resolve_key("leftbracket") == resolve_key("[") == "["
    assert resolve_key("esc") == resolve_key("escape") == "escape"
    assert resolve_key("ENTER") == "return"
    assert resolve_key("elbow") is None


def test_the_separator_is_never_a_key_so_a_trailing_one_is_a_typo():
    """Reading ``cmd+`` as the plus key would make a typo into a valid chord.

    It costs nothing to refuse: macOS has no key code for plus either, since that
    position is shift and equals. Both spellings of it are still available.
    """
    assert parse_chord("cmd+") is None
    assert parse_chord("cmd+plus").key == parse_chord("shift+=").key == "="


def test_nonsense_is_answered_no_rather_than_raised():
    """Both callers are asking a question: is this a chord? Neither wants a raise."""
    for spec in ("", "   ", "cmd", "cmd+", "hyper+a", "cmd+elbow", "+", "cmd+shift+"):
        assert parse_chord(spec) is None, spec


def test_a_chord_survives_being_formatted_and_read_back():
    """Stored in canonical form, so two spellings of one chord compare equal."""
    for spec in ("cmd+shift+p", "option+left", "f12", "ctrl+alt+delete"):
        assert parse_chord(format_chord(parse_chord(spec))) == parse_chord(spec)
    assert format_chord(parse_chord("shift+cmd+p")) == "cmd+shift+p", "modifiers ordered"


def test_every_key_code_is_a_real_virtual_key():
    """One byte, and no duplicates: two names for one position is a typo."""
    assert all(0 <= code <= 0x7F for code in KEY_CODES.values())
    assert len(set(KEY_CODES.values())) == len(KEY_CODES)


# ------------------------------------------------------------------- the gestures


def test_every_gesture_the_pipeline_does_not_handle_itself_is_bindable():
    """Otherwise the engine emits an intent with nowhere for it to go.

    This is the join between the state machine and the table, and it is asserted
    rather than derived so that `control.bindings` stays importable without the
    engine -- which is what lets the API contract publish these names.
    """
    assert set(BINDABLE) == {action.value for action in Action if action not in DISPATCHED}


def test_a_new_gesture_is_emitted_but_not_bound_by_default():
    """Lowering an open palm is also how a hand comes to rest.

    So the downward push exists, streams, and can be bound in one line -- but
    ships doing nothing, because a system-wide action nobody asked for is worse
    than an inert gesture.
    """
    assert "palm_push_down" in BINDABLE
    assert Config().bindings.get("palm_push_down") is None


# ---------------------------------------------------------------------- the table


@pytest.fixture
def table() -> BindingTable:
    return BindingTable(
        {"swipe_left": "desktop_left", "telephone": "dictation"},
        {"Safari": {"swipe_left": "cmd+[", "telephone": MUTED}},
        actions=("desktop_left", "dictation"),
    )


SAFARI = App(bundle="com.apple.Safari", name="Safari")
TERMINAL = App(bundle="com.apple.Terminal", name="Terminal")


def test_the_application_in_front_wins_and_the_global_table_is_the_fallback(table):
    assert table.resolve("swipe_left", SAFARI) == "cmd+["
    assert table.resolve("swipe_left", TERMINAL) == "desktop_left"
    assert table.resolve("swipe_left", None) == "desktop_left"
    assert table.resolve("swipe_right", SAFARI) is None, "unbound stays unbound"


def test_an_application_may_mute_a_gesture_without_unbinding_it_everywhere(table):
    """The only way to say "not here". Clearing it would mean not anywhere."""
    assert table.resolve("telephone", SAFARI) is None
    assert table.resolve("telephone", TERMINAL) == "dictation"


def test_a_scope_may_be_named_however_the_user_thinks_of_the_app():
    """Requiring the bundle id would mean looking one up to write line one."""
    for scope in ("com.apple.Safari", "Safari", "safari", "SAFARI"):
        assert SAFARI.matches(scope), scope
    assert not SAFARI.matches("Terminal")
    assert not SAFARI.matches("")
    assert not App().matches("Safari"), "an unknown app matches no scope"


def test_a_binding_is_refused_at_the_moment_it_is_written(table):
    """Not at the moment the gesture is performed, which is far too late."""
    with pytest.raises(ValueError, match="no gesture"):
        table.set("wiggle", "cmd+a")
    with pytest.raises(ValueError, match="cmd\\+shift\\+p"):
        table.set("swipe_right", "hyper+q")
    with pytest.raises(ValueError, match="no gesture"):
        table.clear("wiggle")


def test_a_binding_may_name_an_action_or_spell_out_a_chord(table):
    table.set("swipe_right", "dictation")
    table.set("palm_push_down", "cmd+w", app="Safari")
    assert table.resolve("swipe_right", None) == "dictation"
    assert table.resolve("palm_push_down", SAFARI) == "cmd+w"
    assert table.resolve("palm_push_down", TERMINAL) is None


def test_clearing_reports_whether_there_was_anything_there(table):
    assert table.clear("swipe_left", app="Safari")
    assert not table.clear("swipe_left", app="Safari")
    assert table.resolve("swipe_left", SAFARI) == "desktop_left", "it falls through now"
    assert "Safari" in table.apps, "the muted telephone binding is still there"

    assert table.clear("telephone", app="Safari")
    assert "Safari" not in table.apps, "an empty scope is not kept"


def test_a_bad_line_in_config_is_dropped_rather_than_fatal(capsys):
    """Starting is more important than one binding. Silence is not an option."""
    table = BindingTable({"swipe_left": "hyper+q", "swipe_right": "cmd+]"}, actions=())

    assert table.resolve("swipe_left", None) is None
    assert table.resolve("swipe_right", None) == "cmd+]"
    assert "swipe_left" in capsys.readouterr().out


def test_the_reported_table_answers_the_question_a_consumer_has(table):
    """Which is "what will this gesture do right now", not "what is configured"."""
    published = table.to_json(SAFARI)

    assert published["scope"] == "Safari"
    assert published["app"] == {"bundle": "com.apple.Safari", "name": "Safari"}
    assert published["resolved"]["swipe_left"] == "cmd+["
    assert published["resolved"]["telephone"] is None
    assert published["default"]["telephone"] == "dictation", "still visible, just muted"
    assert list(published["gestures"]) == list(BINDABLE)


# --------------------------------------------------------------------- the config


def test_config_reads_a_sub_table_as_an_application_scope(tmp_path):
    """Which is how TOML already spells the distinction, so nothing is invented."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[bindings]\n"
        'swipe_left = "desktop_left"\n'
        "\n"
        "[bindings.Safari]\n"
        'swipe_left = "cmd+["\n'
        'palm_push_down = "cmd+w"\n'
    )
    cfg = load(path)

    assert cfg.bindings["swipe_left"] == "desktop_left"
    assert cfg.app_bindings["Safari"] == {"swipe_left": "cmd+[", "palm_push_down": "cmd+w"}

    table = BindingTable(cfg.bindings, cfg.app_bindings, cfg.keys)
    assert table.resolve("swipe_left", SAFARI) == "cmd+["
    assert table.resolve("swipe_left", TERMINAL) == "desktop_left"


def test_the_shipped_config_binds_only_gestures_that_exist(cfg):
    """A binding for a gesture the engine cannot emit is a line that never fires."""
    for gesture in cfg.bindings:
        assert gesture in BINDABLE, gesture
    for scope, rows in cfg.app_bindings.items():
        for gesture in rows:
            assert gesture in BINDABLE, f"[bindings.{scope}] {gesture}"


def test_the_shipped_actions_all_resolve_to_something_sendable(cfg):
    """Every default binding names a real key, so the shipped app is not decorative."""
    table = BindingTable(cfg.bindings, cfg.app_bindings, cfg.keys)
    for gesture in BINDABLE:
        action = table.resolve(gesture, None)
        if action is None:
            continue
        binding = cfg.keys.get(action) or parse_chord(action)
        assert binding is not None, action
        assert resolve_key(binding.key) is not None, action


# ---------------------------------------------------------------------- the focus


def test_the_front_application_is_asked_for_no_faster_than_it_can_change():
    """Once per frame would be thirty AppKit calls a second for a human's answer."""
    asked: list[int] = []

    def ask() -> App:
        asked.append(1)
        return SAFARI if len(asked) == 1 else TERMINAL

    focus = Focus(ttl=1.0, ask=ask)

    assert focus.current(now=100.0) is SAFARI
    assert focus.current(now=100.5) is SAFARI, "held for the interval"
    assert len(asked) == 1
    assert focus.current(now=101.5) is TERMINAL, "and re-asked after it"
