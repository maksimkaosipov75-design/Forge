from providers import is_supported_provider, normalize_provider_name


def register(subparsers):
    parser = subparsers.add_parser("run", help="Run a single task through one provider")
    parser.add_argument("prompt", help="Task prompt")
    parser.add_argument("--provider", default="", help="Provider override")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    if args.provider and not is_supported_provider(args.provider):
        raise SystemExit(f"Unsupported provider: {args.provider}")
    provider_name = normalize_provider_name(args.provider or session.current_provider)
    ready, message = container.provider_is_ready(provider_name)
    if not ready:
        raise SystemExit(f"{provider_name}: {message}")
    runtime = await container.ensure_runtime_started(session, provider_name)

    last_status = {"text": ""}

    async def status_callback(text: str):
        if text and text != last_status["text"]:
            ui.print_status(text)
            last_status["text"] = text

    task_result = await container.execution_service.execute_provider_task(
        session=session,
        runtime=runtime,
        provider_name=provider_name,
        prompt=args.prompt,
        status_callback=status_callback,
    )
    container.remember_task_result(session, task_result)
    ui.print_kv("exit_code", str(task_result.exit_code))
    if task_result.new_files:
        ui.print_kv("new_files", ", ".join(task_result.new_files))
    if task_result.changed_files:
        ui.print_kv("changed_files", ", ".join(task_result.changed_files))
    if task_result.answer_text:
        ui.print_line()
        ui.print_line(task_result.answer_text[:6000])
