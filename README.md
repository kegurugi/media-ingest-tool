# Media Ingest Tool (`mit`)

A macOS command-line tool that offloads camera and audio cards to two or more
destination drives, verifies every copy by checksum, and keeps reports and a
media log on each drive. See [SPEC.md](SPEC.md) for the full specification.

**Status:** build stage 1 of 8 is done: project config, folder and naming logic.
The commands are stubs until later stages.

## Installation

Requires Python 3.9 or newer; developed on Python 3.13 (`brew install python@3.13`).

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the tests:

```bash
.venv/bin/python -m pytest
```

## Project config

Copy `project.example.json` to `project.json` and edit it once per project:

| Key | Meaning |
|---|---|
| `show_code` | 3-letter show code, used in every card name |
| `project_name` | Shown on reports |
| `header_fields` | Label/value pairs for the daily card log (Season, Location, Leg…) |
| `crew` | List of `{"role", "name"}` for the daily card log |
| `cameras` | Camera ID (1–3 letters/digits) → `model`, optional `description`, `shooter`, `media_type` |
| `operators` | DIT/operator names |
| `default_destinations` | Destination volume names, e.g. `SHOW01_DRV01A` |

A camera can be written as just its model (`"A": "Sony FX6"`). Every problem in
the file is reported at once, with the field that's wrong.

## Naming and folders

Card folders are named `[SHOW][YYMMDD][CAM][REEL]`, e.g. `ABC261012A01` or
`ABC261012DR102`, and live at `<DRIVE>/MMDDYYYY/{VIDEO|AUDIO|STILLS}/<CARD>/`.

- **Reel numbers** are the highest existing reel + 1 for that show, date and
  camera, counted across VIDEO, AUDIO and STILLS together. Camera `D` never
  matches drone `DR1`'s cards. If the destinations hold different reels, the
  tool stops.
- **Media type** is proposed; the operator decides. A card is never split
  between folders.
  1. Any video on the card → VIDEO.
  2. No video → the camera's configured `media_type` (a timelapse camera
     configured as VIDEO goes to VIDEO even though its card holds only stills).
  3. Otherwise only audio → AUDIO, only stills → STILLS, else the operator picks.

  Thumbnails inside camera structures (`PRIVATE/M4ROOT`, `CONTENTS`…) aren't
  counted as stills. Scratch audio inside video files doesn't count as audio.
  There's a loud warning if video turns up on an AUDIO camera, or if a card
  containing video is filed outside VIDEO.
- **Shoot date** is the earliest clip date. If clips span more than one date
  (shooting past midnight), the operator has to decide.

## Safety design

- The source card is only ever listed and read, never written, renamed or deleted.
- A card folder is never reused or overwritten. If the card name already
  exists on a destination, under any media type, the tool stops.
- A destination that isn't mounted is an error. The tool never creates a stray
  `/Volumes/<name>` folder on the boot disk.
- Logs and config are written to a temp file and atomically swapped in, so a
  crash can't corrupt them. (If that write fails, its own half-written temp
  file is removed; nothing else is ever deleted.)
- Copy verification, space checks and FAILED handling arrive in stage 2.
