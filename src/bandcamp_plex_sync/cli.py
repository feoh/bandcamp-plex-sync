"""Audit a Bandcamp fan collection against a Plex music directory.

This intentionally does not delete or move existing music. It fetches your
Bandcamp collection, scans your Plex music tree, writes a missing-items report,
and can download purchased FLAC files for the gaps.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sqlite3
import sys
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import browser_cookie3
import cyclopts
import requests
from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError
from requests import Session
from rich.console import Console
from rich.table import Table

app = cyclopts.App(help="Keep a Bandcamp collection audited against a Plex music library.")
console = Console()
err_console = Console(stderr=True)

CACHE_DIR = Path("~/.cache/bandcamp-plex-sync").expanduser()
DEFAULT_MUSIC_ROOT = Path("/nas/music")
DEFAULT_CHECKPOINT_DB = CACHE_DIR / "music-scan.sqlite3"
AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
SKIP_DIRS = {"@eaDir", ".AppleDouble", ".git", "__MACOSX"}
BROWSER_COOKIE_PATTERNS = {
    "firefox": (
        ".mozilla/firefox/*/cookies.sqlite",
        "Library/Application Support/Firefox/Profiles/*/cookies.sqlite",
    ),
    "librewolf": (
        ".librewolf/*/cookies.sqlite",
        ".var/app/io.gitlab.librewolf-community/.librewolf/*/cookies.sqlite",
        "Library/Application Support/LibreWolf/Profiles/*/cookies.sqlite",
    ),
    "chrome": (
        ".config/google-chrome/*/Cookies",
        ".config/google-chrome/*/Network/Cookies",
        ".var/app/com.google.Chrome/config/google-chrome/*/Cookies",
        ".var/app/com.google.Chrome/config/google-chrome/*/Network/Cookies",
        "Library/Application Support/Google/Chrome/*/Cookies",
        "Library/Application Support/Google/Chrome/*/Network/Cookies",
    ),
    "chromium": (
        ".config/chromium/*/Cookies",
        ".config/chromium/*/Network/Cookies",
        ".var/app/org.chromium.Chromium/config/chromium/*/Cookies",
        ".var/app/org.chromium.Chromium/config/chromium/*/Network/Cookies",
        "snap/chromium/common/chromium/*/Cookies",
        "snap/chromium/common/chromium/*/Network/Cookies",
        "Library/Application Support/Chromium/*/Cookies",
        "Library/Application Support/Chromium/*/Network/Cookies",
    ),
    "brave": (
        ".config/BraveSoftware/Brave-Browser/*/Cookies",
        ".config/BraveSoftware/Brave-Browser/*/Network/Cookies",
        ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/*/Cookies",
        ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/*/Network/Cookies",
        "Library/Application Support/BraveSoftware/Brave-Browser/*/Cookies",
        "Library/Application Support/BraveSoftware/Brave-Browser/*/Network/Cookies",
    ),
    "vivaldi": (
        ".config/vivaldi/*/Cookies",
        ".config/vivaldi/*/Network/Cookies",
        ".var/app/com.vivaldi.Vivaldi/config/vivaldi/*/Cookies",
        ".var/app/com.vivaldi.Vivaldi/config/vivaldi/*/Network/Cookies",
        "Library/Application Support/Vivaldi/*/Cookies",
        "Library/Application Support/Vivaldi/*/Network/Cookies",
    ),
    "edge": (
        ".config/microsoft-edge/*/Cookies",
        ".config/microsoft-edge/*/Network/Cookies",
        ".var/app/com.microsoft.Edge/config/microsoft-edge/*/Cookies",
        ".var/app/com.microsoft.Edge/config/microsoft-edge/*/Network/Cookies",
        "Library/Application Support/Microsoft Edge/*/Cookies",
        "Library/Application Support/Microsoft Edge/*/Network/Cookies",
    ),
    "opera": (
        ".config/opera/Cookies",
        ".config/opera/Network/Cookies",
        "Library/Application Support/com.operasoftware.Opera/Cookies",
    ),
}


@dataclass(frozen=True)
class BandcampItem:
    artist: str
    title: str
    item_type: str
    item_id: int | None
    album_title: str | None
    item_title: str | None
    url: str
    purchased: str | None
    download_available: bool | None
    token: str | None
    redownload_url: str | None = None


@dataclass(frozen=True)
class LocalTrack:
    path: str
    artist: str
    albumartist: str
    album: str
    title: str


@dataclass(frozen=True)
class MatchCandidate:
    score: float
    path: str
    artist: str
    album: str
    title: str


@dataclass(frozen=True)
class ScanStats:
    audio_files: int
    metadata_scanned: int
    metadata_reused: int
    removed: int
    complete: bool


@dataclass(frozen=True)
class MusicScan:
    tracks: list[LocalTrack]
    stats: ScanStats


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_json_object(payload: str, context: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse {context}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected {context} to contain a JSON object")
    return parsed


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read JSON file {path}: {exc}") from exc
    return parse_json_object(payload, str(path))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", " and ")
    value = re.sub(r"\b(the|a|an)\b", " ", value)
    value = re.sub(r"\b(deluxe|expanded|remaster(?:ed)?|edition|ep|lp)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def norm_key(*parts: str | None) -> str:
    return " :: ".join(normalize(part) for part in parts if normalize(part))


def first_tag(tags: Any, *names: str) -> str:
    if not tags:
        return ""
    for name in names:
        value = tags.get(name)
        if isinstance(value, list) and value:
            return str(value[0]).strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def infer_title_from_path(path: Path) -> str:
    title = path.stem
    title = re.sub(r"^\s*\d+[\s._-]+", "", title)
    title = re.sub(r"^\s*\d+\s*-\s*", "", title)
    return title.strip()


def track_from_file(path: Path, root: Path) -> LocalTrack | None:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:  # noqa: BLE001 - corrupt tags should not stop a scan.
        err_console.print(f"[yellow]Could not read tags:[/] {path}: {exc}")
        audio = None

    tags = getattr(audio, "tags", None) if audio else None
    album = first_tag(tags, "album")
    title = first_tag(tags, "title")
    artist = first_tag(tags, "artist", "artists")
    albumartist = first_tag(tags, "albumartist", "albumartists", "album artist")

    try:
        relative = path.relative_to(root)
        parts = relative.parts
    except ValueError:
        parts = path.parts

    if not title:
        title = infer_title_from_path(path)
    if not album and len(parts) >= 2:
        album = parts[-2]
    if not artist and len(parts) >= 3:
        artist = parts[-3]
    if not albumartist:
        albumartist = artist

    if not any([artist, albumartist, album, title]):
        return None

    return LocalTrack(
        path=str(path),
        artist=artist,
        albumartist=albumartist,
        album=album,
        title=title,
    )


def open_checkpoint_database(path: Path) -> sqlite3.Connection:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS music_file_checkpoints (
            root TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            ctime_ns INTEGER NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            artist TEXT NOT NULL,
            albumartist TEXT NOT NULL,
            album TEXT NOT NULL,
            title TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (root, path)
        )
        """
    )
    connection.commit()
    return connection


def cached_track(row: sqlite3.Row) -> LocalTrack:
    return LocalTrack(
        path=str(row["path"]),
        artist=str(row["artist"]),
        albumartist=str(row["albumartist"]),
        album=str(row["album"]),
        title=str(row["title"]),
    )


def file_signature(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_dev,
        stat_result.st_ino,
    )


def checkpoint_matches(row: sqlite3.Row, signature: tuple[int, int, int, int, int]) -> bool:
    return signature == (
        row["size"],
        row["mtime_ns"],
        row["ctime_ns"],
        row["device"],
        row["inode"],
    )


def scan_music_tree(
    root: Path,
    max_files: int | None = None,
    *,
    checkpoint_db: Path = DEFAULT_CHECKPOINT_DB,
    rescan_all: bool = False,
) -> MusicScan:
    """Incrementally scan audio metadata, reusing unchanged SQLite checkpoints."""
    root = root.expanduser().absolute()
    if not root.is_dir():
        raise RuntimeError(f"Music root is not a readable directory: {root}")

    root_key = str(root)
    connection = open_checkpoint_database(checkpoint_db)
    try:
        cached_rows = {
            str(row["path"]): row
            for row in connection.execute(
                "SELECT * FROM music_file_checkpoints WHERE root = ?", (root_key,)
            )
        }

        tracks: list[LocalTrack] = []
        seen_paths: set[str] = set()
        updates: list[tuple[LocalTrack, tuple[int, int, int, int, int]]] = []
        metadata_scanned = 0
        metadata_reused = 0
        processed_audio = 0
        limit_reached = False
        scan_errors: list[OSError] = []

        def record_walk_error(error: OSError) -> None:
            scan_errors.append(error)
            err_console.print(f"[yellow]Could not scan directory:[/] {error}")

        for directory, dirnames, filenames in os.walk(root, onerror=record_walk_error):
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
            for filename in sorted(filenames):
                path = Path(directory, filename)
                if path.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                if max_files is not None and processed_audio >= max_files:
                    limit_reached = True
                    break

                try:
                    signature = file_signature(path.stat())
                except OSError as exc:
                    scan_errors.append(exc)
                    err_console.print(f"[yellow]Could not stat audio file:[/] {path}: {exc}")
                    continue

                path_key = str(path)
                seen_paths.add(path_key)
                processed_audio += 1
                row = None if rescan_all else cached_rows.get(path_key)
                if row is not None and checkpoint_matches(row, signature):
                    tracks.append(cached_track(row))
                    metadata_reused += 1
                else:
                    track = track_from_file(path, root)
                    metadata_scanned += 1
                    if track:
                        tracks.append(track)
                        try:
                            final_signature = file_signature(path.stat())
                        except OSError as exc:
                            final_signature = None
                            err_console.print(
                                f"[yellow]Could not verify audio file:[/] {path}: {exc}"
                            )
                        if final_signature == signature:
                            updates.append((track, signature))
                        elif final_signature is not None:
                            err_console.print(
                                f"[yellow]Audio file changed during scan; "
                                f"checkpoint deferred:[/] {path}"
                            )

                if processed_audio % 1000 == 0:
                    err_console.print(
                        f"Checked {processed_audio} audio files "
                        f"({metadata_scanned} new or changed)..."
                    )
            if limit_reached:
                break

        if scan_errors and not limit_reached:
            for path_key in sorted(set(cached_rows) - seen_paths):
                tracks.append(cached_track(cached_rows[path_key]))
                metadata_reused += 1

        complete = not limit_reached and not scan_errors
        stale_paths = set(cached_rows) - seen_paths if complete else set()
        updated_at = now_iso()
        with connection:
            connection.executemany(
                """
                INSERT INTO music_file_checkpoints (
                    root, path, size, mtime_ns, ctime_ns, device, inode,
                    artist, albumartist, album, title, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root, path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    ctime_ns = excluded.ctime_ns,
                    device = excluded.device,
                    inode = excluded.inode,
                    artist = excluded.artist,
                    albumartist = excluded.albumartist,
                    album = excluded.album,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    (
                        root_key,
                        track.path,
                        *signature,
                        track.artist,
                        track.albumartist,
                        track.album,
                        track.title,
                        updated_at,
                    )
                    for track, signature in updates
                ),
            )
            connection.executemany(
                "DELETE FROM music_file_checkpoints WHERE root = ? AND path = ?",
                ((root_key, path) for path in stale_paths),
            )

        return MusicScan(
            tracks=tracks,
            stats=ScanStats(
                audio_files=processed_audio,
                metadata_scanned=metadata_scanned,
                metadata_reused=metadata_reused,
                removed=len(stale_paths),
                complete=complete,
            ),
        )
    finally:
        connection.close()


def redownload_url_for_item(
    raw: dict[str, Any], redownload_urls: dict[str, Any] | None
) -> str | None:
    if isinstance(raw.get("redownload_url"), str):
        return raw["redownload_url"]
    if not redownload_urls:
        return None

    candidates = {
        str(value)
        for value in [
            raw.get("token"),
            raw.get("item_id"),
            raw.get("tralbum_id"),
            raw.get("sale_item_id"),
            f"{raw.get('sale_item_type')}{raw.get('sale_item_id')}",
            f"{raw.get('item_type')}{raw.get('item_id')}",
            f"{raw.get('tralbum_type')}{raw.get('tralbum_id')}",
            f"{raw.get('item_type')}:{raw.get('item_id')}",
            f"{raw.get('tralbum_type')}:{raw.get('tralbum_id')}",
        ]
        if value is not None
    }
    for key, value in redownload_urls.items():
        if str(key) in candidates and isinstance(value, str):
            return value
        if isinstance(value, dict):
            for nested_key in ("url", "redownload_url", "download_url"):
                nested_value = value.get(nested_key)
                if str(key) in candidates and isinstance(nested_value, str):
                    return nested_value
    return None


def item_from_bandcamp(
    raw: dict[str, Any], redownload_urls: dict[str, Any] | None = None
) -> BandcampItem:
    item_type = raw.get("item_type") or raw.get("tralbum_type") or ""
    album_title = raw.get("album_title")
    item_title = raw.get("item_title") or raw.get("title")
    title = album_title or item_title or ""
    player_data = raw.get("player_data") or {}
    artist = raw.get("band_name") or player_data.get("artist_name") or ""
    return BandcampItem(
        artist=str(artist),
        title=str(title),
        item_type=str(item_type),
        item_id=raw.get("item_id") or raw.get("tralbum_id"),
        album_title=str(album_title) if album_title else None,
        item_title=str(item_title) if item_title else None,
        url=str(raw.get("item_url") or player_data.get("url") or ""),
        purchased=raw.get("purchased"),
        download_available=raw.get("download_available"),
        token=raw.get("token"),
        redownload_url=redownload_url_for_item(raw, redownload_urls),
    )


def new_bandcamp_session() -> Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "bandcamp-plex-sync/1.0"
            )
        }
    )
    return session


def browser_cookie_files(browser: str, home: Path | None = None) -> list[Path | None]:
    """Find every browser profile cookie database, not only the default profile."""
    home = home or Path.home()
    paths = {
        path
        for pattern in BROWSER_COOKIE_PATTERNS.get(browser, ())
        for path in home.glob(pattern)
        if path.is_file()
    }
    cookie_files: list[Path | None] = []
    cookie_files.extend(sorted(paths))
    return cookie_files or [None]


def session_owns_collection(session: Session, user: str) -> bool:
    page_data = pagedata_from_profile(session, user)
    fan_data = page_data.get("fan_data") or {}
    identity = (page_data.get("identities") or {}).get("fan") or {}
    return bool(
        fan_data.get("is_own_page")
        or (identity.get("id") is not None and str(identity["id"]) == str(fan_data.get("fan_id")))
    )


def configure_session(cookies_from_browser: bool, user: str | None = None) -> Session:
    if not cookies_from_browser:
        return new_bandcamp_session()
    if not user:
        raise RuntimeError(
            "Authenticated Bandcamp access requires a collection username. "
            "Provide USER, pass --user, or set BANDCAMP_USER."
        )

    errors: list[str] = []
    profiles_checked = 0
    for loader_name in BROWSER_COOKIE_PATTERNS:
        loader = getattr(browser_cookie3, loader_name, None)
        if loader is None:
            continue
        for cookie_file in browser_cookie_files(loader_name):
            try:
                kwargs: dict[str, Any] = {"domain_name": ".bandcamp.com"}
                if cookie_file is not None:
                    kwargs["cookie_file"] = str(cookie_file)
                cookie_jar = loader(**kwargs)
            except Exception as exc:  # noqa: BLE001 - browser stores differ.
                errors.append(f"{loader_name}: {exc}")
                continue
            if not cookie_jar:
                continue

            profiles_checked += 1
            session = new_bandcamp_session()
            session.cookies.update(cookie_jar)
            try:
                if session_owns_collection(session, user):
                    return session
            except Exception as exc:  # noqa: BLE001 - try other browser profiles.
                errors.append(f"{loader_name}: {exc}")

    detail = f" Checked {profiles_checked} profile(s)." if profiles_checked else ""
    if errors:
        detail += f" Browser errors: {'; '.join(errors)}"
    raise RuntimeError(
        f"Could not find authenticated Bandcamp cookies for Bandcamp user {user!r}.{detail} "
        "Log in to Bandcamp in a supported browser profile and try again."
    )


def pagedata_from_profile(session: Session, user: str) -> dict[str, Any]:
    response = session.get(f"https://bandcamp.com/{user}", timeout=30)
    response.raise_for_status()
    match = re.search(r'<div id="pagedata" data-blob="([^"]+)"', response.text)
    if not match:
        raise RuntimeError(
            "Could not find Bandcamp pagedata. Check the username or try "
            "--cookies-from-browser if your collection is private."
        )
    return parse_json_object(html.unescape(match.group(1)), "Bandcamp profile data")


def fetch_collection_items(
    user: str,
    *,
    cookies_from_browser: bool = False,
    include_hidden: bool = False,
    limit: int | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    session = session or configure_session(cookies_from_browser, user)
    page_data = pagedata_from_profile(session, user)
    fan_data = page_data.get("fan_data") or {}
    collection_data = page_data.get("collection_data") or {}
    fan_id = fan_data.get("fan_id")
    if not fan_id:
        raise RuntimeError(f"Could not determine fan_id for Bandcamp user {user!r}")

    redownload_urls = dict(collection_data.get("redownload_urls") or {})
    raw_items: list[dict[str, Any]] = []
    cache = page_data.get("item_cache") or {}
    raw_items.extend((cache.get("collection") or {}).values())
    if include_hidden:
        raw_items.extend((cache.get("hidden") or {}).values())

    last_token = collection_data.get("last_token")
    more_available = bool(last_token)
    while more_available and (limit is None or len(raw_items) < limit):
        count = 100 if limit is None else max(1, min(100, limit - len(raw_items)))
        payload = {"fan_id": fan_id, "older_than_token": last_token, "count": count}
        response = session.post(
            "https://bandcamp.com/api/fancollection/1/collection_items",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        redownload_urls.update(batch.get("redownload_urls") or {})
        raw_items.extend(batch.get("items") or [])
        last_token = batch.get("last_token")
        more_available = bool(batch.get("more_available")) and bool(last_token)

    deduped: dict[str, BandcampItem] = {}
    for raw in raw_items:
        item = item_from_bandcamp(raw, redownload_urls)
        if not item.title or not item.artist:
            continue
        key = f"{item.item_type}:{item.item_id}:{item.url}"
        deduped[key] = item
        if limit is not None and len(deduped) >= limit:
            break

    return {
        "fetched_at": now_iso(),
        "user": user,
        "fan_id": fan_id,
        "source": "bandcamp.com/fancollection",
        "items": [asdict(item) for item in deduped.values()],
    }


def bandcamp_item_keys(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    item_type = str(item.get("item_type") or "")
    item_id = item.get("item_id")
    if item_type and item_id is not None:
        keys.append(("item", item_type, str(item_id)))
    url = str(item.get("url") or "").rstrip("/")
    if url:
        keys.append(("url", "", url))
    return keys


def refresh_report_download_urls(report: dict[str, Any], collection: dict[str, Any]) -> int:
    """Merge fresh protected download URLs into an existing comparison report."""
    lookup = {key: item for item in collection.get("items", []) for key in bandcamp_item_keys(item)}
    refreshed = 0
    for section in ("matched", "possible", "missing"):
        for item in report.get(section, []):
            fresh = next(
                (lookup[key] for key in bandcamp_item_keys(item) if key in lookup),
                None,
            )
            if fresh is None:
                continue
            item["download_available"] = fresh.get("download_available")
            item["redownload_url"] = fresh.get("redownload_url")
            if item["redownload_url"]:
                refreshed += 1
    report["download_urls_refreshed_at"] = collection.get("fetched_at") or now_iso()
    return refreshed


def build_local_indexes(
    tracks: list[LocalTrack],
) -> tuple[set[str], set[str], list[MatchCandidate]]:
    album_keys: set[str] = set()
    track_keys: set[str] = set()
    candidates: list[MatchCandidate] = []

    for track in tracks:
        artists = {track.artist, track.albumartist} - {""}
        for artist in artists:
            if track.album:
                album_keys.add(norm_key(artist, track.album))
            if track.title:
                track_keys.add(norm_key(artist, track.title))
        if track.album:
            album_keys.add(norm_key(track.album))
        if track.title:
            track_keys.add(norm_key(track.title))
        candidates.append(
            MatchCandidate(
                score=0,
                path=track.path,
                artist=track.albumartist or track.artist,
                album=track.album,
                title=track.title,
            )
        )
    return album_keys, track_keys, candidates


def score_candidate(item: BandcampItem, candidate: MatchCandidate) -> float:
    bc_albumish = norm_key(item.artist, item.title)
    bc_title_only = normalize(item.title)
    local_albumish = norm_key(candidate.artist, candidate.album)
    local_trackish = norm_key(candidate.artist, candidate.title)
    album_score = SequenceMatcher(None, bc_albumish, local_albumish).ratio()
    track_score = SequenceMatcher(None, bc_albumish, local_trackish).ratio()
    title_score = SequenceMatcher(None, bc_title_only, normalize(candidate.album)).ratio()
    return max(album_score, track_score, title_score * 0.95)


def compare_payloads(
    collection: dict[str, Any],
    local_scan: dict[str, Any],
    *,
    threshold: float = 0.86,
    max_suggestions: int = 3,
) -> dict[str, Any]:
    items = [BandcampItem(**item) for item in collection.get("items", [])]
    tracks = [LocalTrack(**track) for track in local_scan.get("tracks", [])]
    album_keys, track_keys, candidates = build_local_indexes(tracks)

    matched: list[dict[str, Any]] = []
    possible: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for item in items:
        keys_to_try = []
        if item.item_type in {"album", "a"}:
            keys_to_try = [norm_key(item.artist, item.title), norm_key(item.title)]
            exact = any(key in album_keys for key in keys_to_try)
        else:
            keys_to_try = [norm_key(item.artist, item.title), norm_key(item.title)]
            exact = any(key in track_keys for key in keys_to_try)

        if exact:
            matched.append(asdict(item) | {"status": "matched"})
            continue

        scored = sorted(
            (
                candidate.__class__(
                    score_candidate(item, candidate),
                    candidate.path,
                    candidate.artist,
                    candidate.album,
                    candidate.title,
                )
                for candidate in candidates
            ),
            key=lambda candidate: candidate.score,
            reverse=True,
        )[:max_suggestions]
        suggestions = [asdict(candidate) for candidate in scored if candidate.score >= threshold]
        if suggestions:
            possible.append(asdict(item) | {"status": "possible", "suggestions": suggestions})
        else:
            missing.append(asdict(item) | {"status": "missing"})

    return {
        "generated_at": now_iso(),
        "bandcamp_user": collection.get("user"),
        "music_root": local_scan.get("root"),
        "threshold": threshold,
        "counts": {
            "bandcamp_items": len(items),
            "local_tracks": len(tracks),
            "matched": len(matched),
            "possible": len(possible),
            "missing": len(missing),
        },
        "matched": matched,
        "possible": possible,
        "missing": missing,
    }


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    counts = report["counts"]
    lines = [
        "# Bandcamp ↔ Plex music sync report",
        "",
        f"Generated: {report['generated_at']}",
        f"Bandcamp user: `{report.get('bandcamp_user')}`",
        f"Plex music root: `{report.get('music_root')}`",
        "",
        "## Summary",
        "",
        f"- Bandcamp items: {counts['bandcamp_items']}",
        f"- Local tracks scanned: {counts['local_tracks']}",
        f"- Matched: {counts['matched']}",
        f"- Possible matches to review: {counts['possible']}",
        f"- Missing: {counts['missing']}",
        "",
        "## Missing from Plex",
        "",
    ]
    if report["missing"]:
        for item in report["missing"]:
            purchased = f" — purchased {item['purchased']}" if item.get("purchased") else ""
            lines.append(f"- **{item['artist']} — {item['title']}**{purchased}")
            if item.get("url"):
                lines.append(f"  - {item['url']}")
    else:
        lines.append("No missing items found.")

    lines.extend(["", "## Possible matches", ""])
    if report["possible"]:
        for item in report["possible"]:
            lines.append(f"- **{item['artist']} — {item['title']}**")
            for suggestion in item.get("suggestions", []):
                percent = round(suggestion["score"] * 100)
                lines.append(
                    f"  - {percent}%: {suggestion['artist']} — "
                    f"{suggestion['album']} — {suggestion['title']}"
                )
                lines.append(f"    - `{suggestion['path']}`")
    else:
        lines.append("No possible matches need review.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_missing_urls(path: Path, report: dict[str, Any]) -> None:
    urls = [item["url"] for item in report.get("missing", []) if item.get("url")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def write_missing_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "artist",
                "title",
                "item_type",
                "purchased",
                "download_available",
                "url",
                "redownload_url",
            ],
        )
        writer.writeheader()
        for item in report.get("missing", []):
            writer.writerow({field: item.get(field) for field in writer.fieldnames})


def safe_component(value: str | None, fallback: str = "Unknown") -> str:
    value = (value or fallback).strip() or fallback
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:180] or fallback


def parse_tralbum_page(url: str, session: Session) -> dict[str, Any]:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    match = re.search(r'<script[^>]*data-tralbum="([^"]+)"', response.text)
    if not match:
        raise RuntimeError(f"Could not find Bandcamp track data on {url}")
    data = parse_json_object(html.unescape(match.group(1)), "Bandcamp track data")
    data["page_url"] = response.url
    return data


def bandcamp_art_url(art_id: int | None) -> str | None:
    if not art_id:
        return None
    return f"https://f4.bcbits.com/img/a{art_id}_10.jpg"


def save_mp3_tags(
    path: Path,
    *,
    artist: str,
    album_artist: str,
    album: str,
    title: str,
    track_number: int | None,
) -> None:
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
    tags["artist"] = artist
    tags["albumartist"] = album_artist
    tags["album"] = album
    tags["title"] = title
    if track_number is not None:
        tags["tracknumber"] = str(track_number)
    tags.save(path)


def download_url_to_file(
    session: Session, url: str, path: Path, *, overwrite: bool = False
) -> bool:
    if path.exists() and not overwrite:
        console.print(f"[dim]Already exists, skipping:[/] {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".part")
    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    tmp_path.replace(path)
    return True


def pagedata_from_html(text: str) -> dict[str, Any]:
    match = re.search(r'<div id="pagedata" data-blob="([^"]+)"', text)
    if not match:
        raise RuntimeError("Could not find Bandcamp download data on the page")
    return parse_json_object(html.unescape(match.group(1)), "Bandcamp download data")


def filename_from_content_disposition(header: str | None, fallback: str) -> str:
    if header:
        utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", header)
        if utf8_match:
            return safe_component(unquote(utf8_match.group(1)))
        ascii_match = re.search(r'filename="?([^";]+)"?', header)
        if ascii_match:
            return safe_component(ascii_match.group(1))
    return safe_component(fallback)


def safe_extract_zip(zip_path: Path, destination: Path, *, overwrite: bool = False) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        top_levels = {
            Path(member.filename).parts[0]
            for member in members
            if Path(member.filename).parts and not Path(member.filename).is_absolute()
        }
        strip_top = len(top_levels) == 1
        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                console.print(f"[yellow]Skipping unsafe zip member:[/] {member.filename}")
                continue
            parts = member_path.parts[1:] if strip_top else member_path.parts
            if not parts:
                continue
            target = destination.joinpath(*parts)
            if target.exists() and not overwrite:
                console.print(f"[dim]Already exists, skipping:[/] {target}")
                extracted.append(target)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as dest:
                for chunk in iter(lambda: source.read(1024 * 256), b""):
                    dest.write(chunk)
            extracted.append(target)
    return extracted


def download_response(session: Session, url: str, path: Path, *, overwrite: bool = False) -> Path:
    if path.exists() and not overwrite:
        console.print(f"[dim]Already exists, skipping:[/] {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".part")
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    tmp_path.replace(path)
    return path


def download_purchased_bandcamp_item(
    item: dict[str, Any],
    destination: Path,
    session: Session,
    *,
    download_format: str = "flac",
    overwrite: bool = False,
    keep_archives: bool = False,
) -> list[Path]:
    """Download a purchased Bandcamp item in the requested format."""
    redownload_url = item.get("redownload_url")
    if not redownload_url:
        raise RuntimeError(
            "No purchased-download URL is available for this item. Run `download-missing` "
            "with browser authentication enabled so it can refresh protected URLs."
        )

    response = session.get(str(redownload_url), timeout=30)
    response.raise_for_status()
    page_data = pagedata_from_html(response.text)
    download_items = page_data.get("download_items") or []
    if not download_items:
        raise RuntimeError("Bandcamp download page did not contain downloadable items")

    written: list[Path] = []
    for download_item in download_items:
        downloads = download_item.get("downloads") or {}
        selected = downloads.get(download_format)
        if not selected:
            available = ", ".join(sorted(downloads)) or "none"
            raise RuntimeError(
                f"Format {download_format!r} is not available; available formats: {available}"
            )

        artist = str(
            download_item.get("artist")
            or download_item.get("band_name")
            or item.get("artist")
            or "Unknown Artist"
        )
        title = str(download_item.get("title") or item.get("title") or "Unknown Album")
        album_dir = destination / safe_component(artist) / safe_component(title)
        download_url = html.unescape(str(selected["url"]))
        fallback_name = f"{artist} - {title}.{download_format}"

        with session.get(download_url, stream=True, timeout=120) as file_response:
            file_response.raise_for_status()
            filename = filename_from_content_disposition(
                file_response.headers.get("content-disposition"), fallback_name
            )
            suffix = Path(filename).suffix.lower()
            if not suffix:
                suffix = (
                    ".zip" if download_item.get("download_type") == "a" else f".{download_format}"
                )
                filename = f"{filename}{suffix}"
            target = album_dir / filename
            if target.exists() and not overwrite:
                console.print(f"[dim]Already exists, skipping archive/file:[/] {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_suffix(target.suffix + ".part")
                console.print(f"Downloading {artist} — {title} ({download_format})")
                with tmp_path.open("wb") as f:
                    for chunk in file_response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                tmp_path.replace(target)

        if zipfile.is_zipfile(target):
            extracted = safe_extract_zip(target, album_dir, overwrite=overwrite)
            written.extend(extracted)
            if not keep_archives:
                target.unlink(missing_ok=True)
        else:
            written.append(target)
    return written


def download_bandcamp_item_streams(
    item: dict[str, Any],
    destination: Path,
    session: Session,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Download Bandcamp's playable MP3 streams for one album or track."""
    data = parse_tralbum_page(item["url"], session)
    current = data.get("current") or {}
    tracks = data.get("trackinfo") or []
    album_artist = str(data.get("artist") or item.get("artist") or "Unknown Artist")
    album_title = str(current.get("title") or item.get("title") or "Unknown Album")
    item_type = str(data.get("item_type") or item.get("item_type") or "")
    if item_type == "track" and len(tracks) == 1:
        album_title = str(item.get("album_title") or album_title)

    album_dir = destination / safe_component(album_artist) / safe_component(album_title)
    art_url = bandcamp_art_url(data.get("art_id"))
    if art_url:
        try:
            download_url_to_file(session, art_url, album_dir / "cover.jpg", overwrite=overwrite)
        except Exception as exc:  # noqa: BLE001 - cover art is nice-to-have.
            console.print(f"[yellow]Could not download cover art for {album_title}: {exc}[/]")

    downloaded: list[Path] = []
    for index, track in enumerate(tracks, start=1):
        files = track.get("file") or {}
        stream_url = files.get("mp3-128")
        if not stream_url:
            console.print(
                f"[yellow]No playable MP3 stream for {album_artist} — {track.get('title')}[/]"
            )
            continue
        raw_track_number = track.get("track_num") or index
        try:
            track_number = int(raw_track_number)
        except (TypeError, ValueError):
            track_number = index
        track_title = str(track.get("title") or f"Track {track_number}")
        track_artist = str(track.get("artist") or album_artist)
        filename = f"{track_number:02d} - {safe_component(track_title)}.mp3"
        target = album_dir / filename
        console.print(f"Downloading {album_artist} — {track_title}")
        wrote = download_url_to_file(session, stream_url, target, overwrite=overwrite)
        if wrote or target.exists():
            save_mp3_tags(
                target,
                artist=track_artist,
                album_artist=album_artist,
                album=album_title,
                title=track_title,
                track_number=track_number,
            )
            downloaded.append(target)
    return downloaded


def print_summary(report: dict[str, Any]) -> None:
    counts = report["counts"]
    table = Table(title="Bandcamp ↔ Plex sync audit")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for key, label in [
        ("bandcamp_items", "Bandcamp items"),
        ("local_tracks", "Local tracks"),
        ("matched", "Matched"),
        ("possible", "Possible"),
        ("missing", "Missing"),
    ]:
        table.add_row(label, str(counts[key]))
    console.print(table)

    if report.get("missing"):
        console.print("\n[bold red]Missing items:[/]")
        for item in report["missing"][:20]:
            console.print(f"- {item['artist']} — {item['title']}  [dim]{item.get('url', '')}[/]")
        if len(report["missing"]) > 20:
            console.print(f"[dim]...and {len(report['missing']) - 20} more in the report.[/]")


@app.command
def auth_check(user: str = "") -> None:
    """Verify browser cookies can access a Bandcamp collection's downloads."""
    user = user or os.environ.get("BANDCAMP_USER", "")
    if not user:
        console.print("[red]Provide USER or set BANDCAMP_USER.[/]")
        raise SystemExit(2)

    console.print(f"Checking browser authentication for [bold]{user}[/]...")
    session = configure_session(True, user)
    page_data = pagedata_from_profile(session, user)
    collection_data = page_data.get("collection_data") or {}
    initial_items = list(((page_data.get("item_cache") or {}).get("collection") or {}).values())
    downloadable_items = sum(bool(item.get("download_available")) for item in initial_items)
    redownload_urls = collection_data.get("redownload_urls") or {}
    if downloadable_items and not redownload_urls:
        raise RuntimeError(
            "Bandcamp authenticated the browser session but returned no purchased-download "
            "URLs. Try logging in again or using another browser profile."
        )

    console.print(f"[green]Authenticated browser session found for {user}.[/]")
    console.print(
        f"Collection items: {collection_data.get('item_count', len(initial_items))}; "
        f"authenticated download URLs in initial batch: {len(redownload_urls)}"
    )
    console.print("[dim]No browser cookies were persisted by this command.[/]")


@app.command
def fetch(
    user: str = "",
    output: Path = CACHE_DIR / "bandcamp-collection.json",
    cookies_from_browser: bool = False,
    include_hidden: bool = False,
    limit: int | None = None,
) -> None:
    """Fetch a Bandcamp fan collection to JSON."""
    user = user or os.environ.get("BANDCAMP_USER", "")
    if not user:
        console.print("[red]Provide USER or set BANDCAMP_USER.[/]")
        raise SystemExit(2)
    payload = fetch_collection_items(
        user,
        cookies_from_browser=cookies_from_browser,
        include_hidden=include_hidden,
        limit=limit,
    )
    write_json(output, payload)
    console.print(f"Wrote {len(payload['items'])} Bandcamp items to {output}")


@app.command
def scan(
    music_root: Path = DEFAULT_MUSIC_ROOT,
    output: Path = CACHE_DIR / "local-scan.json",
    checkpoint_db: Path | None = None,
    max_files: int | None = None,
    rescan_all: bool = False,
) -> None:
    """Incrementally scan local Plex music metadata and write it to JSON."""
    checkpoint_db = checkpoint_db or output.parent / DEFAULT_CHECKPOINT_DB.name
    music_scan = scan_music_tree(
        music_root,
        max_files=max_files,
        checkpoint_db=checkpoint_db,
        rescan_all=rescan_all,
    )
    payload = {
        "scanned_at": now_iso(),
        "root": str(music_root.expanduser().absolute()),
        "checkpoint_db": str(checkpoint_db.expanduser().absolute()),
        "scan_stats": asdict(music_scan.stats),
        "tracks": [asdict(track) for track in music_scan.tracks],
    }
    write_json(output, payload)
    stats = music_scan.stats
    console.print(
        f"Wrote {len(music_scan.tracks)} local tracks to {output} "
        f"([green]{stats.metadata_reused} cached[/], "
        f"[yellow]{stats.metadata_scanned} new or changed[/], {stats.removed} removed)"
    )


@app.command
def compare(
    collection_json: Path = CACHE_DIR / "bandcamp-collection.json",
    local_json: Path = CACHE_DIR / "local-scan.json",
    output_dir: Path = CACHE_DIR,
    threshold: float = 0.86,
) -> None:
    """Compare previously fetched/scanned JSON files and write reports."""
    report = compare_payloads(
        load_json(collection_json),
        load_json(local_json),
        threshold=threshold,
    )
    write_outputs(output_dir, report)
    print_summary(report)


@app.command
def download_missing(
    report_json: Path = CACHE_DIR / "sync-report.json",
    user: str = "",
    destination: Path = DEFAULT_MUSIC_ROOT,
    cookies_from_browser: bool = True,
    yes: bool = False,
    overwrite: bool = False,
    limit: int | None = None,
    download_format: str = "flac",
    keep_archives: bool = False,
) -> None:
    """Download report's missing purchased items into the Plex music directory.

    Protected download URLs are refreshed from the current browser session before
    writing files. Pass --yes to actually download files.
    """
    report = load_json(report_json)
    user = user or str(report.get("bandcamp_user") or "") or os.environ.get("BANDCAMP_USER", "")
    session: Session | None = None

    if yes and cookies_from_browser:
        console.print("Refreshing authenticated Bandcamp download URLs...")
        session = configure_session(True, user)
        collection = fetch_collection_items(user, session=session)
        refreshed = refresh_report_download_urls(report, collection)
        write_json(report_json, report)
        console.print(f"Refreshed {refreshed} protected download URL(s) in {report_json}")

    missing = [item for item in report.get("missing", []) if item.get("url")]
    if limit is not None:
        missing = missing[:limit]

    if not missing:
        console.print("No missing downloadable Bandcamp URLs found in the report.")
        return

    with_redownload = [item for item in missing if item.get("redownload_url")]
    missing_redownload = [
        item
        for item in missing
        if bool(item.get("download_available")) and not item.get("redownload_url")
    ]
    not_individually_downloadable = [
        item for item in missing if not bool(item.get("download_available"))
    ]
    console.print(f"Found {len(missing)} missing item(s). Destination: [bold]{destination}[/]")
    console.print(f"Download format: [bold]{download_format}[/]")
    if missing_redownload:
        if cookies_from_browser:
            guidance = "They will be refreshed automatically when downloading with --yes."
            if not user:
                guidance = "Pass --user USER or set BANDCAMP_USER when downloading with --yes."
        else:
            guidance = "Enable --cookies-from-browser to refresh them before downloading."
        console.print(
            f"[yellow]{len(missing_redownload)} downloadable item(s) lack authenticated "
            f"download URLs.[/] {guidance}"
        )
    if not_individually_downloadable:
        console.print(
            f"[dim]{len(not_individually_downloadable)} collection entry/entries are not "
            "individually downloadable (for example, Bandcamp subscriptions).[/]"
        )
    for item in missing[:20]:
        if item.get("redownload_url"):
            marker = "✓"
        elif bool(item.get("download_available")):
            marker = "authenticated URL will be refreshed"
        else:
            marker = "not individually downloadable"
        console.print(f"- {item['artist']} — {item['title']}  [dim]{marker}  {item['url']}[/]")
    if len(missing) > 20:
        console.print(f"[dim]...and {len(missing) - 20} more.[/]")

    if not yes:
        console.print("\nDry run only. Re-run with --yes to download files.")
        return
    if not with_redownload:
        raise SystemExit(2)

    session = session or configure_session(cookies_from_browser, user or None)
    downloaded_count = 0
    failed: list[tuple[dict[str, Any], str]] = []
    for item in with_redownload:
        try:
            downloaded = download_purchased_bandcamp_item(
                item,
                destination,
                session,
                download_format=download_format,
                overwrite=overwrite,
                keep_archives=keep_archives,
            )
            downloaded_count += len(downloaded)
        except Exception as exc:  # noqa: BLE001 - continue with the rest of the queue.
            failed.append((item, str(exc)))
            console.print(f"[red]Failed:[/] {item['artist']} — {item['title']}: {exc}")

    console.print(f"\nDownloaded, extracted, or verified {downloaded_count} file(s).")
    if failed:
        console.print(f"[red]{len(failed)} item(s) failed.[/]")


def write_outputs(output_dir: Path, report: dict[str, Any]) -> None:
    write_json(output_dir / "sync-report.json", report)
    write_markdown_report(output_dir / "sync-report.md", report)
    write_missing_urls(output_dir / "missing-urls.txt", report)
    write_missing_csv(output_dir / "missing.csv", report)
    console.print(f"\nReports written under {output_dir}")


@app.command
def audit(
    user: str = "",
    music_root: Path = DEFAULT_MUSIC_ROOT,
    output_dir: Path = CACHE_DIR,
    checkpoint_db: Path | None = None,
    cookies_from_browser: bool = False,
    include_hidden: bool = False,
    threshold: float = 0.86,
    limit: int | None = None,
    max_files: int | None = None,
    rescan_all: bool = False,
) -> None:
    """Fetch Bandcamp, scan Plex music, compare, and write reports."""
    user = user or os.environ.get("BANDCAMP_USER", "")
    if not user:
        console.print("[red]Provide USER or set BANDCAMP_USER.[/]")
        raise SystemExit(2)

    console.print(f"Fetching Bandcamp collection for [bold]{user}[/]...")
    collection = fetch_collection_items(
        user,
        cookies_from_browser=cookies_from_browser,
        include_hidden=include_hidden,
        limit=limit,
    )
    write_json(output_dir / "bandcamp-collection.json", collection)

    console.print(f"Scanning local music under [bold]{music_root}[/]...")
    checkpoint_db = checkpoint_db or output_dir / DEFAULT_CHECKPOINT_DB.name
    music_scan = scan_music_tree(
        music_root,
        max_files=max_files,
        checkpoint_db=checkpoint_db,
        rescan_all=rescan_all,
    )
    local_scan = {
        "scanned_at": now_iso(),
        "root": str(music_root.expanduser().absolute()),
        "checkpoint_db": str(checkpoint_db.expanduser().absolute()),
        "scan_stats": asdict(music_scan.stats),
        "tracks": [asdict(track) for track in music_scan.tracks],
    }
    write_json(output_dir / "local-scan.json", local_scan)
    stats = music_scan.stats
    console.print(
        f"Music metadata: [green]{stats.metadata_reused} cached[/], "
        f"[yellow]{stats.metadata_scanned} new or changed[/], {stats.removed} removed"
    )

    report = compare_payloads(collection, local_scan, threshold=threshold)
    write_outputs(output_dir, report)
    print_summary(report)


def main() -> None:
    """Run the bandcamp-plex-sync command-line application."""
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
