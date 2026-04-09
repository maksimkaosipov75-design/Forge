import unittest

from cli.command_catalog import all_command_names, grouped_help_lines, textual_command_map


class CliCommandCatalogTests(unittest.TestCase):
    def test_textual_command_map_contains_expected_parity_commands(self):
        command_map = textual_command_map()

        for command in ("/commands", "/compact", "/review", "/usage", "/metrics", "/todos", "/commit", "/run-plan", "/thinking"):
            self.assertIn(command, command_map)

    def test_grouped_help_lines_include_category_headers(self):
        lines = grouped_help_lines()

        self.assertTrue(any("Shell" in line for line in lines))
        self.assertTrue(any("Orchestration" in line for line in lines))

    def test_all_command_names_are_unique(self):
        names = all_command_names()

        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
