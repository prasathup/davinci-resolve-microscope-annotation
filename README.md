# DaVinci Resolve scripts

Python scripts that automate parts of my DaVinci Resolve workflow for microscopy films.

| Script | What it does |
|---|---|
| Full-timeline auto label | Adds or updates a scale bar, scale label and speed label on every clip |
| Rework auto label | The same, but only for clips you tagged or that don't have an overlay yet |
| Retime automation | Speeds up long clips so each one plays for about 4 to 6 seconds (macOS only) |

The scale bar scripts come in Landscape (16:9) and Portrait (9:16) versions.

## How to use

Copy a script into Resolve's scripts folder:

```
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/
```

Then run it from **Workspace > Scripts**. The setup and step-by-step instructions are at the top of each script.

The scale bar scripts also need two things from you:

- a Fusion template for the overlay (each script lists the tool names it expects)
- lens calibration values for your own microscope and camera, since the ones in the scripts are for my setup

## Before you run

- **Back up first.** Duplicate the timeline or project before a run. The scale bar scripts delete any item on V2 that overlaps a clip but doesn't line up with it, so keep V2 for scale bar overlays only.
- **Retime: hands off.** The retime script types into whichever app is in front. Turn on Do Not Disturb and leave the keyboard and mouse alone until the summary prints.
- **Retime: Accessibility access.** It needs this to click Resolve's menus. Grant it to DaVinci Resolve rather than Terminal if you can, and turn it off in **System Settings > Privacy & Security > Accessibility** when you're done.

Tested on macOS with DaVinci Resolve Studio. These are personal tools shared as they are, so they may need small changes for your setup.
