"""islide — interactive whole-slide pathology image viewer for Jupyter."""
from .backend import OpenSlideBackend, SlideBackend
from .cache import TileCache
from .plan import ReadPlan, Tile, plan_viewport, select_level
from .viewport import SlideMeta, Viewport, fit_zoom
from .widget import SlideViewer

__version__ = "0.0.1"

__all__ = [
    "SlideViewer",
    "SlideMeta",
    "Viewport",
    "fit_zoom",
    "TileCache",
    "SlideBackend",
    "OpenSlideBackend",
    "ReadPlan",
    "Tile",
    "plan_viewport",
    "select_level",
]
