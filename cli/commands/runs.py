def register(subparsers):
    parser = subparsers.add_parser("runs", help="List recent runs")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")
    parser.add_argument("--limit", type=int, default=10, help="How many runs to show")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    runs = container.recent_runs(session, limit=args.limit)
    if not runs:
        ui.print_line("No runs found.")
        return
    for index, run in enumerate(runs, start=1):
        ui.print_run_brief(run, index=index)
