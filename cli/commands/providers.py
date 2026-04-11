from core.providers import get_provider_definition, list_provider_models, provider_default_model


def register(subparsers):
    subparsers.add_parser("providers", help="List available providers")


async def handle(args, container, ui):
    for name, path in container.provider_paths.items():
        definition = get_provider_definition(name)
        default_model = provider_default_model(name) or "default"
        model_count = len(list_provider_models(name))
        detail = [
            f"{name} [{definition.transport}]",
            f"label: {definition.label}",
            f"default_model: {default_model}",
            f"specialties: {', '.join(definition.specialties)}",
        ]
        if model_count:
            detail.append(f"models: {model_count} curated")
        detail.append(f"target: {path}")
        ui.print_block(f"Provider · {name}", "\n".join(detail), border_style=definition.accent_color)
