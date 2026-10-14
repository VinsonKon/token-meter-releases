"""Concrete model metadata used internally for labels and cost estimation."""

from __future__ import annotations

from dataclasses import dataclass
import pricing_catalog


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
    # Standard, short-context API-equivalent estimate; USD per 1M tokens.
    # Input / cached input / output. Offline floor verified 2026-10-13:
    # https://developers.openai.com/api/docs/pricing
    # Online data overrides these rates and recognizes models absent here.
    # Daily aggregates do not resolve cache writes, long context or Fast tiers.
    ModelDefinition("gpt-6-astra", "openai", "GPT-6 Astra", (10.00, 1.00, 50.00)),
    ModelDefinition("gpt-6.1-sol", "openai", "GPT-6.1 Sol", (2.00, 0.10, 10.00)),
    ModelDefinition("gpt-6-sol", "openai", "GPT-6 Sol", (2.00, 0.20, 10.00)),
    ModelDefinition("gpt-6-luna", "openai", "GPT-6 Luna", (0.10, 0.01, 0.50)),
    ModelDefinition("gpt-5.6-sol", "openai", "GPT-5.6 Sol", (4.00, 0.40, 20.00)),
    ModelDefinition("gpt-5.6-terra", "openai", "GPT-5.6 Terra", (2.00, 0.20, 12.00)),
    ModelDefinition("gpt-5.6-luna", "openai", "GPT-5.6 Luna", (0.20, 0.02, 1.20)),
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
    online = pricing_catalog.active.lookup(model_id)
    if online:
        canonical_id, rates = online
        existing = next((m for m in MODELS if m.id == canonical_id), None)
        label = existing.display_name if existing else canonical_id
        return ModelDefinition(canonical_id, "openai", label, rates.legacy_tuple())
    names = {name: model for model in MODELS for name in (model.id, *model.aliases)}
    canonical_id = pricing_catalog.matching_model_id(model_id, names)
    if canonical_id:
        return names[canonical_id]
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
