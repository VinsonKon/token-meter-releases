"""AI coding-tool registry used by source selection and protocol branding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


AUTO_SOURCE = "auto"
CODEX_SOURCE = "codex"
CLAUDE_CODE_SOURCE = "claude-code"


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    id: str
    display_name: str
    protocol_provider: str
    aliases: tuple[str, ...] = ()


SOURCES: tuple[SourceDefinition, ...] = (
    SourceDefinition(CODEX_SOURCE, "Codex", "openai", ("openai",)),
    SourceDefinition(
        CLAUDE_CODE_SOURCE,
        "Claude Code",
        "anthropic",
        ("claude", "claude_code", "anthropic"),
    ),
)


def normalize_source_id(value: object) -> str:
    source_id = str(value or "").strip().lower()
    if not source_id or source_id == AUTO_SOURCE:
        return AUTO_SOURCE
    for source in SOURCES:
        if source_id == source.id or source_id in source.aliases:
            return source.id
    # Migrate the old per-model setting without exposing model-level choices.
    if source_id.startswith(("gpt-", "o1", "o3", "o4")):
        return CODEX_SOURCE
    if source_id.startswith("claude-"):
        return CLAUDE_CODE_SOURCE
    return source_id


def resolve_source(value: object) -> SourceDefinition | None:
    source_id = normalize_source_id(value)
    for source in SOURCES:
        if source.id == source_id:
            return source
    return None


def selector_options() -> Iterable[tuple[str, str]]:
    yield AUTO_SOURCE, "Auto (latest active tool)"
    for source in SOURCES:
        yield source.id, source.display_name


def selector_label(selector: object) -> str:
    source_id = normalize_source_id(selector)
    if source_id == AUTO_SOURCE:
        return "Auto"
    resolved = resolve_source(source_id)
    return resolved.display_name if resolved else source_id


def protocol_provider(selector: object) -> str:
    resolved = resolve_source(selector)
    return resolved.protocol_provider if resolved else ""
