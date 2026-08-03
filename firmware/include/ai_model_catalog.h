#pragma once

#include <Arduino.h>

struct SVGIcon;

namespace ai_model {

String providerLabel(const char* providerId);
String modelLabel(const char* modelId);
String planLabel(const char* planId);
const SVGIcon* providerIcon(const char* providerId);

}  // namespace ai_model
