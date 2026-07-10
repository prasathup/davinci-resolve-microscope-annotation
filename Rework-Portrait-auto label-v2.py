#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SCALE BAR — CLIP REWORK (Vertical / 9:16)
==========================================

For VERTICAL (9:16 phone) timelines only.  For landscape (16:9)
timelines, use "Rework timeline scale_bar 08072026.py" instead.

Uses smaller per-lens default scale labels (~half of landscape) so the
bar does not overlap the speed icon on narrow phone frames.

Selectively processes only clips that need attention:
  1. V1 clips tagged with ClipRework = "New" in metadata
  2. V1 clips with NO overlay on V2 (empty gap above them)

After updating, clears the ClipRework tag back to "" automatically.

Fixes scale-bar size drift when switching project from 6K to 4K by
computing the source→comp transform explicitly.

PREREQUISITES
  1. Open DaVinci Resolve Studio and load your project.
  2. Your timeline must have:
     - Video clips on V1 (source track)
     - A "Scale Bar fusion" template overlay on V2, positioned directly
       above the FIRST V1 clip.
  3. The template Fusion comp must contain tools named exactly:
       ScaleBar       (RectangleMask — controls bar width)
       ScalebarText   (Text+ — displays e.g. "100µm")
       SpeedRampText  (Text+ — displays e.g. "2X")
  4. Each source clip should have its Lens Type set in:
       Metadata panel > Camera > Lens Type   (e.g. "5X", "10X", "40X")
  5. To flag a clip for rework:
       Metadata panel > add a custom field "ClipRework" > set value to "New"

HOW TO RUN
  1. Tag any reworked clips: set metadata field ClipRework = "New"
  2. In DaVinci Resolve: Workspace > Scripts
  3. Select this script and click Run.
  4. The script scans V1 for clips tagged "New" or missing V2 overlays.
  5. It places/updates overlays, then clears the tag automatically.
  6. Check the SUMMARY at the end for any clips needing manual attention.

SETUP (first time):
  In Resolve > Library > Metadata > add a custom field called "ClipRework".
  Tag any clip you've reworked with value "New" and re-run this script.
"""

import os, re, sys, time

# ── CONFIG ────────────────────────────────────────────────────────────────────
SOURCE_TRACK_INDEX     = 1
SCALE_BAR_TRACK_INDEX  = 2
REF_FRAME_WIDTH        = 6144          # 6K sensor default (fallback only)
REF_FRAME_HEIGHT       = 3456
ZOOM_TOLERANCE         = 0.05
NICE_STEPS             = [10, 25, 50, 100, 200, 500, 1000]
DEBUG                  = True

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

# Fusion tools that must exist in the template comp
REQUIRED_TOOLS = {"ScaleBar", "ScalebarText", "SpeedRampText"}

# Speed property keys to try via the API
SPEED_PROPERTY_KEYS = [
    "PlaybackSpeed", "Speed", "SpeedPercent", "RetimeSpeed", "ChangeSpeed",
]

# Rework metadata field
CLIPREWORK_FIELD = "ClipRework"
CLIPREWORK_VALUE = "New"

# Overlay position: margin from the corner of the visible footage rect (in
# normalised comp units, 0–1).
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

    When FALLBACK is used the caller MUST print a prominent warning.
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

    result["zoom_x"] = _normalise_zoom(result["zoom_x"])
    result["zoom_y"] = _normalise_zoom(result["zoom_y"])

    return result


# ── GEOMETRY ──────────────────────────────────────────────────────────────────

def compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h):
    """Compute the base scale factor for a vertical timeline."""
    src_aspect = float(sensor_w) / float(sensor_h)
    comp_aspect = float(comp_w) / float(comp_h)

    if abs(src_aspect - comp_aspect) < 0.01:
        scale = float(comp_w) / float(sensor_w)
        return scale, scale, "same_aspect"

    scale = float(comp_h) / float(sensor_h)
    return scale, scale, "fit_height"


def compute_visible_rect(sensor_w, sensor_h, comp_w, comp_h, transform):
    """Compute the bounding box of the visible footage in comp coordinates.

    Returns (left, top, right, bottom, eff_scale_x, fit_mode).
    """
    base_scale, _, fit_mode = compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h)

    zoom_x = transform["zoom_x"]
    zoom_y = transform["zoom_y"]

    eff_scale_x = base_scale * zoom_x
    eff_scale_y = base_scale * zoom_y

    scaled_w = sensor_w * eff_scale_x
    scaled_h = sensor_h * eff_scale_y

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

    Returns (target_microns, w_norm, trace_string).
    """
    calib           = LENS_CALIBRATION.get(lens_key, LENS_CALIBRATION[DEFAULT_LENS])
    px_per_10um     = calib["px_per_10um"]
    default_microns = calib["default_microns"]

    base_scale, _, fit_mode = compute_fit_scale(sensor_w, sensor_h, comp_w, comp_h)
    zoom_x = transform["zoom_x"]
    eff_scale_x = base_scale * zoom_x

    out_px_per_10um = px_per_10um * eff_scale_x

    # Pin reference at zoom=1 so the picker prefers smaller labels as zoom increases
    px_per_10um_at_base = px_per_10um * base_scale
    ref_fraction = (default_microns / 10.0) * px_per_10um_at_base / float(comp_w)

    trace = []
    trace.append(f"sensor {sensor_w}\u00d7{sensor_h}")
    trace.append(f"comp {comp_w}\u00d7{comp_h}")
    trace.append(f"fit={fit_mode} base_scale={base_scale:.4f}")
    trace.append(f"zoom_x={zoom_x:.2f}")
    trace.append(f"eff_scale={eff_scale_x:.4f}")
    trace.append(f"out_px/10\u00b5m={out_px_per_10um:.1f}")

    if abs(zoom_x - 1.0) <= ZOOM_TOLERANCE:
        target_microns = default_microns
        trace.append(f"zoom\u22481 \u2192 default {default_microns}\u00b5m")
    else:
        target_microns = min(
            NICE_STEPS,
            key=lambda m: abs((m / 10.0) * out_px_per_10um / float(comp_w) - ref_fraction)
        )
        trace.append(f"best-fit label={target_microns}\u00b5m")

    bar_comp_px = (target_microns / 10.0) * out_px_per_10um
    w_norm = bar_comp_px / float(comp_w)
    trace.append(f"bar={bar_comp_px:.1f}comp-px \u2192 w_norm={w_norm:.6f}")

    return target_microns, w_norm, " | ".join(trace)


# ── HELPERS ───────────────────────────────────────────────────────────────────
def frames_overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1

def find_overlapping_item(items, start, end):
    for item in (items or []):
        try:
            if frames_overlap(int(item.GetStart()), int(item.GetEnd()), start, end):
                return item
        except Exception: continue
    return None

def get_metadata_by_key(mpi, target_key):
    """Case/space-insensitive metadata lookup."""
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
    """Return (speed_pct, trace_string) showing how speed was calculated."""
    for key in SPEED_PROPERTY_KEYS:
        try:
            val = clip.GetProperty(key)
            if val is not None:
                v = float(val)
                if v > 0:
                    return v, f"from API property '{key}' = {v:.1f}%"
        except Exception: pass
    try:
        src_start = int(clip.GetSourceStartFrame())
        src_end   = int(clip.GetSourceEndFrame())
        tl_dur    = int(clip.GetDuration())
        src_dur   = src_end - src_start
        if tl_dur > 0 and src_dur > 0:
            raw_pct = float(src_dur) / float(tl_dur) * 100.0
            clip_fps = get_clip_fps(clip, timeline_fps)
            if abs(clip_fps - timeline_fps) > 0.01:
                adjusted = raw_pct * (timeline_fps / clip_fps)
                trace = (f"src={src_dur}f / tl={tl_dur}f = {raw_pct:.1f}% (raw)"
                         f" \u2192 FPS adjust ({clip_fps:.0f}\u2192{timeline_fps:.0f})"
                         f" \u2192 {adjusted:.1f}%")
                return adjusted, trace
            else:
                trace = f"src={src_dur}f / tl={tl_dur}f = {raw_pct:.1f}%"
                return raw_pct, trace
    except Exception: pass
    return 100.0, "no retime data found \u2192 assuming 100%"

def format_speed_label(speed_percent):
    """Format speed as a compact label (e.g. "1X", "2X", "2.5X")."""
    speed_x = float(speed_percent) / 100.0
    rounded = round(speed_x)
    if abs(speed_x - rounded) < 0.05:
        return f"{int(rounded)}X"
    return f"{speed_x:.1f}X"

def copy_inspector_properties(src, dst):
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
        tools = comp.GetToolList(False)   # False = ALL tools (True = selected only!)
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
    """Check if comp has all required tools using FindTool."""
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
    """Get Fusion comp directly from a timeline item."""
    try:
        count = item.GetFusionCompCount()
        if count and int(count) > 0:
            return item.GetFusionCompByIndex(1)
    except Exception: pass
    return None

def get_comp_for_overlay(resolve, timeline, overlay_item, fps):
    """Get the fully loaded Fusion comp for the V2 overlay item."""
    comp = try_comp_from_item(overlay_item)
    if comp and has_required_tools(comp):
        if DEBUG:
            print("       Comp: direct access (all tools found)")
        return comp

    navigate_to_comp(resolve, timeline, overlay_item, fps)

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

    if comp:
        print("       \u26a0 Comp loaded after timeout \u2014 some tools may be missing")
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

def duplicate_to_timeline(media_pool, template_item, record_frame, duration_frames, track_index):
    """Place a copy of the template Fusion comp on V2 at the specified position."""
    mp_item = template_item.GetMediaPoolItem()
    if mp_item is None:
        mp_item = search_media_pool(media_pool.GetRootFolder(), template_item.GetName())
    if mp_item is None:
        raise RuntimeError(f"Cannot find '{template_item.GetName()}' in the Media Pool.")

    desired_dur = max(1, int(duration_frames))
    appended = media_pool.AppendToTimeline([{
        "mediaPoolItem": mp_item,
        "startFrame":    0,
        "endFrame":      desired_dur - 1,
        "trackIndex":    track_index,
        "recordFrame":   int(record_frame),
    }])
    if not appended:
        raise RuntimeError(f"AppendToTimeline failed for V{track_index}.")

    new_item = appended[0]
    actual_dur = int(new_item.GetEnd()) - int(new_item.GetStart())
    if actual_dur != desired_dur:
        print(f"       Note: V2 duration {actual_dur}f (wanted {desired_dur}f)")

    copy_inspector_properties(template_item, new_item)
    return new_item

# ── OVERLAY POSITION ADJUSTMENT ───────────────────────────────────────────────

def try_set_tool_center(tool, center_x, center_y):
    """Try to set a Fusion tool's Center position.
    Fusion normalised coordinates: (0,0) = bottom-left, (1,1) = top-right."""
    try:
        tool.SetInput("Center", {1: center_x, 2: center_y})
        return True
    except Exception:
        pass
    try:
        tool.SetInput("CenterX", center_x)
        tool.SetInput("CenterY", center_y)
        return True
    except Exception:
        pass
    return False


def adjust_overlay_positions(comp, visible_rect, comp_w, comp_h):
    """Try to position overlay tools near the top-left of the visible footage."""
    if not comp:
        return

    left, top, right, bottom = visible_rect

    target_x_px = left + OVERLAY_MARGIN_X * comp_w
    target_y_px = top + OVERLAY_MARGIN_Y * comp_h

    target_x_norm = target_x_px / float(comp_w)
    target_y_norm = 1.0 - (target_y_px / float(comp_h))  # flip Y for Fusion

    target_x_norm = max(0.0, min(1.0, target_x_norm))
    target_y_norm = max(0.0, min(1.0, target_y_norm))

    positioned = []
    skipped = []

    tool_names_to_try = []
    try:
        tools = comp.GetToolList(False)
        tl = list(tools.values()) if isinstance(tools, dict) else list(tools)
        for tool in tl:
            try:
                attrs = tool.GetAttrs()
                name = attrs.get("TOOLS_Name", "")
                reg_id = attrs.get("TOOLS_RegID", "")
                if name in REQUIRED_TOOLS:
                    continue
                if any(kw in name.lower() for kw in ("transform", "center", "position",
                                                      "icon", "clock", "overlay",
                                                      "merge", "loader")):
                    tool_names_to_try.append((name, tool))
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
        print(f"       Position : moved {', '.join(positioned)} \u2192 "
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

    scalebar_tool = find_tool(comp, "ScaleBar")
    if scalebar_tool:
        set_ok = False
        for inp in ("Width", "XSize", "Size"):
            try:
                scalebar_tool.SetInput(inp, w_norm)
                print(f"       \u2192 wrote  : ScaleBar.{inp} = {w_norm:.6f}")
                set_ok = True; break
            except Exception: continue
        if not set_ok:
            issues.append("Could not set ScaleBar width")
            print("       \u26a0 Could not set ScaleBar width")
    else:
        issues.append("ScaleBar tool not found")
        print("       \u26a0 ScaleBar tool not found in comp")

    text_tool = find_tool(comp, "ScalebarText")
    if text_tool:
        try:
            text_tool.SetInput("StyledText", label)
            print(f"       \u2192 wrote  : ScalebarText = '{label}'")
        except Exception as exc:
            issues.append(f"ScalebarText: {exc}")
            print(f"       \u26a0 ScalebarText failed: {exc}")
    else:
        issues.append("ScalebarText not found")
        print("       \u26a0 ScalebarText tool not found")

    speed_tool = find_tool(comp, "SpeedRampText")
    if speed_tool:
        try:
            speed_tool.SetInput("StyledText", speed_label)
            print(f"       \u2192 wrote  : SpeedRampText = '{speed_label}'")
        except Exception as exc:
            issues.append(f"SpeedRampText: {exc}")
            print(f"       \u26a0 SpeedRampText failed: {exc}")
    else:
        issues.append("SpeedRampText not found")
        print("       \u26a0 SpeedRampText tool not found")

    # ── Overlay position adjustment ──
    # DISABLED: adjust_overlay_positions() was moving Merge/Transform/Loader
    # Centers, which shifted the entire composited overlay off-screen when the
    # comp unlocked (the scale bar/text/speed icon appear while editing but
    # vanish once processing finishes; SpeedRampText never appears at all).
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

# ── REWORK-SPECIFIC HELPERS ──────────────────────────────────────────────────

def get_clip_rework_flag(clip):
    """Read ClipRework from the clip's Media Pool Item metadata."""
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            val = mpi.GetMetadata(CLIPREWORK_FIELD) or ""
            return str(val).strip()
    except Exception: pass
    return ""

def clear_clip_rework_flag(clip):
    """Clear the ClipRework tag after processing."""
    try:
        mpi = clip.GetMediaPoolItem()
        if mpi:
            mpi.SetMetadata(CLIPREWORK_FIELD, "")
    except Exception: pass

# =============================================================================
#  MAIN
# =============================================================================
def main():
    print("=" * 60)
    print("  SCALE BAR \u2014 CLIP REWORK (Vertical 9:16)")
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
        print(f"    Use 'Rework timeline scale_bar 08072026.py' for 16:9 projects.")
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

    template_item = find_overlapping_item(
        timeline.GetItemListInTrack("video", SCALE_BAR_TRACK_INDEX),
        first_start, first_end,
    )
    if template_item is None:
        print(f"\n\u26a0 ERROR: No overlay on V{SCALE_BAR_TRACK_INDEX} above the first V1 clip.")
        print("  Place a 'Scale Bar fusion' template on V2 above clip #1 and re-run.")
        return

    t_dur = int(template_item.GetEnd()) - int(template_item.GetStart())
    print(f"  Template      : '{template_item.GetName()}' ({t_dur} frames)")
    print(f"  V1 clips      : {len(v1_clips)}")
    print(f"  Trigger       : ClipRework = '{CLIPREWORK_VALUE}' OR empty V2")

    # ── Scan: which clips need work? ─────────────────────────────────────
    v2_items = timeline.GetItemListInTrack("video", SCALE_BAR_TRACK_INDEX) or []
    to_process = []

    for src_clip in v1_clips:
        clip_start = int(src_clip.GetStart())
        clip_end   = int(src_clip.GetEnd())
        rework     = get_clip_rework_flag(src_clip)
        overlay    = find_overlapping_item(v2_items, clip_start, clip_end)

        if overlay is None:
            to_process.append((src_clip, "no overlay on V2", None))
        elif rework.lower() == CLIPREWORK_VALUE.lower():
            to_process.append((src_clip, f"ClipRework='{rework}'", overlay))

    if not to_process:
        print("\n  Nothing to do \u2014 no clips tagged 'New' and no empty V2 gaps found.")
        resolve.OpenPage("edit")
        return

    print(f"  To process    : {len(to_process)}")
    print()
    print("-" * 60)
    print()

    # ── Phase 1: Place any missing overlays (Edit page) ──────────────────
    print("PHASE 1 \u2014 Placing missing overlays")
    print()
    resolve.OpenPage("edit")
    time.sleep(0.5)

    work_list = []

    for src_clip, reason, existing_overlay in to_process:
        clip_start = int(src_clip.GetStart())
        clip_end   = int(src_clip.GetEnd())
        duration   = clip_end - clip_start
        if duration <= 0:
            duration = int(src_clip.GetDuration())
        is_first = (clip_start == first_start and clip_end == first_end)

        if existing_overlay is not None:
            work_list.append((src_clip, existing_overlay, reason))
            print(f"  '{src_clip.GetName()}' \u2014 {reason} (overlay exists, will update)")
        elif is_first:
            work_list.append((src_clip, template_item, reason))
            print(f"  '{src_clip.GetName()}' \u2014 {reason} (template in place)")
        else:
            try:
                item = duplicate_to_timeline(
                    media_pool, template_item,
                    clip_start, duration, SCALE_BAR_TRACK_INDEX,
                )
                work_list.append((src_clip, item, reason))
                print(f"  '{src_clip.GetName()}' \u2014 {reason} \u2192 placed (frames {clip_start}\u2013{clip_end-1})")
            except RuntimeError as exc:
                print(f"  '{src_clip.GetName()}' \u2014 \u26a0 ERROR placing: {exc}")

    # ── Phase 2: Update comps (Fusion page) ───────────────────────────────
    total = len(work_list)
    print()
    print(f"PHASE 2 \u2014 Updating {total} scale bars")
    print()

    processed = 0
    warnings  = []
    resolution_fallback_clips = []

    for i, (src_clip, overlay_item, reason) in enumerate(work_list):
        clip_num  = i + 1
        clip_name = src_clip.GetName()
        clip_start = int(src_clip.GetStart())

        # ── Switch to Edit page & position playhead before reading props ──
        resolve.OpenPage("edit")
        time.sleep(0.2)
        timeline.SetCurrentTimecode(frame_to_timecode(clip_start, fps))
        time.sleep(0.3)

        lens_key, lens_source = get_lens_key(src_clip)
        transform   = get_clip_transform(src_clip)
        sensor_w, sensor_h, res_source = get_source_resolution(src_clip)
        clip_fps    = get_clip_fps(src_clip, fps)
        speed_pct, speed_trace = get_speed_percent(src_clip, fps)
        speed_label = format_speed_label(speed_pct)

        if lens_source == "DEFAULT":
            lens_info = f"{lens_key} (DEFAULT \u2014 no metadata found)"
        elif lens_source == "filename":
            lens_info = f"{lens_key} (from filename)"
        else:
            lens_info = f"{lens_key} (from metadata)"

        print(f"[{clip_num}/{total}] {clip_name}")
        print(f"       Lens     : {lens_info}")
        print(f"       Zoom     : x{transform['zoom_x']:.2f}")
        print(f"       Speed    : {speed_trace} \u2192 label '{speed_label}'")
        print(f"       Source   : {sensor_w}x{sensor_h} ({res_source})  |  "
              f"Comp: {comp_w}x{comp_h}")
        print(f"       Reason   : {reason}")

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

        comp = get_comp_for_overlay(resolve, timeline, overlay_item, fps)
        issues = update_scale_bar(
            comp, lens_key, transform, sensor_w, sensor_h,
            comp_w, comp_h, speed_label,
        )

        clear_clip_rework_flag(src_clip)
        print("       ClipRework tag cleared.")

        if issues:
            warnings.append((clip_num, clip_name, issues))
        else:
            processed += 1
        print()

    # ── Return to Edit page ──
    resolve.OpenPage("edit")

    # ── Summary ──
    print("=" * 60)
    print("SUMMARY")
    print(f"  Total clips   : {total}")
    print(f"  Processed     : {processed}")
    if total - processed > 0:
        print(f"  Issues        : {total - processed}")

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
