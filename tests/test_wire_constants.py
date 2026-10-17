"""Wire-boundary constants — the numeric half of the wire contract.

The trait *names* and the module version are already guarded
(`tests/test_widget.py` + `frontend/test/defaults.test.js` for the names,
`tests/test_versions.py` + `frontend/wirecheck.js` for the versions). The
numbers the same contract carries were not: the JS view clamps its own
transform to `[fit / MIN_ZOOM_PAD, MAX_ZOOM]` and caps its decoded tile
images at `DEFAULT_CACHED_IMAGES`, so drift on one side surfaces to the user
as "the wheel stops working at the extremes" or "tiles vanish early", not as
a failing test (docs/DESIGN.md §6.2, §7).

Reads the JS declarations out of the frontend source — the same technique as
`tests/test_versions.py` reading `package.json` — and compares them with the
Python constants and trait defaults that produce the kernel's own bounds.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from islide.cache import _DEFAULT_MAX_TILES
from islide.viewport import SlideMeta, fit_zoom
from islide.widget import MIN_ZOOM_PAD, MAX_ZOOM, SlideViewer

REPO_ROOT = Path(__file__).resolve().parents[1]
VIEW_JS = REPO_ROOT / "frontend" / "view.js"
DEFAULTS_JS = REPO_ROOT / "frontend" / "defaults.js"
WIDGET_PY = REPO_ROOT / "islide" / "widget.py"

# A slide of known size, so fit_zoom is a real number rather than a symbol.
META = SlideMeta(
    dimensions=(2048, 2048),
    level_count=2,
    level_downsamples=(1.0, 2.0),
    level_dimensions=((2048, 2048), (1024, 1024)),
)


def js_const(name: str) -> float:
    """A top-level `const NAME = <number>;` in frontend/view.js.

    Fails loudly if the declaration is renamed or inlined at its uses: a
    constant that the guard can no longer see is a contract change too.
    """
    m = re.search(rf"^const {name} = (-?\d+(?:\.\d+)?);", VIEW_JS.read_text(), re.M)
    if m is None:
        pytest.fail(
            f"frontend/view.js no longer declares `const {name} = <number>;` — "
            f"update this guard (and the wire contract) together with it"
        )
    return float(m.group(1))


def js_model_defaults() -> dict[str, str]:
    """The `SLIDE_MODEL_DEFAULTS` entries of frontend/defaults.js, as text."""
    text = DEFAULTS_JS.read_text()
    block = text[text.index("= {") + 3 : text.index("};")]
    return dict(re.findall(r"^\s*([a-z_]+):\s*([^,\n]+),", block, re.M))


def js_model_default(name: str) -> float:
    """One numeric `SLIDE_MODEL_DEFAULTS` entry (non-numeric → guard failure)."""
    raw = js_model_defaults().get(name)
    if raw is None:
        pytest.fail(f"frontend/defaults.js has no `{name}` default to compare")
    try:
        return float(raw)
    except ValueError:
        pytest.fail(f"frontend/defaults.js `{name}: {raw}` is not a number")


def py_trait_default(name: str):
    """The default value of a synced Python trait on `SlideViewer`."""
    return SlideViewer.class_traits()[name].default_value


# ---------------------------------------------------------------- zoom bounds
def test_max_zoom_agrees_on_both_sides():
    assert js_const("MAX_ZOOM") == pytest.approx(MAX_ZOOM)


def test_zoom_floor_uses_the_js_pad():
    """The kernel's floor is the JS view's floor: fit zoom / MIN_ZOOM_PAD.

    `_zoom_floor` is called directly with only `_meta` on self — that is all
    the method reads — so the pad being compared is the one the kernel
    actually applies, not a copy of the constant written out again here.
    """
    fit = fit_zoom(META, 960, 540)
    floor = SlideViewer._zoom_floor(SimpleNamespace(_meta=META), 960, 540)
    assert floor == pytest.approx(fit / js_const("MIN_ZOOM_PAD"))
    assert floor == pytest.approx(fit / MIN_ZOOM_PAD)


def test_kernel_has_no_second_copy_of_the_zoom_bounds():
    """`MAX_ZOOM` / `MIN_ZOOM_PAD` are the only place either bound is written.

    The finding this guard exists for was the bounds living as independent
    literals on each side; a re-typed literal on the Python side would make
    the two tests above compare a value that nothing else uses.
    """
    src = WIDGET_PY.read_text()
    assert re.search(r"self\._max_zoom = MAX_ZOOM\b", src)
    assert re.search(r"fit_zoom\([^)]*\) / MIN_ZOOM_PAD\b", src)
    assert not re.search(r"self\._max_zoom = \d", src)
    assert not re.search(r"fit_zoom\([^)]*\) / \d", src)


# ------------------------------------------------------------------ cache cap
def test_cache_cap_is_one_number_across_all_four_declarations():
    """`image_cache_max` (trait), `TileCache` default, and the two JS values.

    The JS cap and the kernel cap limit the same tile set in the same units,
    so a difference between them means one side evicts territory the other
    still believes it holds.
    """
    trait = py_trait_default("image_cache_max")
    assert _DEFAULT_MAX_TILES == trait
    assert js_const("DEFAULT_CACHED_IMAGES") == trait
    assert js_model_default("image_cache_max") == trait


# ------------------------------------------------------------- canvas defaults
def test_canvas_height_default_agrees_on_both_sides():
    """The pre-attach default height, before the kernel's trait value lands."""
    assert js_model_default("canvas_h") == py_trait_default("canvas_h")


def test_planning_width_stays_python_only():
    """`canvas_w` is measured by the view; the JS side declares no width.

    `DEFAULT_PLANNING_WIDTH` backs the headless fit only (docs/DESIGN.md §7).
    A `canvas_w` default appearing in `SLIDE_MODEL_DEFAULTS` would be a new
    trait on the wire, and therefore a bump of the wire version — not a
    default to keep in sync here.
    """
    assert "canvas_w" not in js_model_defaults()
