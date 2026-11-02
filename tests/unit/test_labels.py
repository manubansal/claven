"""Tests for claven.core.labels — Claven's Gmail label names, colours, and renames."""
import pytest

from claven.core.labels import (
    GMAIL_LABEL_COLORS,
    KNOWN,
    LABEL_COLORS,
    PARENT,
    SENT_SCANNED,
    UNKNOWN,
    label_body,
    managed_visibility,
)
from claven.core.labels import COUNTDOWN_LABELS


class TestNames:
    def test_short_codes_under_c(self):
        assert (PARENT, KNOWN, UNKNOWN, SENT_SCANNED) == ("c", "c/k", "c/u", "c/s")

    def test_hourly_countdown_labels(self):
        assert COUNTDOWN_LABELS[:3] == ("c/72h", "c/71h", "c/70h")
        assert COUNTDOWN_LABELS[-2:] == ("c/2h", "c/1h")
        assert len(COUNTDOWN_LABELS) == 72


class TestColors:
    def test_traffic_light(self):
        assert LABEL_COLORS[KNOWN]["backgroundColor"] == "#16a766"   # green
        assert LABEL_COLORS["c/72h"] == LABEL_COLORS["c/13h"]        # calm: light blue
        assert LABEL_COLORS["c/12h"]["backgroundColor"] == "#fad165"  # yellow
        assert LABEL_COLORS["c/5h"] == LABEL_COLORS["c/12h"]
        assert LABEL_COLORS["c/4h"]["backgroundColor"] == "#ffad47"   # orange
        assert LABEL_COLORS["c/2h"] == LABEL_COLORS["c/4h"]
        assert LABEL_COLORS["c/1h"]["backgroundColor"] == "#fb4c2f"   # red
        assert "c/0h" not in LABEL_COLORS

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

    def test_countdown_is_shown_and_coloured(self):
        # Gmail lists nested labels by their parent's setting, not their own.
        body = label_body("c/23h")
        assert body["labelListVisibility"] == "labelShow"
        assert body["messageListVisibility"] == "show"
        assert body["color"] == LABEL_COLORS["c/23h"]

    def test_parent_shows_in_sidebar_only_while_unread_and_not_on_mail(self):
        # Up to 72 countdown labels nest under c: list the tree only while it has unread mail.
        body = label_body(PARENT)
        assert body["labelListVisibility"] == "labelShowIfUnread"
        assert body["messageListVisibility"] == "hide"


class TestManagedVisibility:
    def test_sent_scanned_stays_hidden(self):
        assert managed_visibility(SENT_SCANNED) == {"labelListVisibility": "labelHide", "messageListVisibility": "hide"}

    def test_parent_listed_only_while_unread(self):
        assert managed_visibility(PARENT) == {"labelListVisibility": "labelShowIfUnread"}

    @pytest.mark.parametrize("name", [KNOWN, UNKNOWN, "c/24h", "Newsletter"])
    def test_others_unmanaged(self, name):
        assert managed_visibility(name) == {}

    def test_unstyled_label_gets_plain_defaults(self):
        assert label_body("Newsletter") == {
            "name": "Newsletter", "labelListVisibility": "labelShow", "messageListVisibility": "show",
        }
