from dataclasses import dataclass, field


@dataclass
class MetricsCollector:
    total_tasks: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    orchestrated_runs: int = 0
    orchestrated_successful_runs: int = 0
    orchestrated_failed_runs: int = 0
    total_duration_ms: int = 0
    provider_task_counts: dict[str, int] = field(default_factory=dict)
    provider_failure_counts: dict[str, int] = field(default_factory=dict)
    provider_duration_ms: dict[str, int] = field(default_factory=dict)

    def record_task(self, provider: str, exit_code: int, duration_ms: int):
        self.total_tasks += 1
        if exit_code == 0:
            self.successful_tasks += 1
        else:
            self.failed_tasks += 1
            self.provider_failure_counts[provider] = self.provider_failure_counts.get(provider, 0) + 1

        self.total_duration_ms += max(0, duration_ms)
        self.provider_task_counts[provider] = self.provider_task_counts.get(provider, 0) + 1
        self.provider_duration_ms[provider] = self.provider_duration_ms.get(provider, 0) + max(0, duration_ms)

    def record_orchestrated_run(self, status: str):
        self.orchestrated_runs += 1
        if status == "success":
            self.orchestrated_successful_runs += 1
        else:
            self.orchestrated_failed_runs += 1

    def render_prometheus(self, health_lines: list[str] | None = None) -> str:
        lines = [
            "# TYPE forge_tasks_total counter",
            f"forge_tasks_total {self.total_tasks}",
            "# TYPE forge_tasks_successful_total counter",
            f"forge_tasks_successful_total {self.successful_tasks}",
            "# TYPE forge_tasks_failed_total counter",
            f"forge_tasks_failed_total {self.failed_tasks}",
            "# TYPE forge_task_duration_ms_total counter",
            f"forge_task_duration_ms_total {self.total_duration_ms}",
            "# TYPE forge_orchestrated_runs_total counter",
            f"forge_orchestrated_runs_total {self.orchestrated_runs}",
            "# TYPE forge_orchestrated_runs_successful_total counter",
            f"forge_orchestrated_runs_successful_total {self.orchestrated_successful_runs}",
            "# TYPE forge_orchestrated_runs_failed_total counter",
            f"forge_orchestrated_runs_failed_total {self.orchestrated_failed_runs}",
        ]

        for provider, count in sorted(self.provider_task_counts.items()):
            lines.append(f'forge_provider_tasks_total{{provider="{provider}"}} {count}')
        for provider, count in sorted(self.provider_failure_counts.items()):
            lines.append(f'forge_provider_failures_total{{provider="{provider}"}} {count}')
        for provider, duration_ms in sorted(self.provider_duration_ms.items()):
            lines.append(f'forge_provider_duration_ms_total{{provider="{provider}"}} {duration_ms}')

        if health_lines:
            lines.extend(["", "# health"])
            lines.extend(f"# {line}" for line in health_lines)

        return "\n".join(lines) + "\n"
