def register(subparsers):
    parser = subparsers.add_parser("orchestrate", help="Run a multi-agent orchestration plan")
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
    ui.print_line()

    last_status = {"text": ""}

    async def status_callback(text: str):
        if text and text != last_status["text"]:
            ui.print_status(text)
            last_status["text"] = text

    task_run, aggregate_result = await container.orchestrator_service.run_orchestrated_task(
        session=session,
        plan=plan,
        status_callback=status_callback,
    )
    ui.print_run_detail(task_run)
    if aggregate_result.error_text:
        ui.print_line()
        ui.print_line("error:")
        ui.print_line(aggregate_result.error_text[:3000])
