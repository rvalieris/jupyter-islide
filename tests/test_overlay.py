"""Overlay (full-slide heatmap) tests — headless (no JS view).

Covers ``SlideViewer``'s ``set_overlay`` / ``clear_overlay`` API: a
full-slide PNG (e.g. a model heatmap rendered at the slide's
``get_thumbnail`` scale) is validated (aspect ratio must match the
slide's), transported as a PNG data URL (alpha preserved — unlike the
tile JPEGs), and drawn over the tiles with ``overlay_alpha`` opacity via
the synced ``overlay_img``/``overlay_alpha`` trait pair.
"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from traitlets import TraitError

from islide import SlideViewer
from islide.encode import png_data_url

from util import SLIDE_PATH

SLIDE = str(SLIDE_PATH)

try:
    import openslide  # noqa: F401
    HAS_OPENSLLIDE = True
except ImportError:
    HAS_OPENSLLIDE = False

pytestmark = [
    pytest.mark.skipif(not HAS_OPENSLLIDE, reason="openslide-python not installed"),
]


def _slide_dims() -> tuple[int, int]:
    """The test slide's level-0 dimensions (read once, cached)."""
    if not hasattr(_slide_dims, "dims"):
        import openslide

        s = openslide.open_slide(SLIDE)
        try:
            _slide_dims.dims = tuple(s.dimensions)  # type: ignore[attr-defined]
        finally:
            s.close()
    return _slide_dims.dims  # type: ignore[attr-defined]


def _thumb_like(width: int = 256) -> Image.Image:
    """A heatmap-shaped RGBA image at the slide's aspect ratio, the way
    ``get_thumbnail((width, ...))`` produces it (integer-rounded height)."""
    w, h = _slide_dims()
    th = max(1, round(width * h / w))
    img = Image.new("RGBA", (width, th), (0, 0, 0, 0))
    px = img.load()
    for y in range(th):
        for x in range(width):
            px[x, y] = (255, 0, 0, 128)
    return img


def _decode_png(url: str) -> Image.Image:
    assert url.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


@pytest.fixture()
def viewer(slide_path):
    v = SlideViewer(slide_path)
    v.wait()
    yield v
    v.close()


# ----------------------------------------------------------- png transport
def test_png_data_url_preserves_alpha():
    img = Image.new("RGBA", (8, 4), (255, 0, 0, 128))
    out = _decode_png(png_data_url(img))
    assert out.size == (8, 4) and out.mode == "RGBA"
    assert out.getpixel((0, 0)) == (255, 0, 0, 128)


def test_png_data_url_converts_other_modes():
    assert _decode_png(png_data_url(Image.new("RGB", (8, 4)))).mode == "RGBA"
    assert _decode_png(png_data_url(Image.new("L", (8, 4)))).mode == "RGBA"


# ------------------------------------------------------------- SlideViewer
def test_overlay_trait_defaults(viewer):
    assert viewer.overlay_img == ""
    assert viewer.overlay_alpha == 0.5


def test_overlay_alpha_validates(viewer):
    for bad in (-0.1, 1.1, "x"):
        with pytest.raises(TraitError):
            viewer.overlay_alpha = bad  # type: ignore[assignment]
    assert viewer.overlay_alpha == 0.5


def test_set_overlay_pil_image(viewer):
    img = _thumb_like()
    viewer.set_overlay(img, alpha=0.3)
    assert viewer.overlay_alpha == 0.3
    out = _decode_png(viewer.overlay_img)
    assert out.size == img.size
    assert out.getpixel((0, 0)) == (255, 0, 0, 128)  # alpha survived the wire
    assert "overlay" in viewer.status


def test_set_overlay_path(tmp_path, viewer):
    img = _thumb_like()
    p = tmp_path / "heat.png"
    img.save(p)
    viewer.set_overlay(p)
    assert _decode_png(viewer.overlay_img).size == img.size


def test_set_overlay_replaces_previous(viewer):
    viewer.set_overlay(_thumb_like(256))
    first = viewer.overlay_img
    viewer.set_overlay(_thumb_like(64))
    assert viewer.overlay_img != first
    assert _decode_png(viewer.overlay_img).size == _thumb_like(64).size


def test_set_overlay_alpha_none_keeps_current(viewer):
    viewer.overlay_alpha = 0.2
    viewer.set_overlay(_thumb_like())
    assert viewer.overlay_alpha == 0.2


def test_set_overlay_aspect_mismatch_raises(viewer):
    w, h = _slide_dims()
    mismatch = Image.new("RGBA", (256, max(1, round(256 * 2 * h / w))), (0, 255, 0, 255))
    with pytest.raises(ValueError, match="aspect ratio"):
        viewer.set_overlay(mismatch)
    assert viewer.overlay_img == ""  # rejected: state untouched


def test_set_overlay_bad_source_raises(viewer):
    with pytest.raises(TypeError):
        viewer.set_overlay(123)  # type: ignore[arg-type]


def test_clear_overlay(viewer):
    viewer.set_overlay(_thumb_like())
    assert viewer.overlay_img != ""
    viewer.clear_overlay()
    assert viewer.overlay_img == ""


# ------------------------------------------------- transparency key
@pytest.fixture()
def _duotone(slide_path) -> Image.Image:
    """An overlay-scale image with pure-black and red pixels (both
    half-opaque, so a pre-existing alpha channel is distinguishable)."""
    w, h = _slide_dims()
    th = max(1, round(64 * h / w))
    img = Image.new("RGBA", (64, th), (0, 0, 0, 128))
    for y in range(th):
        for x in range(64):
            if x % 2:
                img.putpixel((x, y), (255, 0, 0, 128))
    return img


def test_set_overlay_default_makes_black_transparent(viewer, _duotone):
    viewer.set_overlay(_duotone)
    out = _decode_png(viewer.overlay_img)
    assert out.getpixel((0, 0)) == (0, 0, 0, 0)       # black -> transparent
    assert out.getpixel((1, 0)) == (255, 0, 0, 128)    # red untouched


def test_set_overlay_custom_transparent_key(viewer, _duotone):
    viewer.set_overlay(_duotone, transparent=(255, 0, 0))
    out = _decode_png(viewer.overlay_img)
    assert out.getpixel((1, 0)) == (255, 0, 0, 0)      # red -> transparent
    assert out.getpixel((0, 0)) == (0, 0, 0, 128)      # black keeps its alpha


def test_set_overlay_transparent_none_keeps_alpha(viewer, _duotone):
    viewer.set_overlay(_duotone, transparent=None)
    out = _decode_png(viewer.overlay_img)
    assert out.getpixel((0, 0)) == (0, 0, 0, 128)      # black kept opaque
    assert out.getpixel((1, 0)) == (255, 0, 0, 128)


def test_set_overlay_transparent_accepts_lists_and_numpy_like(viewer, _duotone):
    # int-like scalars (e.g. numpy ints) and lists are fine
    viewer.set_overlay(_duotone, transparent=[0, 0, 0])
    assert _decode_png(viewer.overlay_img).getpixel((0, 0)) == (0, 0, 0, 0)


def test_set_overlay_bad_transparent_raises(viewer):
    img = _thumb_like()
    for bad in ("x", (0, 0), (0, 0, 0, 0), (-1, 0, 0), (0, 0, 256)):
        with pytest.raises((TypeError, ValueError)):
            viewer.set_overlay(img, transparent=bad)  # type: ignore[arg-type]
    assert viewer.overlay_img == ""  # rejected: state untouched