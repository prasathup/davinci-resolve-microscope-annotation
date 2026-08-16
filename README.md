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

Tested on macOS with DaVinci Resolve Studio. These are personal tools shared as they are, so they may need small changes for your setup.
