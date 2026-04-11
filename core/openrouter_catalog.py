import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib import error, request

from core.providers import ModelDefinition


def _normalize_text(value: str) -> str:
    return " ".join((value or "").strip().casefold().replace("-", " ").replace("_", " ").split())


def _compact_text(value: str) -> str:
    return "".join(ch for ch in (value or "").casefold() if ch.isalnum())


def _format_money(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        amount = float(raw)
    except ValueError:
        return raw
    if amount == 0:
        return "free"
    return f"${amount:g}"


@dataclass(frozen=True)
class OpenRouterModelRecord:
    name: str
    label: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    context_length: int = 0
    prompt_price: str = ""
    completion_price: str = ""
    featured: bool = False

    def to_model_definition(self) -> ModelDefinition:
        notes: list[str] = []
        if self.context_length:
            notes.append(f"{self.context_length:,} ctx")
        if self.prompt_price or self.completion_price:
            prompt = _format_money(self.prompt_price)
            completion = _format_money(self.completion_price)
            if prompt or completion:
                notes.append(f"in {prompt or '?'} / out {completion or '?'}")
        note_text = " · ".join(item for item in notes if item)
        description = self.description.strip()
        if note_text:
            description = f"{description} ({note_text})" if description else note_text
        return ModelDefinition(
            name=self.name,
            label=self.label,
            description=description,
            aliases=self.aliases,
        )


@dataclass(frozen=True)
class ModelResolveResult:
    status: str
    model_name: str = ""
    matches: tuple[ModelDefinition, ...] = ()
    message: str = ""


_FEATURED_MODELS: tuple[OpenRouterModelRecord, ...] = (
    OpenRouterModelRecord(
        name="qwen/qwen3-coder:free",
        label="Qwen3 Coder Free",
        description="Free coding-oriented model for planning, analysis, and lightweight code tasks.",
        aliases=("qwen3", "qwen coder", "qwen free", "free coder", "coder free"),
        featured=True,
    ),
    OpenRouterModelRecord(
        name="minimax/minimax-m2.5:free",
        label="MiniMax M2.5 Free",
        description="Free general-purpose model suited to synthesis, review, and mixed prompts.",
        aliases=("minimax", "m2.5", "minimax free"),
        featured=True,
    ),
    OpenRouterModelRecord(
        name="openrouter/free",
        label="OpenRouter Free Router",
        description="Best-effort free fallback route. Useful for experiments and quick tests.",
        aliases=("free", "router free", "free router"),
        featured=True,
    ),
)


class OpenRouterModelCatalog:
    def __init__(
        self,
        cache_path: Path,
        base_url: str,
        timeout: int = 8,
        ttl_seconds: int = 21600,
        api_key_getter: Callable[[], str] | None = None,
    ):
        self.cache_path = Path(cache_path)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ttl_seconds = ttl_seconds
        self.api_key_getter = api_key_getter or (lambda: "")

    def list_models(self, refresh: bool = False) -> list[ModelDefinition]:
        records = self._load_records(refresh=refresh)
        return [record.to_model_definition() for record in records]

    def resolve_model(self, query: str, refresh: bool = False) -> ModelResolveResult:
        cleaned = (query or "").strip()
        if not cleaned:
            return ModelResolveResult(status="empty", message="No model query provided.")

        records = self._load_records(refresh=refresh)
        if "/" in cleaned:
            for record in records:
                if record.name.casefold() == cleaned.casefold():
                    return ModelResolveResult(status="exact", model_name=record.name)
            return ModelResolveResult(
                status="raw",
                model_name=cleaned,
                message="Using the exact OpenRouter model id you entered.",
            )

        exact_matches = self._find_exact_matches(records, cleaned)
        if len(exact_matches) == 1:
            return ModelResolveResult(status="exact", model_name=exact_matches[0].name)
        if len(exact_matches) > 1:
            return ModelResolveResult(
                status="ambiguous",
                matches=tuple(item.to_model_definition() for item in exact_matches[:8]),
                message=f"'{cleaned}' matches several OpenRouter models.",
            )

        fuzzy_matches = self.search_models(cleaned, refresh=refresh, limit=8)
        if len(fuzzy_matches) == 1:
            return ModelResolveResult(status="exact", model_name=fuzzy_matches[0].name)
        if fuzzy_matches:
            return ModelResolveResult(
                status="ambiguous",
                matches=tuple(fuzzy_matches),
                message=f"'{cleaned}' is ambiguous. Pick one of the closest matches.",
            )
        return ModelResolveResult(
            status="missing",
            message=f"No OpenRouter model matched '{cleaned}'. Try /model openrouter refresh or a broader search term.",
        )

    def search_models(self, query: str, refresh: bool = False, limit: int = 20) -> list[ModelDefinition]:
        records = self._load_records(refresh=refresh)
        lowered = _normalize_text(query)
        compacted = _compact_text(query)
        scored: list[tuple[int, OpenRouterModelRecord]] = []
        for record in records:
            score = self._match_score(record, lowered, compacted)
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].label.casefold(), item[1].name.casefold()))
        return [record.to_model_definition() for _, record in scored[:limit]]

    def _load_records(self, refresh: bool = False) -> list[OpenRouterModelRecord]:
        cached = self._read_cache()
        if not refresh and cached and (time.time() - cached[0] <= self.ttl_seconds):
            return self._merge_records(cached[1])

        fetched = self._fetch_remote_records()
        if fetched:
            self._write_cache(fetched)
            return self._merge_records(fetched)

        if cached:
            return self._merge_records(cached[1])
        return self._merge_records([])

    def _fetch_remote_records(self) -> list[OpenRouterModelRecord]:
        req = request.Request(f"{self.base_url}/models", method="GET")
        api_key = self.api_key_getter().strip()
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
            return []

        models = payload.get("data")
        if not isinstance(models, list):
            return []

        records: list[OpenRouterModelRecord] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            label = str(item.get("name") or item.get("canonical_slug") or model_id).strip()
            description = str(item.get("description") or "").strip()
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            aliases = self._aliases_for_model(
                model_id=model_id,
                label=label,
                canonical_slug=str(item.get("canonical_slug") or "").strip(),
            )
            records.append(
                OpenRouterModelRecord(
                    name=model_id,
                    label=label,
                    description=description,
                    aliases=aliases,
                    context_length=int(item.get("context_length") or 0),
                    prompt_price=str(pricing.get("prompt") or ""),
                    completion_price=str(pricing.get("completion") or ""),
                )
            )
        return records

    def _read_cache(self) -> tuple[float, list[OpenRouterModelRecord]] | None:
        if not self.cache_path.exists():
            return None
        try:
            cache_payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(cache_payload, dict):
            return None
        raw_records = cache_payload.get("models")
        if not isinstance(raw_records, list):
            return None
        records: list[OpenRouterModelRecord] = []
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            try:
                aliases = item.get("aliases")
                record_payload = dict(item)
                if isinstance(aliases, list):
                    record_payload["aliases"] = tuple(str(alias) for alias in aliases)
                elif isinstance(aliases, tuple):
                    record_payload["aliases"] = tuple(str(alias) for alias in aliases)
                else:
                    record_payload["aliases"] = ()
                records.append(OpenRouterModelRecord(**record_payload))
            except TypeError:
                continue
        fetched_at = float(cache_payload.get("fetched_at") or 0)
        return fetched_at, records

    def _write_cache(self, records: list[OpenRouterModelRecord]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "models": [asdict(record) for record in records],
        }
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _merge_records(self, dynamic_records: list[OpenRouterModelRecord]) -> list[OpenRouterModelRecord]:
        merged: dict[str, OpenRouterModelRecord] = {item.name: item for item in dynamic_records}
        for featured in _FEATURED_MODELS:
            existing = merged.get(featured.name)
            if existing is None:
                merged[featured.name] = featured
                continue
            aliases = tuple(dict.fromkeys(tuple(existing.aliases) + tuple(featured.aliases)))
            description = existing.description or featured.description
            merged[featured.name] = OpenRouterModelRecord(
                name=existing.name,
                label=existing.label or featured.label,
                description=description,
                aliases=aliases,
                context_length=existing.context_length,
                prompt_price=existing.prompt_price,
                completion_price=existing.completion_price,
                featured=True,
            )
        ordered = sorted(
            merged.values(),
            key=lambda item: (0 if item.featured else 1, item.label.casefold(), item.name.casefold()),
        )
        return ordered

    def _aliases_for_model(self, model_id: str, label: str, canonical_slug: str = "") -> tuple[str, ...]:
        aliases: list[str] = []
        for raw in (model_id, canonical_slug, label):
            text = (raw or "").strip()
            if not text:
                continue
            aliases.append(text)
            aliases.append(text.replace("/", " "))
            aliases.append(text.split("/")[-1])
            aliases.append(text.split(":")[0])
            aliases.append(text.split("/")[-1].split(":")[0])
        normalized_aliases = [_normalize_text(item) for item in aliases if _normalize_text(item)]
        deduped = list(dict.fromkeys(normalized_aliases))
        return tuple(deduped)

    def _find_exact_matches(self, records: list[OpenRouterModelRecord], query: str) -> list[OpenRouterModelRecord]:
        normalized = _normalize_text(query)
        compacted = _compact_text(query)
        matches: list[OpenRouterModelRecord] = []
        for record in records:
            alias_set = set(record.aliases)
            alias_set.add(_normalize_text(record.name))
            alias_set.add(_normalize_text(record.label))
            if normalized in alias_set or compacted in {_compact_text(item) for item in alias_set}:
                matches.append(record)
        return matches

    def _match_score(self, record: OpenRouterModelRecord, lowered: str, compacted: str) -> int:
        candidates = [record.name, record.label, record.description, *record.aliases]
        best = 0
        for candidate in candidates:
            normalized = _normalize_text(candidate)
            compact_candidate = _compact_text(candidate)
            if not normalized and not compact_candidate:
                continue
            if lowered and normalized == lowered:
                best = max(best, 100)
            elif compacted and compact_candidate == compacted:
                best = max(best, 95)
            elif lowered and normalized.startswith(lowered):
                best = max(best, 80)
            elif compacted and compact_candidate.startswith(compacted):
                best = max(best, 75)
            elif lowered and lowered in normalized:
                best = max(best, 60)
            elif compacted and compacted in compact_candidate:
                best = max(best, 55)
        if record.featured and best:
            best += 5
        return best
