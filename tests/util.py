"""Shared test helpers."""


def screen_covered(plan, sx, sy) -> bool:
    """Is screen point (sx, sy) inside some tile's screen box?"""
    for t in plan.tiles:
        l, tp, w, h = t.screen
        if l - 1e-9 <= sx < l + w + 1e-9 and tp - 1e-9 <= sy < tp + h + 1e-9:
            return True
    return False
