#pragma once

#include <Arduino.h>

struct AA4Font;
struct Glow9SliceMask;

namespace ui_theme {

enum class Id : uint8_t {
  CodexDark = 0,
  Count,
};

struct Colors {
  uint16_t background;
  uint16_t header;
  uint16_t primaryText;
  uint16_t card;
  uint16_t track;
  uint16_t accent;
  uint16_t accentGlow;
  uint16_t muted;
  uint16_t secondaryText;
  uint16_t cardLabel;
  uint16_t cardHint;
  uint16_t status;
  uint16_t warning;
  uint16_t chargingText;
  uint16_t chargingFill;
  uint16_t lowBatteryText;
  uint16_t lowBatteryFill;
  uint16_t error;
  uint16_t limitStatus;
  uint16_t limitGlow;
  uint16_t exhaustedCard;
  uint16_t exhaustedTrack;
  uint16_t exhaustedLabel;
  uint16_t exhaustedHint;
  uint16_t activity;
};

struct Typography {
  const AA4Font* body20;
  const AA4Font* body24;
  const AA4Font* body26;
  const AA4Font* body28;
  const AA4Font* body32;
  const AA4Font* display40;
  const AA4Font* strong20;
  const AA4Font* strong24;
  const AA4Font* strong32;
  const AA4Font* total48;
  const AA4Font* total64;
};

struct Assets {
  const Glow9SliceMask* statusGlow;
  const Glow9SliceMask* progressGlow;
};

struct Metrics {
  int16_t pageMargin;
  int16_t statusY;
  int16_t statusHeight;
  float statusRadius;
  int16_t progressX;
  int16_t progressWidth;
  int16_t progressHeight;
  float progressRadius;
  int16_t cardX;
  int16_t cardWidth;
  float cardRadius;
};

struct Motion {
  uint16_t animationFrameMs;
  uint16_t thinkingCycleMs;
  uint16_t updateDotsCycleMs;
  uint16_t chargingCycleMs;
  uint16_t tokenRollMs;
  float thinkingMinOpacity;
  float thinkingMaxOpacity;
  uint8_t thinkingGlowMinAlpha;
  uint8_t thinkingGlowMaxAlpha;
};

struct Theme {
  Id id;
  const char* key;
  const char* displayName;
  Colors colors;
  Typography type;
  Assets assets;
  Metrics metrics;
  Motion motion;
};

const Theme& active();
Id activeId();
bool select(Id id);

}  // namespace ui_theme
