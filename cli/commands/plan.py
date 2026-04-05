def register(subparsers):
    parser = subparsers.add_parser("plan", help="Build an orchestration plan")
    parser.add_argument("prompt", help="Task prompt")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    planner = container.build_planner(session)
    plan = planner.build_plan(args.prompt)
    ui.print_plan(plan)
