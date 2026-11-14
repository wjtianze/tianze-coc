"""APK 注入与偏移定位的回归测试（需要原版 APK；找不到时跳过）。"""
import hashlib
import io
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

from tianze_coc.offline_build import LIBG, BuildError, expected_libg_sha256, patch_needed, repack

from .test_catalogs import APK

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(APK is None, "未找到原版 APK")
class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(APK) as z:
            cls.libg = z.read(LIBG)

    def test_libg_matches_offsets(self):
        self.assertEqual(hashlib.sha256(self.libg).hexdigest(), expected_libg_sha256())

    def test_patch_needed(self):
        from elftools.elf.elffile import ELFFile

        p = patch_needed(self.libg)
        self.assertEqual(len(p), len(self.libg))
        self.assertLess(sum(a != b for a, b in zip(p, self.libg)), 32)
        e = ELFFile(io.BytesIO(p))
        needed = [t.needed for s in e.iter_segments() if s["p_type"] == "PT_DYNAMIC"
                  for t in s.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
        self.assertIn("libtianze_probe.so", needed)
        self.assertNotIn("libsentry-android.so", needed)
        with self.assertRaises(BuildError):
            patch_needed(p)

    def test_repack_layout(self):
        out = ROOT / "build" / "test-repack.apk"
        out.parent.mkdir(exist_ok=True)
        try:
            repack(Path(APK), out, patch_needed(self.libg), b"\x7fELF-fake-probe")
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                self.assertIn("lib/arm64-v8a/libtianze_probe.so", names)
                self.assertFalse(any(n.startswith("lib/armeabi-v7a/") for n in names))
                self.assertEqual(z.read(LIBG)[:4], b"\x7fELF")
        finally:
            out.unlink(missing_ok=True)

    def test_find_offsets_regression(self):
        tmp = ROOT / "build" / "libg-test.so"
        tmp.parent.mkdir(exist_ok=True)
        tmp.write_bytes(self.libg)
        try:
            r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "re" / "find_offsets.py"), str(tmp),
                                "--build-tag", "18_647_2", "--expect"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
