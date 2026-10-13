# Media Ingest Tool (`mit`)

A macOS command-line tool that offloads camera and audio cards to two or more
destination drives, verifies every copy by checksum, and keeps reports and a
media log on each drive. See [SPEC.md](SPEC.md) for the full specification.

**Status:** build stages 1–3 of 8 are done: config, naming, verified
multi-destination copying and clip metadata. `mit ingest` works end to end.
Reports, the master log and the other commands come in later stages. Sony and
Canon XML sidecars aren't read yet; that waits for real sample cards.

## Installation

Requires Python 3.9 or newer; developed on Python 3.13 (`brew install python@3.13`).
Clip metadata needs `ffprobe` and `exiftool` (`brew install ffmpeg exiftool`).
Without them the offload still works, with a loud warning and blank metadata.

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the tests (the full-volume and killed-copy tests use `hdiutil` and run on macOS only):

```bash
.venv/bin/python -m pytest
```

## Usage

```bash
.venv/bin/mit ingest
```

It asks for anything not given as an option:

| Option | Meaning |
|---|---|
| `--source PATH` | The card, e.g. `/Volumes/A001` |
| `--dest NAME` | Destination volume name (`SHOW01_DRV01A` means `/Volumes/SHOW01_DRV01A`) or path; repeat for each. Defaults come from `project.json` |
| `--camera ID` | Camera ID from `project.json` |
| `--date MMDDYYYY` | Shoot date (overrides the proposal) |
| `--media-type` | VIDEO, AUDIO or STILLS (still confirmed) |
| `--copy-all` | Also copy macOS system files (see below) |
| `--allow-same-volume` | **Testing only.** Allows destinations on the same drive |
| `--config PATH` | Project config (default `project.json`, given before `ingest`) |

You confirm the shoot date, the card name, the media type and the target paths
before anything is copied.

The **shoot date** is proposed from the earliest clip date on the card (read
with exiftool, without changing the card). If the clips fall on more than one
date, there's a loud warning and you must type the date; no default is
offered. If no clip has a date, you're asked to type it. Exit status: `0` PASS, `1` FAIL or cancelled, `2` error before
copying, `130` interrupted.

### What gets copied

Everything the camera wrote, with the card's folder structure (including empty
folders) reproduced exactly. macOS clutter that the Mac adds to any mounted card
is skipped by default and listed in the card record: `.Spotlight-V100`,
`.Trashes`, `.fseventsd`, `.TemporaryItems`, `.DocumentRevisions-V100`,
`.DS_Store`, `.apdisk` and `._*` files. `--copy-all` copies them too.

### Interrupted or failed cards

A hidden marker, `.mit_incomplete.json`, is written into each card folder when
copying starts and removed only when every file on every destination verifies.
On a FAIL it stays and records why.

When you next offload for that camera and day, the tool shows the incomplete
card and asks whether the card in the reader is the same one. It also warns you
if the volume name, file count or size differ. If you say yes:
- the incomplete folder is **moved** to `<DRIVE>/_INCOMPLETE/<CARD>_<timestamp>/`
  (with any old card record), never deleted;
- the card is copied fresh under the **same reel number**.

If you say no, the next reel number is used and the incomplete card stays flagged.

### Card record

`_REPORTS/<date>/<CARD>_card.json` on every destination holds each file's size,
source xxHash64 and verification result per destination, the skipped clutter,
and each clip's metadata. It is never overwritten.

### Clip metadata

For every clip: filename, path, size, xxHash64, duration, start timecode,
codec, resolution, frame rate, creation date/time, camera model, and for audio
the sample rate and channels. It's read after verification from the first
destination that verified, not from the card.

- **Clips** are video, standalone audio and stills. Sony proxies
  (`M4ROOT/SUB`), thumbnails and sidecar files are copied and hashed but aren't
  listed as clips.
- **Sources:** ffprobe gives codec, resolution, frame rate, duration and video
  timecode; exiftool gives dates, camera model and stills data. Each value
  records its source in `sources`.
- **Audio timecode** is calculated exactly from the BWF time reference and the
  timecode rate in the file's iXML. Without an iXML rate it stays blank.
- **Dates are kept as the camera wrote them.** MP4/MOV container times are
  labelled UTC, but many cameras write local time there, so they aren't
  converted. This needs checking against real FX6/C80 cards.
- **Nothing is guessed.** A field no source provides stays blank, and unreadable
  files get a note.

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
- Each copy must go to a **separate physical drive**, and no destination can
  be the source card or inside it. At least two destinations are required.
- **Space check first.** If any destination lacks room (with files rounded up to
  whole disk blocks, plus a 100 MB margin), nothing is created anywhere.
- Each source file is read once. The xxHash64 is computed from those bytes as
  they're written to every destination at once. Destination files are created
  with "fail if it exists", so not even a single file can be overwritten.
  Each finished file is flushed to the drive with macOS `F_FULLFSYNC`, which
  also empties the drive's own write cache. Modification times are copied.
- **Verification re-reads every destination file** and compares its hash with
  the source hash; a matching size is never enough. Missing files, missing
  folders and unexpected extra files also fail. The OS file cache is turned off
  (`F_NOCACHE`) both when writing and when re-reading, so the re-read should
  come from the drive and not from memory. *Caveat:* this is the strongest
  check available without unmounting the drive. It can't be proven from a
  test that every byte came off the disk, and a drive's own internal cache is
  outside the tool's control.
- Any failure on any destination marks the whole card **FAILED**: a loud
  warning, the marker kept, and a non-zero exit status.
- Logs and config are written to a temp file and atomically swapped in, so a
  crash can't corrupt them.
- **Deletions.** The tool never deletes media or folders. It deletes only two
  kinds of file, both its own: the incomplete marker after a verified copy, and
  a half-written temp file if an atomic write fails.
