from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "bin" / "bandcamp-plex-sync"
LOADER = importlib.machinery.SourceFileLoader("bandcamp_plex_sync", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
LOADER.exec_module(MODULE)


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
