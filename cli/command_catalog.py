from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    category: str
    description: str


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("/help", "Shell", "Show the full help screen"),
    CommandSpec("/commands", "Shell", "Show all available commands"),
    CommandSpec("/home", "Shell", "Return to the start page"),
    CommandSpec("/new", "Shell", "Reset the visible workspace"),
    CommandSpec("/clear", "Session", "Clear session state, history, and last results"),
    CommandSpec("/cls", "Shell", "Clear only the visible stream output"),
    CommandSpec("/save", "Shell", "Save last answer to file (/save [filename])"),
    CommandSpec("/export", "Shell", "Export the current stream (/export [md|txt])"),
    CommandSpec("/retry", "Shell", "Re-run the last prompt"),
    CommandSpec("/expand", "Shell", "Show the full last answer"),
    CommandSpec("/copy", "Shell", "Copy the last answer to clipboard"),
    CommandSpec("/paste", "Shell", "Show clipboard content in stream"),
    CommandSpec("/history", "Shell", "Show recent input history (/history [n])"),
    CommandSpec("/quit", "Shell", "Exit the shell"),
    CommandSpec("/exit", "Shell", "Exit the shell"),
    CommandSpec("/cd", "Workspace", "Change working directory (/cd <path>)"),
    CommandSpec("/cwd", "Workspace", "Print current working directory"),
    CommandSpec("/diff", "Workspace", "Show git diff summary"),
    CommandSpec("/commit", "Workspace", "Create git commit (/commit [message])"),
    CommandSpec("/status", "Status", "Show current session info"),
    CommandSpec("/limits", "Status", "Show provider health and rate limits"),
    CommandSpec("/usage", "Status", "Show token usage and task counts"),
    CommandSpec("/metrics", "Status", "Show internal metrics"),
    CommandSpec("/stats", "Status", "Show per-provider performance stats"),
    CommandSpec("/todos", "Status", "Extract TODOs from the last answer"),
    CommandSpec("/thinking", "Status", "Control reasoning visibility (/thinking off|compact|full)"),
    CommandSpec("/auth", "Models", "Manage API credentials"),
    CommandSpec("/model", "Models", "Show or change provider models"),
    CommandSpec("/smoke", "Models", "Run a lightweight provider smoke test"),
    CommandSpec("/provider", "Providers", "Switch default provider"),
    CommandSpec("/providers", "Providers", "List available providers"),
    CommandSpec("/runs", "History", "List recent runs"),
    CommandSpec("/show", "History", "Show run details by index"),
    CommandSpec("/artifacts", "History", "List latest artifact files"),
    CommandSpec("/review", "History", "Review the last result with another provider"),
    CommandSpec("/compact", "Session", "Trim or filter saved history"),
    CommandSpec("/plan", "Orchestration", "Preview an AI orchestration plan"),
    CommandSpec("/run-plan", "Orchestration", "Execute the last previewed plan"),
    CommandSpec("/orchestrate", "Orchestration", "Run a multi-agent orchestration"),
    CommandSpec("/replan", "Orchestration", "Retry or continue a failed orchestration"),
    CommandSpec("/recover", "Orchestration", "Resume from a crash checkpoint"),
    CommandSpec("/remote-control", "Remote", "Start or manage Telegram remote access"),
    CommandSpec("/!", "Shell", "Send text verbatim to the agent (bypasses Forge routing; agent slash cmds won't work in batch mode)"),
)


def textual_command_map() -> dict[str, tuple[str, str]]:
    return {item.name: (item.category, item.description) for item in COMMAND_SPECS}


def all_command_names() -> list[str]:
    return [item.name for item in COMMAND_SPECS]


def grouped_help_lines() -> list[str]:
    lines: list[str] = []
    current_category = None
    for item in COMMAND_SPECS:
        if item.category != current_category:
            if lines:
                lines.append("")
            lines.append(f"[bold]{item.category}[/bold]")
            current_category = item.category
        lines.append(f"  {item.name:<18} {item.description}")
    lines.extend(
        [
            "",
            "[bold]Input shortcuts[/bold]",
            "  @provider:prompt   run with specific provider  e.g. @claude:explain this",
            "  @file.py           inline file content in prompt",
        ]
    )
    return lines


def quick_reference_commands() -> list[tuple[str, str]]:
    return [
        ("/help", "help"),
        ("/commands", "all commands"),
        ("/provider", "switch provider"),
        ("/auth", "manage API keys"),
        ("/model", "change model"),
        ("/smoke", "quick provider test"),
        ("/thinking", "reasoning visibility"),
        ("/plan", "preview orchestration"),
        ("/run-plan", "execute preview"),
        ("/orchestrate", "multi-agent run"),
        ("/review", "review last result"),
        ("/runs", "run history"),
        ("/remote-control", "Telegram access"),
    ]
