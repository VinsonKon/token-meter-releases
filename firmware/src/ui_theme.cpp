#include "ui_theme.h"

#include "glow_9slice.h"
#include "sofia_aa_fonts.h"

namespace {

constexpr uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

constexpr ui_theme::Theme kThemes[] = {
    {
        ui_theme::Id::CodexDark,
        "codex-dark",
        "Codex Dark",
        {
            rgb565(0, 0, 0),       // background
            rgb565(69, 74, 236),   // header
            rgb565(255, 255, 255), // primaryText
            rgb565(38, 38, 38),    // card
            rgb565(71, 71, 71),    // track
            rgb565(98, 103, 255),  // accent
            rgb565(92, 97, 255),   // accentGlow
            rgb565(128, 128, 128), // muted
            rgb565(181, 181, 186), // secondaryText
            rgb565(179, 179, 179), // cardLabel
            rgb565(128, 128, 128), // cardHint
            rgb565(100, 104, 255), // status
            rgb565(255, 177, 0),   // warning
            rgb565(162, 255, 108), // chargingText
            rgb565(162, 255, 108), // chargingFill
            rgb565(255, 108, 108), // lowBatteryText
            rgb565(255, 93, 93),   // lowBatteryFill
            rgb565(255, 100, 110), // error
            rgb565(255, 107, 107), // limitStatus
            rgb565(255, 100, 100), // limitGlow
            rgb565(209, 95, 95),   // exhaustedCard
            rgb565(216, 119, 119), // exhaustedTrack
            rgb565(241, 207, 207), // exhaustedLabel
            rgb565(232, 175, 175), // exhaustedHint
            rgb565(118, 177, 255), // activity
        },
        {
            &SofiaMedium20,
            &SofiaMedium24,
            &SofiaMedium26,
            &SofiaMedium28,
            &SofiaMedium32,
            &SofiaMedium40,
            &SofiaBold20,
            &SofiaBold24,
            &SofiaBold32,
            &SofiaBlack48,
            &SofiaBlack64,
        },
        {&StatusGlow36, &ProgressGlow20},
        {
            30,   // pageMargin
            28,   // statusY
            36,   // statusHeight
            18.0f,// statusRadius
            40,   // progressX
            400,  // progressWidth
            20,   // progressHeight
            8.0f, // progressRadius
            20,   // cardX
            440,  // cardWidth
            20.0f,// cardRadius
        },
        {
            40,    // animationFrameMs
            1400,  // thinkingCycleMs
            1200,  // updateDotsCycleMs
            1600,  // chargingCycleMs
            1500,  // tokenRollMs
            0.30f, // thinkingMinOpacity
            1.00f, // thinkingMaxOpacity
            64,    // thinkingGlowMinAlpha
            230,   // thinkingGlowMaxAlpha
        },
    },
};

static_assert(
    sizeof(kThemes) / sizeof(kThemes[0]) ==
        static_cast<size_t>(ui_theme::Id::Count),
    "Theme registry and Id enum are out of sync");

ui_theme::Id currentTheme = ui_theme::Id::CodexDark;

}  // namespace

namespace ui_theme {

const Theme& active() {
  return kThemes[static_cast<uint8_t>(currentTheme)];
}

Id activeId() {
  return currentTheme;
}

bool select(Id id) {
  if (static_cast<uint8_t>(id) >= static_cast<uint8_t>(Id::Count)) return false;
  currentTheme = id;
  return true;
}

}  // namespace ui_theme
