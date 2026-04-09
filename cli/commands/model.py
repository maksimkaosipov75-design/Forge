from providers import (
    is_supported_provider,
    list_provider_models,
    normalize_provider_name,
    provider_default_model,
)


def register(subparsers):
    parser = subparsers.add_parser("model", help="Show or change provider models")
    parser.add_argument("provider", nargs="?", default="", help="Provider name")
    parser.add_argument("model", nargs="?", default="", help="Model name or 'default'")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


def _render_provider_models(session, provider_name: str) -> list[str]:
    current = session.provider_models.get(provider_name, "").strip()
    resolved = current or provider_default_model(provider_name) or "default"
    lines = [f"provider: {provider_name}", f"current: {resolved}"]
    catalog = list_provider_models(provider_name)
    if catalog:
        lines.append("")
        lines.append("available:")
        for item in catalog:
            marker = "*" if item.name == current else "-"
            lines.append(f"  {marker} {item.name}  {item.label}")
    return lines


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)

    if not args.provider:
        for provider_name in container.provider_paths:
            ui.print_block(
                f"Model · {provider_name}",
                "\n".join(_render_provider_models(session, provider_name)),
                border_style=provider_name,
            )
        return

    provider_name = normalize_provider_name(args.provider)
    if not is_supported_provider(provider_name):
        raise SystemExit(f"Unsupported provider: {args.provider}")

    if not args.model:
        ui.print_block(
            f"Model · {provider_name}",
            "\n".join(_render_provider_models(session, provider_name)),
            border_style=provider_name,
        )
        return

    new_model = "" if args.model.lower() == "default" else args.model.strip()
    session.provider_models[provider_name] = new_model
    container.reset_runtime(session, provider_name)
    container.save_session(session)
    label = new_model or provider_default_model(provider_name) or "default"
    ui.print_notice(
        f"{provider_name} model set to {label}. The new model will be used on the next prompt.",
        provider=provider_name,
        kind="success",
    )
