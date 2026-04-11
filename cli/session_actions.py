from __future__ import annotations

import subprocess

from core.providers import provider_default_model


def extract_todos(answer_text: str) -> list[str]:
    todos: list[str] = []
    for raw_line in (answer_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("- [ ] ", "* [ ] ", "- [x] ", "* [x] ")):
            todos.append(line)
            continue
        if line.startswith(("TODO:", "Todo:", "todo:")):
            todos.append(line)
    return todos


def build_review_request(
    task_prompt: str,
    answer_text: str,
    touched_files: list[str],
    review_focus: str = "",
) -> str:
    sections = [
        "You are reviewing the result of another coding agent.",
        "Be concise and practical.",
        "Report:",
        "1. Verdict",
        "2. Bugs or risks",
        "3. Missing tests or validation",
        "4. Recommended next step",
        "",
        "Original task:",
        task_prompt or "(unknown)",
        "",
        "Touched files:",
        "\n".join(f"- {item}" for item in touched_files[:20]) or "- none",
        "",
        "Result to review:",
        answer_text or "(empty result)",
    ]
    if review_focus.strip():
        sections.extend(["", f"Extra focus: {review_focus.strip()}"])
    return "\n".join(sections)


def compact_session(session, keep: int | None = None, needle: str = "") -> str:
    if needle:
        lowered = needle.lower()
        session.history = [
            item for item in session.history
            if lowered in item.prompt.lower() or lowered in item.answer_text.lower()
        ]
        session.run_history = [
            item for item in session.run_history
            if lowered in item.prompt.lower() or lowered in item.answer_text.lower()
        ]
        if session.history:
            session.last_task_result = session.history[-1]
        if session.run_history:
            session.last_task_run = session.run_history[-1]
        return f"History filtered by '{needle}'. tasks={len(session.history)} runs={len(session.run_history)}"

    keep = max(1, keep or 3)
    session.history = session.history[-keep:]
    session.run_history = session.run_history[-keep:]
    if session.history:
        session.last_task_result = session.history[-1]
    if session.run_history:
        session.last_task_run = session.run_history[-1]
    return f"History compacted to the last {keep} entries."


def clear_session_state(session, container) -> str:
    for runtime in session.runtimes.values():
        if runtime.manager.is_running:
            try:
                import asyncio
                asyncio.create_task(runtime.manager.stop())
            except Exception:
                pass
        runtime.parser.clear_full_buffer()
    session.last_task_result = type(session.last_task_result)(provider=session.current_provider)
    session.last_task_run = None
    session.history.clear()
    session.run_history.clear()
    session.last_plan = None
    container.clear_session_storage(session)
    return "Session cleared."


def render_usage_lines(session, provider_paths: dict[str, str]) -> list[str]:
    lines = ["Usage"]
    for provider_name in provider_paths:
        runtime = session.runtimes.get(provider_name)
        stats = session.provider_stats.get(provider_name)
        if runtime:
            last_in, last_out, total_in, total_out = runtime.parser.get_token_usage()
        else:
            last_in = last_out = total_in = total_out = 0
        lines.extend(
            [
                "",
                provider_name,
                f"  model: {session.provider_models.get(provider_name, '').strip() or provider_default_model(provider_name) or 'default'}",
                f"  tasks: {stats.total_tasks if stats else 0}",
                f"  success: {stats.successful_tasks if stats else 0}",
                f"  failed: {stats.failed_tasks if stats else 0}",
                f"  last tokens: {last_in} in / {last_out} out",
                f"  total tokens: {total_in} in / {total_out} out",
            ]
        )
    return lines


def render_todos_lines(session) -> list[str]:
    todos = extract_todos(session.last_task_result.answer_text)
    if not todos:
        return ["No TODO items found in the last answer."]
    return ["TODOs", "", *[f"  {item}" for item in todos[:20]]]


def build_commit_message(session, explicit: str = "") -> str:
    derived = explicit.strip() or session.last_task_result.prompt.strip() or "AI: update project"
    compact = " ".join(derived.split())[:120]
    return compact or "AI: update project"


def get_thinking_mode(session) -> str:
    mode = session.ui_preferences.get("thinking_mode", "").strip().lower()
    return mode if mode in {"off", "compact", "full"} else "compact"


def set_thinking_mode(session, mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized not in {"off", "compact", "full"}:
        return "Thinking mode must be one of: off, compact, full."
    session.ui_preferences["thinking_mode"] = normalized
    return f"Thinking mode set to {normalized}."


def run_git_commit(cwd: str, message: str) -> tuple[bool, str]:
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if inside.returncode != 0:
            return False, "Current directory is not a git repository."

        status = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if status.returncode != 0:
            return False, status.stderr.strip() or "git status failed."
        if not status.stdout.strip():
            return False, "No changes to commit."

        add = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        if add.returncode != 0:
            return False, add.stderr.strip() or "git add failed."

        commit = subprocess.run(
            ["git", "commit", "-m", message],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=15,
        )
        if commit.returncode != 0:
            return False, commit.stderr.strip() or commit.stdout.strip() or "git commit failed."

        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        short = rev.stdout.strip() if rev.returncode == 0 else "unknown"
        return True, f"{short}  {message}"
    except Exception as exc:
        return False, str(exc)


async def run_review_pass(container, session, review_focus: str = "") -> tuple[bool, str, str]:
    last_result = session.last_task_result
    if not last_result.answer_text.strip() and not last_result.touched_files:
        return False, "Nothing to review yet.", ""

    source_provider = last_result.provider or session.current_provider
    review_provider = next(
        (
            candidate
            for candidate in ("openrouter", "claude", "codex", "qwen")
            if candidate != source_provider
            and candidate in container.provider_paths
            and container.provider_is_ready(candidate)[0]
        ),
        session.current_provider,
    )
    runtime = await container.ensure_runtime_started(session, review_provider)
    review_prompt = build_review_request(
        task_prompt=last_result.prompt,
        answer_text=last_result.answer_text,
        touched_files=last_result.touched_files,
        review_focus=review_focus,
    )
    previous_result = session.last_task_result
    previous_active = session.active_provider
    try:
        session.active_provider = review_provider
        result = await container.execution_service.execute_provider_task(
            session=session,
            runtime=runtime,
            provider_name=review_provider,
            prompt=review_prompt,
        )
    finally:
        session.last_task_result = previous_result
        session.active_provider = previous_active

    if result.exit_code != 0:
        return False, f"Review failed via {review_provider}", result.error_text or ""

    if session.last_task_run:
        session.last_task_run.review_provider = review_provider
        session.last_task_run.review_prompt = review_prompt
        session.last_task_run.review_answer = result.answer_text
        session.last_task_run.artifact_file = container.session_store.write_run_artifact(session, session.last_task_run)
    container.save_session(session)
    return True, review_provider, result.answer_text
