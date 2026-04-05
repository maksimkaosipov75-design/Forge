def register(subparsers):
    subparsers.add_parser("providers", help="List available providers")


async def handle(args, container, ui):
    for name, path in container.provider_paths.items():
        ui.print_line(f"{name}\t{path}")
