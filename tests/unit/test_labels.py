"""Tests for claven.core.labels — Claven's Gmail label names, colours, and renames."""
import pytest

from claven.core.labels import (
    GMAIL_LABEL_COLORS,
    KNOWN,
    LABEL_COLORS,
    LEGACY_NAMES,
    PARENT,
    SENT_SCANNED,
    UNKNOWN,
    label_body,
)
from claven.core.sunset import COUNTDOWN_LABELS


class TestNames:
    def test_short_codes_under_c(self):
        assert (PARENT, KNOWN, UNKNOWN, SENT_SCANNED) == ("c", "c/k", "c/u", "c/s")

    def test_every_old_name_maps_to_a_current_label(self):
        current = {KNOWN, UNKNOWN, SENT_SCANNED, *COUNTDOWN_LABELS}
        assert set(LEGACY_NAMES.values()) == current
        assert LEGACY_NAMES["known-sender"] == KNOWN
        assert LEGACY_NAMES["unknown-sender"] == UNKNOWN
        assert LEGACY_NAMES["claven/sent-scanned"] == SENT_SCANNED
        assert LEGACY_NAMES["sunset/under 1h left"] == "c/<1h"


class TestColors:
    def test_traffic_light(self):
        assert LABEL_COLORS[KNOWN]["backgroundColor"] == "#16a766"   # green
        assert LABEL_COLORS["c/3d"] == LABEL_COLORS["c/24h"]         # calm: light blue
        assert LABEL_COLORS["c/12h"]["backgroundColor"] == "#fad165"  # yellow
        assert LABEL_COLORS["c/4h"]["backgroundColor"] == "#ffad47"   # orange
        assert LABEL_COLORS["c/1h"]["backgroundColor"] == "#fb4c2f"   # red
        assert LABEL_COLORS["c/<1h"]["backgroundColor"] == "#ac2b16"  # dark red

    @pytest.mark.parametrize("name", sorted(LABEL_COLORS))
    def test_colors_are_from_gmail_palette(self, name):
        # Gmail rejects any colour outside its fixed palette.
        assert LABEL_COLORS[name]["backgroundColor"] in GMAIL_LABEL_COLORS
        assert LABEL_COLORS[name]["textColor"] in GMAIL_LABEL_COLORS


class TestLabelBody:
    def test_known_is_shown_and_coloured(self):
        assert label_body(KNOWN) == {
            "name": "c/k",
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
            "color": LABEL_COLORS[KNOWN],
        }

    def test_sent_scanned_is_hidden_everywhere(self):
        body = label_body(SENT_SCANNED)
        assert body["labelListVisibility"] == "labelHide"
        assert body["messageListVisibility"] == "hide"
        assert "color" not in body

    def test_parent_shows_in_sidebar_but_not_on_mail(self):
        body = label_body(PARENT)
        assert body["labelListVisibility"] == "labelShow"
        assert body["messageListVisibility"] == "hide"

    def test_unstyled_label_gets_plain_defaults(self):
        assert label_body("Newsletter") == {
            "name": "Newsletter", "labelListVisibility": "labelShow", "messageListVisibility": "show",
        }
