from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from requests.cookies import cookiejar_from_dict

from bandcamp_plex_sync import cli as MODULE


class BrowserCookieSessionTests(unittest.TestCase):
    def test_authenticated_session_requires_collection_username(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires a collection username"):
            MODULE.configure_session(True)

    def test_auth_check_reports_authenticated_download_access(self) -> None:
        session = MODULE.new_bandcamp_session()
        page_data = {
            "collection_data": {
                "item_count": 42,
                "redownload_urls": {"one": "protected", "two": "protected"},
            },
            "item_cache": {
                "collection": {
                    "one": {"download_available": True},
                    "two": {"download_available": True},
                }
            },
        }
        with (
            patch.object(MODULE, "configure_session", return_value=session) as configure,
            patch.object(MODULE, "pagedata_from_profile", return_value=page_data),
            patch.object(MODULE.console, "print") as print_message,
        ):
            MODULE.auth_check("listener")

        configure.assert_called_once_with(True, "listener")
        output = " ".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("Authenticated browser session found", output)
        self.assertIn("Collection items: 42", output)
        self.assertIn("initial batch: 2", output)

    def test_finds_cookie_databases_in_every_browser_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            default = home / ".config/vivaldi/Default/Cookies"
            profile = home / ".config/vivaldi/Profile 1/Cookies"
            default.parent.mkdir(parents=True)
            profile.parent.mkdir(parents=True)
            default.touch()
            profile.touch()

            self.assertEqual(MODULE.browser_cookie_files("vivaldi", home), [default, profile])

    def test_selects_profile_that_owns_requested_collection(self) -> None:
        cookie_files = [Path("Default/Cookies"), Path("Profile 1/Cookies")]

        def load_cookies(*, domain_name: str, cookie_file: str):
            self.assertEqual(domain_name, ".bandcamp.com")
            profile = Path(cookie_file).parent.name
            return cookiejar_from_dict({"browser_profile": profile})

        def profile_page(session, _user: str):
            return {
                "fan_data": {"is_own_page": session.cookies.get("browser_profile") == "Profile 1"}
            }

        fake_browser_cookie3 = SimpleNamespace(vivaldi=load_cookies)
        with (
            patch.object(MODULE, "browser_cookie3", fake_browser_cookie3),
            patch.object(MODULE, "BROWSER_COOKIE_PATTERNS", {"vivaldi": ()}),
            patch.object(MODULE, "browser_cookie_files", return_value=cookie_files),
            patch.object(MODULE, "pagedata_from_profile", side_effect=profile_page),
        ):
            session = MODULE.configure_session(True, "listener")

        self.assertEqual(session.cookies.get("browser_profile"), "Profile 1")

    def test_rejects_browser_cookies_that_are_not_authenticated(self) -> None:
        cookie_jar = cookiejar_from_dict({"client_id": "anonymous"})
        fake_browser_cookie3 = SimpleNamespace(vivaldi=lambda **_kwargs: cookie_jar)
        with (
            patch.object(MODULE, "browser_cookie3", fake_browser_cookie3),
            patch.object(MODULE, "BROWSER_COOKIE_PATTERNS", {"vivaldi": ()}),
            patch.object(MODULE, "browser_cookie_files", return_value=[None]),
            patch.object(
                MODULE,
                "pagedata_from_profile",
                return_value={"fan_data": {"is_own_page": False}},
            ),
            self.assertRaisesRegex(RuntimeError, "authenticated Bandcamp cookies"),
        ):
            MODULE.configure_session(True, "listener")


class DownloadMissingTests(unittest.TestCase):
    @staticmethod
    def report_item() -> dict[str, object]:
        return {
            "artist": "Artist",
            "title": "Album",
            "item_type": "album",
            "item_id": 123,
            "url": "https://artist.bandcamp.com/album/example",
            "download_available": True,
            "redownload_url": None,
        }

    def test_dry_run_says_download_will_refresh_urls_without_audit(self) -> None:
        report = {"bandcamp_user": "listener", "missing": [self.report_item()]}
        with (
            patch.object(MODULE, "load_json", return_value=report),
            patch.object(MODULE.console, "print") as print_message,
        ):
            MODULE.download_missing(report_json=Path("report.json"))

        output = " ".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("refreshed automatically", output)
        self.assertNotIn("audit", output.lower())

    def test_download_refreshes_and_persists_protected_urls(self) -> None:
        report_item = self.report_item()
        report = {"bandcamp_user": "listener", "missing": [report_item]}
        fresh_item = dict(report_item) | {"redownload_url": "https://bandcamp.com/download/token"}
        collection = {"fetched_at": "2026-07-31T00:00:00Z", "items": [fresh_item]}
        session = MODULE.new_bandcamp_session()
        report_path = Path("report.json")

        with (
            patch.object(MODULE, "load_json", return_value=report),
            patch.object(MODULE, "configure_session", return_value=session) as configure,
            patch.object(MODULE, "fetch_collection_items", return_value=collection) as fetch,
            patch.object(MODULE, "write_json") as write,
            patch.object(MODULE, "download_purchased_bandcamp_item", return_value=[]) as download,
            patch.object(MODULE.console, "print"),
        ):
            MODULE.download_missing(report_json=report_path, yes=True)

        configure.assert_called_once_with(True, "listener")
        fetch.assert_called_once_with("listener", session=session)
        write.assert_called_once_with(report_path, report)
        self.assertEqual(report_item["redownload_url"], fresh_item["redownload_url"])
        download.assert_called_once()


class IncrementalMusicScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_path = Path(self.temporary_directory.name)
        self.music_root = temporary_path / "music"
        self.music_root.mkdir()
        self.checkpoint_db = temporary_path / "checkpoints.sqlite3"

    @staticmethod
    def track_reader(path: Path, _root: Path):
        title = path.read_text(encoding="utf-8")
        return MODULE.LocalTrack(
            path=str(path),
            artist="Artist",
            albumartist="Artist",
            album="Album",
            title=title,
        )

    def scan(self, *, max_files: int | None = None, rescan_all: bool = False):
        with patch.object(MODULE, "track_from_file", side_effect=self.track_reader) as reader:
            result = MODULE.scan_music_tree(
                self.music_root,
                max_files=max_files,
                checkpoint_db=self.checkpoint_db,
                rescan_all=rescan_all,
            )
        return result, reader.call_count

    def test_reuses_unchanged_tracks_and_refreshes_filesystem_changes(self) -> None:
        first_path = self.music_root / "01 - First.mp3"
        first_path.write_text("First", encoding="utf-8")

        first_scan, reads = self.scan()
        self.assertEqual(reads, 1)
        self.assertEqual(first_scan.stats.metadata_scanned, 1)
        self.assertEqual(first_scan.stats.metadata_reused, 0)

        unchanged_scan, reads = self.scan()
        self.assertEqual(reads, 0)
        self.assertEqual(unchanged_scan.stats.metadata_scanned, 0)
        self.assertEqual(unchanged_scan.stats.metadata_reused, 1)
        self.assertEqual(unchanged_scan.tracks[0].title, "First")

        first_path.write_text("First (remastered)", encoding="utf-8")
        changed_scan, reads = self.scan()
        self.assertEqual(reads, 1)
        self.assertEqual(changed_scan.stats.metadata_scanned, 1)
        self.assertEqual(changed_scan.tracks[0].title, "First (remastered)")

        first_path.unlink()
        second_path = self.music_root / "02 - Second.flac"
        second_path.write_text("Second", encoding="utf-8")
        final_scan, reads = self.scan()
        self.assertEqual(reads, 1)
        self.assertEqual(final_scan.stats.removed, 1)
        self.assertEqual([track.title for track in final_scan.tracks], ["Second"])

        with sqlite3.connect(self.checkpoint_db) as connection:
            cached_paths = connection.execute("SELECT path FROM music_file_checkpoints").fetchall()
        self.assertEqual(cached_paths, [(str(second_path),)])

    def test_rescan_all_refreshes_unchanged_tracks(self) -> None:
        (self.music_root / "track.mp3").write_text("Track", encoding="utf-8")
        self.scan()

        refreshed_scan, reads = self.scan(rescan_all=True)

        self.assertEqual(reads, 1)
        self.assertEqual(refreshed_scan.stats.metadata_scanned, 1)
        self.assertEqual(refreshed_scan.stats.metadata_reused, 0)

    def test_limited_scan_does_not_remove_unvisited_checkpoints(self) -> None:
        for filename in ("01.mp3", "02.mp3"):
            (self.music_root / filename).write_text(filename, encoding="utf-8")

        initial_scan, reads = self.scan()
        self.assertEqual(len(initial_scan.tracks), 2)
        self.assertEqual(reads, 2)

        limited_scan, reads = self.scan(max_files=1)
        self.assertEqual(len(limited_scan.tracks), 1)
        self.assertEqual(reads, 0)
        self.assertFalse(limited_scan.stats.complete)
        self.assertEqual(limited_scan.stats.removed, 0)

        full_scan, reads = self.scan()
        self.assertEqual(len(full_scan.tracks), 2)
        self.assertEqual(reads, 0)
        self.assertEqual(full_scan.stats.metadata_reused, 2)


if __name__ == "__main__":
    unittest.main()
