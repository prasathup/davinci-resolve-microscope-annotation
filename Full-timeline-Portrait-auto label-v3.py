#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SCALE BAR: FULL TIMELINE  (Vertical / 9:16)
===========================================

I built this with Sonnet to add an accurate scale bar to my microscopy
clips that stays correct when I zoom in or crop in Resolve.

For each clip on V1, it updates the overlay above it on V2, or places a
new one, showing:
  - a scale bar sized from the objective lens calibration
  - the scale label, e.g. "50µm"
  - the clip speed, e.g. "2X"

The bar is correct at any timeline resolution (6K, 4K, HD). When a clip
is zoomed, the label switches to another round value (e.g. 50µm to 25µm
at 2× zoom), so the bar keeps about the same size on screen.

Default labels here are about half the landscape ones (e.g. 50µm instead
of 100µm at 40X), so the bar fits next to the speed icon on a narrow
phone frame.

For 16:9 timelines, use the Landscape version. To update only the clips
you changed, use the Rework script.

PREREQUISITES
  1. Your project open in DaVinci Resolve Studio, with clips on V1.
  2. A scale bar template on V2 above the first clip. It's a Fusion
     composition (also in the Media Pool) with three tools named:
       ScaleBar       rectangle mask that sets the bar width
       ScalebarText   Text+ for the scale label
       SpeedRampText  Text+ for the speed label
     New overlays copy the template, including its position.
  3. The objective lens on each clip, in
       Metadata panel > Camera > Lens Type   (e.g. "5X", "10X", "40X")
     If it's missing, the script reads it from the clip name, then
     falls back to 40X with a warning.
  4. V2 used only for scale bar overlays. The script deletes V2 items
     that overlap a clip but don't line up with it.
  5. Auto track selector off.

HOW TO RUN
  1. Workspace > Scripts, select this script and click Run.
  2. Let it finish. Resolve may switch to the Fusion page and back.
  3. Check the SUMMARY in Workspace > Console for clips that need a look.

CONFIGURATION
  - LENS_CALIBRATION: pixels per 10µm and the default label for each
    lens. These values are for my microscope and camera.
  - NICE_STEPS: label values used when a clip is zoomed.
  - DEBUG = False: quieter console.
"""

import os, re, sys, time

# ── CONFIG ────────────────────────────────────────────────────────────────────
SOURCE_TRACK_INDEX     = 1
SCALE_BAR_TRACK_INDEX  = 2
REF_FRAME_WIDTH        = 6144          # 6K sensor default (fallback only)
REF_FRAME_HEIGHT       = 3456
ZOOM_TOLERANCE         = 0.05
NICE_STEPS             = [10, 25, 50, 100, 200, 500, 1000]
# Target on-screen width of the scale bar, as a fraction of the comp/frame
# width (0.25 ≈ a quarter of the visible width). The bar label is then snapped
# to the nearest NICE_STEP, so the actual rendered bar may be ±~40% of this.
# Larger → bigger bar / rounder labels; smaller → tighter bar. Zoom-aware:
# when a clip is inspector-zoomed, the label shrinks (each screen µm is
# worth fewer pixels) so the bar keeps ~this visual size across all clips.
TARGET_BAR_FRACTION    = 0.25
DEBUG                  = True          # extra diagnostics — set False once stable

# Per-lens defaults are ~half of landscape so the bar fits the narrow frame
LENS_CALIBRATION = {
    "63X":  {"px_per_10um": 346.5,  "default_microns": 25},
    "40X":  {"px_per_10um": 220.0,  "default_microns": 50},
    "20X":  {"px_per_10um": 110.0,  "default_microns": 100},
    "10X":  {"px_per_10um": 55.0,   "default_microns": 25},
    "5X":   {"px_per_10um": 27.5,   "default_microns": 50},
    "2.5X": {"px_per_10um": 14.057, "default_microns": 100},
}
DEFAULT_LENS = "40X"

SKIP_IF_OVERLAY_EXISTS = True   # True = update existing overlays in place

# Fusion tools that must exist in the template comp
REQUIRED_TOOLS = {"ScaleBar", "ScalebarText", "SpeedRampText"}

# Speed property keys to try via the API (may work in future Resolve versions)
SPEED_PROPERTY_KEYS = [
    "PlaybackSpeed", "Speed", "SpeedPercent", "RetimeSpeed", "ChangeSpeed",
]

# Overlay position: margin from the corner of the visible footage rect (in
# normalised comp units, 0–1).  Adjust these to taste.
OVERLAY_MARGIN_X = 0.02    # 2% from left edge of visible footage
OVERLAY_MARGIN_Y = 0.02    # 2% from top edge of visible footage

# ── RESOLVE BOOTSTRAP ─────────────────────────────────────────────────────────
def get_resolve():
    import builtins, inspect
    for src in (globals(), vars(builtins)):
        if "resolve" in src:
            return src["resolve"]
    frame = inspect.currentframe()
    if frame and frame.f_back and "resolve" in frame.f_back.f_globals:
        return frame.f_back.f_globals["resolve"]
    mac_api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    for api_root in (os.environ.get("RESOLVE_SCRIPT_API", ""), mac_api):
        mod_path = os.path.join(api_root, "Modules")
        if os.path.isdir(mod_path) and mod_path not in sys.path:
            sys.path.append(mod_path)
        try:
            import DaVinciResolveScript as bmd
            res = bmd.scriptapp("Resolve")
            if res: return res
        except Exception: pass
    raise RuntimeError("Cannot connect to DaVinci Resolve API.")


# ── SOURCE RESOLUTION ─────────────────────────────────────────────────────────

def get_source_resolution(clip):
    """Read the clip's native sensor resolution from its MediaPoolItem.

    Returns (sensor_w, sensor_h, source_string) where source_string is one of:
      'media_pool'  — successfully read from the clip's media properties
      'FALLBACK'    — could not read; using REF_FRAME_WIDTH × REF_FRAME_HEIGHT

    When FALLBACK is used the caller MUST print a prominent warning because
    the scale bar may be wrong.
    """
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            res_str = mpi.GetClipProperty("Resolution")
            if res_str and "x" in str(res_str).lower():
                parts = str(res_str).lower().split("x")
                w = int(parts[0].strip())
                h = int(parts[1].strip())
                if w > 0 and h > 0:
                    return w, h, "media_pool"
    except Exception:
        pass

    # Second attempt: try individual width/height properties
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            all_props = mpi.GetClipProperty()
            if isinstance(all_props, dict):
                for wk in ("Resolution Width", "ResolutionWidth", "Width"):
                    for hk in ("Resolution Height", "ResolutionHeight", "Height"):
                        wv = all_props.get(wk)
                        hv = all_props.get(hk)
                        if wv and hv:
                            w, h = int(wv), int(hv)
                            if w > 0 and h > 0:
                                return w, h, "media_pool"
    except Exception:
        pass

    return REF_FRAME_WIDTH, REF_FRAME_HEIGHT, "FALLBACK"


# ── TRANSFORM READING ─────────────────────────────────────────────────────────

def _normalise_zoom(raw):
    """Resolve reports ZoomX/ZoomY as a float where 1.0 = 100%.
    Some versions report 100.0 for 100% — normalise to 1.0-based."""
    v = float(raw)
    return (v / 100.0) if v > 10.0 else v

def get_clip_transform(clip):
    """Read the full Inspector transform for a timeline clip.

    Returns a dict with keys:
        zoom_x, zoom_y  — float, 1.0 = no zoom
        pan, tilt       — float, pixels
        crop_left, crop_right, crop_top, crop_bottom — float, pixels
    All values default to identity (zoom=1, pan/tilt=0, crop=0) if unreadable.
    """
    result = {
        "zoom_x": 1.0, "zoom_y": 1.0,
        "pan": 0.0, "tilt": 0.0,
        "crop_left": 0.0, "crop_right": 0.0,
        "crop_top": 0.0, "crop_bottom": 0.0,
    }
    key_map = {
        "ZoomX": "zoom_x", "ZoomY": "zoom_y",
        "Pan": "pan", "Tilt": "tilt",
        "CropLeft": "crop_left", "CropRight": "crop_right",
        "CropTop": "crop_top", "CropBottom": "crop_bottom",
    }

    # Try reading all properties at once
    try:
        props = clip.GetProperty()
        if isinstance(props, dict):
            for api_key, our_key in key_map.items():
                for pk, pv in props.items():
                    if pk.lower() == api_key.lower() and pv is not None:
                        result[our_key] = float(pv)
                        break
    except Exception:
        pass

    # Try reading individually (may succeed where batch fails)
    for api_key, our_key in key_map.items():
        if our_key.startswith("zoom") and result[our_key] == 1.0:
            try:
                val = clip.GetProperty(api_key)
                if val is not None:
                    result[our_key] = float(val)
            except Exception:
                pass
        elif result[our_key] == 0.0:
            try:
                val = clip.GetProperty(api_key)
                if val is not None:
                    result[our_key] = float(val)
            except Exception:
                pass

    # Normalise zoom values (handle 100.0 = 100% vs 1.0 = 100%)
    result["zoom_x"] = _normalise_zoom(result["zoom_x"])
    result["zoom_y"] = _normalise_zoom(result["zoom_y"])

    return result


# ── GEOMETRY ──────────────────────────────────────────────────────────────────

def compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h):
    """Compute the base scale factor for a vertical timeline.

    Landscape source on a vertical comp uses fit-by-height: full height
    shown, left/right cropped (Resolve default for portrait timelines).
    Returns (base_scale_x, base_scale_y, fit_mode_string).
    """
    src_aspect = float(sensor_w) / float(sensor_h)
    comp_aspect = float(comp_w) / float(comp_h)

    if abs(src_aspect - comp_aspect) < 0.01:
        scale = float(comp_w) / float(sensor_w)
        return scale, scale, "same_aspect"

    # Vertical comp — fit by height
    scale = float(comp_h) / float(sensor_h)
    return scale, scale, "fit_height"


def compute_visible_rect(sensor_w, sensor_h, comp_w, comp_h, transform):
    """Compute the bounding box of the visible footage in comp coordinates.

    Returns (left, top, right, bottom) in comp pixel coordinates, and
    the effective scale factor (comp pixels per sensor pixel on X axis).

    This accounts for:
      - Resolve's fit scaling (fit-by-height for vertical)
      - Inspector ZoomX/ZoomY
      - Inspector Pan/Tilt
    """
    base_scale, _, fit_mode = compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h)

    zoom_x = transform["zoom_x"]
    zoom_y = transform["zoom_y"]

    # Effective scale: base fit × inspector zoom
    eff_scale_x = base_scale * zoom_x
    eff_scale_y = base_scale * zoom_y

    # Scaled footage dimensions in comp space
    scaled_w = sensor_w * eff_scale_x
    scaled_h = sensor_h * eff_scale_y

    # Centre of comp + pan/tilt offset
    # Pan/Tilt are in comp pixels (positive Pan = right, positive Tilt = down)
    cx = comp_w / 2.0 + transform["pan"]
    cy = comp_h / 2.0 + transform["tilt"]

    rect_left   = cx - scaled_w / 2.0
    rect_top    = cy - scaled_h / 2.0
    rect_right  = cx + scaled_w / 2.0
    rect_bottom = cy + scaled_h / 2.0

    return rect_left, rect_top, rect_right, rect_bottom, eff_scale_x, fit_mode


# ── SCALE LOGIC (resolution-aware) ───────────────────────────────────────────

def compute_scale(lens_key, transform, sensor_w, sensor_h, comp_w, comp_h):
    """Calculate the normalised width for the scale bar rectangle mask.

    Unlike the old version, this accounts for the source→comp transform so the
    bar is correct at any timeline resolution and aspect ratio.

    Returns (target_microns, w_norm, trace_string).
    """
    calib           = LENS_CALIBRATION.get(lens_key, LENS_CALIBRATION[DEFAULT_LENS])
    px_per_10um     = calib["px_per_10um"]       # sensor pixels per 10µm
    default_microns = calib["default_microns"]

    # ── Effective scale: sensor px → comp px ──
    base_scale, _, fit_mode = compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h)
    zoom_x = transform["zoom_x"]
    eff_scale_x = base_scale * zoom_x

    # Output pixels-per-10µm in the comp
    out_px_per_10um = px_per_10um * eff_scale_x

    # Reference fraction (for NICE_STEPS selection): what the default micron
    # label would look like in the comp AT ZOOM = 1 (the look the user likes).
    # Key fix: the target is pinned at zoom=1, NOT recomputed from the zoomed
    # out_px_per_10um. Previously 'ref_fraction' used out_px_per_10um, so both
    # the reference and every candidate scaled equally with zoom — making the
    # comparison zoom-invariant (a 3.5X clip picked the same label as 1.0X,
    # and since default_microns is itself a NICE_STEP it always won at distance
    # 0). By pinning the target to the zoom=1 pixels-per-10µm, zooming makes
    # the candidate fractions grow while the target stays fixed, so the picker
    # correctly prefers a SMALLER µm label as zoom increases (each screen µm is
    # worth more pixels) → the bar shrinks with zoom. At zoom≈1 the candidates
    # match the target exactly, reproducing the original behaviour unchanged.
    px_per_10um_at_base = px_per_10um * base_scale    # zoom=1 comp px per 10µm
    ref_fraction = (default_microns / 10.0) * px_per_10um_at_base / float(comp_w)

    trace = []
    trace.append(f"sensor {sensor_w}×{sensor_h}")
    trace.append(f"comp {comp_w}×{comp_h}")
    trace.append(f"fit={fit_mode} base_scale={base_scale:.4f}")
    trace.append(f"zoom_x={zoom_x:.2f}")
    trace.append(f"eff_scale={eff_scale_x:.4f}")
    trace.append(f"out_px/10µm={out_px_per_10um:.1f}")

    if abs(zoom_x - 1.0) <= ZOOM_TOLERANCE:
        target_microns = default_microns
        trace.append(f"zoom≈1 → default {default_microns}µm")
    else:
        target_microns = min(
            NICE_STEPS,
            key=lambda m: abs((m / 10.0) * out_px_per_10um / float(comp_w) - ref_fraction)
        )
        trace.append(f"best-fit label={target_microns}µm")

    bar_comp_px = (target_microns / 10.0) * out_px_per_10um
    w_norm = bar_comp_px / float(comp_w)
    trace.append(f"bar={bar_comp_px:.1f}comp-px → w_norm={w_norm:.6f}")

    return target_microns, w_norm, " | ".join(trace)


# ── HELPERS ───────────────────────────────────────────────────────────────────
def frames_overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1

def overlay_aligned_to_clip(overlay_start, overlay_end, clip_start, clip_end,
                            start_tol=2, dur_tol=2):
    """True only when an overlay owns this clip (start + duration match).

    Loose overlap is NOT enough: an oversized previous overlay can cover later
    V1 clips and must not be treated as theirs.
    """
    o_start = int(overlay_start)
    o_end = int(overlay_end)
    c_start = int(clip_start)
    c_end = int(clip_end)
    if abs(o_start - c_start) > start_tol:
        return False
    if abs((o_end - o_start) - (c_end - c_start)) > dur_tol:
        return False
    return True

def find_overlapping_item(items, start, end):
    """Any overlap (used only to locate the template above clip #1)."""
    for item in (items or []):
        try:
            if frames_overlap(int(item.GetStart()), int(item.GetEnd()), start, end):
                return item
        except Exception: continue
    return None

def find_aligned_overlay(items, clip_start, clip_end, start_tol=2, dur_tol=2):
    """Return the V2 overlay that belongs to this V1 clip, or None."""
    for item in (items or []):
        try:
            if overlay_aligned_to_clip(
                int(item.GetStart()), int(item.GetEnd()),
                clip_start, clip_end, start_tol, dur_tol,
            ):
                return item
        except Exception:
            continue
    return None

def source_end_frame_for_timeline_duration(desired_tl_frames, media_fps, timeline_fps):
    """Inclusive AppendToTimeline endFrame for a desired *timeline* duration.

    startFrame/endFrame are in the media-pool item's native FPS. On a mismatched
    timeline FPS, Resolve retimes: tl_frames ≈ src_frames * timeline_fps / media_fps.
    """
    desired = max(1, int(desired_tl_frames))
    m_fps = float(media_fps) if media_fps else 0.0
    t_fps = float(timeline_fps) if timeline_fps else 0.0
    if m_fps <= 0 or t_fps <= 0 or abs(m_fps - t_fps) < 0.01:
        return desired - 1
    src_count = max(1, int(round(desired * m_fps / t_fps)))
    return src_count - 1

def correct_source_end_frame(prev_end_frame, actual_tl, desired_tl):
    """Closed-loop correction after a measured AppendToTimeline duration miss."""
    src_used = max(1, int(prev_end_frame) + 1)
    actual = max(1, int(actual_tl))
    desired = max(1, int(desired_tl))
    src_needed = max(1, int(round(src_used * desired / float(actual))))
    return src_needed - 1

def get_mpi_fps(mp_item, fallback_fps):
    """Read media-pool FPS; fall back to timeline FPS if unavailable."""
    if mp_item is not None:
        for key in ("FPS", "Frame Rate", "Video Frame Rate"):
            try:
                raw = get_clip_property_by_key(mp_item, key)
                if raw not in (None, ""):
                    val = float(str(raw).strip().replace(",", "."))
                    if val > 0:
                        return val
            except Exception:
                pass
    return float(fallback_fps)

# ── Robust metadata / property access ─────────────────────────────────────────
def get_metadata_by_key(mpi, target_key):
    """Case/space-insensitive metadata lookup (handles dict and list-of-lists)."""
    try:
        val = mpi.GetMetadata(target_key)
        if val: return val
    except Exception: pass
    norm = target_key.lower().replace(" ", "").replace("_", "")
    try:
        all_meta = mpi.GetMetadata()
        if isinstance(all_meta, dict):
            for k, v in all_meta.items():
                if k.lower().replace(" ", "").replace("_", "") == norm:
                    return v
        elif isinstance(all_meta, list) and len(all_meta) == 2:
            for k, v in zip(all_meta[0], all_meta[1]):
                if k.lower().replace(" ", "").replace("_", "") == norm:
                    return v
    except Exception: pass
    return None

def get_clip_property_by_key(mpi, target_key):
    """Case/space-insensitive clip property lookup."""
    try:
        val = mpi.GetClipProperty(target_key)
        if val: return val
    except Exception: pass
    norm = target_key.lower().replace(" ", "").replace("_", "")
    try:
        all_props = mpi.GetClipProperty()
        if isinstance(all_props, dict):
            for k, v in all_props.items():
                if k.lower().replace(" ", "").replace("_", "") == norm:
                    return v
    except Exception: pass
    return None

def get_lens_key(clip):
    """Return (lens_key, source) where source is 'metadata', 'filename', or 'DEFAULT'."""
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            text = (get_clip_property_by_key(mpi, "LensType")
                    or get_metadata_by_key(mpi, "LensType"))
            if text:
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*[xX]", str(text))
                if m:
                    key = m.group(1) + "X"
                    if key in LENS_CALIBRATION:
                        return key, "metadata"
        text_name = clip.GetName()
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*[xX]", str(text_name))
        if m:
            key = m.group(1) + "X"
            if key in LENS_CALIBRATION:
                return key, "filename"
    except Exception: pass
    return DEFAULT_LENS, "DEFAULT"

def get_inspector_zoom(clip):
    """Legacy helper — still used for speed calculation context.
    For scale computation, use get_clip_transform() instead."""
    try:
        val = clip.GetProperty("ZoomX")
        if val is not None:
            raw = float(val)
            return (raw / 100.0) if raw > 10.0 else raw
    except Exception: pass
    try:
        props = clip.GetProperty()
        if isinstance(props, dict):
            for k, v in props.items():
                if k.lower() == "zoomx":
                    raw = float(v)
                    return (raw / 100.0) if raw > 10.0 else raw
    except Exception: pass
    return 1.0

def get_clip_fps(clip, timeline_fps):
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            fps_val = get_clip_property_by_key(mpi, "FPS")
            if fps_val:
                return float(fps_val)
    except Exception: pass
    return timeline_fps

def get_speed_percent(clip, timeline_fps=24.0):
    """Return the clip's playback speed as a percentage (100.0 = normal)."""
    # Attempt 1: direct property lookup (may work in future Resolve versions)
    for key in SPEED_PROPERTY_KEYS:
        try:
            val = clip.GetProperty(key)
            if val is not None:
                v = float(val)
                if v > 0:
                    return v
        except Exception: pass

    # Attempt 2: infer from source vs timeline frame counts
    try:
        src_start = int(clip.GetSourceStartFrame())
        src_end   = int(clip.GetSourceEndFrame())
        tl_dur    = int(clip.GetDuration())
        src_dur   = src_end - src_start
        if tl_dur > 0 and src_dur > 0:
            pct = float(src_dur) / float(tl_dur) * 100.0
            clip_fps = get_clip_fps(clip, timeline_fps)
            if abs(clip_fps - timeline_fps) > 0.01:
                pct = pct * (timeline_fps / clip_fps)
            return pct
    except Exception: pass

    return 100.0

def format_speed_label(speed_percent):
    """
    Format speed as a compact label.  Always shows a value (including 1X).
    Uses uppercase X (not the unicode × symbol).
      100% -> "1X"    200% -> "2X"    250% -> "2.5X"    150% -> "1.5X"
    """
    speed_x = float(speed_percent) / 100.0
    rounded = round(speed_x)
    if abs(speed_x - rounded) < 0.05:     # within 5% of integer → show integer
        return f"{int(rounded)}X"
    return f"{speed_x:.1f}X"

def copy_inspector_properties(src, dst):
    """Copy inspector properties from template to new overlay."""
    try:
        props = src.GetProperty()
        if isinstance(props, dict):
            for k, v in props.items():
                try: dst.SetProperty(k, v)
                except Exception: pass
    except Exception: pass

def search_media_pool(folder, name):
    for clip in folder.GetClipList():
        if clip.GetName() == name: return clip
    for sub in folder.GetSubFolderList():
        r = search_media_pool(sub, name)
        if r: return r
    return None

def frame_to_timecode(frame_index, fps):
    fi = int(frame_index)
    fps_int = int(round(float(fps)))
    ff = fi % fps_int
    ts = fi // fps_int
    return f"{ts//3600:02d}:{(ts%3600)//60:02d}:{ts%60:02d}:{ff:02d}"

def find_tool(comp, name):
    """Find a Fusion tool by name. Uses FindTool first, then iterates all tools."""
    try:
        t = comp.FindTool(name)
        if t: return t
    except Exception: pass
    try:
        tools = comp.GetToolList(False)   # False = ALL tools (True = selected only)
        tl = list(tools.values()) if isinstance(tools, dict) else list(tools)
        for tool in tl:
            try:
                n = tool.GetAttrs().get("TOOLS_Name", "")
                if n == name or n.lower() == name.lower(): return tool
            except Exception: continue
    except Exception: pass
    return None

# ── FUSION COMP ACCESS ────────────────────────────────────────────────────────

def has_required_tools(comp):
    """Check if comp has all required tools using FindTool (not GetToolList).
    GetToolList(True) returns only SELECTED tools — which caused the old
    10-second timeout.  FindTool searches all tools by name and works reliably."""
    if not comp:
        return False
    for name in REQUIRED_TOOLS:
        try:
            t = comp.FindTool(name)
            if not t: return False
        except Exception:
            return False
    return True

def try_comp_from_item(item):
    """Get Fusion comp directly from a timeline item (bypasses Fusion page)."""
    try:
        count = item.GetFusionCompCount()
        if count and int(count) > 0:
            return item.GetFusionCompByIndex(1)
    except Exception: pass
    return None

def get_comp_for_overlay(resolve, timeline, overlay_item, fps):
    """
    Get the fully loaded Fusion comp for the V2 overlay item.
    Strategy: direct access first, then Fusion page navigation with retries.
    """
    # Quick try: direct comp access (no page switch needed)
    comp = try_comp_from_item(overlay_item)
    if comp and has_required_tools(comp):
        if DEBUG:
            print("       Comp: direct access (all tools found)")
        return comp

    # Navigate to Fusion page to trigger comp initialisation
    navigate_to_comp(resolve, timeline, overlay_item, fps)

    # Retry loop — should typically succeed on first attempt now
    for attempt in range(20):
        comp = try_comp_from_item(overlay_item)
        if comp and has_required_tools(comp):
            if DEBUG and attempt > 0:
                print(f"       Comp: loaded on attempt {attempt+1}")
            return comp

        try:
            fusion = resolve.Fusion()
            if fusion:
                active = fusion.GetCurrentComp()
                if active and has_required_tools(active):
                    if DEBUG:
                        print(f"       Comp: via Fusion page on attempt {attempt+1}")
                    return active
        except Exception: pass

        if attempt > 0 and attempt % 5 == 0:
            print(f"       Waiting for comp... attempt {attempt+1}/20")
        time.sleep(0.5)

    # Timeout — return whatever we have (update_scale_bar will skip missing tools)
    if comp:
        print("       \u26a0 Comp loaded after timeout — some tools may be missing")
    else:
        print("       \u26a0 No comp available after timeout")
    return comp

def navigate_to_comp(resolve, timeline, overlay_item, fps):
    """Move Edit-page playhead to overlay_item start, then switch to Fusion page."""
    clip_start = int(overlay_item.GetStart())
    resolve.OpenPage("edit")
    time.sleep(0.2)
    timeline.SetCurrentTimecode(frame_to_timecode(clip_start, fps))
    time.sleep(0.3)
    resolve.OpenPage("fusion")
    time.sleep(1.0)

# ── TIMELINE PLACEMENT ────────────────────────────────────────────────────────

def _append_overlay(media_pool, mp_item, record_frame, end_frame, track_index):
    appended = media_pool.AppendToTimeline([{
        "mediaPoolItem": mp_item,
        "startFrame":    0,
        "endFrame":      int(end_frame),
        "trackIndex":    track_index,
        "recordFrame":   int(record_frame),
        "mediaType":     1,
    }])
    if not appended:
        raise RuntimeError(f"AppendToTimeline failed for V{track_index}.")
    return appended[0]


def resolve_template_mp_item(media_pool, template_item):
    """Stable MediaPoolItem for cloning — survives DeleteClips on the timeline item."""
    mp_item = None
    try:
        mp_item = template_item.GetMediaPoolItem()
    except Exception:
        mp_item = None
    if mp_item is None:
        mp_item = search_media_pool(media_pool.GetRootFolder(), template_item.GetName())
    if mp_item is None:
        raise RuntimeError(f"Cannot find '{template_item.GetName()}' in the Media Pool.")
    return mp_item


def duplicate_to_timeline(media_pool, template_item, record_frame, duration_frames,
                          track_index, timeline_fps, timeline=None, mp_item=None):
    """Place a copy of the template Fusion comp on V2 at the specified position.

    Compensates for media-pool FPS ≠ timeline FPS (common after Resolve updates
    or when a Fusion template was created under a different project FPS). Retries
    once with a measured closed-loop correction if duration is still wrong.
    """
    if mp_item is None:
        mp_item = resolve_template_mp_item(media_pool, template_item)

    desired_dur = max(1, int(duration_frames))
    media_fps = get_mpi_fps(mp_item, timeline_fps)
    end_frame = source_end_frame_for_timeline_duration(
        desired_dur, media_fps, timeline_fps,
    )

    if DEBUG and abs(float(media_fps) - float(timeline_fps)) >= 0.01:
        print(f"       Placement: media FPS {media_fps} → timeline {timeline_fps}; "
              f"endFrame={end_frame} (for {desired_dur} tl frames)")

    new_item = _append_overlay(media_pool, mp_item, record_frame, end_frame, track_index)
    actual_dur = int(new_item.GetEnd()) - int(new_item.GetStart())

    if abs(actual_dur - desired_dur) > 2 and timeline is not None:
        corrected_end = correct_source_end_frame(end_frame, actual_dur, desired_dur)
        if DEBUG:
            print(f"       Placement retry: got {actual_dur}f, correcting "
                  f"endFrame {end_frame} → {corrected_end}")
        try:
            timeline.DeleteClips([new_item], False)
        except Exception:
            pass
        time.sleep(0.15)
        new_item = _append_overlay(
            media_pool, mp_item, record_frame, corrected_end, track_index,
        )
        actual_dur = int(new_item.GetEnd()) - int(new_item.GetStart())

    if abs(actual_dur - desired_dur) > 2:
        print(f"       \u26a0 Note: V2 duration {actual_dur}f (wanted {desired_dur}f)")
    elif DEBUG and actual_dur != desired_dur:
        print(f"       Note: V2 duration {actual_dur}f (wanted {desired_dur}f, within tol)")

    try:
        copy_inspector_properties(template_item, new_item)
    except Exception:
        pass
    return new_item

# ── OVERLAY POSITION ADJUSTMENT ───────────────────────────────────────────────

def try_set_tool_center(tool, center_x, center_y):
    """Try to set a Fusion tool's Center position.
    Fusion normalised coordinates: (0,0) = bottom-left, (1,1) = top-right.
    Returns True if successfully set."""
    try:
        tool.SetInput("Center", {1: center_x, 2: center_y})
        return True
    except Exception:
        pass
    # Some tools use separate X/Y inputs
    try:
        tool.SetInput("CenterX", center_x)
        tool.SetInput("CenterY", center_y)
        return True
    except Exception:
        pass
    return False


def adjust_overlay_positions(comp, visible_rect, comp_w, comp_h):
    """Try to position overlay tools near the top-left of the visible footage.

    visible_rect = (left, top, right, bottom) in comp pixel coordinates.
    Fusion uses normalised coordinates where (0,0) = bottom-left and
    (1,1) = top-right.  We convert accordingly.

    This is best-effort: if the tools don't have settable Center properties,
    we skip them and log a note.
    """
    if not comp:
        return

    left, top, right, bottom = visible_rect

    # Target position: top-left of the visible footage + small margin
    # In comp pixel space:
    target_x_px = left + OVERLAY_MARGIN_X * comp_w
    target_y_px = top + OVERLAY_MARGIN_Y * comp_h

    # Convert to Fusion normalised coordinates:
    # Fusion X: 0 = left, 1 = right (same direction as pixel X)
    # Fusion Y: 0 = bottom, 1 = top (INVERTED from pixel Y)
    target_x_norm = target_x_px / float(comp_w)
    target_y_norm = 1.0 - (target_y_px / float(comp_h))  # flip Y

    # Clamp to valid range
    target_x_norm = max(0.0, min(1.0, target_x_norm))
    target_y_norm = max(0.0, min(1.0, target_y_norm))

    positioned = []
    skipped = []

    # Try to position each tool that might have a Center property
    # We search for any tool with "clock", "icon", "transform", or known overlay names
    tool_names_to_try = []
    try:
        tools = comp.GetToolList(False)
        tl = list(tools.values()) if isinstance(tools, dict) else list(tools)
        for tool in tl:
            try:
                attrs = tool.GetAttrs()
                name = attrs.get("TOOLS_Name", "")
                reg_id = attrs.get("TOOLS_RegID", "")
                # Skip the tools we handle separately (ScaleBar, ScalebarText, SpeedRampText)
                if name in REQUIRED_TOOLS:
                    continue
                # Try tools that sound like they control position
                if any(kw in name.lower() for kw in ("transform", "center", "position",
                                                      "icon", "clock", "overlay",
                                                      "merge", "loader")):
                    tool_names_to_try.append((name, tool))
                # Also try any Transform tools
                if reg_id in ("Transform", "DVE", "Merge"):
                    tool_names_to_try.append((name, tool))
            except Exception:
                continue
    except Exception:
        pass

    for name, tool in tool_names_to_try:
        if try_set_tool_center(tool, target_x_norm, target_y_norm):
            positioned.append(name)
        else:
            skipped.append(name)

    if positioned:
        print(f"       Position : moved {', '.join(positioned)} → "
              f"({target_x_norm:.3f}, {target_y_norm:.3f}) [top-left of footage]")
    if skipped and DEBUG:
        print(f"       Position : could not move: {', '.join(skipped)}")
    if not positioned and not skipped:
        if DEBUG:
            print("       Position : no movable overlay tools found (using template defaults)")


# ── SCALE BAR UPDATE ──────────────────────────────────────────────────────────

def fix_loader_globals(comp):
    """Reset Loader nodes' GlobalIn/GlobalOut to match the comp's render range."""
    if not comp: return
    try:
        ca = comp.GetAttrs()
        g_start = ca.get("COMPN_GlobalStart", 0)
        g_end   = ca.get("COMPN_GlobalEnd", 1000)
    except Exception: return
    try:
        tools = comp.GetToolList(False)
        tl = list(tools.values()) if isinstance(tools, dict) else list(tools)
        for tool in tl:
            try:
                if tool.GetAttrs().get("TOOLS_RegID", "") == "Loader":
                    tool.SetAttrs({
                        "TOOLNT_EnabledRegion_Start": g_start,
                        "TOOLNT_EnabledRegion_End":   g_end,
                    })
                    try:
                        tool.SetInput("GlobalIn", g_start)
                        tool.SetInput("GlobalOut", g_end)
                        tool.SetInput("HoldFirstFrame", 1)
                        tool.SetInput("HoldLastFrame", 2000)
                        tool.SetInput("Loop", 1)
                    except Exception: pass
            except Exception: continue
    except Exception: pass

def update_scale_bar(comp, lens_key, transform, sensor_w, sensor_h,
                     comp_w, comp_h, speed_label=""):
    """Update the scale bar, text, speed, and overlay positions in the Fusion comp.
    Returns a list of issue strings (empty = success)."""
    issues = []
    if not comp:
        return ["No Fusion comp available"]

    fix_loader_globals(comp)

    target_microns, w_norm, scale_trace = compute_scale(
        lens_key, transform, sensor_w, sensor_h, comp_w, comp_h
    )
    label = f"{target_microns}\u00b5m"

    print(f"       Scale    : {scale_trace}")

    try: comp.Lock()
    except Exception: pass

    # ── Scale bar width ──
    scalebar_tool = find_tool(comp, "ScaleBar")
    if scalebar_tool:
        set_ok = False
        for inp in ("Width", "XSize", "Size"):
            try:
                scalebar_tool.SetInput(inp, w_norm)
                print(f"       ScaleBar.{inp:5s} = {w_norm:.6f}  ({label} @ {lens_key})")
                set_ok = True; break
            except Exception: continue
        if not set_ok:
            issues.append("Could not set ScaleBar width")
            print("       \u26a0 Could not set ScaleBar width")
    else:
        issues.append("ScaleBar tool not found")
        print("       \u26a0 ScaleBar tool not found in comp")

    # ── Scale text ──
    text_tool = find_tool(comp, "ScalebarText")
    if text_tool:
        try:
            text_tool.SetInput("StyledText", label)
            print(f"       ScalebarText   = '{label}'")
        except Exception as exc:
            issues.append(f"ScalebarText: {exc}")
            print(f"       \u26a0 ScalebarText failed: {exc}")
    else:
        issues.append("ScalebarText not found")
        print("       \u26a0 ScalebarText tool not found")

    # ── Speed text ──
    speed_tool = find_tool(comp, "SpeedRampText")
    if speed_tool:
        try:
            speed_tool.SetInput("StyledText", speed_label)
            print(f"       SpeedRampText  = '{speed_label}'")
        except Exception as exc:
            issues.append(f"SpeedRampText: {exc}")
            print(f"       \u26a0 SpeedRampText failed: {exc}")
    else:
        issues.append("SpeedRampText not found")
        print("       \u26a0 SpeedRampText tool not found")

    # ── Overlay position adjustment ──
    # DISABLED: adjust_overlay_positions() was moving Merge/Transform/Loader
    # Centers, which shifted the entire composited overlay off-screen.
    # The position fix for vertical needs a different approach — it requires
    # knowing the exact tool names in the template that control element
    # positions (not pipeline tools like Merge).
    # The scale bar WIDTH fix works correctly without this.
    #
    # visible_rect = compute_visible_rect(
    #     sensor_w, sensor_h, comp_w, comp_h, transform
    # )
    # rect_left, rect_top, rect_right, rect_bottom, eff_scale, fit_mode = visible_rect
    # adjust_overlay_positions(
    #     comp,
    #     (rect_left, rect_top, rect_right, rect_bottom),
    #     comp_w, comp_h,
    # )

    try: comp.Unlock()
    except Exception: pass
    return issues

# =============================================================================
#  MAIN
# =============================================================================
def main():
    print("=" * 60)
    print("  SCALE BAR \u2014 FULL TIMELINE PROCESSOR (Vertical 9:16)")
    print("=" * 60)

    resolve    = get_resolve()
    project    = resolve.GetProjectManager().GetCurrentProject()
    timeline   = project.GetCurrentTimeline()
    media_pool = project.GetMediaPool()
    fps        = float(timeline.GetSetting("timelineFrameRate") or 24)

    # ── Read project resolution ──
    comp_w, comp_h = REF_FRAME_WIDTH, REF_FRAME_HEIGHT
    try:
        w_val = project.GetSetting("timelineResolutionWidth")
        h_val = project.GetSetting("timelineResolutionHeight")
        if w_val: comp_w = int(w_val)
        if h_val: comp_h = int(h_val)
    except Exception: pass

    # ── Header ──
    print()
    print("PROJECT SETTINGS")
    print(f"  Timeline      : {timeline.GetName()}")
    print(f"  Resolution    : {comp_w} x {comp_h}  [VERTICAL 9:16]")
    if comp_h <= comp_w:
        print(f"  \u26a0 WARNING: Timeline looks landscape ({comp_w}x{comp_h}).")
        print(f"    Use the Landscape version of this script for 16:9 projects.")
    print(f"  Timeline FPS  : {fps}")

    v1_clips = sorted(
        timeline.GetItemListInTrack("video", SOURCE_TRACK_INDEX) or [],
        key=lambda c: int(c.GetStart()),
    )
    if not v1_clips:
        print(f"\n\u26a0 ERROR: No clips on V{SOURCE_TRACK_INDEX}.")
        return

    first_clip  = v1_clips[0]
    first_start = int(first_clip.GetStart())
    first_end   = int(first_clip.GetEnd())

    # ── Find template ──
    template_item = find_overlapping_item(
        timeline.GetItemListInTrack("video", SCALE_BAR_TRACK_INDEX),
        first_start, first_end,
    )
    if template_item is None:
        print(f"\n\u26a0 ERROR: No overlay found on V{SCALE_BAR_TRACK_INDEX} above the first V1 clip.")
        print("  Place a 'Scale Bar fusion' template on V2 above clip #1 and re-run.")
        return

    t_dur = int(template_item.GetEnd()) - int(template_item.GetStart())
    print(f"  Template      : '{template_item.GetName()}' ({t_dur} frames)")
    print(f"  V1 clips      : {len(v1_clips)}")

    try:
        template_mp_item = resolve_template_mp_item(media_pool, template_item)
        tmpl_fps = get_mpi_fps(template_mp_item, fps)
        if abs(tmpl_fps - fps) >= 0.01:
            print(f"  Template FPS  : {tmpl_fps}  (timeline {fps} — will convert on place)")
        else:
            print(f"  Template FPS  : {tmpl_fps}")
    except RuntimeError as exc:
        print(f"\n\u26a0 ERROR: {exc}")
        return

    # ── Validate template has required tools ──
    template_comp = try_comp_from_item(template_item)
    if template_comp:
        missing = []
        for name in REQUIRED_TOOLS:
            if not find_tool(template_comp, name):
                missing.append(name)
        if missing:
            print(f"\n\u26a0 WARNING: Template comp is missing tools: {', '.join(missing)}")
            print("  The script will still run, but those values cannot be updated.")
            print("  Check that your template has tools named: ScaleBar, ScalebarText, SpeedRampText")
    else:
        print("\n  Note: Could not inspect template comp (will be loaded per-clip)")

    print()
    print("-" * 60)
    print()

    # ── Process clips ─────────────────────────────────────────────────────
    processed  = 0
    skipped    = 0
    warnings   = []   # list of (clip_num, clip_name, [issues])
    resolution_fallback_clips = []   # track clips where resolution reading failed

    for idx, src_clip in enumerate(v1_clips):
        clip_num   = idx + 1
        clip_name  = src_clip.GetName()
        clip_start = int(src_clip.GetStart())
        clip_end   = int(src_clip.GetEnd())
        duration   = clip_end - clip_start
        if duration <= 0:
            duration = int(src_clip.GetDuration())

        # ── Switch to Edit page & position playhead BEFORE reading props ──
        resolve.OpenPage("edit")
        time.sleep(0.2)
        timeline.SetCurrentTimecode(frame_to_timecode(clip_start, fps))
        time.sleep(0.3)

        # ── Read clip properties ──
        lens_key, lens_source = get_lens_key(src_clip)
        transform = get_clip_transform(src_clip)
        sensor_w, sensor_h, res_source = get_source_resolution(src_clip)
        clip_fps  = get_clip_fps(src_clip, fps)
        speed_pct = get_speed_percent(src_clip, fps)
        speed_label = format_speed_label(speed_pct)

        # ── Build info line ──
        if lens_source == "DEFAULT":
            lens_info = f"{lens_key} (DEFAULT \u2014 no metadata)"
        elif lens_source == "filename":
            lens_info = f"{lens_key} (filename)"
        else:
            lens_info = f"{lens_key}"

        fps_note = ""
        if abs(clip_fps - fps) > 0.01:
            fps_note = f"  |  Clip FPS: {clip_fps}"

        print(f"[{clip_num}/{len(v1_clips)}] {clip_name}")
        print(f"       Lens: {lens_info}  |  Zoom: x{transform['zoom_x']:.2f}  |  "
              f"Speed: {speed_label}{fps_note}")
        print(f"       Source: {sensor_w}x{sensor_h} ({res_source})  |  "
              f"Comp: {comp_w}x{comp_h}")

        # ── Resolution fallback warning ──
        if res_source == "FALLBACK":
            print(f"       \u26a0\u26a0\u26a0 WARNING: Could not read source resolution from clip!")
            print(f"       \u26a0\u26a0\u26a0 Using fallback: {REF_FRAME_WIDTH}x{REF_FRAME_HEIGHT}")
            print(f"       \u26a0\u26a0\u26a0 Scale bar may be WRONG if actual source is not {REF_FRAME_WIDTH}x{REF_FRAME_HEIGHT}!")
            resolution_fallback_clips.append((clip_num, clip_name))
            warnings.append((clip_num, clip_name,
                            [f"Source resolution FALLBACK to {REF_FRAME_WIDTH}x{REF_FRAME_HEIGHT} — "
                             f"verify scale bar manually"]))

        if lens_source == "DEFAULT":
            warnings.append((clip_num, clip_name,
                            [f"Lens defaulted to {DEFAULT_LENS} \u2014 check metadata"]))

        # ── Transform trace (DEBUG) ──
        if DEBUG:
            print(f"       Transform: ZoomX={transform['zoom_x']:.3f}  "
                  f"ZoomY={transform['zoom_y']:.3f}  "
                  f"Pan={transform['pan']:.1f}  Tilt={transform['tilt']:.1f}")
            base_s, _, fit = compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h)
            eff_s = base_s * transform["zoom_x"]
            print(f"       Geometry : fit={fit}  base_scale={base_s:.4f}  "
                  f"eff_scale={eff_s:.4f}")

        # ── Ensure overlay on V2 ──
        # Aligned match only (start + duration). Loose overlap from an oversized
        # previous overlay must NOT count as "exists" for this clip.
        v2_items = timeline.GetItemListInTrack("video", SCALE_BAR_TRACK_INDEX) or []
        overlay_item = find_aligned_overlay(v2_items, clip_start, clip_end)

        if overlay_item is None:
            blockers = []
            for item in v2_items:
                try:
                    if frames_overlap(
                        int(item.GetStart()), int(item.GetEnd()),
                        clip_start, clip_end,
                    ):
                        blockers.append(item)
                except Exception:
                    continue
            if blockers:
                try:
                    timeline.DeleteClips(blockers, False)
                    print(f"       Overlay: removed {len(blockers)} misaligned V2 item(s)")
                    time.sleep(0.2)
                except Exception as exc:
                    print(f"       \u26a0 Could not clear blocking V2 items: {exc}")

            try:
                overlay_item = duplicate_to_timeline(
                    media_pool, template_item,
                    clip_start, duration, SCALE_BAR_TRACK_INDEX,
                    fps, timeline=timeline, mp_item=template_mp_item,
                )
                template_item = overlay_item
                print(f"       Overlay: placed (frames {clip_start}\u2013{clip_end - 1})")
                time.sleep(0.3)
            except RuntimeError as exc:
                print(f"       \u26a0 ERROR placing overlay: {exc}")
                warnings.append((clip_num, clip_name, [f"Placement failed: {exc}"]))
                skipped += 1
                print()
                continue
        else:
            print("       Overlay: exists \u2014 updating")

        # ── Get Fusion comp and update ──
        comp = get_comp_for_overlay(resolve, timeline, overlay_item, fps)
        issues = update_scale_bar(
            comp, lens_key, transform, sensor_w, sensor_h,
            comp_w, comp_h, speed_label,
        )

        if issues:
            warnings.append((clip_num, clip_name, issues))
            skipped += 1
        else:
            processed += 1
        print()

    # ── Return to Edit page ──
    resolve.OpenPage("edit")

    # ── Summary ──
    print("=" * 60)
    print("SUMMARY")
    print(f"  Total clips   : {len(v1_clips)}")
    print(f"  Processed     : {processed}")
    if skipped > 0:
        print(f"  Issues        : {skipped}")

    # ── Resolution fallback summary ──
    if resolution_fallback_clips:
        print()
        print("  \u26a0\u26a0\u26a0 RESOLUTION FALLBACK WARNING:")
        print(f"  {len(resolution_fallback_clips)} clip(s) could not have their source resolution read.")
        print(f"  The script used the fallback value {REF_FRAME_WIDTH}x{REF_FRAME_HEIGHT}.")
        print(f"  If your source footage is NOT {REF_FRAME_WIDTH}x{REF_FRAME_HEIGHT}, the scale bars")
        print(f"  on these clips WILL BE WRONG.  Please verify manually:")
        for num, name in resolution_fallback_clips:
            print(f"    [{num}] {name}")

    if warnings:
        print()
        print("  \u26a0 CLIPS NEEDING ATTENTION:")
        for num, name, issue_list in warnings:
            print(f"    [{num}] {name}")
            for issue in issue_list:
                print(f"        - {issue}")
    else:
        print()
        print("  All clips processed successfully.")

    print("=" * 60)

if __name__ == "__main__":
    main()
