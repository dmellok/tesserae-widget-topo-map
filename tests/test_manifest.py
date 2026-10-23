"""The options panel stays short: a handful of options up front, the rest
nested under the choice they depend on or under More options."""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = json.loads(
    (Path(__file__).resolve().parent.parent / "plugin.json").read_text(encoding="utf-8")
)
OPTIONS = MANIFEST["cell_options"]

# Saved cells key on these names; the order is how the panel reads.
ORDER = [
    "location",
    "span_m",
    "show_roads",
    "feet",
    "theme",
    "c_ground",
    "c_contour",
    "c_index",
    "c_water",
    "show_parks",
    "c_green",
    "c_road",
    "show_rail",
    "c_rail",
    "show_relief",
    "c_text",
    "interval_m",
    "index_every",
    "show_labels",
    "weight",
    "show_peak",
    "show_label",
    "label",
    "show_country",
]

UP_FRONT = ["location", "span_m", "show_roads", "feet", "theme"]


def _up_front(o: dict) -> bool:
    return not o.get("show_if") and not o.get("advanced")


def test_option_names_and_order() -> None:
    assert [o["name"] for o in OPTIONS] == ORDER


def test_few_options_up_front() -> None:
    assert [o["name"] for o in OPTIONS if _up_front(o)] == UP_FRONT
    assert len(UP_FRONT) <= 5


def test_show_if_refers_to_real_options() -> None:
    names = {o["name"] for o in OPTIONS}
    for o in OPTIONS:
        for key in o.get("show_if") or {}:
            assert key in names, (o["name"], key)


def test_advanced_options_come_last() -> None:
    seen = False
    for o in OPTIONS:
        if o.get("advanced"):
            seen = True
        else:
            assert not seen, f"{o['name']} follows an advanced option"


def test_no_em_dashes_in_the_panel() -> None:
    assert "\u2014" not in json.dumps(OPTIONS, ensure_ascii=False)
