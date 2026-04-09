from cli.commands import artifacts, auth, model, orchestrate, plan, providers, remote_control, run, runs, show, smoke


def register_commands(subparsers):
    auth.register(subparsers)
    providers.register(subparsers)
    model.register(subparsers)
    plan.register(subparsers)
    run.register(subparsers)
    smoke.register(subparsers)
    orchestrate.register(subparsers)
    runs.register(subparsers)
    show.register(subparsers)
    artifacts.register(subparsers)
    remote_control.register(subparsers)
