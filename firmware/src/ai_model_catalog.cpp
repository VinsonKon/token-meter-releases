#include "ai_model_catalog.h"

#include <string.h>

#include "svg_icons.h"

namespace {

struct ModelLabel {
  const char* id;
  const char* label;
};

constexpr ModelLabel kOpenAiModels[] = {
    {"gpt-5.6-sol", "GPT-5.6 Sol"},
    {"gpt-5.6-terra", "GPT-5.6 Terra"},
    {"gpt-5.6-luna", "GPT-5.6 Luna"},
    {"gpt-5.6", "GPT-5.6"},
    {"gpt-5.5", "GPT-5.5"},
    {"gpt-5.4-mini", "GPT-5.4 Mini"},
    {"gpt-5.4", "GPT-5.4"},
    {"gpt-5.3-codex", "GPT-5.3 Codex"},
    {"gpt-5.2-codex", "GPT-5.2 Codex"},
    {"gpt-5.2", "GPT-5.2"},
};

bool startsWithModel(const char* value, const char* model) {
  if (!value || !model) return false;
  const size_t length = strlen(model);
  return strncmp(value, model, length) == 0 &&
         (value[length] == '\0' || value[length] == '-');
}

}  // namespace

namespace ai_model {

String providerLabel(const char* providerId) {
  if (!providerId || providerId[0] == '\0' || strcmp(providerId, "openai") == 0) {
    return "Codex";
  }
  if (strcmp(providerId, "anthropic") == 0 || strcmp(providerId, "claude") == 0) {
    return "Claude Code";
  }
  String label = providerId;
  if (label.length() > 0) label[0] = toupper(label[0]);
  return label;
}

String modelLabel(const char* modelId) {
  if (!modelId || modelId[0] == '\0') return "Auto";
  for (const auto& model : kOpenAiModels) {
    if (startsWithModel(modelId, model.id)) return model.label;
  }
  String label = modelId;
  label.replace('_', ' ');
  return label;
}

String planLabel(const char* planId) {
  if (!planId || planId[0] == '\0') return "";
  // Codex currently reports this internal plan id for the Pro 5x monthly tier.
  if (strcmp(planId, "prolite") == 0) return "Pro 5x";
  String label = planId;
  label.replace('_', ' ');
  label.replace('-', ' ');
  if (label.length() > 0) label[0] = toupper(label[0]);
  return label;
}

const SVGIcon* providerIcon(const char* providerId) {
  // Provider-specific icons can be registered here without changing renderer.
  if (providerId &&
      (strcmp(providerId, "anthropic") == 0 || strcmp(providerId, "claude") == 0)) {
    return nullptr;
  }
  return &ChatGpt32;
}

}  // namespace ai_model
