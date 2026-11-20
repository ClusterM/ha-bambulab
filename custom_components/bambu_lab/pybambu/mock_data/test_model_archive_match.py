"""Tests for identifying a 3MF by its embedded Title and plate gcode."""

import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from pybambu.media_sources import RemoteMediaFile
from pybambu.models import (
    ARCHIVE_MATCH_DOWNLOAD_LIMIT,
    PrintJob,
    archive_matches_task,
    download_fits_budget,
)


TASK = "AzureFulm Reusable Spool"
GCODE = "/data/Metadata/plate_1.gcode"


def _zip_bytes(title: str, gcode_name: str | None = "Metadata/plate_1.gcode") -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "3D/3dmodel.model",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                f'<metadata name="Title">{title}</metadata>'
                "</model>"
            ),
        )
        if gcode_name is not None:
            archive.writestr(gcode_name, "; gcode\n")
    return buffer.getvalue()


def _remote(name: str, size: int) -> RemoteMediaFile:
    return RemoteMediaFile(
        name=name,
        path=f"/cache/{name}",
        size=size,
        media_type="model",
        source="tcp6000",
        storage="external",
    )


class ArchiveMatchTests(unittest.TestCase):
    def test_title_and_plate_gcode_match(self):
        with zipfile.ZipFile(BytesIO(_zip_bytes(TASK))) as archive:
            self.assertTrue(archive_matches_task(archive, TASK, GCODE))

    def test_wrong_title_does_not_match(self):
        with zipfile.ZipFile(BytesIO(_zip_bytes("Other"))) as archive:
            self.assertFalse(archive_matches_task(archive, TASK, GCODE))

    def test_missing_plate_gcode_does_not_match(self):
        with zipfile.ZipFile(BytesIO(_zip_bytes(TASK, gcode_name=None))) as archive:
            self.assertFalse(archive_matches_task(archive, TASK, GCODE))

    def test_next_file_past_the_budget_does_not_fit(self):
        downloaded = 40 * 1024 * 1024
        self.assertFalse(download_fits_budget(downloaded, 20 * 1024 * 1024))
        self.assertTrue(download_fits_budget(0, ARCHIVE_MATCH_DOWNLOAD_LIMIT))


class TitleScanTests(unittest.TestCase):
    def setUp(self):
        self.job = PrintJob.__new__(PrintJob)
        self.job._subtask_name = TASK
        self.job.gcode_file = GCODE
        self.job._client = MagicMock()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.downloaded: list[str] = []

    def _write(self, name: str, payload: bytes) -> str:
        path = Path(self.directory.name) / name
        path.write_bytes(payload)
        return str(path)

    def _scan(self, files: list[RemoteMediaFile], cached: set[str] | None = None):
        cached = cached or set()
        paths = {
            remote.basename: self._write(remote.basename, remote_payload)
            for remote, remote_payload in files
        }
        remote_files = [remote for remote, _payload in files]

        def download(_source, remote_file, progress_callback=None):
            self.downloaded.append(remote_file.basename)
            return paths[remote_file.basename]

        self.job._model_file_is_cached = lambda remote_file: remote_file.basename in cached
        self.job._remote_file_is_stable = lambda *args, **kwargs: True
        self.job._download_model_file_from_remote = download
        source = MagicMock()
        source.name = "tcp6000"
        return self.job._download_model_matching_archive([source], remote_files)

    def test_skips_a_newer_title_and_returns_the_downloaded_match(self):
        newer = _remote("Other.gcode.3mf", 1000)
        match = _remote("AzureFilmSpool_Single side, print x2.gcode.3mf", 2000)
        missing_gcode = _remote("Same Title.gcode.3mf", 500)
        result = self._scan([
            (newer, _zip_bytes("Other")),
            (missing_gcode, _zip_bytes(TASK, gcode_name=None)),
            (match, _zip_bytes(TASK)),
        ])
        self.assertEqual(Path(result).name, match.basename)
        self.assertEqual(
            self.downloaded,
            [newer.basename, missing_gcode.basename, match.basename],
        )

    def test_stops_before_a_file_that_would_pass_the_budget(self):
        first = _remote("first.gcode.3mf", 40 * 1024 * 1024)
        second = _remote("second.gcode.3mf", 20 * 1024 * 1024)
        result = self._scan([
            (first, _zip_bytes("Other")),
            (second, _zip_bytes(TASK)),
        ])
        self.assertIsNone(result)
        self.assertEqual(self.downloaded, [first.basename])

    def test_cached_file_does_not_consume_the_budget(self):
        cached = _remote("cached.gcode.3mf", 40 * 1024 * 1024)
        match = _remote("match.gcode.3mf", 20 * 1024 * 1024)
        result = self._scan(
            [
                (cached, _zip_bytes("Other")),
                (match, _zip_bytes(TASK)),
            ],
            cached={cached.basename},
        )
        self.assertEqual(Path(result).name, match.basename)
        self.assertEqual(self.downloaded, [cached.basename, match.basename])
