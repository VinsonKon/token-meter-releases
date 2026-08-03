"""Concrete model metadata used internally for labels and cost estimation."""

from __future__ import annotations

from dataclasses import dataclass
@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    id: str
    provider_id: str
    display_name: str
    pricing_usd_per_m: tuple[float, float, float] | None
    aliases: tuple[str, ...] = ()


PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition("openai", "OpenAI"),
)


MODELS: tuple[ModelDefinition, ...] = (
    ModelDefinition("gpt-5.6-sol", "openai", "GPT-5.6 Sol", (5.00, 0.50, 30.00)),
    ModelDefinition("gpt-5.6-terra", "openai", "GPT-5.6 Terra", (2.50, 0.25, 15.00)),
    ModelDefinition("gpt-5.6-luna", "openai", "GPT-5.6 Luna", (1.00, 0.10, 6.00)),
    ModelDefinition("gpt-5.6", "openai", "GPT-5.6", (5.00, 0.50, 30.00)),
    ModelDefinition("gpt-5.5", "openai", "GPT-5.5", (5.00, 0.50, 30.00)),
    ModelDefinition("gpt-5.4-mini", "openai", "GPT-5.4 Mini", (0.75, 0.075, 4.50)),
    ModelDefinition("gpt-5.4", "openai", "GPT-5.4", (2.50, 0.25, 15.00)),
    ModelDefinition("gpt-5.3-codex", "openai", "GPT-5.3 Codex", (1.75, 0.175, 14.00)),
    ModelDefinition("gpt-5.2-codex", "openai", "GPT-5.2 Codex", (1.75, 0.175, 14.00)),
    ModelDefinition("gpt-5.2", "openai", "GPT-5.2", (1.75, 0.175, 14.00)),
)


def normalize_model_id(value: object) -> str:
    return str(value or "").strip().lower()


def resolve_model(value: object) -> ModelDefinition | None:
    model_id = normalize_model_id(value)
    if not model_id:
        return None
    candidates = sorted(MODELS, key=lambda item: len(item.id), reverse=True)
    for model in candidates:
        names = (model.id, *model.aliases)
        for name in names:
            if model_id == name or model_id.startswith(name + "-"):
                return model
    return None


def canonical_model_id(value: object) -> str:
    resolved = resolve_model(value)
    return resolved.id if resolved else normalize_model_id(value)


def provider_id_for_model(value: object) -> str:
    resolved = resolve_model(value)
    return resolved.provider_id if resolved else "openai"


def pricing_for_model(value: object) -> tuple[float, float, float] | None:
    resolved = resolve_model(value)
    return resolved.pricing_usd_per_m if resolved else None
