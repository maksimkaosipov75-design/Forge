def register(subparsers):
    parser = subparsers.add_parser("plan", help="Build an orchestration plan")
    parser.add_argument("prompt", help="Task prompt")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    planner = container.build_ai_planner(session)
    planning_provider = container.pick_planning_provider(session)
    planning_runtime = await container.ensure_runtime_started(session, planning_provider)
    plan = await planner.build_plan(
        args.prompt,
        container.execution_service,
        session,
        planning_runtime,
    )
    session.last_plan = plan
    container.save_session(session)
    ui.print_plan(plan)
