# Packaging & Dependencies

```
.
├── pyproject.toml            # hatchling + hatch-jupyter-builder hook;
│                             #   deps: ipywidgets>=8, openslide-python>=1.4,
│                             #   pillow>=9; MIT license
├── LICENSE                   # MIT
├── islide/
│   ├── __init__.py           # SlideViewer, Viewport, …
│   │                         # + _jupyter_labextension_paths()
│   ├── widget.py             # the DOMWidget + state machine
│   ├── backend.py            # SlideBackend protocol, OpenSlideBackend
│   ├── plan.py               # viewport -> read plan (pure)
│   ├── annotations.py        # GeoJSON -> canonical annotation document (pure)
│   ├── viewport.py           # SlideMeta + Viewport (pure)
│   ├── cache.py              # TileCache
│   ├── fetch.py              # plan -> tiles (cache + one read, cropped)
│   └── encode.py             # tile -> JPEG data URL
└── frontend/                 # JS canvas view (npm: jupyter-islide)
    ├── tilemath.js  compositor.js  annotations.js  polydraw.js
    ├── blend.js                     # level cross-fade (pure)
    ├── model.js  view.js  defaults.js  wirecheck.js  labextension.js  index.js
    ├── style/index.css
    ├── sync-version.mjs             # keeps the frontend version in sync
    ├── labextension/  # build output (gitignored) — compiled labextension
    └── test/             # node --test (the pure modules)
```

## Versions

- **Package version** (`0.6.x`): `pyproject.toml` is the single source of
  truth; `frontend/package.json` and `frontend/package-lock.json` are kept in
  sync by `frontend/sync-version.mjs`, which also runs as the npm `prebuild`
  hook. A guard test pins all three
  (`tests/test_versions.py`, [docs/testing.md](testing.md)).
- **Widget module version** (`2.1.0`): the `_model_module_version` /
  `_view_module_version` pair, declared on the Python side
  (`islide/widget.py`) and mirrored in `frontend/defaults.js` (`ISLIDE_MODULE_VERSION`,
  pinned by `frontend/test/defaults.test.js`). It is the **wire** version —
  bumped when the trait contract changes (the `annotations` trait's type
  became the canonical annotation document at `2.0.0`; the per-chunk
  `tiles`/`tile_geo` push and the `resync` attach counter at `2.1.0`),
  independent of the package version.

## Dependencies

- **Python**: `ipywidgets>=8`, `openslide-python>=1.4` (uses the OO API —
  [docs/openslide-api.md](openslide-api.md)), `pillow>=9`.
- **System lib**: `libopenslide` — the conda-forge `openslide` package,
  `libopenslide0` on Debian/Ubuntu, or `pip install openslide-bin` (a
  libopenslide wheel for environments where the system package is
  inconvenient).
- **JS**: npm package `jupyter-islide` (the wire module name registered with
  the widget registry is `jupyter-islide`), no runtime dependency beyond
  `@jupyter-widgets/base` (declared a shared/singleton package so it is never
  bundled into the extension bundle).

## Release packaging: one `pip install`, Python + extension

The wheel is built by `hatchling` with the `hatch-jupyter-builder` hook
(`[tool.hatch.build.hooks.jupyter-builder]` in `pyproject.toml`). At wheel
build time the hook runs `npm install` + `npm run build` in `frontend/` —
i.e. `node sync-version.mjs` (the npm `prebuild` hook) +
`jupyter-builder build .` — producing `frontend/labextension/`
(the `outputDir` in `frontend/package.json`). hatchling's `shared-data` then
places that directory at `share/jupyter/labextensions/jupyter-islide/` inside
the wheel, the standard JupyterLab 4 labextensions discovery path. So:

- `pip install jupyter-islide` installs **both** the Python package and the
  pre-built extension in one step (no end-user npm).
- The build env gets `jupyterlab==4.*` automatically (declared in
  `[build-system] requires`); Node/npm must be on the PATH.
- `islide/_jupyter_labextension_paths()` (matching the ipyleaflet pattern)
  points at `../frontend/labextension` → `dest: jupyter-islide`, which is what
  `jupyter-builder develop islide` uses for the editable live-reload
  workflow.
- Mirrors ipyleaflet's mechanism (hatch `jupyter-builder` hook +
  `shared-data`); a single package (`jupyter-islide`) rather than splitting
  into a pure-Python package + a separate extension package.

## Development workflow

- **Python-only changes**: `pip install -e .` (or a plain venv over the
  tree); the frontend is untouched.
- **Frontend live reload**: `jupyter-builder develop islide` (builds the
  extension in place and registers it for the dev server; re-runs the npm
  build on each `jupyter lab` start).
- **Release**: `python -m build --wheel` from a clean tree — the
  jupyter-builder hook does the npm build; the resulting wheel carries the
  extension under `share/jupyter/labextensions/jupyter-islide/`.
