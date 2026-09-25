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
 */
import { DOMWidgetView } from '@jupyter-widgets/base';
import * as math from './tilemath.js';
import { drawScene } from './compositor.js';
import { drawAnnotations } from './annotations.js';
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
    this._lastSentViewport = null;
    this._syncTimer = null;
    this._drawQueued = false;
    this._dragging = null;
    this._cursor = null; // canvas-relative pointer pos, drives the l0 readout
    this._annotAlpha = 1; // view-local overlay opacity (toolbar slider)

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
        <canvas class="islide-canvas"></canvas>
        <canvas class="islide-annotations"></canvas>
        <div class="islide-minimap">
          <img class="islide-minimap-img" alt="minimap"/>
          <canvas class="islide-minimap-rect"></canvas>
        </div>
      </div>
      <div class="islide-toolbar">
        <button class="islide-btn" data-action="zoom-out">−</button>
        <button class="islide-btn" data-action="zoom-in">+</button>
        <button class="islide-btn" data-action="fit">fit</button>
        <button class="islide-btn" data-action="1:1">1:1</button>
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
    this.listenTo(this.model, 'change:annotations', this._requestDraw);
    this._onMetaChange();
    this._onSlideOpen();
    this._onMinimapChange();
    this._onStatusChange();
    // Seed the image cache from whatever tiles the model already holds:
    // the kernel's initial render usually completes before this view is
    // attached, so no change:tiles event fires for them. Without this the
    // first draw renders empty (m:all) until the viewport happens to
    // change the plan (a later push then merges), leaving the initial
    // viewport blank.
    this._mergeTiles();
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

  _onTileGeoChange() {
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

  _onStatusChange() {
    this._status.textContent = this.model.get('status');
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
      this._images.delete(this._images.keys().next().value);
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
    this._canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
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
      this._dragging = { x: e.clientX, y: e.clientY, moved: false };
    });
    this._canvas.addEventListener('pointermove', (e) => {
      const rect = this._canvas.getBoundingClientRect();
      this._cursor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      this._updateCursorReadout();
      if (!this._dragging || !this._transform) return;
      const dx = e.clientX - this._dragging.x;
      const dy = e.clientY - this._dragging.y;
      this._dragging.x = e.clientX;
      this._dragging.y = e.clientY;
      this._dragging.moved = true;
      this._transform = math.panTransform(this._transform, dx, dy);
      this._requestDraw();
    });
    const endDrag = (e) => {
      if (!this._dragging) return;
      const moved = this._dragging.moved;
      this._dragging = null;
      if (moved) this._scheduleSync();
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
    });

    this._canvas.addEventListener('dblclick', (e) => {
      if (!this._transform) return;
      const rect = this._canvas.getBoundingClientRect();
      const [minZoom, maxZoom] = this._zoomBounds();
      this._transform = math.zoomAtCursor(
        this._transform, 2,
        e.clientX - rect.left, e.clientY - rect.top,
        minZoom, maxZoom,
      );
      this._requestDraw();
      this._scheduleSync();
    });

    const bar = this.el.querySelector('.islide-toolbar');
    bar.addEventListener('click', (e) => {
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

  _toolbarAction(action) {
    const t = this._transform;
    const meta = this.model.get('meta');
    const [minZoom, maxZoom] = this._zoomBounds();
    switch (action) {
      case 'zoom-in':
        this._transform = math.zoomAtCursor(
          t, 2, t.canvasW / 2, t.canvasH / 2, minZoom, maxZoom);
        break;
      case 'zoom-out':
        this._transform = math.zoomAtCursor(
          t, 1 / 2, t.canvasW / 2, t.canvasH / 2, minZoom, maxZoom);
        break;
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

  _minimapJump(e) {
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
    drawScene(ctx, {
      transform: this._transform,
      meta: this.model.get('meta'),
      tileGeo: this.model.get('tile_geo'),
      images: this._images,
    });
    this._drawAnnotations();
    this._drawMinimapViewport();
    this._updateReadout();
    this._updateCursorReadout();
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
    drawAnnotations(actx, {
      transform: t,
      shapes: this.model.get('annotations') || [],
      alpha: this._annotAlpha,
    });
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
