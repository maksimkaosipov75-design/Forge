def register(subparsers):
    parser = subparsers.add_parser("artifacts", help="List saved artifact files")
    parser.add_argument("--chat-id", type=int, default=0, help="Chat/session id placeholder")
    parser.add_argument("--limit", type=int, default=10, help="How many artifacts to list")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    artifacts = container.latest_artifact_files(session, limit=args.limit)
    ui.print_artifacts(artifacts)
