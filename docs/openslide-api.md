# OpenSlide / openslide-python API notes

Verified against openslide-python 1.4.6 / libopenslide 4.0.1 (the generic-TIFF
vendor, `data/CMU-1.tiff`). `islide/backend.py` is the only module that
touches openslide; the notes below are the contract it relies on.

- `openslide.open_slide(path) -> OpenSlide` (object-oriented API; the
  C-style `lowlevel` module also exists).
- **Properties, not callables** (easy to get wrong — older docs show the C
  call style):
  - `slide.dimensions -> (w, h)` (level 0)
  - `slide.level_count -> int`
  - `slide.level_downsamples -> (1.0, 2.0, 4.0, …)`
  - `slide.level_dimensions -> ((w0,h0), (w1,h1), …)`
- `slide.read_region(location=(x,y), level=L, size=(w,h)) -> PIL.Image`
  (mode `RGBA`). `location` is in level-0 coordinates; `size` is the region
  size (pixels at level L).
  - **Out-of-bounds** (verified, generic-tiff / libopenslide 4.0.1): the
    return is always exactly `size`, and out-of-bounds areas are filled with
    *transparent black* — compositing onto white before JPEG gives the
    expected white background at the slide edge. Fill color is
    vendor-dependent, but "always exactly `size`" holds.
  - **Location anchor** (verified, generic-tiff / 4.0.1): the returned
    image's top-left is level pixel `floor(location / ds)` per axis
    (unaligned level-0 locations are accepted). Therefore to anchor a read
    at exactly level pixel `p` use `location = ceil(p * ds)` (see
    `islide/plan.py`'s grid anchor; tested for the real downsample factors
    of the test slide, including the non-integer ones).
- `slide.get_best_level_for_downsample(ds) -> int` — picks the *coarsest*
  level with `ds_level <= requested` (verified: it can return a level that
  up-samples). islide uses its own never-up-scale selector instead: smallest
  `L` with `ds[L] >= 1/zoom` (see `islide/plan.py`'s `select_level`).
- `slide.get_thumbnail(size=(w,h)) -> PIL.Image`
- `slide.properties` dict carries `openslide.mpp-x/y`,
  `openslide.objective-power`, `openslide.vendor`, per-level dims/ds, …
- `slide.color_profile` exists; islide ignores color management and
  documents it (no ICC processing in the pipeline).
