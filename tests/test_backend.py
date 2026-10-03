"""Backend + constructor tests for the ``slide=`` constructor argument.

Covers ``OpenSlideBackend.from_object`` (wrapping an already-opened,
duck-typed openslide-API object) and the ``SlideViewer(slide=...)``
constructor — the seam for custom slide libraries that mirror openslide's
OO API over different slide types.

Runs without openslide installed: the fake slide below stands in for the
custom library's object.
"""
from __future__ import annotations

from PIL import Image

import pytest

from islide import OpenSlideBackend, SlideViewer, SlideMeta


class FakeSlide:
    """Minimal openslide-OO-API stand-in over a single PIL image (1 level)."""

    def __init__(self, image: Image.Image, path: str = "fake://slide", mpp: float | None = 0.5):
        self.path = path
        self._img = image
        w, h = image.size
        self.properties = {"openslide.vendor": "fake"}
        if mpp is not None:
            self.properties["openslide.mpp-x"] = f"{mpp}"
        self.dimensions = (w, h)
        self.level_count = 1
        self.level_downsamples = (1.0,)
        self.level_dimensions = ((w, h),)
        self.closed = False

    def read_region(self, location, level, size):
        assert level == 0
        x, y = int(location[0]), int(location[1])
        w, h = int(size[0]), int(size[1])
        return self._img.crop((x, y, x + w, y + h)).copy()

    def get_thumbnail(self, size):
        return self._img.resize(tuple(int(s) for s in size))

    def close(self):
        self.closed = True


def _fake_slide(w=512, h=256):
    img = Image.new("RGBA", (w, h), (10, 200, 30, 255))
    return FakeSlide(img)


# --------------------------------------------------------------- from_object
def test_from_object_wraps_fake_slide():
    fake = _fake_slide()
    try:
        b = OpenSlideBackend.from_object(fake)
        assert isinstance(b.meta, SlideMeta)
        assert b.meta.dimensions == (512, 256)
        assert b.meta.level_count == 1
        assert b.meta.level_downsamples == (1.0,)
        assert b.meta.level_dimensions == ((512, 256),)
        assert b.meta.mpp == 0.5
        assert b.meta.vendor == "fake"
        assert b.path == "fake://slide"
        img = b.read_region((0, 0), 0, (64, 64))
        assert img.size == (64, 64)
        thumb = b.thumbnail((32, 16))
        assert thumb.size == (32, 16)
    finally:
        b.close()
    assert fake.closed


def test_from_object_rejects_non_conforming_object():
    class Almost:
        pass

    with pytest.raises(TypeError, match="not an openslide-compatible"):
        OpenSlideBackend.from_object(Almost())


def test_object_path_prefers_public_path_attr():
    from islide.backend import _object_path

    class HasPath:
        path = "/a/b.svs"

    class HasPrivateFilename:
        _filename = "/c/d.svs"

    assert _object_path(HasPath()) == "/a/b.svs"
    assert _object_path(HasPrivateFilename()) == "/c/d.svs"
    assert _object_path(object()) == ""


def test_from_object_rejects_incomplete_duck():
    class NoThumbnail:  # mirrors a library that forgot get_thumbnail
        pass

    with pytest.raises(TypeError, match="get_thumbnail"):
        OpenSlideBackend.from_object(NoThumbnail())


# ---------------------------------------------------------- widget constructors
def test_slide_viewer_with_fake_slide():
    fake = _fake_slide()
    v = SlideViewer(slide=fake)
    try:
        v.wait()
        assert v.slide_open
        assert v.meta["dimensions"] == [512, 256]
        assert v.meta["mpp"] == 0.5
        assert v.path == "fake://slide"
        plan = v.render()
        assert len(plan.tiles) > 0
        assert v.tiles  # pushed to the synced trait
    finally:
        v.close()
    assert fake.closed


def test_slide_viewer_with_none_raises():
    with pytest.raises(ValueError, match="provide a slide path"):
        SlideViewer()


def test_slide_viewer_rejects_path_and_slide_together():
    with pytest.raises(ValueError, match="not both"):
        SlideViewer("some/path.svs", slide=_fake_slide())
