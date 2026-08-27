"""Running the project's own checks, and deciding which ones may be run.

The cheapest and most trustworthy critic for code is not a model. It is the test
suite: it costs nothing per run, it cannot be talked round, and it cannot invent
a pass. An agent that can run the project's checks can tell whether what it just
wrote actually works, instead of asking another model to guess.

The commands are not written by a model. Forge proposes them from the project's
own files, a person approves the set once, and from then on the check tool may
run those and nothing else. That is a much smaller thing to trust than an
approval model over arbitrary shell commands, and it is enough for this:
verification repeats hundreds of times inside a task, so asking each time would
defeat the purpose, while a command a model composed on the fly would put the
whole question back.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 600
MAX_CHECK_OUTPUT_CHARS = 4000


@dataclass(frozen=True)
class Check:
    """One command the project uses to tell whether it is healthy."""

    label: str
    command: str
    source: str

    def to_dict(self) -> dict:
        return {"label": self.label, "command": self.command, "source": self.source}

    @staticmethod
    def from_dict(data: dict) -> "Check":
        return Check(
            label=str(data.get("label", "")),
            command=str(data.get("command", "")),
            source=str(data.get("source", "")),
        )


@dataclass(frozen=True)
class CheckResult:
    check: Check
    passed: bool
    output: str


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.debug("Could not read %s: %s", path, exc)
        return ""


def _python_for(root: Path) -> str:
    """The interpreter a Python check should use.

    A bare ``python`` in the shell is whatever is on PATH, which for a project
    with a virtual environment is the wrong one — the checks then fail on
    missing dependencies rather than on anything about the code, which is a
    verdict that tells the agent nothing true. Prefer the project's own
    interpreter when there is one.

    Forward slashes throughout: the shell running these is bash on every
    platform, and it accepts them on Windows too.
    """
    for directory in (".venv", "venv"):
        for relative in ("Scripts/python.exe", "bin/python"):
            if (root / directory / relative.replace("/", "\\")).is_file() or (root / directory / relative).is_file():
                return f"{directory}/{relative}"
    return "python"


def detect_checks(workspace: Path) -> list[Check]:
    """Propose the commands this project uses to check itself.

    Deliberately conservative: only patterns that are unambiguous. A wrong guess
    here is worse than a missing one, because the whole point is that the person
    approving the list can see at a glance that it is right.
    """
    root = Path(workspace)
    found: list[Check] = []
    python = _python_for(root)

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = _read(pyproject)
        if "pytest" in text:
            found.append(Check("tests", f"{python} -m pytest -q", "pyproject.toml"))
        if "[tool.ruff" in text:
            found.append(Check("lint", f"{python} -m ruff check .", "pyproject.toml"))
        if "[tool.mypy" in text:
            found.append(Check("types", f"{python} -m mypy .", "pyproject.toml"))

    # unittest projects have no marker in pyproject; the layout is the signal.
    if not any(item.label == "tests" for item in found):
        tests_dir = root / "tests"
        if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
            found.append(
                Check("tests", f"{python} -m unittest discover -s tests -q", "tests/")
            )

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(_read(package_json)).get("scripts", {})
        except Exception as exc:
            log.debug("package.json is not readable as JSON: %s", exc)
            scripts = {}
        for name, label in (("test", "tests"), ("lint", "lint"), ("typecheck", "types"), ("build", "build")):
            if isinstance(scripts, dict) and name in scripts:
                found.append(Check(label, f"npm run {name}", "package.json"))

    if (root / "Cargo.toml").is_file():
        found.append(Check("tests", "cargo test", "Cargo.toml"))

    if (root / "go.mod").is_file():
        found.append(Check("tests", "go test ./...", "go.mod"))

    makefile = root / "Makefile"
    if makefile.is_file():
        targets = set(re.findall(r"^([A-Za-z0-9_-]+):", _read(makefile), flags=re.M))
        for target in ("test", "check", "lint"):
            if target in targets:
                label = "tests" if target == "test" else target
                found.append(Check(label, f"make {target}", "Makefile"))

    # Keep the first proposal per label so the list stays short and obvious.
    seen: set[str] = set()
    unique: list[Check] = []
    for item in found:
        if item.label in seen:
            continue
        seen.add(item.label)
        unique.append(item)
    return unique


class CheckStore:
    """Which commands a person has approved, per workspace.

    Kept in Forge's own state rather than inside the project: approving a set of
    commands is a statement about this machine and this user, not a property of
    the repository, and writing a file into someone's project uninvited is rude.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log.warning("Could not read approved checks from %s: %s", self.path, exc)
            return {}

    @staticmethod
    def _key(workspace: Path) -> str:
        return str(Path(workspace).resolve())

    def approved_for(self, workspace: Path) -> list[Check] | None:
        """Returns None when this workspace has never been asked about."""
        entry = self._load().get(self._key(workspace))
        if entry is None:
            return None
        return [Check.from_dict(item) for item in entry if isinstance(item, dict)]

    def approve(self, workspace: Path, checks: list[Check]) -> None:
        data = self._load()
        data[self._key(workspace)] = [item.to_dict() for item in checks]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save approved checks to %s: %s", self.path, exc)


def format_approval_request(checks: list[Check]) -> str:
    lines = [
        "Forge would like to run this project's own checks to verify its work.",
        "These commands come from the project's files and will not change:",
        "",
    ]
    for item in checks:
        lines.append(f"  {item.label:<6} {item.command}    (from {item.source})")
    lines += [
        "",
        "Approve for this project? Answering no means the agent verifies nothing.",
    ]
    return "\n".join(lines)


def summarise(results: list[CheckResult]) -> str:
    """What goes back to the model.

    Failing output first and in full, passing checks reduced to a line. A model
    deciding what to do next needs the failure, not the reassurance.
    """
    if not results:
        return "No checks are configured for this project."

    passed = [item for item in results if item.passed]
    failed = [item for item in results if not item.passed]

    lines: list[str] = []
    if not failed:
        names = ", ".join(item.check.label for item in passed)
        return f"PASS — all checks passed ({names})."

    lines.append(f"FAIL — {len(failed)} of {len(results)} checks failed.")
    for item in failed:
        lines += ["", f"--- {item.check.label}: {item.check.command}", item.output.strip()]
    if passed:
        lines += ["", "Passed: " + ", ".join(item.check.label for item in passed)]
    return "\n".join(lines)


class VerificationService:
    """Approves a command set once, then runs it on demand."""

    def __init__(self, store: CheckStore):
        self._store = store

    def describe(self, workspace: Path) -> list[str]:
        """Lines describing what would run here, for the /helpers command."""
        approved = self._store.approved_for(workspace)
        if approved is None:
            proposed = detect_checks(workspace)
            if not proposed:
                return ["  not approved yet, and nothing was detected to propose"]
            lines = ["  not approved yet. Would propose:"]
            lines += [f"    {item.label:<6} {item.command}" for item in proposed]
            return lines
        if not approved:
            return ["  declined for this workspace, so nothing is ever run"]
        return [f"  {item.label:<6} {item.command}" for item in approved]

    async def verify(
        self,
        workspace: Path,
        run_command: Callable[[str, int], Awaitable[str]],
        ask: "Callable[[str, str], Awaitable[str | None]] | None" = None,
        notify: Callable[[str], None] | None = None,
    ) -> str:
        approved = self._store.approved_for(workspace)

        if approved is None:
            candidates = detect_checks(workspace)
            if not candidates:
                self._store.approve(workspace, [])
                return (
                    "No checks could be detected for this project — no test suite, "
                    "lint or build command was recognised. Verify the work another way."
                )
            if ask is None:
                return (
                    "Verification needs the check commands to be approved once, and there "
                    "is no way to ask right now. Run the project's checks with bash instead."
                )
            answer = await ask("approval", format_approval_request(candidates))
            if not _is_yes(answer):
                self._store.approve(workspace, [])
                return "The check commands were not approved, so nothing was run."
            self._store.approve(workspace, candidates)
            approved = candidates

        if not approved:
            return (
                "No checks are approved for this project. Nothing was run."
            )

        results: list[CheckResult] = []
        for item in approved:
            if notify:
                notify(f"✅ {item.label}: {item.command}")
            raw = await run_command(_with_exit_marker(item.command), CHECK_TIMEOUT_SECONDS)
            passed, output = _split_exit_marker(raw)
            if len(output) > MAX_CHECK_OUTPUT_CHARS:
                output = output[:MAX_CHECK_OUTPUT_CHARS] + "\n... (output truncated)"
            results.append(CheckResult(check=item, passed=passed, output=output))

        return summarise(results)


def _is_yes(answer: "str | None") -> bool:
    return (answer or "").strip().lower() in {"y", "yes", "да", "ok", "approve", "true", "1"}


EXIT_MARKER_PREFIX = "__FORGE_EXIT_"


def _with_exit_marker(command: str) -> str:
    """Make the shell report the exit status alongside the output.

    The whole value of a deterministic check is that its verdict is a fact, so
    it must not be inferred from what the output looks like. PersistentShell
    hands back text rather than a status, so the status is asked for explicitly
    and printed on its own line.
    """
    return f"{command}\necho \"{EXIT_MARKER_PREFIX}$?__\""


def _split_exit_marker(raw: str) -> tuple[bool, str]:
    """Pull the status line back out, and return (passed, output without it)."""
    kept: list[str] = []
    status: int | None = None
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(EXIT_MARKER_PREFIX) and stripped.endswith("__"):
            digits = stripped[len(EXIT_MARKER_PREFIX):-2]
            if digits.isdigit():
                status = int(digits)
                continue
        kept.append(line)

    output = "\n".join(kept).strip()
    if status is not None:
        return status == 0, output

    # No marker came back: the command timed out, the shell died, or we are on
    # the cmd.exe fallback where the syntax does not apply. Treat that as a
    # failure rather than guessing - a false pass tells the agent something
    # untrue, which is the one outcome that makes the check worse than nothing.
    return False, output or "[no exit status returned by the shell]"
