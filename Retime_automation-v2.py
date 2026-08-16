#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RETIME AUTOMATION: SPEED UP LONG V1 CLIPS  (macOS only)
=======================================================

I built this with Sonnet to retime all the long microscopy clips on a
timeline in one go. Every V1 clip longer than 6 s is sped up to play in
3.8–6 s (about 4.5 s) at a clean speed such as 1.5×, 2× or 3×, and set to
Optical Flow. Shorter clips stay as they are.

Short clips make the timeline easy to scroll through and compare, so I
can see what I have. Then I rework single clips as the film needs it
like to slow down or show a detail.

Resolve's scripting API can't change clip speed, so the script uses the
Change Clip Speed dialog through AppleScript and checks each result
through the API. So it only runs on macOS, and the keyboard and mouse
must stay untouched while it runs.

PREREQUISITES
  1. DaVinci Resolve Studio on macOS, in English (menus are clicked by
     name), with the default Shift+V shortcut (Select Nearest Clip/Gap).
  2. Accessibility access for the app that starts the script (Terminal,
     or DaVinci Resolve itself):
       System Settings > Privacy & Security > Accessibility
  3. For Terminal runs: Preferences > System > General >
     External scripting using = Local.

HOW TO RUN
  1. Open the timeline on the Edit page.
  2. Start the script from Terminal:
         python3 Retime_automation-v2.py --dry-run    # plan only
         python3 Retime_automation-v2.py              # real run
     or from Resolve: Workspace > Scripts, select this script. A Resolve
     run is always a real run; output shows in Workspace > Console.
  3. During the 4 s countdown, click the timeline so it has focus. Then
     leave the keyboard and mouse alone until the summary prints.
  4. Read the summary:
       RETIMED ✓   speed applied and checked
       CHECK ⚠     duration changed, but not as expected; take a look
       FAILED ✖    speed couldn't be applied after 3 tries
       SKIP        already 6 s or shorter
     To inspect a clip, select it and press Cmd+R.

  Ripple is off, so shortened clips leave gaps. Close them afterwards if
  you need to.

HOW IT WORKS (per clip)
  1. Select: Edit > Deselect All, playhead to the clip's middle (API),
     then Shift+V. Confirmed when Clip > Change Clip Duration… is
     enabled. (GetCurrentVideoItem can't confirm it: it returns the clip
     under the playhead, not the selected one.)
  2. Open: Clip > Clip Speed > Change Clip Speed. The Speed field opens
     focused, with its value selected.
  3. Fill: type the speed % and press Return. No Cmd+A: in this floating
     dialog it selects every timeline clip instead of the field text.
  4. Check: re-read the duration through the API. Unchanged: retry, up
     to 3 times. Changed: set Optical Flow, and mark CHECK if it's more
     than ~6% off. No retry then, since a second change would stack.

CONFIGURATION
  - MIN_TARGET_DURATION, MAX_TARGET_DURATION, IDEAL_TARGET: the target
    duration window, in seconds.
  - CLEAN_MULTIPLIERS: the speeds to choose from.
  - Timing values: raise them if Resolve is slow to respond.
  - DEBUG = False: quieter console.
"""

import os
import sys
import time
import subprocess


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MIN_TARGET_DURATION = 3.8
MAX_TARGET_DURATION = 6.0
IDEAL_TARGET        = 4.5

CLEAN_MULTIPLIERS = [
    1.1, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0,
    6.0, 8.0, 10.0, 15.0, 20.0,
]

SOURCE_TRACK_INDEX = 1          # V1

# Verification
MAX_ATTEMPTS_PER_CLIP = 3
VERIFY_POLL_SECONDS   = 3.0
VERIFY_TOLERANCE_PCT  = 0.06    # ±6 % of expected duration counts as OK

# Timing (seconds)
AUTOMATION_COUNTDOWN = 4
INTER_CLIP_DELAY     = 0.8
DIALOG_SETTLE_DELAY  = 0.6
SELECT_SETTLE_DELAY  = 0.4

DEBUG = True

# ── Verified menu paths (probed 2026-07 on Resolve 19, this build) ────────
# Selection detector: this top-level item is enabled iff a clip is selected.
SELECTION_DETECTOR_ITEM = "Change Clip Duration\u2026"
# The dialog opener lives in the Clip > "Clip Speed" submenu.
SPEED_SUBMENU        = "Clip Speed"
SPEED_DIALOG_ITEM    = "Change Clip Speed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESOLVE API BOOTSTRAP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_resolve():
    """Handle to the running Resolve — internal console or external run."""
    import builtins, inspect

    for ns in (globals(), vars(builtins)):
        if "resolve" in ns:
            return ns["resolve"]

    frame = inspect.currentframe()
    if frame and frame.f_back and "resolve" in frame.f_back.f_globals:
        return frame.f_back.f_globals["resolve"]

    mac_api = (
        "/Library/Application Support/Blackmagic Design/"
        "DaVinci Resolve/Developer/Scripting"
    )
    for api_root in (os.environ.get("RESOLVE_SCRIPT_API", ""), mac_api):
        mod_path = os.path.join(api_root, "Modules")
        if os.path.isdir(mod_path) and mod_path not in sys.path:
            sys.path.append(mod_path)
        try:
            import DaVinciResolveScript as bmd      # noqa: F401
            res = bmd.scriptapp("Resolve")
            if res:
                return res
        except Exception:
            pass

    raise RuntimeError(
        "Cannot connect to DaVinci Resolve.\n"
        "  • Make sure DaVinci Resolve Studio is running.\n"
        "  • Preferences → System → General → "
        "'External scripting using' = Local."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SPEED MULTIPLIER LOGIC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def choose_speed_multiplier(original_duration_sec):
    """Return (multiplier, resulting_duration_sec) for a clip duration."""
    if original_duration_sec <= MAX_TARGET_DURATION:
        return 1.0, original_duration_sec

    best_mult = best_dur = None
    best_diff = float("inf")

    for mult in CLEAN_MULTIPLIERS:
        result = original_duration_sec / mult
        if MIN_TARGET_DURATION <= result <= MAX_TARGET_DURATION:
            diff = abs(result - IDEAL_TARGET)
            if diff < best_diff:
                best_diff, best_mult, best_dur = diff, mult, result

    if best_mult:
        return best_mult, best_dur

    # No clean multiplier lands in range: compress as much as possible
    # without dropping under MIN.
    for mult in reversed(CLEAN_MULTIPLIERS):
        result = original_duration_sec / mult
        if result >= MIN_TARGET_DURATION:
            return mult, result

    m = CLEAN_MULTIPLIERS[0]
    return m, original_duration_sec / m


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TIMECODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def frame_to_timecode(frame, fps):
    """Absolute timeline frame → 'HH:MM:SS:FF' (non-drop)."""
    fps_i = int(round(fps))
    ff = frame % fps_i
    total_sec = frame // fps_i
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(
        total_sec // 3600, (total_sec % 3600) // 60, total_sec % 60, ff)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# APPLESCRIPT (all GUI actions are coordinate-free)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_applescript(script, timeout=15):
    """Execute AppleScript. Returns (stdout, stderr) strings."""
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, encoding='utf-8',
                           timeout=timeout)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "applescript timeout"
    except Exception as exc:
        return "", str(exc)


def activate_resolve():
    run_applescript('tell application "DaVinci Resolve" to activate')
    time.sleep(0.7)


def send_keystroke(applescript_key_expr):
    """Send a keystroke to Resolve (must be frontmost)."""
    return run_applescript(
        f'tell application "System Events" to tell process "Resolve" '
        f'to {applescript_key_expr}'
    )


def press_escape():
    send_keystroke("key code 53")
    time.sleep(0.25)


def deselect_all():
    """Edit → Deselect All (coordinate-free)."""
    out, err = run_applescript('''
tell application "System Events"
    tell process "Resolve"
        try
            click menu item "Deselect All" of menu "Edit" of menu bar 1
            return "ok"
        on error e
            return "err:" & e
        end try
    end tell
end tell
''')
    return out == "ok"


def clip_is_selected():
    """True iff a clip is GUI-selected (menu-enabled detector)."""
    out, _ = run_applescript(f'''
tell application "System Events"
    tell process "Resolve"
        try
            return (enabled of menu item "{SELECTION_DETECTOR_ITEM}" of menu "Clip" of menu bar 1) as string
        on error
            return "absent"
        end try
    end tell
end tell
''')
    return out == "true"


def open_speed_dialog():
    """
    Clip → 'Clip Speed' submenu → 'Change Clip Speed'.
    Opening a submenu item requires clicking the parent first so it renders.
    """
    out, err = run_applescript(f'''
tell application "System Events"
    tell process "Resolve"
        try
            click menu item "{SPEED_DIALOG_ITEM}" of menu 1 of menu item "{SPEED_SUBMENU}" of menu "Clip" of menu bar 1
            return "ok"
        on error e
            return "err:" & e
        end try
    end tell
end tell
''')
    if DEBUG:
        print(f"    Open Change Clip Speed: "
              f"{'ok' if out == 'ok' else (out or err)}")
    return out == "ok"


def fill_speed_dialog(speed_percent):
    """
    Type the speed % into the (focused) Speed field, then confirm.

    The dialog opens with the Speed field focused and its value pre-selected,
    so typing replaces it. We deliberately do NOT send Cmd+A: the dialog is a
    non-modal floating window, so Cmd+A fires the app-wide Edit > Select All
    (highlighting all timeline clips and stealing focus from the dialog) rather
    than selecting the field text — that was the v4 failure. Return activates
    the default 'Change' button. Ripple left at default OFF.
    """
    speed_str = str(int(round(speed_percent)))
    out, err = run_applescript(f'''
tell application "System Events"
    tell process "Resolve"
        delay {DIALOG_SETTLE_DELAY}
        keystroke "{speed_str}"
        delay 0.25
        key code 36
        return "ok"
    end tell
end tell
''')
    if DEBUG:
        print(f"    Fill Speed field ('{speed_str}%' + Return): "
              f"{out or err}")
    return out == "ok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SELECTION (coordinate-free)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def select_clip(clip, timeline, tl_fps):
    """
    Select one clip with no pixel coordinates:
      Deselect All → move playhead to clip centre (API) → Shift+V.
    Confirms via the menu-enabled detector. Returns True on success.
    """
    center = (clip.GetStart() + clip.GetEnd()) // 2
    tc = frame_to_timecode(center, tl_fps)

    deselect_all()
    time.sleep(0.2)
    timeline.SetCurrentTimecode(tc)
    time.sleep(SELECT_SETTLE_DELAY)
    send_keystroke('keystroke "v" using shift down')   # Select Nearest Clip
    time.sleep(SELECT_SETTLE_DELAY)

    ok = clip_is_selected()
    if DEBUG:
        print(f"    Playhead → {tc}; Shift+V; "
              f"selected = {ok}")
    return ok


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VERIFICATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def wait_for_duration_change(clip, original_frames, timeout=VERIFY_POLL_SECONDS):
    """Poll GetDuration() until it differs from original or times out."""
    deadline = time.time() + timeout
    dur = original_frames
    while time.time() < deadline:
        try:
            dur = clip.GetDuration()
        except Exception:
            break
        if dur != original_frames:
            break
        time.sleep(0.3)
    return dur


def duration_matches(actual_frames, expected_frames):
    tolerance = max(3, int(expected_frames * VERIFY_TOLERANCE_PCT))
    return abs(actual_frames - expected_frames) <= tolerance


def set_optical_flow(clip, resolve):
    """Set Optical Flow retime processing via the API (3 = Optical Flow)."""
    try:
        optical_flow = getattr(resolve, "RETIME_OPTICAL_FLOW", 3)
        result = clip.SetProperty("RetimeProcess", optical_flow)
        if DEBUG:
            print(f"    Optical Flow set via API: {result}")
    except Exception as exc:
        if DEBUG:
            print(f"    SetProperty RetimeProcess failed: {exc}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PER-CLIP PIPELINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def apply_speed_to_clip(clip, speed_multiplier, resolve, timeline, tl_fps):
    """
    One clip: select → open dialog → type speed → verify via API.

    Returns "RETIMED ✓" | "CHECK ⚠" | "FAILED ✖".
    """
    speed_pct       = speed_multiplier * 100.0
    original_frames = clip.GetDuration()
    expected_frames = int(round(original_frames / speed_multiplier))

    for attempt in range(1, MAX_ATTEMPTS_PER_CLIP + 1):
        if attempt > 1:
            print(f"    Retry {attempt}/{MAX_ATTEMPTS_PER_CLIP}...")
            press_escape()

        if not select_clip(clip, timeline, tl_fps):
            continue

        if not open_speed_dialog():
            continue

        fill_speed_dialog(speed_pct)

        new_frames = wait_for_duration_change(clip, original_frames)

        if new_frames == original_frames:
            print("    ✖ Duration unchanged — dialog likely not applied.")
            continue

        if duration_matches(new_frames, expected_frames):
            print(f"    ✓ Verified: {original_frames}f → {new_frames}f "
                  f"(expected {expected_frames}f)")
            set_optical_flow(clip, resolve)
            return "RETIMED ✓"

        print(f"    ⚠ Duration changed to {new_frames}f but expected "
              f"{expected_frames}f — flagged for review.")
        set_optical_flow(clip, resolve)
        return "CHECK ⚠"

    press_escape()
    return "FAILED ✖"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONSOLE OUTPUT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DIV  = "─" * 64
DIVW = "═" * 64


def print_banner():
    print("")
    print(DIVW)
    print("  AUTOMATIC RETIME CONTROLS — GUI AUTOMATION")
    print(DIVW)


def print_summary(results):
    print("")
    print(DIVW)
    print("  RETIME SUMMARY")
    print(DIVW)
    if not results:
        print("  No clips found.")
        return

    name_w = max(min(max(len(r["name"]) for r in results), 32), 10)
    sep = (f"  {'─'*3}  {'─'*name_w}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*12}")
    hdr = (f"  {'#':>3}  {'Clip Name':<{name_w}}  {'Original':>8}"
           f"  {'Target':>8}  {'Speed':>7}  Status")
    print(sep); print(hdr); print(sep)

    counts = {}
    for r in results:
        tag = r.get("status", "?")
        counts[tag] = counts.get(tag, 0) + 1
        print(f"  {r['index']:>3}  {r['name'][:name_w]:<{name_w}}"
              f"  {r['original']:>8}  {r['target']:>8}"
              f"  {r['speed']:>7}  {tag}")
    print(sep)

    print("")
    for tag, n in sorted(counts.items()):
        print(f"  {tag}: {n}")
    print("")
    print("  VERIFY: select a retimed clip → Cmd+R → confirm the speed %.")
    print("  Clips marked CHECK ⚠ need a manual look (unexpected duration).")
    print(DIVW)
    print("")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    dry_run = "--dry-run" in sys.argv

    print_banner()

    print(f"\n{DIV}\n  PHASE 1: Connecting to DaVinci Resolve...\n{DIV}")

    if sys.platform != "darwin":
        print("  ✖ This script uses AppleScript and requires macOS.")
        return

    try:
        resolve = get_resolve()
    except RuntimeError as exc:
        print(f"  ✖ {exc}")
        return

    pm      = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if not project:
        print("  ✖ No open project.")
        return

    timeline = project.GetCurrentTimeline()
    if not timeline:
        print("  ✖ No active timeline.")
        return

    tl_name    = timeline.GetName() or "Untitled"
    tl_fps_raw = timeline.GetSetting("timelineFrameRate")
    tl_fps     = float(tl_fps_raw) if tl_fps_raw else 24.0

    print(f"  OK Project  : {project.GetName()}")
    print(f"  OK Timeline : {tl_name}  @ {tl_fps} fps")

    v1_clips = timeline.GetItemListInTrack("video", SOURCE_TRACK_INDEX)
    if not v1_clips:
        print(f"  ✖ No clips on V{SOURCE_TRACK_INDEX}.")
        return

    print(f"  OK V{SOURCE_TRACK_INDEX} clips  : {len(v1_clips)}")

    # ── Phase 2: Analyse ─────────────────────────────────────────────
    print(f"\n{DIV}\n  PHASE 2: Analysing clip durations...\n{DIV}\n")

    plan    = []
    results = []

    for i, clip in enumerate(v1_clips, 1):
        dur_sec   = clip.GetDuration() / tl_fps
        mult, tgt = choose_speed_multiplier(dur_sec)
        name      = clip.GetName()

        if mult > 1.0:
            speed_pct = int(round(mult * 100))
            print(f"  [{i:2}]  {name[:36]:<36}  {dur_sec:6.1f}s  →  "
                  f"{tgt:5.2f}s  ({mult}× = {speed_pct}%)")
            row = {"index": i, "name": name,
                   "original": f"{dur_sec:.1f}s", "target": f"{tgt:.2f}s",
                   "speed": f"{speed_pct}%", "status": "PENDING"}
            plan.append((clip, mult, dur_sec, tgt, row))
            results.append(row)
        else:
            print(f"  [{i:2}]  {name[:36]:<36}  {dur_sec:6.1f}s  SKIP")
            results.append({"index": i, "name": name,
                            "original": f"{dur_sec:.1f}s",
                            "target": f"{dur_sec:.1f}s", "speed": "100%",
                            "status": f"SKIP ≤{MAX_TARGET_DURATION}s"})

    if not plan:
        print(f"\n  All clips already ≤{MAX_TARGET_DURATION}s. Nothing to do.")
        return

    print(f"\n  → {len(plan)} clip(s) to retime.")

    if dry_run:
        print("\n  DRY RUN — stopping before GUI automation.")
        return

    # ── Phase 3: Countdown ───────────────────────────────────────────
    print(f"\n{DIV}\n  PHASE 3: Preparing GUI automation...\n{DIV}")
    print("")
    print("  ╔══════════════════════════════════════════════════════╗")
    print("  ║  READY — DO NOT TOUCH THE KEYBOARD                   ║")
    print("  ║  Requirements: Edit page open, timeline focused      ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    print("")

    for i in range(AUTOMATION_COUNTDOWN, 0, -1):
        # Resolve's fu_stdout has no flush(); omit flush=True.
        print(f"  Starting in {i}...  ", end='\r')
        time.sleep(1)
    print("  Starting!             \n")

    activate_resolve()
    press_escape()      # dismiss anything left open

    # ── Phase 4: Apply ───────────────────────────────────────────────
    print(f"\n{DIV}\n  PHASE 4: Applying {len(plan)} speed change(s)...\n{DIV}")

    for clip, mult, orig_sec, tgt_sec, row in plan:
        speed_pct_display = int(round(mult * 100))
        print(f"\n  ▶  {clip.GetName()[:50]}")
        print(f"     {orig_sec:.1f}s  →  {tgt_sec:.2f}s  @ {speed_pct_display}%")
        row["status"] = apply_speed_to_clip(
            clip, mult, resolve, timeline, tl_fps)
        time.sleep(INTER_CLIP_DELAY)

    # ── Phase 5: Summary ─────────────────────────────────────────────
    print_summary(results)


if __name__ == "__main__":
    main()
else:
    main()
