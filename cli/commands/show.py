def register(subparsers):
    parser = subparsers.add_parser("show", help="Show run details")
    parser.add_argument("index", type=int, help="Run number from `runs` output")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    run = container.run_by_index(session, args.index)
    if run is None:
        ui.print_line("Run not found.")
        return
    ui.print_run_detail(run)
