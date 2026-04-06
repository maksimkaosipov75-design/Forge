import argparse
import asyncio

from cli.commands import register_commands
from cli.shell import run_shell
from cli.textual_app import run_textual_shell
from cli.ui import CliUi
from runtime import RuntimeContainer


COMMAND_MODULES = {
    "providers": "cli.commands.providers",
    "plan": "cli.commands.plan",
    "run": "cli.commands.run",
    "orchestrate": "cli.commands.orchestrate",
    "runs": "cli.commands.runs",
    "show": "cli.commands.show",
    "artifacts": "cli.commands.artifacts",
    "remote-control": "cli.commands.remote_control",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Multi-agent runtime CLI")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id for shell mode")
    parser.add_argument("--textual", action="store_true", help="Run the optional Textual full-screen shell")
    subparsers = parser.add_subparsers(dest="command")
    register_commands(subparsers)
    return parser


async def async_main(args):
    container = RuntimeContainer()
    ui = CliUi()

    if not args.command:
        await run_shell(container, ui, chat_id=args.chat_id)
        return

    module = __import__(COMMAND_MODULES[args.command], fromlist=["handle"])
    await module.handle(args, container, ui)


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command and args.textual:
        container = RuntimeContainer()
        run_textual_shell(container, chat_id=args.chat_id)
        return

    asyncio.run(async_main(args))


def textual_main():
    """Entry point for the `forge` / single-word launcher — opens Textual UI directly."""
    import sys
    parser = build_parser()
    # Inject --textual if no subcommand given so bare `forge` opens the TUI
    if len(sys.argv) == 1 or (len(sys.argv) > 1 and sys.argv[1].startswith("--chat-id")):
        sys.argv.insert(1, "--textual")
    args = parser.parse_args()
    container = RuntimeContainer()
    run_textual_shell(container, chat_id=args.chat_id)
