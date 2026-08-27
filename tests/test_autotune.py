"""Threshold fitting, including its refusal to guess."""

from __future__ import annotations

from mindcontrol.autotune import Suggestion, _split_threshold, patch_config


def test_threshold_lands_in_the_gap():
    low = [0.10] * 30 + [0.14]
    high = [0.60] * 30 + [0.52]
    value, reason = _split_threshold(low, high, 0.5, "test")
    assert value is not None
    assert max(low) < value < min(high)
    assert "gap" in reason


def test_position_slides_across_the_gap():
    low, high = [0.1] * 30, [0.5] * 30
    near, _ = _split_threshold(low, high, 0.2, "test")
    far, _ = _split_threshold(low, high, 0.8, "test")
    assert near < far


def test_overlapping_clusters_are_refused():
    """The important negative case: no separation means no number."""
    values = [0.3 + i * 0.001 for i in range(60)]
    value, reason = _split_threshold(values[:30], values[:30], 0.5, "test")
    assert value is None
    assert "overlap" in reason


def test_sparse_data_is_refused():
    value, reason = _split_threshold([0.1] * 3, [0.9] * 3, 0.5, "test")
    assert value is None
    assert "not enough samples" in reason


def test_outliers_do_not_define_the_boundary():
    """One stray frame must not drag the threshold with it."""
    clean = [0.1] * 40
    contaminated = [*clean, 0.95]
    a, _ = _split_threshold(clean, [0.6] * 40, 0.5, "test")
    b, _ = _split_threshold(contaminated, [0.6] * 40, 0.5, "test")
    assert a == b


def test_patching_preserves_comments_and_layout(tmp_path):
    """The config's commentary is the reason it is readable; keep it."""
    path = tmp_path / "config.toml"
    path.write_text(
        "# leading note\n"
        "[gestures]\n"
        "# how close counts as pinched\n"
        "pinch_close = 0.30  # trailing note\n"
        "pinch_open = 0.45\n"
        "\n"
        "[pointer]\n"
        "pinch_close = 9.99\n"
    )
    applied = patch_config(
        path, [Suggestion("gestures", "pinch_close", 0.30, 0.22, "fitted")]
    )

    text = path.read_text()
    assert applied == ["gestures.pinch_close = 0.22"]
    assert "pinch_close = 0.22  # trailing note" in text
    assert "# how close counts as pinched" in text
    assert "# leading note" in text
    # The same key in another section must be left alone.
    assert "pinch_close = 9.99" in text


def test_patching_ignores_unchanged_values(tmp_path):
    path = tmp_path / "config.toml"
    original = "[gestures]\npinch_close = 0.30\n"
    path.write_text(original)
    assert patch_config(path, [Suggestion("gestures", "pinch_close", 0.30, 0.30, "same")]) == []
    assert path.read_text() == original


def test_refusals_are_never_written(tmp_path):
    path = tmp_path / "config.toml"
    original = "[gestures]\npinch_close = 0.30\n"
    path.write_text(original)
    assert patch_config(path, [Suggestion("gestures", "pinch_close", 0.30, None, "declined")]) == []
    assert path.read_text() == original


def test_writing_half_a_hysteresis_pair_is_refused(cfg):
    """Taking only the lower threshold can push it above the upper one.

    The pair is a band -- close below one, release above the other -- and the gap
    is what stops a hand hovering at the boundary from chattering. Inverted, a
    single steady hand reads as closed *and* open and clicks every frame; six
    clicks in one second, measured.
    """
    from mindcontrol.autotune import Suggestion, _broken_pairs

    lower_only = [Suggestion("gestures", "pinch_close", cfg.gestures.pinch_close, 0.533, "")]
    assert _broken_pairs(cfg.gestures, lower_only), "an inverted band was accepted"

    both = [
        *lower_only,
        Suggestion("gestures", "pinch_open", cfg.gestures.pinch_open, 0.726, ""),
    ]
    assert not _broken_pairs(cfg.gestures, both), "a well-ordered pair was refused"


def test_an_unrelated_threshold_is_not_blocked_by_the_pair_check(cfg):
    from mindcontrol.autotune import Suggestion, _broken_pairs

    unrelated = [Suggestion("gestures", "thumb_extended", cfg.gestures.thumb_extended, 1.043, "")]
    assert not _broken_pairs(cfg.gestures, unrelated)
