from cli.commands import artifacts, model, orchestrate, plan, providers, remote_control, run, runs, show


def register_commands(subparsers):
    providers.register(subparsers)
    model.register(subparsers)
    plan.register(subparsers)
    run.register(subparsers)
    orchestrate.register(subparsers)
    runs.register(subparsers)
    show.register(subparsers)
    artifacts.register(subparsers)
    remote_control.register(subparsers)
