"""The recording script, and the subsets worth recording on their own."""

from __future__ import annotations

import pytest

from mindcontrol.sessions.autotune import analyse
from mindcontrol.sessions.record import FOCUS, SCRIPT, select


def test_no_focus_records_everything():
    assert select(None) == SCRIPT
    assert select(()) == SCRIPT


def test_a_focus_keeps_script_order():
    """Order is not cosmetic: the baseline has to be captured before any pose."""
    for name in FOCUS:
        chosen = [prompt.label for prompt in select((name,))]
        assert chosen == [p.label for p in SCRIPT if p.label in chosen]
        assert chosen[0] == "none"


def test_focus_groups_combine():
    both = {p.label for p in select(("pinch", "swipe"))}
    assert both == set(FOCUS["pinch"]) | set(FOCUS["swipe"])


def test_an_unknown_focus_names_the_real_ones():
    with pytest.raises(ValueError, match="no such focus"):
        select(("wrist-flick",))


def test_every_focus_label_exists():
    labels = {prompt.label for prompt in SCRIPT}
    for name, wanted in FOCUS.items():
        assert set(wanted) <= labels, f"{name} names a prompt the script does not have"


@pytest.mark.parametrize("name", sorted(FOCUS))
def test_a_focus_is_shorter_than_the_full_script(name):
    """A subset that saves no time is not worth offering."""
    assert sum(p.seconds for p in select((name,))) < sum(p.seconds for p in SCRIPT)


def test_focused_recordings_still_fit_their_thresholds(tmp_path, cfg):
    """The point of the groups: each carries the contrast its own fit needs.

    A threshold is a boundary between two clusters, so re-recording only the
    failing pose leaves nothing to separate it from. `pinch_close` fitted from
    pinches alone would be declined for want of an open hand to contrast against,
    and the user would have recorded for nothing.
    """
    from conftest import write_session

    expected = {"pinch": "pinch_close", "swipe": "swipe_min_speed"}
    for name, key in expected.items():
        labels = set(FOCUS[name])
        session = write_session(tmp_path / f"{name}.jsonl", keep=labels)
        fitted = {
            s.key: s.proposed for s in analyse(session, cfg.gestures, cfg.tracking) if s.proposed
        }
        assert key in fitted, f"focus '{name}' cannot fit {key}, the threshold it exists for"
