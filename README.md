# bandcamp-plex-sync

`bandcamp-plex-sync` helps keep a purchased Bandcamp collection in sync with a
local Plex music library.

It can:

- Fetch your Bandcamp fan collection.
- Incrementally scan a Plex-style local music tree such as `/nas/music`.
- Cache scanned tags in SQLite so unchanged audio files are not reopened each run.
- Report albums/tracks that appear to be missing locally.
- Download missing **purchased FLAC** files from Bandcamp.
- Extract Bandcamp album ZIPs into Plex-friendly `Artist/Album/` directories.

The tool is intentionally conservative: it never deletes existing music and it
dry-runs downloads unless you pass `--yes`.

## Requirements

- Linux/macOS with Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) installed
- A browser where you are logged in to Bandcamp
- A local Plex music directory, defaulting to `/nas/music`

Bandcamp purchased FLAC downloads require authentication. The tool reads
Bandcamp cookies from your browser with `browser-cookie3`; no Bandcamp password
is stored by this tool.

## Installation

Install the published command with `uv`:

```bash
uv tool install bandcamp-plex-sync
bandcamp-plex-sync --help
```

To run from a source checkout:

```bash
git clone git@github.com:feoh/bandcamp-plex-sync.git
cd bandcamp-plex-sync
uv sync
uv run bandcamp-plex-sync --help
```

The checkout also retains a development wrapper with inline `uv` dependency
metadata:

```bash
./bin/bandcamp-plex-sync --help
```

## Quick start

Run an authenticated audit, then download missing FLAC albums/tracks:

```bash
bandcamp-plex-sync audit YOUR_BANDCAMP_USERNAME --cookies-from-browser
bandcamp-plex-sync download-missing --yes
```

For the original use case:

```bash
bandcamp-plex-sync audit feoh --cookies-from-browser
bandcamp-plex-sync download-missing --yes
```

By default, files are written under `/nas/music`.

## Commands

### `audit`

Fetches your Bandcamp collection, scans the local music directory, compares both,
and writes reports.

```bash
bandcamp-plex-sync audit USER --cookies-from-browser
```

Useful options:

```bash
bandcamp-plex-sync audit USER \
  --music-root /nas/music \
  --output-dir ~/.cache/bandcamp-plex-sync \
  --cookies-from-browser \
  --include-hidden
```

Outputs:

```text
~/.cache/bandcamp-plex-sync/
  bandcamp-collection.json   # fetched Bandcamp collection metadata
  local-scan.json            # scanned local audio metadata and incremental scan stats
  music-scan.sqlite3         # persistent file signatures and cached tag metadata
  sync-report.json           # machine-readable comparison
  sync-report.md             # human-readable report
  missing-urls.txt           # Bandcamp item pages for missing items
  missing.csv                # spreadsheet-friendly missing item list
```

### `download-missing`

Downloads missing purchased items from `sync-report.json`.

```bash
bandcamp-plex-sync download-missing --yes
```

FLAC is the default:

```bash
bandcamp-plex-sync download-missing --download-format flac --yes
```

Safety/testing options:

```bash
# Dry run; shows what would download
bandcamp-plex-sync download-missing

# Test with one item
bandcamp-plex-sync download-missing --limit 1 --yes

# Write somewhere other than /nas/music
bandcamp-plex-sync download-missing --destination ./downloads --yes

# Keep Bandcamp ZIP archives after extraction
bandcamp-plex-sync download-missing --keep-archives --yes

# Replace existing files
bandcamp-plex-sync download-missing --overwrite --yes
```

Downloaded album ZIPs are extracted into:

```text
/nas/music/Artist/Album/
```

Single-track FLAC downloads are written similarly:

```text
/nas/music/Artist/Track Title/Artist - Track Title.flac
```

### `auth-check`

Verify that one of the supported browser profiles is logged in to the account
that owns the requested collection and that Bandcamp exposes protected download
URLs:

```bash
bandcamp-plex-sync auth-check USER
```

This is a diagnostic check only. It does not create a persistent login or store
browser cookies.

### `fetch`

Only fetch Bandcamp metadata:

```bash
bandcamp-plex-sync fetch USER --cookies-from-browser
```

### `scan`

Incrementally scan local music metadata:

```bash
bandcamp-plex-sync scan --music-root /nas/music
```

The first run reads tags from every audio file and records the file size,
nanosecond modification/change times, device, and inode in
`~/.cache/bandcamp-plex-sync/music-scan.sqlite3`. Later runs still walk the
filesystem to detect additions, modifications, renames, and deletions, but they
reuse cached tags for unchanged files instead of opening and parsing every audio
file. This makes repeat scans much faster while retaining change detection.

The `scan` and `audit` commands both support:

```bash
# Put the checkpoint database somewhere else
bandcamp-plex-sync scan --checkpoint-db /path/to/music-checkpoints.sqlite3

# Ignore cached signatures and refresh every track's metadata
bandcamp-plex-sync scan --rescan-all
```

When `--output` or `--output-dir` is changed without an explicit
`--checkpoint-db`, the database is placed alongside the JSON output.

### `compare`

Compare previously fetched/scanned JSON files:

```bash
bandcamp-plex-sync compare
```

## Authentication and browser cookies

Use `--cookies-from-browser` with `fetch` or `audit` when you want purchased
download URLs. `download-missing` uses browser authentication by default because
opening Bandcamp's protected download pages requires it. Authentication is
command-scoped; no login session is persisted between invocations.

Use `auth-check USER` to diagnose authentication independently before running an
audit or download.

Every discovered profile in each supported browser is checked, rather than only
the browser's default profile. The tool verifies that the selected session owns
the requested Bandcamp collection before accepting its cookies; this prevents an
anonymous or wrong-profile session from silently producing empty download URLs.

Supported browser cookie stores are tried in this order:

1. Firefox
2. LibreWolf
3. Chrome
4. Chromium
5. Brave
6. Vivaldi
7. Edge
8. Opera

If cookies fail to load:

- Make sure you are logged in to Bandcamp in one of those browsers.
- Close the browser if its cookie database is locked.
- Try running the command from the same desktop user account as the browser.
- Confirm the username passed to `audit` is the collection owned by the logged-in
  Bandcamp account.

Bandcamp subscription collection entries are not individually downloadable.
Their album and track releases appear as separate downloadable collection items.

## Matching behavior

The audit compares normalized artist, album, and track metadata. It uses local
audio tags when present and falls back to Plex-style paths:

```text
Artist/Album/Track.ext
```

The report distinguishes:

- `matched`: confident local match
- `possible`: fuzzy match worth reviewing
- `missing`: no good local match found

You can tune fuzzy matching with:

```bash
bandcamp-plex-sync audit USER --threshold 0.90
```

Higher thresholds produce fewer possible matches.

## Privacy and safety

- The tool does not store your Bandcamp password.
- Authenticated Bandcamp redownload URLs are saved in
  `bandcamp-collection.json` and `sync-report.json`; treat these files as
  private.
- Existing music is skipped by default.
- The local SQLite checkpoint database contains file paths and music tags, but
  no Bandcamp credentials or browser cookies.
- Nothing is downloaded unless `download-missing --yes` is used.
- Nothing is deleted from your Plex library.

## Development

```bash
uv sync
uv run python -m compileall -q src tests
uv run pre-commit run --all-files
uv run mypy src/bandcamp_plex_sync
uv run python -m unittest discover -s tests -v
uv build
```

## License

MIT
