from providers import is_supported_provider, normalize_provider_name


SMOKE_PROMPT = (
    "Reply in exactly two short lines.\n"
    "Line 1: SMOKE_OK\n"
    "Line 2: one short sentence naming the selected model if you know it."
)


def register(subparsers):
    parser = subparsers.add_parser("smoke", help="Run a lightweight provider smoke test")
    parser.add_argument("provider", nargs="?", default="", help="Provider to test")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    provider_name = normalize_provider_name(args.provider or session.current_provider)
    if not is_supported_provider(provider_name):
        raise SystemExit(f"Unsupported provider: {args.provider}")

    ready, message = container.provider_is_ready(provider_name)
    if not ready:
        raise SystemExit(f"{provider_name}: {message} Use `forge auth {provider_name}` first.")

    runtime = await container.ensure_runtime_started(session, provider_name)
    result = await container.execution_service.execute_provider_task(
        session=session,
        runtime=runtime,
        provider_name=provider_name,
        prompt=SMOKE_PROMPT,
    )
    container.remember_task_result(session, result)

    ui.print_block(
        f"Smoke · {provider_name}",
        "\n".join(
            [
                f"exit_code: {result.exit_code}",
                f"model: {result.model_name or 'default'}",
                f"transport: {result.transport}",
                f"tokens: {result.total_input_tokens} in / {result.total_output_tokens} out",
                "",
                result.answer_text[:2000] or (result.error_text[:2000] if result.error_text else "No response."),
            ]
        ),
        border_style=provider_name,
    )
