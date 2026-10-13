# Media Ingest Tool (`mit`)

A macOS command-line tool that offloads camera and audio cards to two or more
destination drives, verifies every copy by checksum, and keeps reports and a
media log on each drive. See [SPEC.md](SPEC.md) for the full specification.

**Status:** build stages 1–7 of 8 are done: config, naming, verified
multi-destination copying, clip metadata, the per-card job report, the master
log with its spreadsheet export and project history, the daily card log, and
`mit verify`. Phase 2 (ASC MHL manifests, proxies) is next. Sony and
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
| `--operator NAME` | Operator, by name or number from `project.json` (asked if there are several) |
| `--notes TEXT` | Notes for this card (otherwise asked at the confirmation step) |
| `--copy-all` | Also copy macOS system files (see below) |
| `--allow-same-volume` | **Testing only.** Allows destinations on the same drive |
| `--config PATH` | Project config (default `project.json`, given before `ingest`) |

You confirm the shoot date, the day details (first card of a day only), the
card name, the media type and the target paths before anything is copied.

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
  (with any old card record and report), never deleted;
- the card is copied fresh under the **same reel number**.

If you say no, the next reel number is used and the incomplete card stays flagged.

### Job report

`<CARD>_report.html` is written after every offload, PASS or FAIL, to
`_REPORTS/<date>/` on every destination and into each card folder, so the
report travels with the media. It shows:
- the header: project, card, camera, shoot date, offload time, operator,
  source and destination volumes, and a large PASS/FAIL banner;
- a clip table with metadata and a verification column per destination;
- other files (sidecars, proxies, ...), totals and notes. Totals show the
  copy time and speed, the verify time (every destination re-read) and the
  total time.

On a FAIL, every failed file is listed first in red. A copy that fails partway
also gets a FAIL report, with files marked NOT VERIFIED. Stills cards with more
than 100 stills get a summary, with the full list in a collapsed section.

The report is a single self-contained HTML file. For a PDF, open it in a
browser and use **File > Print > Save as PDF**; it's laid out for landscape
printing.

Rebuild the `_REPORTS` copies for a day from the card records:

```bash
.venv/bin/mit report 10122026
```

Destinations default to `default_destinations` in `project.json`; use `--dest`
to name them. The report copy inside each card folder is never rewritten. It
stays exactly as it was when the card was verified.

### Master log and spreadsheet

Each drive keeps its own master log, `_LOGS/media_log.sqlite`. It is the source
of truth for that drive, cumulative across every offload to it:
- `offloads`: one row per offload attempt, with this drive's result;
- `clips`: one row per clip, tagged with card, date, camera, offload time,
  this drive's volume and its verification status there;
- `files`: every file's hash, including sidecars (used by `mit verify`).

Failed attempts stay in the log as history. When a card is recopied, the earlier
attempt is marked superseded and left out of totals.

After every offload, `_LOGS/[DRIVE]_media_log.xlsx` is regenerated from the
log. It has **Clips**, **Cards** and **Days** sheets (Days has the running total
on the drive). It's a read-only view: edits made in it are never read back.
Card names, reels, timecodes, checksums and dates are text cells; GB has two
decimals; durations are `HH:MM:SS`; failures are red; superseded attempts are
grey.

**Safe writes.** The log is never changed in place. Each update copies it to a
temp file, changes the copy, runs SQLite's integrity check, flushes it and
atomically swaps it in. A lock file (`_LOGS/.media_log.lock`) makes a second
offload running at the same time wait its turn. If the log can't be updated,
the offload still passes (the media verified) and a loud warning says to run
`mit log rebuild`.

```bash
.venv/bin/mit log export
```

```bash
.venv/bin/mit log rebuild
```

`export` regenerates the spreadsheet. `rebuild` adds any card records on the
drive (from `_REPORTS` and `_INCOMPLETE`) that the log is missing, then exports.
It never removes rows from a healthy log. A damaged log is renamed aside, not
deleted, and rebuilt from the records. Both take `--dest`; otherwise they use
`default_destinations` from `project.json`.

### Project history (all drive sets)

Each drive's log only knows its own contents. So that totals carry on when a
season moves to new drives (e.g. DRV01A/B to DRV02A/B), every offload is also
recorded in `project_history.sqlite`, kept on the Mac **next to
`project.json`**. It uses the same safe writes as the drive logs.

- Each drive's master log gets a copy of the all-drives summary after every
  offload. Its spreadsheet shows it on an **All drives** sheet, and the Days
  sheet gains a **Project total GB (all drives)** column. A later drive
  therefore carries the earlier drives' totals even when they're on a shelf.
- **Reel numbers are never reused across drive sets.** If camera A shot A01 to
  A03 on DRV01 and the day continues on DRV02, the next card is proposed as A04.

```bash
.venv/bin/mit history show
```

```bash
.venv/bin/mit history rebuild --dest DRV01A --dest DRV02A
```

`show` prints the totals per drive set. `rebuild` adds the card records on the
connected drives to the history; this is the recovery path if the Mac's copy is
lost, for example on a new laptop. It then refreshes those drives' all-drives
summaries. A damaged history is renamed aside, not deleted.

### Daily card log

`_REPORTS/<date>/<date>_daily_card_log.html` replaces the hand-written card
log. It's rebuilt on every destination after each offload for that date, by
`mit report <date>`, and by `mit daylog <date>`. It shows:
- the show, the day's header fields (Season, Location, Leg, ...), crew, shoot
  date and drive names;
- a PASS/FAIL status (PASS only if every card that day passed), with failed
  cards listed;
- one line per camera, e.g. *Cam D: 1 Card, 64.10 GB (Timelapse, Shooter
  Name)*, with each card listed under it;
- totals: cards and GB for the day, the running total on each drive and the
  project total across all drives (both up to and including that date), and
  each drive's capacity and free space;
- camera notes (each card's notes) and day notes.

If the destinations disagree on any total, or one isn't connected, the page
says so. It prints on one portrait page for a typical day (Print > Save as PDF).
If a day is split across drive sets, each set's log covers the cards it holds.

**Day details.** Header fields and crew change during a season, so they're
saved per day: in `_REPORTS/<date>/<date>_day.json` on every destination and
in `days/` next to `project.json`. On the first offload of a new day, `mit
ingest` shows the previous day's details (or `project.json`'s on the first day)
and asks *"Use these for 10/13/2026? [Y/n]"*. Answering **n** walks through
each field: Enter keeps it, typing replaces it, `-` removes it, and you can add
new fields at the end. Later cards that day don't ask again.

```bash
.venv/bin/mit daylog 10132026
```

edits a day's header fields, crew and day notes at any time and rebuilds its
log. `--notes "..."` sets the day notes without prompts.

### Verifying card folders later

`mit verify` re-hashes card folders against the hashes in the log, for drive
spot-checks or checking a shuttle drive on arrival. It only reads the media.

```bash
.venv/bin/mit verify /Volumes/SHOW01_DRV01B/10122026/VIDEO/ABC261012A01
```

```bash
.venv/bin/mit verify --drive SHOW01_DRV01B --date 10122026
```

```bash
.venv/bin/mit verify --drive SHOW01_DRV01B
```

- Give one or more card folders, or `--drive` to check every card the drive's
  log lists (optionally just one `--date`). With `--drive`, a card folder that
  should be there but isn't is a **FAIL**, and card folders on the drive that
  the log doesn't list are noted.
- **Where the hashes come from:** the drive's master log (each card's current
  offload). Without a healthy log, the card record in `_REPORTS` is used
  instead. If the log and card record disagree, that's a FAIL.
- **What fails:** a changed byte (MISMATCH), a missing, truncated or unreadable
  file, or a missing folder.
- **Not failures:** unexpected extra files are listed as warnings. macOS clutter
  such as `.DS_Store` and the job report copy are ignored.
- **Results** are recorded in the drive's master log and on the spreadsheet's
  **Verifications** sheet (failures in red). `--no-record` writes nothing.
- **Exit status:** `0` all passed, `1` a card failed, `2` error (not a card
  folder, nothing to verify against).

Verify reads every byte: a full 8 TB drive at about 1 GB/s takes over two
hours, so `--date` is useful for spot-checks.

### Card record

`_REPORTS/<date>/<CARD>_card.json` on every destination holds each file's size,
source xxHash64 and verification result per destination, the skipped clutter,
each clip's metadata, and the job details: project, camera, operator, notes,
times and transfer speed. It is never overwritten. Reports are built from it,
and the master log can be rebuilt from it.

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
- **Sony XML sidecars** (`<clip>M01.XML`) are read first for the recording
  time, camera model and serial: they carry local time with its UTC offset.
  The FX6 writes UTC into its MXF files, so without the sidecar evening clips
  would land on the next day. The C80 MP4's own EXIF block has local time with
  its offset, so the C80 sidecar isn't needed.
- **Otherwise dates are kept as the camera wrote them** (container times are
  not converted from "UTC").
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
