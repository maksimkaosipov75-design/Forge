import getpass

from core.providers import normalize_provider_name


def register(subparsers):
    parser = subparsers.add_parser("auth", help="Manage provider API credentials")
    parser.add_argument("action", nargs="?", default="", help="status | remove | <provider>")
    parser.add_argument("target", nargs="?", default="", help="Provider name when needed")
    parser.add_argument("--key", default="", help="API key override")
    parser.add_argument("--chat-id", type=int, default=0, help="Local session id")


def _status_lines(container) -> list[str]:
    lines = ["Credentials", ""]
    for provider_name in container.provider_paths:
        if provider_name == "openrouter":
            source = "env" if container.settings.OPENROUTER_API_KEY.strip() else ("saved" if container.credential_store.has_api_key(provider_name) else "missing")
            lines.append(f"{provider_name}: {source}")
    return lines


async def handle(args, container, ui):
    action = (args.action or "").strip().lower()

    if not action:
        provider_name = "openrouter"
        if not container.resolve_api_key(provider_name):
            api_key = args.key.strip() or getpass.getpass("OpenRouter API key: ").strip()
            if not api_key:
                raise SystemExit("No API key entered.")
            container.credential_store.set_api_key(provider_name, api_key)
            ui.print_notice(f"Saved credentials for {provider_name}.", provider=provider_name, kind="success")
            return
        ui.print_block(
            "Auth",
            "openrouter: configured\n\nUse `forge auth openrouter` to replace the key or `forge auth remove openrouter` to delete it.",
            border_style="green",
        )
        return

    if action == "status":
        ui.print_block("Auth", "\n".join(_status_lines(container)), border_style="green")
        return

    if action == "remove":
        provider_name = normalize_provider_name(args.target)
        if provider_name != "openrouter":
            raise SystemExit("Only OpenRouter API credentials are currently managed through `forge auth`.")
        container.credential_store.delete_api_key(provider_name)
        ui.print_notice(f"Removed saved credentials for {provider_name}.", provider=provider_name, kind="success")
        return

    provider_name = normalize_provider_name(action)
    if provider_name != "openrouter":
        raise SystemExit("Only OpenRouter API credentials are currently managed through `forge auth`.")

    api_key = args.key.strip() or getpass.getpass("OpenRouter API key: ").strip()
    if not api_key:
        raise SystemExit("No API key entered.")

    container.credential_store.set_api_key(provider_name, api_key)
    ui.print_notice(f"Saved credentials for {provider_name}.", provider=provider_name, kind="success")
