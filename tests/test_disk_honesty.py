"""The disk reading describes the volume Ava writes to, and admits when it is virtual.

Reported from the same Windows laptop as tests/test_fit_pool_honesty.py. The
monitor said `Disk · 27.2 GB / 1006.9 GB — 928.5 GB free` while Windows said
156 GB free of 952 GB. Neither figure was a rounding error: `_disk` measured `/`,
the CONTAINER's root, which on Docker Desktop is an overlay on a sparse .vhdx
that advertises its ~1 TB nominal maximum and its own emptiness no matter how
full the drive behind it is.

Two halves, the same two the memory pool and engine locality each needed:

  * measure the right thing — AVA_HOME, where models and weights actually land,
    so the number answers the only question it is asked ("will this pull fit").
  * do not then LIE about it — where the backing store is not observable from in
    here, say the reading is capped rather than invent the host's figure.

Run: .venv/bin/python -m pytest tests/test_disk_honesty.py -q
"""
import unittest
from unittest import mock

from ava_bridge import hardware, settings

_MOUNTINFO = (
    "21 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "55 21 0:50 / /data rw,relatime - 9p drvfs rw,trans=virtio\n"
    "61 21 0:61 / /mnt/models rw,relatime - ext4 /dev/sdb1 rw\n"
)


class MeasuresWhereDataLands(unittest.TestCase):
    def test_it_measures_ava_home_not_the_container_root(self):
        """`/` is the container's own filesystem; AVA_HOME is the bind mount a
        model pull actually fills."""
        with mock.patch.object(hardware.shutil, "disk_usage") as du:
            du.return_value = mock.Mock(total=100 * 1024 ** 3,
                                        used=40 * 1024 ** 3,
                                        free=60 * 1024 ** 3)
            hardware._disk()
        du.assert_called_once()
        self.assertEqual(du.call_args[0][0], str(settings.AVA_HOME),
                         "the disk reading must describe AVA_HOME's volume")

    def test_the_payload_names_the_volume_and_its_standing(self):
        with mock.patch.object(hardware.shutil, "disk_usage") as du:
            du.return_value = mock.Mock(total=100 * 1024 ** 3,
                                        used=40 * 1024 ** 3,
                                        free=60 * 1024 ** 3)
            out = hardware._disk("/data")
        self.assertEqual(out["path"], "/data")
        self.assertIn("capped", out)
        self.assertIn("cap_kind", out)
        self.assertEqual(out["total_gb"], 100.0)
        self.assertEqual(out["free_gb"], 60.0)
        self.assertEqual(out["used_pct"], 40)

    def test_an_unreadable_path_still_answers_the_contract(self):
        """A blank panel is worse than a blank number: the keys must survive."""
        with mock.patch.object(hardware.shutil, "disk_usage",
                               side_effect=OSError("gone")):
            out = hardware._disk("/nope")
        self.assertIsNone(out["total_gb"])
        self.assertEqual(out["path"], "/nope")
        self.assertFalse(out["capped"])
        self.assertIsNone(out["cap_kind"])


class FstypeOfTheCoveringMount(unittest.TestCase):
    def _fstype(self, path):
        with mock.patch("builtins.open", mock.mock_open(read_data=_MOUNTINFO)), \
             mock.patch.object(hardware.os.path, "realpath", lambda p: p):
            return hardware._fstype_of(path)

    def test_the_deepest_mount_wins(self):
        """`/` and `/data` both cover `/data/models`; only the deeper one
        describes what it is stored on."""
        self.assertEqual(self._fstype("/data/models"), "9p")

    def test_a_path_under_root_gets_root(self):
        self.assertEqual(self._fstype("/var/lib"), "ext4")

    def test_an_exact_mount_point_matches_itself(self):
        self.assertEqual(self._fstype("/mnt/models"), "ext4")

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self):
        """`/database` is not under `/data`, however it sorts."""
        self.assertEqual(self._fstype("/database"), "ext4")

    def test_unreadable_mountinfo_is_not_fatal(self):
        with mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertEqual(hardware._fstype_of("/data"), "")


class CapIsFlaggedNotInvented(unittest.TestCase):
    def _cap(self, fstype, osrelease):
        with mock.patch.object(hardware.hwinfo, "_read_sysfs",
                               return_value=osrelease):
            return hardware._disk_cap(fstype)

    def test_wsl2_ext4_is_capped(self):
        """The sparse vhdx case: correct for everything inside, not the machine's."""
        self.assertEqual(self._cap("ext4", "5.15.0-microsoft-standard-WSL2"),
                         (True, "wsl2-vm"))

    def test_a_windows_path_mounted_back_in_is_not(self):
        """statvfs on drvfs/9p reads the real Windows volume, so it needs no caveat
        even though the kernel underneath is still WSL2."""
        for fs in ("drvfs", "9p", "virtiofs", "cifs", "nfs"):
            self.assertEqual(self._cap(fs, "5.15.0-microsoft-standard-WSL2"),
                             (False, None), fs)

    def test_a_plain_linux_box_is_not_capped(self):
        self.assertEqual(self._cap("ext4", "6.8.0-generic"), (False, None))

    def test_an_unreadable_osrelease_does_not_invent_a_cap(self):
        self.assertEqual(self._cap("ext4", None), (False, None))

    def test_it_speaks_the_memory_pools_vocabulary(self):
        """One word for one condition. hwinfo._pool_cap already returns
        'wsl2-vm' for the same VM; a second spelling would make the two
        readings look like different findings."""
        from ava_bridge import hwinfo
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", return_value=None), \
             mock.patch.object(hwinfo, "_read_sysfs",
                               return_value="5.15.0-microsoft-standard-WSL2"):
            self.assertEqual(hwinfo._pool_cap(32.0)[1], self._cap("ext4",
                             "5.15.0-microsoft-standard-WSL2")[1])


if __name__ == "__main__":
    unittest.main()
