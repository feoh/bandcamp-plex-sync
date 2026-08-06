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

Fetch your Bandcamp collection, incrementally scan the local library, and download
missing purchased music in one command:

```bash
bandcamp-plex-sync sync --user YOUR_BANDCAMP_USERNAME --yes
```

After the first run, the saved report supplies the username, so repeat runs are:

```bash
bandcamp-plex-sync sync --yes
```

By default, files are written under `/nas/music`.

## Commands

### `sync`

Every invocation fetches the current Bandcamp collection, incrementally scans the
destination music library, compares the two, and writes fresh reports. It downloads
missing purchased items when `--yes` is provided; without `--yes`, it is a dry run.

Outputs are stored under `~/.cache/bandcamp-plex-sync/`:

```text
bandcamp-collection.json   # fetched Bandcamp collection metadata
local-scan.json            # scanned local audio metadata and incremental scan stats
music-scan.sqlite3         # persistent file signatures and cached tag metadata
sync-report.json           # machine-readable comparison and download progress
sync-report.md             # human-readable report
missing-urls.txt           # Bandcamp item pages for missing items
missing.csv                # spreadsheet-friendly missing item list
```

The local tree is still walked to detect additions, changes, and deletions, but
unchanged audio tags come from `music-scan.sqlite3` rather than being reread from
every music file. Only new or changed files require metadata parsing after the
initial scan.

FLAC is the default:

```bash
bandcamp-plex-sync sync --download-format flac --yes
```

Safety/testing options:

```bash
# Dry run; shows what would download
bandcamp-plex-sync sync

# Test with one item
bandcamp-plex-sync sync --limit 1 --yes

# Write somewhere other than /nas/music
bandcamp-plex-sync sync --destination ./downloads --yes

# Keep Bandcamp ZIP archives after extraction
bandcamp-plex-sync sync --keep-archives --yes

# Replace existing files
bandcamp-plex-sync sync --overwrite --yes
```

Downloaded album ZIPs are extracted into:

```text
/nas/music/Artist/Album/
```

Single-track FLAC downloads are written similarly:

```text
/nas/music/Artist/Track Title/Artist - Track Title.flac
```

After every successfully downloaded or verified item, `sync` immediately moves that
item from `missing` to `completed` in `sync-report.json`, records its file paths, and
updates the Markdown, URL, and CSV reports. Interrupted runs therefore resume with
only unfinished items. The next invocation starts with another fresh, incremental
comparison, so files downloaded earlier are recognized locally before Bandcamp
download pages are opened.

### `auth-check`

Verify that one of the supported browser profiles is logged in to the account
that owns the requested collection and that Bandcamp exposes protected download
URLs:

```bash
bandcamp-plex-sync auth-check USER
```

This is a diagnostic check only. It does not create a persistent login or store
browser cookies.

#### Incremental scanning

The first `sync` run reads tags from every audio file and records the file size,
nanosecond modification/change times, device, and inode in
`~/.cache/bandcamp-plex-sync/music-scan.sqlite3`. Later runs still walk the
filesystem to detect additions, modifications, renames, and deletions, but reuse
cached tags for unchanged files instead of opening and parsing every audio file.

```bash
# Put the checkpoint database somewhere else
bandcamp-plex-sync sync --checkpoint-db /path/to/music-checkpoints.sqlite3 --yes

# Ignore cached signatures and refresh every track's metadata
bandcamp-plex-sync sync --rescan-all --yes
```

## Authentication and browser cookies

`sync` uses browser authentication by default because Bandcamp's purchased-download
pages are protected. Authentication is command-scoped; no login session is persisted
between invocations.

Use `auth-check USER` to diagnose authentication independently before running `sync`.

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
- Confirm the collection username is owned by the logged-in Bandcamp account.

Bandcamp subscription collection entries are not individually downloadable.
Their album and track releases appear as separate downloadable collection items.

## Matching behavior

`sync` compares normalized artist, album, and track metadata. It uses local audio
tags when present and falls back to Plex-style paths:

```text
Artist/Album/Track.ext
```

The report distinguishes:

- `matched`: confident local match
- `possible`: fuzzy match worth reviewing
- `missing`: no good local match found

You can tune fuzzy matching with:

```bash
bandcamp-plex-sync sync --threshold 0.90 --yes
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
- Nothing is downloaded unless `sync --yes` is used.
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
