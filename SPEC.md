# Media Ingest Tool — Specification

## Purpose
A command-line tool (Python 3, macOS) that offloads camera and audio cards to two or more destination drives. For each card it:
- copies the card to every destination,
- verifies every copy by checksum,
- extracts clip metadata,
- generates a per-card job report,
- appends to a running media log stored on each drive.

It is built to replace manual folder setup and per-card paperwork with a production-specific, auditable workflow.

## Non-negotiable safety rules
1. **Never write to, rename, or delete anything on the source card.** Open the source read-only.
2. **Never overwrite.** If a destination card folder already exists, stop and ask the operator.
3. **A copy is "verified" only after the destination file is re-read and its hash matches the source hash.** Matching file sizes or a successful copy call do not count.
4. Before copying, check free space on every destination. Abort if any destination lacks room.
5. If any file fails verification on any destination:
   - mark the whole card **FAILED** in the report and log,
   - print a loud warning,
   - exit with a non-zero status.
6. The tool never deletes files, anywhere.
7. Write logs safely: write to a temp file, then atomically replace the original, so a crash can't corrupt the master log.

## Drive structure
Drives are named manually by the operator before offload. The tool does not rename drives.

```
<DRIVE>/
├── 10122026/                 # shoot date, MMDDYYYY
│   ├── VIDEO/
│   │   ├── ABC261012A01/     # card folder (naming below); card contents copied exactly as-is
│   │   └── ABC261012B01/
│   ├── AUDIO/
│   │   └── ABC261012S01/
│   └── STILLS/               # only created when needed
├── _REPORTS/
│   └── 10122026/
│       ├── 10122026_daily_card_log.html
│       └── ABC261012A01_report.html
└── _LOGS/
    ├── media_log.sqlite      # master log, source of truth
    └── [DRIVE]_media_log.xlsx  # formatted spreadsheet export, regenerated after every offload
```

- The original card folder structure (e.g. Sony `PRIVATE/M4ROOT`, Canon `CONTENTS`) is preserved exactly inside the card folder. NLEs and camera software depend on it.
- A copy of each job report is also saved inside its card folder, so the report travels with the media.
- Every destination drive gets an identical structure, including its own `_REPORTS` and `_LOGS`.

## Card folder naming
Format: `[SHOW][YY][MM][DD][CAM][REEL]`

| Part | Rule | Example |
|---|---|---|
| SHOW | 3-letter show code from project config | `ABC` |
| YY MM DD | Shoot date | `261012` |
| CAM | Camera ID from project config. Usually one letter, but up to 3 alphanumeric characters (e.g. `DR1`, `DR2` for drones) | `A` |
| REEL | 2 digits, sequential per camera per day, starting at `01` | `01` |

Examples:
- `ABC261012A01` is the first card from camera A on Oct 12, 2026.
- `ABC261012DR102` is the second card from drone 1 on the same day.

Parsing rule: the first 3 characters are SHOW, the next 6 are the date, the last 2 are REEL, and whatever is in between is CAM.

### Determining the shoot date
- Read the earliest clip creation date from the card's metadata and propose it.
- The operator must confirm or override it, because a shoot day can run past midnight.

### Determining the reel number
- Scan the destination `[date]/VIDEO` (or `AUDIO`/`STILLS`) folder for existing folders matching the same show, date and camera.
- Propose the next number in sequence.
- The operator confirms. If the destinations disagree about the next number, stop.

### Media type
- Auto-detect VIDEO, AUDIO or STILLS from file types and card structure.
- The operator confirms.

## Project config (`project.json`)
Created once per project:
- `show_code` (3 letters)
- `project_name`
- `header_fields`: custom key-value fields printed on the daily card log, defined per show. Examples: `Season: 12`, `Location: Base Camp`, `Leg: 1`. They are editable at the start of each day, since location and leg change during a season.
- `crew`: roles and names shown on the daily log (e.g. Producer, DP, DIT). Editable per day.
- `cameras`: camera ID → model, plus an optional description and shooter. For example:
  - `A: Sony FX6`
  - `D: Timelapse, shooter "Name"`
  - `DR1: Drone 1`
  - `T: Stills, shooter "Name"`
  - Audio recorders get IDs too (e.g. `S: Sound Devices MixPre`).
- `operators`: list of names, for the DIT/operator field
- default destination volume names. Drives are named manually; example convention: `SHOW01_DRV01A` / `SHOW01_DRV01B` for the A and B copies.

## Workflow (one card)
1. The operator runs `ingest`, chooses the source card, the destinations, and the camera letter.
2. The tool proposes date, reel number and media type, then shows the target path. The operator confirms.
3. Space check on all destinations.
4. Hash the source (xxHash64) while copying to all destinations.
5. Re-read each destination and verify against the source hashes.
6. Extract metadata per clip.
7. Write the job report (HTML; PDF optional) and append to the master log on every destination.
8. Print a summary: PASS/FAIL, file count, total size, elapsed time, and the report path.

## Metadata per clip
Collect: filename, relative path, file size, xxHash64, duration, start timecode, codec, resolution, frame rate, and creation date/time.

Sources:
- `ffprobe`
- `exiftool`
- Sony and Canon XML sidecar files, when present (they carry timecode and camera details)

Leave a field blank if it isn't available. Never guess a value.

## Job report (per card)
**Header:**
- project and show code
- card name, camera letter and model
- shoot date
- offload date/time
- operator
- source volume
- destination volumes
- overall PASS/FAIL

**Clip table:** one row per clip with the metadata above, plus per-destination verification status.

**Totals:** clip count, total size, total duration, elapsed time, transfer speed.

**Notes:** a free-text field the operator can fill in at confirmation (e.g. "B-cam interviews, card ejected mid-clip 0032").

## Daily card log (per shoot date)
This report replaces the hand-written daily card log. It is regenerated automatically after every offload for that date, so it is always current. Write it to `_REPORTS/[date]/` on every destination. HTML is required; PDF is optional.

**Header:**
- show name
- custom header fields (e.g. Season, Location, Leg)
- crew (e.g. Producer, DP, DIT)
- shoot date
- **drive name(s)**: the destination volumes, e.g. `SHOW01_DRV01A & SHOW01_DRV01B`

**Per-camera list:** one line per camera that shot that day, giving:
- camera ID,
- card count,
- GB for that camera that day,
- description and shooter from the config (e.g. "Cam D: 1 Card (Timelapse, Shooter Name)").

Each card name is listed under its camera.

**Totals:**
- **Total cards** for the day
- **Total GB** for the day
- **Running total GB on the drive**: everything ever offloaded to that drive, taken from the master log
- **Drive capacity and free space remaining**, per destination

If the destinations disagree on any total, flag it.

**Camera Notes:** collected from each card's operator notes, plus a free-text field for day-level notes.

**Status:** PASS only if every card that day passed verification. Otherwise, list the failed cards.

## Master log (per drive)
- One row per clip, tagged with card name, date, camera, offload timestamp, destination volume and verification status.
- It is cumulative across every offload to that drive, so the drive carries a complete, searchable record of its contents.
- **SQLite is the source of truth.** The spreadsheet is a read-only view, and edits made in it are never read back into the log.

### Spreadsheet export (`[DRIVE]_media_log.xlsx`)
Regenerated from SQLite after every offload, using `openpyxl`. Write it to a temp file, then atomically replace the old one.

**Sheets:**
- **Clips:** one row per clip.
- **Cards:** one row per card, with camera, shoot date, clip count, GB, offload time, operator, PASS/FAIL and notes.
- **Days:** one row per shoot date, with card count, GB for the day, and the running total GB on the drive.

**Formatting:**
- Store card names, reel numbers, timecode, checksums and dates as **text cells**, so Excel can't strip leading zeros, convert timecode to clock time, or turn hashes into scientific notation.
- Bold, frozen header row; autofilter on every sheet; sensible column widths.
- GB shown to two decimals; durations as `HH:MM:SS`.
- Failed verifications highlighted in red.
- A title row naming the drive, the show, and the time of the last export.

## Command-line interface
- `ingest`: interactive offload, as described above.
- `verify <card folder>`: re-hash an existing card folder against the hashes in the log. Use this for drive spot-checks or to check a shuttle drive on arrival.
- `report <date>`: regenerate the card reports and the daily card log for a date.
- `daylog <date>`: edit that day's header fields, crew and day notes, then regenerate the daily card log.
- `log export`: regenerate the .xlsx from the SQLite log.

## Phase 2 (after MVP works)
- **ASC MHL** manifest generated per card.
- **Proxies** with ffmpeg after verification passes: ProRes Proxy or H.264, preserving timecode and reel name, written to a `PROXIES/` folder next to the card folder.

## Testing requirements
- Use **copies** of real FX6 and C80 cards, plus a fake audio card. Never test on original media.
- **Corruption test:** after copying, change one byte in a destination file. `verify` must detect it and report FAIL.
- **Interrupted copy:** kill the process mid-copy. The rerun must not overwrite anything and must clearly flag the incomplete folder.
- **Full-destination test:** use a small volume and confirm the tool aborts before copying.
- **Reel numbering:** offload three cards from the same camera on one day and confirm they are numbered 01, 02, 03.
- **Midnight case:** a card whose clips span two dates triggers the operator confirmation.

## Build order
1. Project config, plus folder and naming logic, with tests.
2. Copy to multiple destinations with xxHash64 and read-back verification.
3. Metadata extraction.
4. Job report.
5. Master log (SQLite and .xlsx export).
6. Daily card log.
7. The `verify` command.
8. Phase 2 items.

Commit to git after each stage. Write a README covering installation, usage, and the safety design.

## Open decisions (operator to confirm)
- Whether audio recorders follow the same naming with their own ID (assumed yes).
- Whether a PDF report is needed in addition to HTML.
