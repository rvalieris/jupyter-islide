"""jupyter-islide — interactive whole-slide pathology image viewer for Jupyter."""
from importlib.metadata import PackageNotFoundError, version

from .annotations import apply_edit, normalize_ring, parse_annotations
from .backend import OpenSlideBackend, SlideBackend
from .cache import TileCache
from .plan import ReadPlan, Tile, plan_viewport, select_level
from .viewport import SlideMeta, Viewport, fit_zoom
from .widget import SlideViewer


def _jupyter_labextension_paths():
    """Tell JupyterLab where the pre-built widget extension lives.

    The ``hatch-jupyter-builder`` hook builds ``frontend/`` into
    ``frontend/labextension/`` at wheel-build time; that directory sits next
    to this package, hence the ``..`` in ``src``.
    """
    return [{"src": "../frontend/labextension", "dest": "jupyter-islide"}]


try:
    __version__ = version("jupyter-islide")
except PackageNotFoundError:  # package not installed (e.g. running from source)
    __version__ = "0.0.0"

__all__ = [
    "SlideViewer",
    "parse_annotations",
    "normalize_ring",
    "apply_edit",
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
