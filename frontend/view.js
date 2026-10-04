/**
 * islide canvas view (M1).
 *
 * Owns: the DOM (canvas + minimap + toolbar), all mouse/keyboard input, the
 * local pan/zoom transform (smooth between round-trips), and the canvas
 * compositor. Python owns: the slide, tile planning/fetch/cache/encode.
 *
 * Data flow:
 *   user input -> local transform (instant draw)
 *              -> debounced `viewport` trait (Python plans + fetches tiles)
 *   Python     -> `tiles` (data URLs) + `tile_geo` (level-space rects)
 *              -> decoded image cache -> canvas
 *   Python     -> `annotations` (canonical document; M3.5) -> overlay pass
 */
import { DOMWidgetView } from '@jupyter-widgets/base';
import * as math from './tilemath.js';
import * as blend from './blend.js';
import { drawScene, drawOverlay } from './compositor.js';
import {
  CLICK_THRESHOLD_PX, MODE_DRAWING, MODE_IDLE, drawDraftPolygon,
  hitTestDraftVertex, polyDrawInit, polyEvent,
} from './polydraw.js';
import {
  drawAnnotations, drawVertexHandles, featurePositions, hitTest,
  hitTestVertex, withMovedVertex,
} from './annotations.js';
import './style/index.css';

const SYNC_DEBOUNCE_MS = 120;
const MAX_CACHED_IMAGES = 400;
const MIN_ZOOM_PAD = 4; // min zoom = fit / 4 (matches the Python side)
const MAX_ZOOM = 16;

export class SlideView extends DOMWidgetView {
  render() {
    super.render();
    this._transform = null;
    this._images = new Map();
    this._tileGeo = {}; // M5: accumulated tile geometry (see _onTileGeoChange)
    this._blend = null; // M5: in-flight level cross-fade (blend.js state)
    this._overlayImg = null; // decoded full-slide overlay image (trait: overlay_img)
    this._lastSentViewport = null;
    this._syncTimer = null;
    this._drawQueued = false;
    this._dragging = null;
    this._cursor = null; // canvas-relative pointer pos, drives the l0 readout
    this._annotAlpha = 1; // view-local overlay opacity (toolbar slider)
    // M4 annotation editing (DESIGN.md §6.5): the selected feature id
    // (view-local, view state); `annotation_edit` is the JS->Py wire.
    this._selectedId = null;
    // M3 polygon drawing (DESIGN.md §6.4): local drawing state (never
    // synced); `last_polygon` is the only JS->Py write.
    this._poly = polyDrawInit();
    // M6 vertex editing (DESIGN.md §6.7): `_vertexDrag` is the grabbed
    // vertex (null when no handle was pressed): {kind: 'draft' | 'feature',
    // id? (feature only), index}; `_vertexMove` is the live local preview
    // of a dragged saved-feature vertex. It is kept after the release
    // commit (drawing from the pushed set would flash the old position):
    // cleared by the `annotations` push (accepted move) or by the
    // `edit ignored` status (refused move — no push comes).
    this._vertexDrag = null;
    this._vertexMove = null;
    this._localStatus = null; // view-local status line, overrides the trait

    // Must exist before _buildDom(): that is where observe(this._canvas)
    // happens, and only the RO keeps the transform (and hence the tile
    // reprojection) in step when the container resizes (sidebar open/close,
    // panel drags) between user interactions.
    this._resizeObserver =
      typeof ResizeObserver !== 'undefined'
        ? new ResizeObserver(() => this._onResize())
        : null;
    this._buildDom();
    this._bindModel();
    this._bindEvents();
    this.displayed.then(() => {
      this._onResize();
      this._maybeSendInitialViewport();
      this._requestDraw();
    });
  }

  // ------------------------------------------------------------------ DOM
  _buildDom() {
    this.el.classList.add('islide-view');
    this.el.innerHTML = `
      <div class="islide-canvas-wrap">
        <canvas class="islide-canvas" data-jp-suppress-context-menu></canvas>
        <canvas class="islide-annotations"></canvas>
        <div class="islide-minimap">
          <img class="islide-minimap-img" alt="minimap"/>
          <canvas class="islide-minimap-rect"></canvas>
        </div>
      </div>
      <div class="islide-toolbar">
        <button class="islide-btn" data-action="fit">fit</button>
        <button class="islide-btn" data-action="1:1">1:1</button>
        <button class="islide-btn" data-action="annotate" aria-pressed="false">annotate</button>
        <button class="islide-btn" data-action="del" disabled>del</button>
        <button class="islide-btn" data-action="label" disabled>label</button>
        <button class="islide-btn" data-action="color" disabled>color</button>
        <label class="islide-alpha" title="annotation opacity">
          <span class="islide-alpha-label">α</span>
          <input class="islide-alpha-input" type="range" min="0" max="1"
                 step="0.05" value="1"/>
        </label>
        <span class="islide-readout"></span>
        <span class="islide-cursor" title="cursor position, level-0 px"></span>
        <span class="islide-status"></span>
      </div>`;
    this._canvasWrap = this.el.querySelector('.islide-canvas-wrap');
    this._canvas = this.el.querySelector('.islide-canvas');
    this._annotCanvas = this.el.querySelector('.islide-annotations');
    this._alphaInput = this.el.querySelector('.islide-alpha-input');
    this._minimapWrap = this.el.querySelector('.islide-minimap');
    this._minimapImg = this.el.querySelector('.islide-minimap-img');
    this._minimapRect = this.el.querySelector('.islide-minimap-rect');
    this._readout = this.el.querySelector('.islide-readout');
    this._cursorEl = this.el.querySelector('.islide-cursor');
    this._status = this.el.querySelector('.islide-status');
    // M4: the toolbar element and its buttons (annotate = the A-key draw
    // toggle; del/label/color act on the selected feature, disabled until
    // a selection exists).
    this._toolbarEl = this.el.querySelector('.islide-toolbar');
    this._annotateBtn = this.el.querySelector('[data-action="annotate"]');
    this._actionBtns = {
      del: this.el.querySelector('[data-action="del"]'),
      label: this.el.querySelector('[data-action="label"]'),
      color: this.el.querySelector('[data-action="color"]'),
    };
    // M3: keyboard-driven drawing — the root holds keyboard focus (a click
    // on the canvas area focuses the nearest focusable ancestor, this
    // div); see the keydown binding in _bindEvents.
    this.el.tabIndex = 0;
    if (this._resizeObserver) {
      this._resizeObserver.observe(this._canvas);
    }
    this._applyCanvasHeight();
  }

  // ------------------------------------------------------- model bindings
  _bindModel() {
    this.listenTo(this.model, 'change:viewport', this._onViewportChange);
    this.listenTo(this.model, 'change:tiles', this._onTilesChange);
    this.listenTo(this.model, 'change:tile_geo', this._onTileGeoChange);
    this.listenTo(this.model, 'change:meta', this._onMetaChange);
    this.listenTo(this.model, 'change:slide_open', this._onSlideOpen);
    this.listenTo(this.model, 'change:minimap_img', this._onMinimapChange);
    this.listenTo(this.model, 'change:status', this._onStatusChange);
    this.listenTo(this.model, 'change:canvas_h', this._applyCanvasHeight);
    this.listenTo(this.model, 'change:annotations', this._onAnnotationsChange);
    this.listenTo(this.model, 'change:overlay_img', this._onOverlayImgChange);
    this.listenTo(this.model, 'change:overlay_alpha', this._requestDraw);
    this._onMetaChange();
    this._onSlideOpen();
    this._onMinimapChange();
    this._onStatusChange();
    this._onOverlayImgChange();
    // Seed the image cache from whatever tiles the model already holds:
    // the kernel's initial render usually completes before this view is
    // attached, so no change:tiles event fires for them. Without this the
    // first draw renders empty (m:all) until the viewport happens to
    // change the plan (a later push then merges), leaving the initial
    // viewport blank.
    this._mergeTiles();
    // M5: seed the accumulated tile geometry the same way — no
    // change:tile_geo event fires for the kernel's initial render either.
    this._onTileGeoChange();
  }

  _onViewportChange() {
    const vp = this.model.get('viewport');
    if (vp) {
      // Apply Python's viewport (programmatic API / initial fit). Never
      // re-send from here: interaction-only updates are the sole writers
      // of the `viewport` trait from the JS side.
      this._transform = math.viewportToTransform(vp);
    }
    this._requestDraw();
  }

  _onTilesChange() {
    this._mergeTiles();
    this._requestDraw();
  }

  /** M5: accumulate tile geometry across pushes — Python pushes the
   * center chunk first and then the full set, and drawScene draws
   * everything accumulated (not just the latest push, which would blank
   * the canvas behind the incoming center chunk). Also records the level
   * cross-fade: when the selected pyramid level (math.selectLevel on the
   * local zoom, mirroring Python's select_level) changes between pushes,
   * the old level fades out over blend.BLEND_MS while the new level draws
   * at full opacity. The second push of a two-stage render re-enters with
   * the same selected level, so the in-flight fade is kept, not reset.
   */
  _onTileGeoChange() {
    const geo = this.model.get('tile_geo') || {};
    for (const key of Object.keys(geo)) {
      this._tileGeo[key] = geo[key];
    }
    const meta = this.model.get('meta');
    const t = this._transform;
    if (meta && t) {
      const sel = math.selectLevel(meta.level_downsamples, t.zoom);
      if (this._blend === null || this._blend.to !== sel) {
        const prev = this._blend ? this._blend.to : sel;
        this._blend = blend.startTransition(prev, sel, Date.now());
      }
    }
    this._requestDraw();
  }

  _onMetaChange() {
    this._maybeSendInitialViewport();
  }

  _onSlideOpen() {
    this._maybeSendInitialViewport();
  }

  _onMinimapChange() {
    const url = this.model.get('minimap_img');
    if (url) {
      this._minimapImg.src = url;
    }
    this._layoutMinimap();
  }

  /** Overlay (heatmap) image: decode the trait's PNG data URL once;
   * re-decode only when a different overlay is pushed; '' removes it.
   */
  _onOverlayImgChange() {
    const url = this.model.get('overlay_img') || '';
    if (!url) {
      this._overlayImg = null;
      this._requestDraw();
      return;
    }
    if (this._overlayImg && this._overlayImg.src === url) return;
    const img = new Image();
    img.onload = () => {
      img._ready = true;
      this._requestDraw();
    };
    img.onerror = () => {
      console.error('[islide] overlay image FAILED to decode');
    };
    img.src = url;
    this._overlayImg = img;
    this._requestDraw();
  }

  _onStatusChange() {
    // M6: a refused vertex move (`edit ignored: ...`) leaves the pushed
    // document unchanged and pushes nothing — drop the pending preview
    // so the drawn document and the handles agree again (the accepted
    // path is cleared by the `annotations` push instead).
    if (this._vertexMove !== null
        && String(this.model.get('status')).startsWith('edit ignored')) {
      this._vertexMove = null;
      this._requestDraw();
    }
    this._updateStatus();
  }

  /** View-local status line (M3 drawing messages); overrides the trait. */
  _setLocalStatus(text) {
    this._localStatus = text;
    this._updateStatus();
  }

  /** Any annotations push re-validates the M4 selection: a selected id
   * that is no longer in the set (clear_annotations(), a set_annotations()
   * replace, a delete round-trip) clears it; the del/label/color buttons
   * track the selection either way. It also drops the M6 pending vertex-move
   * preview: the pushed document (an accepted set_vertex, a
   * set_annotations() replace, ...) is what gets drawn from now on. (A
   * refused move pushes nothing; its `edit ignored` status clears the
   * preview instead — see _onStatusChange.)
   */
  _onAnnotationsChange() {
    // Drop the M6 pending vertex-move preview — unless a vertex drag is
    // live right now, where `_vertexMove` is that drag's *live* preview and
    // the arriving push is from an earlier commit (keep the live one; it
    // is retired by its own round-trip when this drag releases).
    if (this._vertexDrag === null) {
      this._vertexMove = null;
    }
    if (this._selectedId !== null) {
      const doc = this.model.get('annotations');
      const features = doc && Array.isArray(doc.features) ? doc.features : [];
      if (!features.some((f) => f && f.id === this._selectedId)) {
        this._selectedId = null;
      }
    }
    this._updateAnnotationButtons();
    this._requestDraw();
  }

  /** Stale drawing messages (cancel/discard) clear on the next interaction.
   */
  _clearLocalStatusIfIdle() {
    if (this._poly.mode !== MODE_DRAWING && this._localStatus !== null) {
      this._localStatus = null;
      this._updateStatus();
    }
  }

  _updateStatus() {
    this._status.textContent = this._localStatus !== null
      ? this._localStatus
      : (this.model.get('status') || '');
  }

  /**
   * Apply the user-set viewport height (Python `canvas_h` trait, CSS px).
   * The ResizeObserver then re-syncs the resized viewport to Python, which
   * re-plans the tiles at the new canvas size.
   */
  _applyCanvasHeight() {
    const h = this.model.get('canvas_h');
    if (h) {
      this._canvasWrap.style.height = `${h}px`;
    }
  }

  _mergeTiles() {
    const tiles = this.model.get('tiles');
    for (const [key, url] of Object.entries(tiles)) {
      if (this._images.has(key)) continue;
      const img = new Image();
      img.onload = () => {
        img._ready = true;
        this._requestDraw();
      };
      img.onerror = () => {
        console.error('[islide] tile image FAILED to decode', key);
        this._requestDraw();
      };
      img.src = url;
      this._images.set(key, img);
    }
    while (this._images.size > MAX_CACHED_IMAGES) {
      const key = this._images.keys().next().value;
      this._images.delete(key);
      delete this._tileGeo[key]; // M5: evict geometry in lockstep with images
    }
  }

  // ------------------------------------------------------------- viewport
  _zoomBounds() {
    const meta = this.model.get('meta');
    const t = this._transform;
    const fit = meta && t ? math.fitZoom(meta, t.canvasW, t.canvasH) : 0;
    return [fit / MIN_ZOOM_PAD, MAX_ZOOM];
  }

  _maybeSendInitialViewport() {
    const meta = this.model.get('meta');
    if (
      !meta ||
      !this.model.get('slide_open') ||
      !this.el.clientWidth ||
      this._lastSentViewport
    ) {
      return;
    }
    const t = {
      cx: meta.dimensions[0] / 2,
      cy: meta.dimensions[1] / 2,
      zoom: math.fitZoom(meta, this._canvas.clientWidth, this._canvas.clientHeight),
      canvasW: this._canvas.clientWidth,
      canvasH: this._canvas.clientHeight,
    };
    this._transform = t;
    this._sendViewport();
    this._requestDraw();
  }

  /** Debounce interaction-driven viewport updates to Python. */
  _scheduleSync() {
    if (this._syncTimer !== null) return;
    this._syncTimer = window.setTimeout(() => {
      this._syncTimer = null;
      this._sendViewport();
    }, SYNC_DEBOUNCE_MS);
  }

  _sendViewport() {
    if (!this._transform) return;
    const vp = math.transformToViewport(this._transform);
    if (this._lastSentViewport && vp.cx === this._lastSentViewport.cx &&
        vp.cy === this._lastSentViewport.cy &&
        vp.zoom === this._lastSentViewport.zoom &&
        vp.canvas_w === this._lastSentViewport.canvas_w &&
        vp.canvas_h === this._lastSentViewport.canvas_h) {
      return;
    }
    this._lastSentViewport = vp;
    this.model.set('viewport', vp);
    this.model.save();
  }

  // ---------------------------------------------------------------- events
  _bindEvents() {
    // The right mouse button is a pan gesture on the canvas (both idle and
    // drawing modes), so keep context menus out of it. JupyterLab decides
    // via `event.target.closest('[data-jp-suppress-context-menu]')` (see
    // the attribute on the canvas) before opening its menu, which is why
    // this preventDefault alone does not stop it; it also covers older
    // JupyterLab versions and the browser's native menu.
    this._canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    // M3 keyboard (DESIGN.md §6.4): A toggles polygon drawing, Esc cancels
    // it. While the alpha input has focus its keys are left alone.
    this.el.addEventListener('keydown', (e) => {
      // Keys typed in a toolbar input (the alpha slider, the M4 label /
      // color editors) are the input's own business.
      if (e.target && e.target.tagName === 'INPUT') return;
      if (e.key === 'a' || e.key === 'A') {
        e.preventDefault();
        this._toggleDrawMode();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this._cancelDrawMode();
      }
    });

    this._canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      this._clearLocalStatusIfIdle();
      if (!this._transform) return;
      const factor = Math.exp(-e.deltaY * 0.002);
      const rect = this._canvas.getBoundingClientRect();
      const [minZoom, maxZoom] = this._zoomBounds();
      this._transform = math.zoomAtCursor(
        this._transform, factor,
        e.clientX - rect.left, e.clientY - rect.top,
        minZoom, maxZoom,
      );
      this._requestDraw();
      this._scheduleSync();
    }, { passive: false });

    this._canvas.addEventListener('pointerdown', (e) => {
      if (!this._transform) return;
      this._canvas.setPointerCapture(e.pointerId);
      // M3 click vs pan: a pointerup within ~4 CSS px of the pointerdown is
      // a click (a draft vertex in drawing mode); beyond that the gesture
      // is a pan (left or right drag).
      this._dragging = {
        x: e.clientX, y: e.clientY,
        sx: e.clientX, sy: e.clientY,
        button: e.button,
        moved: false,
      };
      // M6: a press on a vertex handle grabs the vertex (DESIGN.md §6.7):
      // the drag moves the vertex, never pans, and the still-click on
      // release neither adds a draft vertex nor re-selects.
      this._vertexDrag = this._grabVertex(e);
    });
    this._canvas.addEventListener('pointermove', (e) => {
      const rect = this._canvas.getBoundingClientRect();
      this._cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      this._updateCursorReadout();
      if (!this._dragging || !this._transform) {
        // Not panning: in drawing mode the dashed closure segments track
        // the cursor, so repaint on every move.
        if (this._poly.mode === MODE_DRAWING) this._requestDraw();
        return;
      }
      const dx = e.clientX - this._dragging.x;
      const dy = e.clientY - this._dragging.y;
      this._dragging.x = e.clientX;
      this._dragging.y = e.clientY;
      this._dragging.moved = true;
      if (this._vertexDrag) {
        // M6: the grabbed vertex follows the cursor (no pan).
        const [lx, ly] = math.screenToL0(
          this._transform, e.clientX - rect.left, e.clientY - rect.top);
        if (this._vertexDrag.kind === 'draft') {
          this._poly = polyEvent(this._poly, {
            type: 'move_vertex',
            index: this._vertexDrag.index, x: lx, y: ly,
          }).state;
        } else {
          // Live preview over the saved document; Python stays the
          // authority (release commits `set_vertex`).
          this._vertexMove = {
            id: this._vertexDrag.id,
            index: this._vertexDrag.index, x: lx, y: ly,
          };
        }
        this._requestDraw();
      } else {
        this._transform = math.panTransform(this._transform, dx, dy);
        this._requestDraw();
      }
    });
    const endDrag = (e) => {
      if (!this._dragging) return;
      const d = this._dragging;
      this._dragging = null;
      const vd = this._vertexDrag;
      this._vertexDrag = null;
      const dist = Math.hypot(e.clientX - d.sx, e.clientY - d.sy);
      if (vd) {
        // M6: the gesture started on a vertex handle (DESIGN.md §6.7):
        // no pan happened, and the still-click release neither adds a
        // draft vertex nor re-selects.
        if (vd.kind === 'feature' && d.moved && this._vertexMove) {
          // Commit the move: JS->Py last-event slot, Python re-validates
          // (a degenerate move is refused, the document unchanged) and
          // pushes the new document. `_vertexMove` stays up in the
          // meantime — the pushed set still carries the pre-move position,
          // and clearing it here would draw the shape at its old place for
          // a few frames. The round-trip resolves it: the `annotations`
          // push (accepted) or the `edit ignored` status (refused).
          this.model.set('annotation_edit', {
            op: 'set_vertex', id: vd.id,
            index: vd.index, x: this._vertexMove.x, y: this._vertexMove.y,
          });
          this.model.save();
        }
        // No else-clear: a still click (`!d.moved`) or a draft drag never
        // touched `_vertexMove` — if it is non-null here it is a previous
        // drag's still-pending preview, and only the round-trip (push or
        // `edit ignored` status) may retire it.
        this._requestDraw();
      } else if (d.moved) {
        this._scheduleSync();
      }
      if (!vd && dist < CLICK_THRESHOLD_PX && d.button === 0
          && this._poly.mode === MODE_DRAWING && this._transform) {
        // A still left click in drawing mode: append the cursor's slide
        // position (unclamped level-0 px) as the next draft vertex.
        // M6: the selection goes with the new draft (its first vertex
        // supersedes it; later vertices are no-ops).
        if (this._selectedId !== null) {
          this._selectedId = null;
          this._updateAnnotationButtons();
        }
        const rect = this._canvas.getBoundingClientRect();
        const [x, y] = math.screenToL0(
          this._transform, e.clientX - rect.left, e.clientY - rect.top);
        this._poly = polyEvent(this._poly, { type: 'vertex', x, y }).state;
        this._requestDraw();
      } else if (!vd && dist < CLICK_THRESHOLD_PX && d.button === 0
          && this._poly.mode === MODE_IDLE && this._transform) {
        // M4: a still left click in idle mode selects the topmost
        // annotation under the cursor (a miss deselects).
        const rect = this._canvas.getBoundingClientRect();
        const id = hitTest(
          this.model.get('annotations') || {},
          this._transform,
          e.clientX - rect.left,
          e.clientY - rect.top,
        );
        if (id !== this._selectedId) {
          this._selectedId = id;
          this._updateAnnotationButtons();
          this._requestDraw();
        }
      }
      this._clearLocalStatusIfIdle();
      // Touch pointers vanish on release: nothing is hovering anymore.
      if (e.pointerType !== 'mouse') {
        this._cursor = null;
        this._updateCursorReadout();
      }
    };
    this._canvas.addEventListener('pointerup', endDrag);
    this._canvas.addEventListener('pointercancel', endDrag);
    this._canvas.addEventListener('pointerleave', () => {
      this._cursor = null;
      this._updateCursorReadout();
      if (this._poly.mode === MODE_DRAWING) this._requestDraw();
    });

    this._toolbarEl.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn || !this._transform) return;
      this._toolbarAction(btn.dataset.action);
    });

    // View-local annotation opacity (not synced; display state only).
    this._alphaInput.addEventListener('input', () => {
      this._annotAlpha = Number(this._alphaInput.value);
      this._requestDraw();
    });

    // Minimap: click/drag to move the view center.
    const mini = this._minimapWrap;
    mini.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      mini.setPointerCapture(e.pointerId);
      this._minimapJump(e);
      const move = (ev) => this._minimapJump(ev);
      const up = () => {
        mini.removeEventListener('pointermove', move);
        mini.removeEventListener('pointerup', up);
      };
      mini.addEventListener('pointermove', move);
      mini.addEventListener('pointerup', up);
    });
  }

  // -------------------------------------------------- M3 polygon drawing
  _toggleDrawMode() {
    const entering = this._poly.mode !== MODE_DRAWING;
    if (entering && !this.model.get('slide_open')) {
      // A before the slide is open: no-op with a reason.
      this._setLocalStatus('Slide not open');
      return;
    }
    const { state, result } = polyEvent(this._poly, { type: 'toggle' });
    this._poly = state;
    this._canvas.classList.toggle('islide-drawing', state.mode === MODE_DRAWING);
    this._annotateBtn.setAttribute(
      'aria-pressed', String(state.mode === MODE_DRAWING));
    // M6: the selection is preserved across the mode toggle: in drawing
    // mode the selected feature's vertex handles stay up, so a selected
    // annotation can be entered-annotate -> drag-vertex edited without
    // re-selecting (the first draft vertex still supersedes it).
    if (result && result.op === 'save') {
      // JS->Py last-event wire (DESIGN.md §6.4): open ring, level-0 px,
      // unclamped. Python normalizes, appends, and reports via `status`.
      this.model.set('last_polygon', result.ring);
      this.model.save();
      this._setLocalStatus(null);
    } else if (result && result.op === 'discard') {
      this._setLocalStatus('Discarded: polygon needs ≥ 3 non-collinear points');
    } else {
      this._setLocalStatus('Polygon: click to add points — A saves, Esc cancels');
    }
    this._requestDraw();
  }

  _cancelDrawMode() {
    const { state, result } = polyEvent(this._poly, { type: 'cancel' });
    if (!result) return;
    this._poly = state;
    this._canvas.classList.toggle('islide-drawing', false);
    this._setLocalStatus('Polygon cancelled');
    this._requestDraw();
  }

  _toolbarAction(action) {
    this._clearLocalStatusIfIdle();
    if (action === 'annotate') {
      this._toggleDrawMode();
      return;
    }
    if (action === 'del' || action === 'label' || action === 'color') {
      this._annotationAction(action);
      return;
    }
    const t = this._transform;
    const meta = this.model.get('meta');
    switch (action) {
      case 'fit':
        this._transform = {
          cx: meta.dimensions[0] / 2,
          cy: meta.dimensions[1] / 2,
          zoom: math.fitZoom(meta, t.canvasW, t.canvasH),
          canvasW: t.canvasW,
          canvasH: t.canvasH,
        };
        break;
      case '1:1':
        this._transform = math.makeTransform(
          t.cx, t.cy, 1, t.canvasW, t.canvasH);
        break;
    }
    this._requestDraw();
    this._scheduleSync();
  }

  // --------------------------------------------- M4: annotation editing
  _selectedFeature() {
    if (this._selectedId === null) return null;
    const doc = this.model.get('annotations');
    const features = doc && Array.isArray(doc.features) ? doc.features : [];
    return features.find((f) => f && f.id === this._selectedId) || null;
  }

  /**
   * M6 (DESIGN.md §6.7): the vertex handle pressed at pointerdown —
   * {kind: 'draft' | 'feature', id? (feature only), index} or null.
   * Draft vertices grab in drawing mode (before the selected feature's,
   * since a press can sit on both); the selected feature's vertices grab
   * in both modes (that is what makes a saved annotation draggable).
   */
  _grabVertex(e) {
    if (e.button !== 0 || !this._transform) return null;
    const rect = this._canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (this._poly.mode === MODE_DRAWING && this._poly.draft.length) {
      const i = hitTestDraftVertex(this._poly.draft, this._transform, x, y);
      if (i !== null) return { kind: 'draft', index: i };
    }
    const f = this._selectedFeature();
    if (f) {
      const i = hitTestVertex(f, this._transform, x, y);
      if (i !== null) return { kind: 'feature', id: f.id, index: i };
    }
    return null;
  }

  _updateAnnotationButtons() {
    const selected = this._selectedId !== null;
    for (const b of Object.values(this._actionBtns)) {
      b.disabled = !selected;
    }
  }

  /** del / label / color buttons (DESIGN.md §6.5): the edit commands,
   * issued through the `annotation_edit` last-event slot.
   */
  _annotationAction(action) {
    if (this._selectedId === null) return;
    const id = this._selectedId;
    if (action === 'del') {
      this._selectedId = null;
      this._updateAnnotationButtons();
      this.model.set('annotation_edit', { op: 'delete', id });
      this.model.save();
      this._requestDraw();
      return;
    }
    const f = this._selectedFeature();
    if (!f) return;
    if (action === 'label') this._openLabelEditor(f);
    else this._openColorEditor(f);
  }

  /** Inline label editor (toolbar, next to the label button): pre-filled
   * from the feature; Enter/blur commits, Esc cancels, empty-after-trim
   * commits null (clears the label). The selection survives the edit.
   */
  _openLabelEditor(f) {
    const props = f.properties || {};
    const current = typeof props.label === 'string' ? props.label : '';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'islide-label-input';
    input.value = current;
    input.placeholder = 'label';
    let closed = false;
    const onBlurred = () => finish(true);
    const finish = (commit) => {
      if (closed) return;
      closed = true;
      input.removeEventListener('blur', onBlurred);
      if (commit) {
        const text = input.value;
        this.model.set('annotation_edit', {
          op: 'set_label',
          id: this._selectedId,
          label: text.trim() === '' ? null : text,
        });
        this.model.save();
      }
      if (input.parentNode) input.parentNode.removeChild(input);
    };
    const onKey = (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') finish(true);
      else if (e.key === 'Escape') finish(false);
    };
    input.addEventListener('keydown', onKey);
    input.addEventListener('blur', onBlurred);
    this._toolbarEl.appendChild(input);
    input.focus();
    input.select();
  }

  /** Inline color editor: native stroke/fill pickers + a clear-fill
   * checkbox (a native color input cannot encode transparent). Pre-filled
   * from the feature (null -> default black / clear-fill); each change
   * commits the full {color, fill} pair and closes.
   */
  _openColorEditor(f) {
    const props = f.properties || {};
    const isHex = (v) => typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v);
    const strokeInput = document.createElement('input');
    strokeInput.type = 'color';
    strokeInput.title = 'stroke';
    strokeInput.value = isHex(props.color) ? props.color : '#000000';
    const fillInput = document.createElement('input');
    fillInput.type = 'color';
    fillInput.title = 'fill';
    fillInput.value = isHex(props.fill) ? props.fill : '#000000';
    const clearFill = document.createElement('input');
    clearFill.type = 'checkbox';
    clearFill.checked = !isHex(props.fill);
    clearFill.title = 'clear fill';
    const clearFillLabel = document.createElement('label');
    clearFillLabel.className = 'islide-clear-fill';
    clearFillLabel.append(clearFill, ' clear fill');
    const box = document.createElement('span');
    box.className = 'islide-color-edit';
    box.append(strokeInput, fillInput, clearFillLabel);
    let closed = false;
    const commit = () => {
      if (closed) return;
      closed = true;
      this.model.set('annotation_edit', {
        op: 'set_color',
        id: this._selectedId,
        color: strokeInput.value,
        fill: clearFill.checked ? null : fillInput.value,
      });
      this.model.save();
      if (box.parentNode) box.parentNode.removeChild(box);
    };
    strokeInput.addEventListener('change', commit);
    // Picking a fill color wins over "clear fill": the checkbox follows
    // the picker, so the chosen color is what gets committed.
    fillInput.addEventListener('change', () => {
      clearFill.checked = false;
      commit();
    });
    clearFill.addEventListener('change', commit);
    this._toolbarEl.appendChild(box);
  }

  _minimapJump(e) {
    this._clearLocalStatusIfIdle();
    const meta = this.model.get('meta');
    const t = this._transform;
    if (!meta || !t) return;
    const rect = this._minimapImg.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / rect.width;
    const fy = (e.clientY - rect.top) / rect.height;
    this._transform = math.makeTransform(
      fx * meta.dimensions[0],
      fy * meta.dimensions[1],
      t.zoom, t.canvasW, t.canvasH,
    );
    this._requestDraw();
    this._scheduleSync();
  }

  // -------------------------------------------------------------- drawing
  _requestDraw() {
    if (this._drawQueued) return;
    this._drawQueued = true;
    requestAnimationFrame(() => {
      this._drawQueued = false;
      this._drawNow();
    });
  }

  _drawNow() {
    const t = this._transform;
    if (!t || !this.model.get('meta')) return;
    const dpr = window.devicePixelRatio || 1;
    const w = this._canvas.clientWidth;
    const h = this._canvas.clientHeight;
    if (w && h && (t.canvasW !== w || t.canvasH !== h)) {
      // The canvas was resized without us redrawing (e.g. the RO has not
      // reported the latest size of an animated resize). Re-sync the
      // transform to the live size: the backing store below is sized from
      // the same w/h, so the scene must be laid out for the same size or
      // the tiles get stretched. The sync makes sure Python gets the new
      // canvas size as well.
      this._transform = math.makeTransform(t.cx, t.cy, t.zoom, w, h);
      this._scheduleSync();
    }
    if (this._canvas.width !== Math.round(w * dpr)) {
      this._canvas.width = Math.round(w * dpr);
    }
    if (this._canvas.height !== Math.round(h * dpr)) {
      this._canvas.height = Math.round(h * dpr);
    }
    const ctx = this._canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // M5: cross-fade the incoming level. `_blend` is the transition
    // recorded at the last tile_geo push; levelAlphas() yields the
    // per-level alpha (new level at 1, old level fading 1 -> 0). When no
    // transition is in flight the alphas reduce to {selected: 1} and the
    // rAF loop below stops — the fade never runs at rest.
    const now = Date.now();
    const levelAlphas = this._blend ? blend.levelAlphas(this._blend, now) : null;
    drawScene(ctx, {
      transform: this._transform,
      meta: this.model.get('meta'),
      tileGeo: this._tileGeo,
      images: this._images,
      levelAlphas,
    });
    // Overlay (heatmap): over the tiles, under the annotation canvas.
    const alpha = Number(this.model.get('overlay_alpha'));
    drawOverlay(ctx, {
      transform: this._transform,
      meta: this.model.get('meta'),
      img: this._overlayImg,
      alpha: Number.isFinite(alpha) ? alpha : 0.5,
    });
    this._drawAnnotations();
    this._drawMinimapViewport();
    this._updateReadout();
    this._updateCursorReadout();
    // M5: step the cross-fade on the next frame until it completes.
    if (this._blend && !blend.done(this._blend, now)) {
      this._requestDraw();
    }
  }

  /** M2: overlay pass — read-only annotation shapes, reprojected each frame. */
  _drawAnnotations() {
    const t = this._transform;
    const dpr = window.devicePixelRatio || 1;
    const w = this._canvas.clientWidth;
    const h = this._canvas.clientHeight;
    if (!w || !h) return;
    const c = this._annotCanvas;
    if (c.width !== Math.round(w * dpr)) c.width = Math.round(w * dpr);
    if (c.height !== Math.round(h * dpr)) c.height = Math.round(h * dpr);
    const actx = c.getContext('2d');
    actx.setTransform(dpr, 0, 0, dpr, 0, 0);
    actx.clearRect(0, 0, w, h);
    // M6: the live vertex-move preview (a saved-feature drag in flight);
    // cleared on commit and on any annotations push.
    const rawDoc = this.model.get('annotations') || {};
    const doc = this._vertexMove
      ? withMovedVertex(
          rawDoc, this._vertexMove.id, this._vertexMove.index,
          this._vertexMove.x, this._vertexMove.y)
      : rawDoc;
    drawAnnotations(actx, {
      transform: t,
      annotations: doc,
      alpha: this._annotAlpha,
      selectedId: this._selectedId,
    });
    // M6 (DESIGN.md §6.7): the draggable vertex handles, on top of the
    // shapes: the selected feature's vertices (both modes — a still click
    // grabs, a drag commits `set_vertex`) and, while drawing, the draft's
    // vertices (a still click grabs, a drag moves the draft vertex; a
    // still click on a handle never adds a new draft vertex).
    if (this._selectedId !== null) {
      const f = (Array.isArray(doc.features) ? doc.features : [])
        .find((f) => f && f.id === this._selectedId);
      if (f) {
        const hl =
          this._vertexDrag &&
          this._vertexDrag.kind === 'feature' &&
          this._vertexDrag.id === f.id
            ? this._vertexDrag.index
            : -1;
        drawVertexHandles(actx, t, featurePositions(f), hl);
      }
    }
    // M3: the in-progress polygon draft, on top of the imported features,
    // under the same alpha slider (DESIGN.md §6.4).
    if (this._poly.mode === MODE_DRAWING) {
      drawDraftPolygon(actx, {
        transform: t,
        draft: this._poly.draft,
        cursor: this._cursor,
        alpha: this._annotAlpha,
      });
      if (this._poly.draft.length) {
        const hl =
          this._vertexDrag && this._vertexDrag.kind === 'draft'
            ? this._vertexDrag.index
            : -1;
        drawVertexHandles(actx, t, this._poly.draft, hl);
      }
    }
  }

  _layoutMinimap() {
    const meta = this.model.get('meta');
    const url = this.model.get('minimap_img');
    if (!meta || !url) return;
    // Fit the overview inside the minimap box, preserving aspect.
    const boxW = 92, boxH = 180;
    const [sw, sh] = meta.dimensions;
    const scale = Math.min(boxW / sw, boxH / sh);
    const w = Math.max(1, Math.round(sw * scale));
    const h = Math.max(1, Math.round(sh * scale));
    this._minimapImg.style.width = `${w}px`;
    this._minimapImg.style.height = `${h}px`;
    this._minimapRect.width = w;
    this._minimapRect.height = h;
    this._minimapRect.style.width = `${w}px`;
    this._minimapRect.style.height = `${h}px`;
  }

  _drawMinimapViewport() {
    const meta = this.model.get('meta');
    const t = this._transform;
    if (!meta || !t) return;
    const w = this._minimapRect.width;
    const h = this._minimapRect.height;
    const ctx = this._minimapRect.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    const [sw, sh] = meta.dimensions;
    const bbox = math.viewportL0Bbox(t);
    const x = (bbox.x0 / sw) * w;
    const y = (bbox.y0 / sh) * h;
    const ww = ((bbox.x1 - bbox.x0) / sw) * w;
    const hh = ((bbox.y1 - bbox.y0) / sh) * h;
    ctx.strokeStyle = '#0066cc';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(
      Math.max(0, x), Math.max(0, y),
      Math.min(w, x + ww) - Math.max(0, x),
      Math.min(h, y + hh) - Math.max(0, y),
    );
  }

  _updateReadout() {
    const meta = this.model.get('meta');
    const t = this._transform;
    if (!meta || !t) return;
    const mpp = meta.mpp;
    const mppTxt = mpp ? ` · ${(mpp / t.zoom).toPrecision(3)} µm/px` : '';
    this._readout.textContent =
      `${t.zoom < 0.01 ? t.zoom.toExponential(1) : t.zoom.toPrecision(3)}×${mppTxt}`;
  }

  /**
   * Live cursor position in level-0 px (view-local, never synced).
   * Called on pointer move and after every draw (a toolbar zoom under a
   * stationary cursor moves the slide point under it, so the readout
   * must be re-based on the new transform).
   */
  _updateCursorReadout() {
    const el = this._cursorEl;
    if (!el) return;
    const t = this._transform;
    if (!t || !this._cursor) {
      el.textContent = '';
      return;
    }
    const [lx, ly] = math.screenToL0(t, this._cursor.x, this._cursor.y);
    el.textContent = `${Math.round(lx)}, ${Math.round(ly)}`;
  }

  // -------------------------------------------------------------- resizing
  _onResize() {
    const t = this._transform;
    const w = this._canvas.clientWidth;
    const h = this._canvas.clientHeight;
    if (!w || !h) return;
    if (t) {
      if (t.canvasW !== w || t.canvasH !== h) {
        this._transform = math.makeTransform(
          t.cx, t.cy, t.zoom, w, h);
        // Draw synchronously: the ResizeObserver callback runs after
        // layout but *before* paint, so the current frame is already
        // painted at the new size. Deferring to requestAnimationFrame
        // instead leaves a one-frame gap where the old backing store is
        // CSS-stretched to the new box — visibly squishing/stretching the
        // tiles while JupyterLab animates the sidebar open/closed.
        this._drawNow();
        this._scheduleSync();
      }
    } else {
      this._maybeSendInitialViewport();
    }
  }

  remove() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
    }
    if (this._syncTimer !== null) {
      window.clearTimeout(this._syncTimer);
    }
    super.remove();
  }
}
