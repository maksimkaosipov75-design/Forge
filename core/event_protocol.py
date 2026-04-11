import json
from typing import Any


FORGE_EVENT_PREFIX = "FORGE_EVENT "


def encode_forge_event(event_type: str, text: str = "", **payload: Any) -> str:
    data: dict[str, Any] = {"type": event_type, "text": text}
    data.update(payload)
    return FORGE_EVENT_PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def decode_forge_event(line: str) -> dict[str, Any] | None:
    if not isinstance(line, str) or not line.startswith(FORGE_EVENT_PREFIX):
        return None
    raw = line[len(FORGE_EVENT_PREFIX):].strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def extract_forge_event(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    embedded = payload.get("forge_event")
    if isinstance(embedded, dict) and embedded.get("type"):
        return embedded
    protocol = str(payload.get("protocol") or payload.get("_forge_protocol") or "").strip().lower()
    if protocol == "forge_event" and payload.get("type"):
        return payload
    return None
