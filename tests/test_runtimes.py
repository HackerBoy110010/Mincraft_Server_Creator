import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from minecraft_server_creator.runtimes import RuntimeErrorMCSC, host_platform, safe_extract_tar, safe_extract_zip


class RuntimeTests(unittest.TestCase):
    def test_platform_aliases(self):
        self.assertEqual(host_platform("Linux", "x86_64"), ("linux", "x64"))
        self.assertEqual(host_platform("Darwin", "arm64"), ("mac", "aarch64"))
        self.assertEqual(host_platform("Windows", "AMD64"), ("windows", "x64"))

    def test_platform_rejects_unknown_architecture(self):
        with self.assertRaises(RuntimeErrorMCSC):
            host_platform("Linux", "mips")

    def test_zip_extracts_safe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            archive_path = temp_path / "runtime.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("jdk/bin/java", "fake executable")
            destination = temp_path / "out"
            safe_extract_zip(archive_path, destination)
            self.assertEqual((destination / "jdk" / "bin" / "java").read_text(encoding="utf-8"), "fake executable")

    def test_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            archive_path = temp_path / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaises(RuntimeErrorMCSC):
                safe_extract_zip(archive_path, temp_path / "out")
            self.assertFalse((temp_path / "escape.txt").exists())

    def test_tar_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            archive_path = temp_path / "bad.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("../escape.txt")
                data = b"bad"
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            with self.assertRaises(RuntimeErrorMCSC):
                safe_extract_tar(archive_path, temp_path / "out")
            self.assertFalse((temp_path / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
