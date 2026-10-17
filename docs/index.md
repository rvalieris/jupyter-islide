# Docs

Reader map. Everything here describes the system **as it is** — planned and in-progress work
is not documentation and lives outside this directory.

| Document | Covers | Read it when |
|---|---|---|
| [DESIGN.md](DESIGN.md) | The hub: the problem, goals and non-goals, the architecture, coordinates, the rendering pipeline, the JS view, the Python API and its traits, the `SlideBackend` seam | You are changing anything structural, or need the whole picture |
| [GLOSSARY.md](GLOSSARY.md) | The project's vocabulary: one term per concept, each with the synonyms it refuses | Before naming anything — an issue title, a test name, a proposal |
| [annotations.md](annotations.md) | The canonical annotation document: structure, semantics, invariants, import normalization, the edit commands, drawing and vertex editing | You are touching annotations on either side of the wire |
| [openslide-api.md](openslide-api.md) | The OpenSlide contract `islide/backend.py` relies on, and the API behaviour verified against it (out-of-bounds reads, location anchoring, level choice) | You are touching the backend or the read plan |
| [testing.md](testing.md) | The Python and JS suites, the test slide, per-file coverage, and the cross-language trait-contract guard | You are adding or changing tests |
| [packaging.md](packaging.md) | Repo layout, the package vs. wire version rules, dependencies, and the single-`pip install` release build | You are packaging, releasing, or adding a dependency |

`DESIGN.md` links out to the four deep dives, so it is the normal starting point.

User-facing usage lives in [README.md](../README.md) and `examples/`.

`GLOSSARY.md` is binding, not advisory: issue titles, test names, and proposals name domain
concepts with its terms, never the ones it lists as avoided.


