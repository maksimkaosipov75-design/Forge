import unittest

from bridge_cli import build_parser


class BridgeCliParserTests(unittest.TestCase):
    def test_build_parser_supports_run_and_orchestrate(self):
        parser = build_parser()

        run_args = parser.parse_args(["run", "fix bug", "--provider", "codex"])
        orch_args = parser.parse_args(["orchestrate", "build desktop app"])

        self.assertEqual(run_args.command, "run")
        self.assertEqual(run_args.provider, "codex")
        self.assertEqual(orch_args.command, "orchestrate")

    def test_build_parser_supports_runs_and_show(self):
        parser = build_parser()

        runs_args = parser.parse_args(["runs", "--limit", "5"])
        show_args = parser.parse_args(["show", "2"])

        self.assertEqual(runs_args.command, "runs")
        self.assertEqual(runs_args.limit, 5)
        self.assertEqual(show_args.index, 2)

    def test_build_parser_supports_shell_chat_id(self):
        parser = build_parser()

        args = parser.parse_args(["--chat-id", "42"])

        self.assertIsNone(args.command)
        self.assertEqual(args.chat_id, 42)

    def test_build_parser_supports_remote_control(self):
        parser = build_parser()

        args = parser.parse_args(["remote-control", "status"])

        self.assertEqual(args.command, "remote-control")
        self.assertEqual(args.action, "status")

    def test_build_parser_keeps_shell_without_command(self):
        parser = build_parser()

        args = parser.parse_args([])

        self.assertIsNone(args.command)

    def test_build_parser_supports_textual_flag(self):
        parser = build_parser()

        args = parser.parse_args(["--textual"])

        self.assertTrue(args.textual)
        self.assertIsNone(args.command)


if __name__ == "__main__":
    unittest.main()
