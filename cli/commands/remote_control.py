from cli.remote_control import RemoteControlManager


def register(subparsers):
    parser = subparsers.add_parser("remote-control", help="Manage Telegram remote control")
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "status", "stop", "logs"],
        help="Remote-control action",
    )


async def handle(args, container, ui):
    manager = RemoteControlManager()

    if args.action == "start":
        status = manager.start()
        ui.print_remote_status(status, message="Telegram remote control started.")
        return
    if args.action == "status":
        ui.print_remote_status(manager.load_status())
        return
    if args.action == "stop":
        status = manager.stop()
        ui.print_remote_status(status, message="Telegram remote control stopped.")
        return

    logs = manager.tail_logs()
    if not logs:
        ui.print_line("No remote-control logs yet.")
        return
    ui.print_block("Remote Control Logs", logs)
