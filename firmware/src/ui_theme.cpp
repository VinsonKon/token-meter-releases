#include "ui_theme.h"

#include "frame_clock.h"
#include "glow_9slice.h"
#include "sofia_aa_fonts.h"
#include "skin_runtime.h"

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
            24,   // statusY: retain the header center at y=46
            44,   // statusHeight: larger glyph body and labels
            8.0f, // statusRadius: approved compact text-only backplate
            40,   // progressX
            400,  // progressWidth
            20,   // progressHeight
            8.0f, // progressRadius
            20,   // cardX
            440,  // cardWidth
            20.0f,// cardRadius
        },
        {
            frame_clock::kAnimationFrameUs, // 60 FPS target; cycle durations unchanged
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
  if (skin::imported()) {
    static Theme importedTheme;
    static uint32_t importedRevision = UINT32_MAX;
    if (importedRevision != skin::revision()) {
      importedTheme = kThemes[0];
      const auto* skinHeader = skin::active();
      importedTheme.key = skinHeader->id;
      static_assert(sizeof(Colors) == 25 * sizeof(uint16_t), "skin color ABI");
      uint16_t colors[25];
      for (int i = 0; i < 25; ++i) {
        const uint32_t c = skinHeader->colors[i];
        colors[i] = rgb565(c >> 16, (c >> 8) & 255, c & 255);
      }
      memcpy(&importedTheme.colors, colors, sizeof(colors));
      using namespace skin_format;
      auto& m = importedTheme.metrics;
      m.statusY = skin::layout(StatusY);
      m.statusRadius = skin::layout(StatusRadius);
      m.cardX = skin::layout(CardX); m.cardWidth = skin::layout(CardWidth);
      m.cardRadius = skin::layout(CardRadius);
      m.progressX = m.cardX + skin::layout(ProgressInset);
      m.progressWidth = m.cardWidth - 2 * skin::layout(ProgressInset);
      m.progressHeight = skin::layout(ProgressHeight);
      m.progressRadius = skin::layout(ProgressRadius);
      importedRevision = skin::revision();
    }
    return importedTheme;
  }
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
