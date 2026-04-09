from providers import (
    is_supported_provider,
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
    catalog = session._container_model_catalog(provider_name)  # type: ignore[attr-defined]
    if catalog:
        lines.append("")
        lines.append("available:")
        for item in catalog[:10]:
            marker = "*" if item.name == current else "-"
            lines.append(f"  {marker} {item.name}  {item.label}")
        if provider_name == "openrouter":
            lines.append("")
            lines.append("tip: forge model openrouter sonnet")
            lines.append("tip: forge model openrouter free")
            lines.append("tip: forge model openrouter refresh")
    return lines


async def handle(args, container, ui):
    session = container.get_session(args.chat_id)
    session._container_model_catalog = lambda provider_name: container.list_available_models(provider_name)  # type: ignore[attr-defined]

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

    requested = args.model.strip()
    if provider_name == "openrouter" and requested.lower() == "refresh":
        refreshed = container.list_available_models(provider_name, refresh=True)
        ui.print_notice(
            f"Refreshed OpenRouter model catalog ({len(refreshed)} models cached).",
            provider=provider_name,
            kind="success",
        )
        ui.print_block(
            f"Model · {provider_name}",
            "\n".join(_render_provider_models(session, provider_name)),
            border_style=provider_name,
        )
        return

    resolution = container.resolve_model_selection(provider_name, requested)
    if resolution.status == "ambiguous":
        lines = [resolution.message or "Several models matched your query.", ""]
        for item in resolution.matches[:8]:
            lines.append(f"- {item.label}  [{item.name}]")
        ui.print_block(f"Model Search · {provider_name}", "\n".join(lines), border_style=provider_name)
        return
    if resolution.status == "missing":
        ui.print_notice(resolution.message, provider=provider_name, kind="warning")
        return

    new_model = resolution.model_name
    session.provider_models[provider_name] = new_model
    container.reset_runtime(session, provider_name)
    container.save_session(session)
    label = new_model or provider_default_model(provider_name) or "default"
    ui.print_notice(
        f"{provider_name} model set to {label}. The new model will be used on the next prompt.",
        provider=provider_name,
        kind="success",
    )
