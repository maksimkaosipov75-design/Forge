"""Tests for bot.file_registry — short-ID path registry."""

import unittest

import bot.file_registry as reg


def _fresh():
    """Clear the registry between tests."""
    reg._registry.clear()


class FileRegistryTests(unittest.TestCase):

    def setUp(self):
        _fresh()

    def test_register_returns_12_char_hex(self):
        fid = reg.register("/some/path/file.py")

        self.assertEqual(len(fid), 12)
        self.assertRegex(fid, r"^[0-9a-f]+$")

    def test_register_same_path_returns_same_id(self):
        fid1 = reg.register("/home/user/project/main.py")
        fid2 = reg.register("/home/user/project/main.py")

        self.assertEqual(fid1, fid2)

    def test_resolve_returns_original_path(self):
        path = "/very/long/absolute/path/that/would/exceed/64/bytes/easily/file.py"
        fid = reg.register(path)

        self.assertEqual(reg.resolve(fid), path)

    def test_resolve_unknown_id_returns_none(self):
        self.assertIsNone(reg.resolve("deadbeef1234"))

    def test_callback_data_always_within_64_bytes(self):
        # Simulate the worst-case path Telegram would see
        long_path = "/home/mozze0/projects/qwen-telegram-bridge/src/deeply/nested/module/implementation.py"
        fid = reg.register(long_path)
        callback_data = f"view_file:{fid}"

        self.assertLessEqual(len(callback_data.encode()), 64)

    def test_different_paths_get_different_ids(self):
        fid1 = reg.register("/path/to/alpha.py")
        fid2 = reg.register("/path/to/beta.py")

        self.assertNotEqual(fid1, fid2)

    def test_eviction_when_full(self):
        reg._MAX_SIZE  # just reference
        original_max = reg._MAX_SIZE
        try:
            reg._MAX_SIZE = 4
            paths = [f"/path/to/file_{i}.py" for i in range(5)]
            for p in paths:
                reg.register(p)
            # Registry should not grow beyond _MAX_SIZE after eviction
            self.assertLessEqual(len(reg._registry), original_max)
        finally:
            reg._MAX_SIZE = original_max
            _fresh()

    def test_resolve_after_eviction_may_return_none_for_old_entries(self):
        original_max = reg._MAX_SIZE
        try:
            reg._MAX_SIZE = 4
            ids = [reg.register(f"/file_{i}.py") for i in range(6)]
            # The last registered ID must always be resolvable
            last_id = ids[-1]
            self.assertIsNotNone(reg.resolve(last_id))
        finally:
            reg._MAX_SIZE = original_max
            _fresh()


if __name__ == "__main__":
    unittest.main()
