"""Regression tests for the notebook re-run / display-timing scenarios:
a view that attaches before the slide has finished opening, and re-running
the construction cell (a new widget while the previous one may still be
open). Both must keep serving tiles when the user zooms.
"""
from __future__ import annotations

import time

from islide import SlideViewer

from test_widget_chunks import BIG, FakeSlide


def settle(v, timeout=10.0):
    deadline = time.monotonic() + timeout
    while v._rendering_bg and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not v._rendering_bg


FIT = {"cx": 2048, "cy": 2048, "zoom": 1.0, "canvas_w": 960, "canvas_h": 540}
ZOOMED = {"cx": 1536, "cy": 1536, "zoom": 4.0, "canvas_w": 960, "canvas_h": 540}


def zoom_pushes(v):
    pushes = []
    v.observe(lambda c: pushes.append(c["new"]), names="tiles")
    v.viewport = dict(ZOOMED)
    settle(v)
    return pushes


def union_of(pushes):
    union = {}
    for p in pushes:
        union.update(p)
    return union


def test_attach_before_open_then_zoom():
    """The view attaches (resync bump) before `slide_open` arrives; the
    open's render then streams in, and a later zoom still pushes."""
    v = SlideViewer(slide=FakeSlide(BIG))
    v.resync = v.resync + 1  # JS attach bump, before open completes
    v.wait()
    settle(v)
    v.viewport = dict(FIT)  # the view's fit echo (JS-originated)
    settle(v)
    pushes = zoom_pushes(v)
    assert len(pushes) >= 1, "no tile pushes on zoom after attach-before-open"
    assert union_of(pushes)


def test_rerun_cell_while_previous_widget_open():
    """Re-running the construction cell creates a second widget while the
    first (zombie) comm may still exist; the new widget must still zoom."""
    a = SlideViewer(slide=FakeSlide(BIG))
    a.wait()
    a.resync = a.resync + 1
    settle(a)
    a.viewport = dict(FIT)
    settle(a)

    b = SlideViewer(slide=FakeSlide(BIG))
    b.wait()
    b.resync = b.resync + 1
    settle(b)

    pushes = zoom_pushes(b)
    assert len(pushes) >= 1, "no tile pushes on zoom of the re-run widget"
    assert union_of(pushes)

    a.close()
