import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass
from typing import Callable
from urllib import error, request
from urllib.parse import urlencode

from core.openrouter_catalog import ModelResolveResult, _compact_text, _normalize_text
from core.providers import ModelDefinition, list_provider_models


@dataclass(frozen=True)
class LocalModelRecord:
    name: str
    label: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    installed: bool = False
    size: int = 0
    source: str = "local"
    supports_tools: bool | None = None

    def to_model_definition(self) -> ModelDefinition:
        notes: list[str] = []
        notes.append("installed" if self.installed else "not installed")
        if self.supports_tools is True:
            notes.append("tools")
        elif self.supports_tools is False:
            notes.append("chat-only")
        else:
            notes.append("tools unknown")
        if self.source == "huggingface":
            notes.append("Hugging Face")
        if self.size:
            notes.append(_format_size(self.size))
        description = self.description.strip()
        note_text = " · ".join(notes)
        if note_text:
            description = f"{description} ({note_text})" if description else note_text
        return ModelDefinition(
            name=self.name,
            label=self.label,
            capabilities=("tool_use",) if self.supports_tools is True else (),
            description=description,
            aliases=self.aliases,
        )


@dataclass(frozen=True)
class LocalModelPullResult:
    ok: bool
    model_name: str
    message: str


@dataclass(frozen=True)
class LocalModelPullProgress:
    model_name: str
    status: str
    percent: int | None = None
    installed: bool = False


PullProgressCallback = Callable[[LocalModelPullProgress], None]


class LocalModelCatalog:
    def __init__(
        self,
        base_url_getter: Callable[[], str],
        timeout: int = 8,
        ttl_seconds: int = 30,
    ):
        self.base_url_getter = base_url_getter
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self._cache: tuple[float, list[LocalModelRecord]] | None = None
        self._hf_cache: dict[tuple[str, int], tuple[float, list[LocalModelRecord]]] = {}
        self._hf_ttl_seconds = max(30, min(600, ttl_seconds * 10))

    def list_models(self, refresh: bool = False, tools_only: bool = False) -> list[ModelDefinition]:
        records = self._load_records(refresh=refresh)
        if tools_only:
            records = [record for record in records if record.supports_tools is True]
        return [record.to_model_definition() for record in records]

    def search_models(
        self,
        query: str,
        refresh: bool = False,
        limit: int = 20,
        tools_only: bool = False,
    ) -> list[ModelDefinition]:
        records = self._load_records(refresh=refresh)
        records = self._with_huggingface_records(records, query, limit=limit)
        if tools_only:
            records = [record for record in records if record.supports_tools is True]
        lowered = _normalize_text(query)
        compacted = _compact_text(query)
        scored: list[tuple[int, LocalModelRecord]] = []
        for record in records:
            score = self._match_score(record, lowered, compacted)
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(key=lambda item: (not item[1].installed, -item[0], item[1].label.casefold(), item[1].name.casefold()))
        return [record.to_model_definition() for _, record in scored[:limit]]

    def resolve_model(self, query: str, refresh: bool = False, require_tools: bool = False) -> ModelResolveResult:
        cleaned = (query or "").strip()
        if not cleaned:
            return ModelResolveResult(status="empty", message="No model query provided.")

        records = self._load_records(refresh=refresh)
        records = self._with_huggingface_records(records, cleaned, limit=12)
        exact_matches = self._find_exact_matches(records, cleaned)
        if require_tools and exact_matches:
            if not any(item.supports_tools is True for item in exact_matches):
                return ModelResolveResult(
                    status="missing",
                    message=f"'{cleaned}' is not marked as tool-capable. Use /model local tools to pick an agentic local model.",
                )
            exact_matches = [item for item in exact_matches if item.supports_tools is True]
        if len(exact_matches) == 1:
            return ModelResolveResult(status="exact", model_name=exact_matches[0].name)
        if len(exact_matches) > 1:
            return ModelResolveResult(
                status="ambiguous",
                matches=tuple(item.to_model_definition() for item in exact_matches[:8]),
                message=f"'{cleaned}' matches several local models.",
            )

        fuzzy_matches = self.search_models(cleaned, refresh=refresh, limit=8, tools_only=require_tools)
        if len(fuzzy_matches) == 1:
            return ModelResolveResult(status="exact", model_name=fuzzy_matches[0].name)
        if fuzzy_matches:
            return ModelResolveResult(
                status="ambiguous",
                matches=tuple(fuzzy_matches),
                message=f"'{cleaned}' is ambiguous. Pick one of the closest local models.",
            )

        if require_tools:
            return ModelResolveResult(
                status="missing",
                message=f"'{cleaned}' is not marked as tool-capable. Use /model local tools to pick an agentic local model.",
            )

        return ModelResolveResult(
            status="raw",
            model_name=cleaned,
            message="Using the exact local model id you entered.",
        )

    def is_model_installed(self, model_name: str, refresh: bool = False) -> bool:
        cleaned = (model_name or "").strip().casefold()
        if not cleaned:
            return False
        return any(
            record.installed and record.name.casefold() == cleaned
            for record in self._load_records(refresh=refresh)
        )

    async def pull_model(
        self,
        model_name: str,
        progress_callback: PullProgressCallback | None = None,
    ) -> LocalModelPullResult:
        cleaned = (model_name or "").strip()
        if not cleaned:
            return LocalModelPullResult(False, "", "No local model name provided.")

        self._emit_pull_progress(progress_callback, cleaned, "Preparing download", 0)
        ollama = shutil.which("ollama")
        if ollama:
            proc = await asyncio.create_subprocess_exec(
                ollama,
                "pull",
                cleaned,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output = await self._collect_ollama_pull_output(proc, cleaned, progress_callback)
            if proc.returncode == 0:
                self._cache = None
                self._emit_pull_progress(progress_callback, cleaned, "Installed", 100, installed=True)
                return LocalModelPullResult(True, cleaned, output or f"Pulled {cleaned}.")
            return LocalModelPullResult(
                False,
                cleaned,
                _pull_error_message(cleaned, output or f"ollama pull exited with {proc.returncode}."),
            )

        return await asyncio.get_running_loop().run_in_executor(
            None,
            self._pull_model_http,
            cleaned,
            progress_callback,
        )

    def _load_records(self, refresh: bool = False) -> list[LocalModelRecord]:
        if not refresh and self._cache and time.time() - self._cache[0] <= self.ttl_seconds:
            return self._cache[1]
        installed = self._fetch_installed_records()
        records = self._merge_records(installed)
        self._cache = (time.time(), records)
        return records

    def _fetch_installed_records(self) -> list[LocalModelRecord]:
        records = self._fetch_openai_models()
        tag_records = self._fetch_ollama_tags()
        merged: dict[str, LocalModelRecord] = {item.name: item for item in records}
        for record in tag_records:
            merged[record.name] = record
        return list(merged.values())

    def _fetch_openai_models(self) -> list[LocalModelRecord]:
        payload = self._get_json(f"{self._openai_base_url()}/models")
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        records: list[LocalModelRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                records.append(
                    LocalModelRecord(
                        name=model_id,
                        label=_label_from_model_id(model_id),
                        description="Served by the local OpenAI-compatible endpoint.",
                        aliases=_aliases_for_model_id(model_id),
                        installed=True,
                        supports_tools=_infer_local_model_tool_support(model_id),
                    )
                )
        return records

    def _fetch_ollama_tags(self) -> list[LocalModelRecord]:
        payload = self._get_json(f"{self._ollama_root_url()}/api/tags")
        items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        records: list[LocalModelRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("name") or item.get("model") or "").strip()
            if not model_id:
                continue
            records.append(
                LocalModelRecord(
                    name=model_id,
                    label=_label_from_model_id(model_id),
                    description="Installed in Ollama.",
                    aliases=_aliases_for_model_id(model_id),
                    installed=True,
                    size=int(item.get("size") or 0),
                    supports_tools=_infer_local_model_tool_support(model_id),
                )
            )
        return records

    async def _collect_ollama_pull_output(
        self,
        proc: asyncio.subprocess.Process,
        model_name: str,
        progress_callback: PullProgressCallback | None,
    ) -> str:
        output_parts: list[str] = []
        buffer = ""
        stdout = proc.stdout
        if stdout is None:
            await proc.wait()
            return ""
        while True:
            chunk = await stdout.read(256)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            output_parts.append(text)
            buffer += text
            pieces = re.split(r"[\r\n]+", buffer)
            buffer = pieces.pop() if pieces else ""
            for piece in pieces:
                self._handle_pull_output_line(piece, model_name, progress_callback)
        if buffer.strip():
            self._handle_pull_output_line(buffer, model_name, progress_callback)
        await proc.wait()
        return _clean_pull_output("".join(output_parts))

    def _pull_model_http(
        self,
        model_name: str,
        progress_callback: PullProgressCallback | None = None,
    ) -> LocalModelPullResult:
        payload = json.dumps({"name": model_name, "stream": True}).encode("utf-8")
        req = request.Request(
            f"{self._ollama_root_url()}/api/pull",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        last_status = ""
        completed_ok = False
        try:
            with request.urlopen(req, timeout=max(self.timeout, 60)) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    error_message = str(data.get("error") or "").strip()
                    if error_message:
                        return LocalModelPullResult(False, model_name, _pull_error_message(model_name, error_message))
                    status = str(data.get("status") or "").strip()
                    if status:
                        last_status = status
                    completed = data.get("completed")
                    total = data.get("total")
                    percent = _percent_from_completed_total(completed, total)
                    installed = status.casefold() in {"success", "installed"}
                    completed_ok = completed_ok or installed
                    self._emit_pull_progress(progress_callback, model_name, status or "Downloading", percent, installed=installed)
        except (error.URLError, error.HTTPError, TimeoutError, OSError) as exc:
            return LocalModelPullResult(False, model_name, f"Failed to pull local model: {exc}")
        if not completed_ok:
            return LocalModelPullResult(
                False,
                model_name,
                _pull_error_message(model_name, last_status or "Ollama pull ended before success."),
            )
        self._cache = None
        self._emit_pull_progress(progress_callback, model_name, "Installed", 100, installed=True)
        return LocalModelPullResult(True, model_name, last_status or f"Pulled {model_name}.")

    def _handle_pull_output_line(
        self,
        line: str,
        model_name: str,
        progress_callback: PullProgressCallback | None,
    ) -> None:
        cleaned = _clean_pull_output(line)
        if not cleaned:
            return
        percent = _percent_from_text(cleaned)
        installed = "success" in cleaned.casefold() or "installed" in cleaned.casefold()
        self._emit_pull_progress(progress_callback, model_name, _short_pull_status(cleaned), percent, installed=installed)

    @staticmethod
    def _emit_pull_progress(
        progress_callback: PullProgressCallback | None,
        model_name: str,
        status: str,
        percent: int | None = None,
        installed: bool = False,
    ) -> None:
        if not progress_callback:
            return
        progress_callback(
            LocalModelPullProgress(
                model_name=model_name,
                status=status,
                percent=percent,
                installed=installed,
            )
        )

    def _get_json(self, url: str) -> dict:
        try:
            with request.urlopen(request.Request(url, method="GET"), timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            return {}

    def _get_json_list(self, url: str, timeout: int | None = None) -> list:
        try:
            with request.urlopen(request.Request(url, method="GET"), timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            return []
        return payload if isinstance(payload, list) else []

    def _openai_base_url(self) -> str:
        return self.base_url_getter().strip().rstrip("/") or "http://127.0.0.1:11434/v1"

    def _ollama_root_url(self) -> str:
        base = self._openai_base_url()
        return base[:-3] if base.endswith("/v1") else base

    def _merge_records(self, installed_records: list[LocalModelRecord]) -> list[LocalModelRecord]:
        merged: dict[str, LocalModelRecord] = {item.name: item for item in installed_records}
        installed_names = {name.casefold() for name in merged}
        for model in list_provider_models("local"):
            if model.name.casefold() in installed_names:
                continue
            merged[model.name] = LocalModelRecord(
                name=model.name,
                label=model.label,
                description=model.description,
                aliases=model.aliases,
                installed=False,
                supports_tools=("tool_use" in model.capabilities or "agentic" in model.capabilities),
            )
        return sorted(merged.values(), key=lambda item: (not item.installed, item.label.casefold(), item.name.casefold()))

    def _with_huggingface_records(
        self,
        records: list[LocalModelRecord],
        query: str,
        limit: int,
    ) -> list[LocalModelRecord]:
        cleaned = (query or "").strip()
        if len(cleaned) < 2:
            return records
        merged: dict[str, LocalModelRecord] = {record.name.casefold(): record for record in records}
        for record in self._fetch_huggingface_records(cleaned, limit=limit):
            merged.setdefault(record.name.casefold(), record)
        return list(merged.values())

    def _fetch_huggingface_records(self, query: str, limit: int = 12) -> list[LocalModelRecord]:
        cache_key = (query.strip().casefold(), max(1, min(limit, 20)))
        cached = self._hf_cache.get(cache_key)
        if cached and time.time() - cached[0] <= self._hf_ttl_seconds:
            return cached[1]

        params = urlencode(
            {
                "search": query,
                "filter": "gguf",
                "sort": "downloads",
                "direction": "-1",
                "limit": str(max(1, min(limit, 20))),
            }
        )
        payload = self._get_json_list(f"https://huggingface.co/api/models?{params}", timeout=min(self.timeout, 8))
        records: list[LocalModelRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            repo_id = str(item.get("modelId") or item.get("id") or "").strip()
            if not repo_id or "/" not in repo_id:
                continue
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            tag_text = ", ".join(str(tag) for tag in tags[:4] if tag)
            downloads = item.get("downloads")
            details = ["Hugging Face model available to install via Ollama as hf.co/<repo>."]
            if isinstance(downloads, int):
                details.append(f"{downloads:,} downloads.")
            if tag_text:
                details.append(f"Tags: {tag_text}.")
            name = f"hf.co/{repo_id}"
            records.append(
                LocalModelRecord(
                    name=name,
                    label=repo_id,
                    description=" ".join(details),
                    aliases=_aliases_for_model_id(repo_id) + (repo_id,),
                    installed=False,
                    source="huggingface",
                    supports_tools=_infer_local_model_tool_support(repo_id),
                )
            )
        self._hf_cache[cache_key] = (time.time(), records)
        return records

    @staticmethod
    def _find_exact_matches(records: list[LocalModelRecord], query: str) -> list[LocalModelRecord]:
        lowered = query.casefold()
        return [
            record for record in records
            if lowered in {record.name.casefold(), record.label.casefold(), *(alias.casefold() for alias in record.aliases)}
        ]

    @staticmethod
    def _match_score(record: LocalModelRecord, lowered: str, compacted: str) -> int:
        if not lowered:
            return 1
        haystack = _normalize_text(" ".join((record.name, record.label, record.description, *record.aliases)))
        compact_haystack = _compact_text(" ".join((record.name, record.label, *record.aliases)))
        if lowered == _normalize_text(record.name) or lowered == _normalize_text(record.label):
            return 100
        if lowered in haystack:
            return 80
        if compacted and compacted in compact_haystack:
            return 65
        tokens = [token for token in lowered.split() if token]
        if tokens and all(token in haystack for token in tokens):
            return 45
        return 0


def _label_from_model_id(model_id: str) -> str:
    stem = model_id.split("/")[-1].split(":")[0]
    return " ".join(part.capitalize() for part in stem.replace("-", " ").replace("_", " ").split()) or model_id


def _aliases_for_model_id(model_id: str) -> tuple[str, ...]:
    base = model_id.split("/")[-1]
    stem = base.split(":")[0]
    aliases = {base, stem, stem.replace("-", " "), stem.replace("_", " ")}
    return tuple(item for item in aliases if item and item != model_id)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PERCENT_RE = re.compile(r"(\d{1,3})%")


def _clean_pull_output(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text)
    cleaned = cleaned.replace("\u2800", " ")
    cleaned = re.sub(r"[▏▎▍▌▋▊▉█━─]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _percent_from_text(text: str) -> int | None:
    match = _PERCENT_RE.search(text)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def _percent_from_completed_total(completed, total) -> int | None:
    try:
        completed_value = float(completed)
        total_value = float(total)
    except (TypeError, ValueError):
        return None
    if total_value <= 0:
        return None
    return max(0, min(100, int((completed_value / total_value) * 100)))


def _short_pull_status(text: str) -> str:
    if not text:
        return "Downloading"
    # Keep the readable left side of Ollama's progress line and let percent
    # live in the dedicated progress field.
    return _PERCENT_RE.sub("", text).strip(" -")


def _pull_error_message(model_name: str, raw: str) -> str:
    text = _clean_pull_output(raw)
    if not text:
        text = "Ollama pull failed."
    lowered = text.casefold()
    hint = ""
    if model_name.startswith(("hf.co/", "huggingface.co/")) or "pulling manifest" in lowered or "manifest" in lowered:
        hint = (
            " For Hugging Face installs, pick a GGUF repository, usually ending in '-GGUF', "
            "or an exact quant tag such as hf.co/user/repo:Q4_K_M. Non-GGUF Hugging Face "
            "models need conversion/import before Ollama can run them."
        )
    return f"Failed to install {model_name}: {text}.{hint}"


def _infer_local_model_tool_support(model_id: str) -> bool | None:
    lowered = model_id.casefold()
    if any(marker in lowered for marker in ("tool", "function-calling", "function_calling", "function-calling")):
        return True
    if any(marker in lowered for marker in ("qwen2.5-coder", "qwen3", "devstral", "hermes", "firefunction")):
        return True
    return None
