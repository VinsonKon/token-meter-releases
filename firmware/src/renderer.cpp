#include "renderer.h"

#include <Arduino_GFX_Library.h>
#include <esp_heap_caps.h>
#include <math.h>
#include <time.h>

#include "ai_model_catalog.h"
#include "board_config.h"
#include "glow_9slice.h"
#include "sofia_aa_fonts.h"
#include "sofia_bold28.h"
#include "status_label.h"
#include "status_motion.h"
#include "status_pixel.h"
#include "status_tile.h"
#include "svg_icons.h"
#include "ui_theme.h"
#include "skin_runtime.h"
#include "skin_pill.h"

namespace {

const ui_theme::Theme& theme() {
  return ui_theme::active();
}

Arduino_DataBus* bus = nullptr;
Arduino_CO5300* panel = nullptr;
Arduino_Canvas* canvas = nullptr;
Arduino_GFX* gfx = nullptr;
uint16_t* rotatedFramebuffer = nullptr;
uint16_t* partialFramebuffer = nullptr;
constexpr uint32_t kPartialFramebufferPixels = LCD_WIDTH * 100u;
bool totalAnimationInitialized = false;
bool totalAnimationRunning = false;
uint64_t displayedTotal = 0;
uint64_t totalAnimationStartValue = 0;
uint64_t totalAnimationTarget = 0;
uint32_t totalAnimationStartMs = 0;
ui::Page currentPage = ui::Page::Dashboard;
uint8_t currentBrightness = 70;
uint8_t currentVolume = 50;
uint8_t currentRotation = 0;
bool isDisplaySleeping = false;
String currentDeviceName = codex_meter::kDeviceName;
String currentUpdateStatus;
uint8_t currentUpdateProgress = 0;
String currentProviderId = "openai";
String currentPlanLabel;
status_motion::Transition statusTransition;
String currentStatusLabel;
uint32_t statusLoopStartedMs = 0;
bool statusTransitionNeedsFrame = false;
status_tile::Background statusBackground;
status_tile::Backdrop cachedStatusBackdrop;
status_label::Backplate statusBackplate;
status_label::Look cachedStatusLight = {};
int16_t cachedStatusRight = 0;
status_motion::State cachedStatusState = status_motion::State::Idle;
status_pixel::Mode cachedIconMode = status_pixel::Mode::Thinking;
const ui_theme::Theme* cachedStatusTheme = nullptr;
const AA4Font* cachedStatusFont = nullptr;
int16_t cachedStatusTextX = 0;
int16_t cachedStatusBaseline = 0;
uint16_t cachedStatusForeground = 0;
static_assert(status_tile::kSize == status_pixel::kSize, "status cache size mismatch");

// SofiaMedium24 uppercase pixels for the dashboard label occupy y=98..114
// at this baseline. Keep every independently refreshed animation rectangle
// outside that range so concurrent THINKING/token animations cannot erase it.
constexpr int16_t kTotalTokensBaseline = 115;
constexpr int16_t kTodayTokensGlyphTop = 98;
constexpr int16_t kTodayTokensGlyphBottomExclusive = 115;
// Includes glyph glow and the longest status pill, but never the independently
// refreshed battery or the TODAY TOKENS label.
constexpr int16_t kStatusRegionWidth = 368;
constexpr int16_t kStatusRightPadding = 12;
portMUX_TYPE statusStatsMux = portMUX_INITIALIZER_UNLOCKED;
ui::StatusRenderStats statusStats = {};
uint64_t statusTotalUs = 0;
uint32_t statusFirstFrameMs = 0;
uint32_t statusLastFrameMs = 0;
uint8_t statusStatsState = 255;
class StatusFrameTimer {
 public:
  explicit StatusFrameTimer(status_motion::State state)
      : state_(static_cast<uint8_t>(state)), started_(micros()) {}
  ~StatusFrameTimer() {
    const uint32_t elapsed = micros() - started_;
    const uint32_t now = millis();
    portENTER_CRITICAL(&statusStatsMux);
    if (statusStatsState != state_) {
      statusStats = {};
      statusStats.state = state_;
      statusStatsState = state_;
      statusTotalUs = 0;
      statusFirstFrameMs = now;
    }
    ++statusStats.frames;
    statusTotalUs += elapsed;
    statusStats.averageUs = statusTotalUs / statusStats.frames;
    if (elapsed > statusStats.maximumUs) statusStats.maximumUs = elapsed;
    if (elapsed > 33000) ++statusStats.overBudget;
    statusLastFrameMs = now;
    const uint32_t duration = statusLastFrameMs - statusFirstFrameMs;
    statusStats.fpsTenths = duration
        ? static_cast<uint32_t>(uint64_t(statusStats.frames - 1) * 10000 / duration) : 0;
    portEXIT_CRITICAL(&statusStatsMux);
  }
 private:
  uint8_t state_;
  uint32_t started_;
};
static_assert(kStatusRegionWidth < 370, "status must not erase battery pixels");
static_assert(kStatusRegionWidth * kTodayTokensGlyphTop <=
                  kPartialFramebufferPixels,
              "status animation must fit the partial framebuffer");

constexpr bool useSingleQuotaLayout(bool primaryValid,
                                    bool secondaryValid) {
  // A weekly-only Codex account has no 5H bucket. Its sole official window
  // uses the dedicated single-card Figma composition in both live and test
  // data. A short-only or completely empty packet keeps the fixed layout.
  return !primaryValid && secondaryValid;
}

static_assert(useSingleQuotaLayout(false, true),
              "weekly-only quota must use the single-card layout");
static_assert(!useSingleQuotaLayout(true, true),
              "dual quotas must keep the two-card layout");
static_assert(!useSingleQuotaLayout(true, false),
              "short-only quota must keep the fixed layout");
static_assert(!useSingleQuotaLayout(false, false),
              "empty quotas must keep the fixed layout");

uint16_t& framebufferPixel(uint16_t* framebuffer, int16_t x, int16_t y) {
  return framebuffer[static_cast<uint32_t>(y) * LCD_WIDTH + x];
}

void flushFrame() {
  if (!canvas || !panel) return;
  if (currentRotation == 0 || !rotatedFramebuffer) {
    canvas->flush();
    return;
  }

  const uint16_t* source = canvas->getFramebuffer();
  for (int16_t y = 0; y < LCD_HEIGHT; ++y) {
    const uint32_t sourceRow = static_cast<uint32_t>(y) * LCD_WIDTH;
    for (int16_t x = 0; x < LCD_WIDTH; ++x) {
      uint32_t destination = 0;
      if (currentRotation == 1) {
        destination = static_cast<uint32_t>(x) * LCD_WIDTH +
                      (LCD_WIDTH - 1 - y);
      } else if (currentRotation == 2) {
        destination = static_cast<uint32_t>(LCD_HEIGHT - 1 - y) * LCD_WIDTH +
                      (LCD_WIDTH - 1 - x);
      } else {
        destination = static_cast<uint32_t>(LCD_WIDTH - 1 - x) * LCD_WIDTH + y;
      }
      rotatedFramebuffer[destination] = source[sourceRow + x];
    }
  }
  panel->draw16bitRGBBitmap(0, 0, rotatedFramebuffer, LCD_WIDTH, LCD_HEIGHT);
}

// Copy and submit only a logical Canvas rectangle. This avoids rotating and
// transferring all 230,400 pixels for small UI animations. The destination
// rectangle is transformed for the current 90-degree display orientation.
void flushRegion(int16_t x, int16_t y, int16_t width, int16_t height) {
  if (!canvas || !panel || width <= 0 || height <= 0) return;
  x = constrain(x, 0, LCD_WIDTH);
  y = constrain(y, 0, LCD_HEIGHT);
  width = min<int16_t>(width, LCD_WIDTH - x);
  height = min<int16_t>(height, LCD_HEIGHT - y);
  const uint32_t pixelCount = static_cast<uint32_t>(width) * height;
  if (!partialFramebuffer || pixelCount > kPartialFramebufferPixels) {
    flushFrame();
    return;
  }

  const uint16_t* source = canvas->getFramebuffer();
  int16_t destinationX = x;
  int16_t destinationY = y;
  int16_t destinationWidth = width;
  int16_t destinationHeight = height;

  if (currentRotation == 0) {
    for (int16_t row = 0; row < height; ++row) {
      memcpy(partialFramebuffer + static_cast<uint32_t>(row) * width,
             source + static_cast<uint32_t>(y + row) * LCD_WIDTH + x,
             static_cast<size_t>(width) * sizeof(uint16_t));
    }
  } else if (currentRotation == 1) {
    destinationX = LCD_WIDTH - (y + height);
    destinationY = x;
    destinationWidth = height;
    destinationHeight = width;
    for (int16_t sourceY = 0; sourceY < height; ++sourceY) {
      for (int16_t sourceX = 0; sourceX < width; ++sourceX) {
        partialFramebuffer[
            static_cast<uint32_t>(sourceX) * destinationWidth +
            (height - 1 - sourceY)] =
            source[static_cast<uint32_t>(y + sourceY) * LCD_WIDTH +
                   x + sourceX];
      }
    }
  } else if (currentRotation == 2) {
    destinationX = LCD_WIDTH - (x + width);
    destinationY = LCD_HEIGHT - (y + height);
    for (int16_t sourceY = 0; sourceY < height; ++sourceY) {
      for (int16_t sourceX = 0; sourceX < width; ++sourceX) {
        partialFramebuffer[
            static_cast<uint32_t>(height - 1 - sourceY) * width +
            (width - 1 - sourceX)] =
            source[static_cast<uint32_t>(y + sourceY) * LCD_WIDTH +
                   x + sourceX];
      }
    }
  } else {
    destinationX = y;
    destinationY = LCD_HEIGHT - (x + width);
    destinationWidth = height;
    destinationHeight = width;
    for (int16_t sourceY = 0; sourceY < height; ++sourceY) {
      for (int16_t sourceX = 0; sourceX < width; ++sourceX) {
        partialFramebuffer[
            static_cast<uint32_t>(width - 1 - sourceX) * destinationWidth +
            sourceY] =
            source[static_cast<uint32_t>(y + sourceY) * LCD_WIDTH +
                   x + sourceX];
      }
    }
  }

  panel->draw16bitRGBBitmap(destinationX, destinationY, partialFramebuffer,
                            destinationWidth, destinationHeight);
}

uint16_t blend565(uint16_t background, uint16_t foreground, uint8_t alpha) {
  if (alpha == 0) return background;
  if (alpha >= 15) return foreground;
  const uint8_t inverse = 15 - alpha;
  const uint8_t r = ((((background >> 11) & 0x1F) * inverse) +
                     (((foreground >> 11) & 0x1F) * alpha) + 7) / 15;
  const uint8_t g = ((((background >> 5) & 0x3F) * inverse) +
                     (((foreground >> 5) & 0x3F) * alpha) + 7) / 15;
  const uint8_t b = (((background & 0x1F) * inverse) +
                     ((foreground & 0x1F) * alpha) + 7) / 15;
  return (r << 11) | (g << 5) | b;
}

uint16_t blend565_8(uint16_t background, uint16_t foreground, uint8_t alpha) {
  if (alpha == 0) return background;
  if (alpha == 255) return foreground;
  const uint16_t inverse = 255 - alpha;
  const uint8_t r = ((((background >> 11) & 0x1F) * inverse) +
                     (((foreground >> 11) & 0x1F) * alpha) + 127) / 255;
  const uint8_t g = ((((background >> 5) & 0x3F) * inverse) +
                     (((foreground >> 5) & 0x3F) * alpha) + 127) / 255;
  const uint8_t b = (((background & 0x1F) * inverse) +
                     ((foreground & 0x1F) * alpha) + 127) / 255;
  return (r << 11) | (g << 5) | b;
}

float roundedRectDistance(float px, float py, float x, float y,
                          float width, float height, float radius) {
  radius = fminf(radius, fminf(width, height) * 0.5f);
  const float halfWidth = width * 0.5f;
  const float halfHeight = height * 0.5f;
  const float qx = fabsf(px - (x + halfWidth)) - (halfWidth - radius);
  const float qy = fabsf(py - (y + halfHeight)) - (halfHeight - radius);
  const float outsideX = fmaxf(qx, 0.0f);
  const float outsideY = fmaxf(qy, 0.0f);
  return sqrtf(outsideX * outsideX + outsideY * outsideY) +
         fminf(fmaxf(qx, qy), 0.0f) - radius;
}

// Signed-distance rasterization keeps SVG-style rounded geometry smooth at
// any radius without allocating a temporary high-resolution framebuffer.
void fillAARoundedRect(int16_t x, int16_t y, int16_t width, int16_t height,
                       float radius, uint16_t color) {
  if (width <= 0 || height <= 0) return;
  radius = fminf(radius, fminf(width, height) * 0.5f);
  uint16_t* framebuffer = canvas->getFramebuffer();
  for (int16_t py = max<int16_t>(0, y);
       py < min<int16_t>(LCD_HEIGHT, y + height); ++py) {
    for (int16_t px = max<int16_t>(0, x);
         px < min<int16_t>(LCD_WIDTH, x + width); ++px) {
      // Only the four corners need a signed-distance/square-root test.
      // Every pixel in either central strip has full coverage already.
      if ((px + 0.5f >= x + radius && px + 0.5f <= x + width - radius) ||
          (py + 0.5f >= y + radius && py + 0.5f <= y + height - radius)) {
        framebufferPixel(framebuffer, px, py) = color;
        continue;
      }
      const float distance = roundedRectDistance(
          px + 0.5f, py + 0.5f, x, y, width, height, radius);
      const float coverage = 0.5f - distance;
      if (coverage <= 0.0f) continue;
      const uint8_t alpha = coverage >= 1.0f
                                ? 15
                                : static_cast<uint8_t>(coverage * 15.0f + 0.5f);
      uint16_t& target = framebufferPixel(framebuffer, px, py);
      target = blend565(target, color, alpha);
    }
  }
}

constexpr uint8_t kGlowBayer4x4[] = {
    0, 8, 2, 10,
    12, 4, 14, 6,
    3, 11, 1, 9,
    15, 7, 13, 5,
};

void blendGlowPixel(int16_t x, int16_t y, uint16_t color,
                    uint8_t normalizedAlpha, uint8_t peakAlpha) {
  if (normalizedAlpha == 0 || peakAlpha == 0 ||
      x < 0 || x >= LCD_WIDTH || y < 0 || y >= LCD_HEIGHT) return;
  int16_t alpha =
      (static_cast<uint16_t>(normalizedAlpha) * peakAlpha + 127u) / 255u;
  // Ordered dither keeps the faint Gaussian tail from stepping in RGB565.
  if (normalizedAlpha < 250) {
    const int8_t bias =
        (static_cast<int8_t>(kGlowBayer4x4[(y & 3) * 4 + (x & 3)]) - 7) / 2;
    alpha = constrain(alpha + bias, 0, 255);
  }
  if (alpha == 0) return;
  uint16_t& target = framebufferPixel(canvas->getFramebuffer(), x, y);
  target = blend565_8(target, color, static_cast<uint8_t>(alpha));
}

// Draw four corners and four edge profiles as a nine-slice. The masks come
// from Figma's transparent PNG exports, including its axis-specific rounding.
void drawNineSliceGlow(const Glow9SliceMask* mask, int16_t x, int16_t y,
                       int16_t width, int16_t height, uint16_t color,
                       uint8_t peakAlpha) {
  if (!mask || peakAlpha == 0 || width <= 0 || height <= 0) return;
  Glow9SliceMask loaded;
  memcpy_P(&loaded, mask, sizeof(loaded));
  const int16_t radius = loaded.radius;
  const int16_t spreadX = loaded.spreadX;
  const int16_t spreadY = loaded.spreadY;
  const int16_t sizeX = radius + spreadX;
  const int16_t sizeY = radius + spreadY;
  if (width < radius * 2 || height < radius * 2) return;

  // Four mirrored corner tiles.
  for (int16_t localY = 0; localY < sizeY; ++localY) {
    for (int16_t localX = 0; localX < sizeX; ++localX) {
      const uint8_t topLeft = pgm_read_byte(
          loaded.cornerTL + static_cast<uint32_t>(localY) * sizeX + localX);
      const uint8_t topRight = pgm_read_byte(
          loaded.cornerTR + static_cast<uint32_t>(localY) * sizeX + localX);
      const uint8_t bottomLeft = pgm_read_byte(
          loaded.cornerBL + static_cast<uint32_t>(localY) * sizeX + localX);
      const uint8_t bottomRight = pgm_read_byte(
          loaded.cornerBR + static_cast<uint32_t>(localY) * sizeX + localX);
      blendGlowPixel(x - spreadX + localX, y - spreadY + localY,
                     color, topLeft, peakAlpha);
      blendGlowPixel(x + width - radius + localX, y - spreadY + localY,
                     color, topRight, peakAlpha);
      blendGlowPixel(x - spreadX + localX, y + height - radius + localY,
                     color, bottomLeft, peakAlpha);
      blendGlowPixel(x + width - radius + localX,
                     y + height - radius + localY,
                     color, bottomRight, peakAlpha);
    }
  }

  // Stretch the top and bottom Gaussian edge profiles between the corners.
  for (int16_t local = 0; local < sizeY; ++local) {
    const uint8_t nearTop = pgm_read_byte(loaded.edgeTop + local);
    const uint8_t nearBottom = pgm_read_byte(loaded.edgeBottom + local);
    for (int16_t px = x + radius; px < x + width - radius; ++px) {
      blendGlowPixel(px, y - spreadY + local,
                     color, nearTop, peakAlpha);
      blendGlowPixel(px, y + height - radius + local,
                     color, nearBottom, peakAlpha);
    }
  }

  // Stretch the left and right profiles through the vertical center slice.
  for (int16_t local = 0; local < sizeX; ++local) {
    const uint8_t nearLeft = pgm_read_byte(loaded.edgeLeft + local);
    const uint8_t nearRight = pgm_read_byte(loaded.edgeRight + local);
    for (int16_t py = y + radius; py < y + height - radius; ++py) {
      blendGlowPixel(x - spreadX + local, py,
                     color, nearLeft, peakAlpha);
      blendGlowPixel(x + width - radius + local, py,
                     color, nearRight, peakAlpha);
    }
  }
}

AA4Glyph readGlyph(const AA4Font* font, char value, AA4Font* localFont = nullptr) {
  AA4Font loadedFont;
  if (!localFont) {
    memcpy_P(&loadedFont, font, sizeof(loadedFont));
    localFont = &loadedFont;
  }
  uint16_t code = static_cast<uint8_t>(value);
  if (code < localFont->first || code > localFont->last) code = '?';
  if (code < localFont->first || code > localFont->last) code = localFont->first;
  AA4Glyph glyph;
  memcpy_P(&glyph, localFont->glyphs + (code - localFont->first), sizeof(glyph));
  return glyph;
}

void drawText(const String& text, int16_t x, int16_t baseline,
              uint16_t color, const AA4Font* font,
              const status_tile::Rect* clip = nullptr) {
  AA4Font loadedFont;
  memcpy_P(&loadedFont, font, sizeof(loadedFont));
  uint16_t* framebuffer = canvas->getFramebuffer();
  int16_t cursorX = x;

  for (size_t charIndex = 0; charIndex < text.length(); ++charIndex) {
    const AA4Glyph glyph = readGlyph(font, text[charIndex], &loadedFont);
    if (clip && (cursorX + glyph.xOffset >= clip->x + clip->width ||
                 cursorX + glyph.xOffset + glyph.width <= clip->x ||
                 baseline + glyph.yOffset >= clip->y + clip->height ||
                 baseline + glyph.yOffset + glyph.height <= clip->y)) {
      cursorX += glyph.xAdvance;
      continue;
    }
    const uint32_t pixelCount = static_cast<uint32_t>(glyph.width) * glyph.height;
    for (uint32_t pixel = 0; pixel < pixelCount; ++pixel) {
      const uint8_t packed = pgm_read_byte(
          loadedFont.bitmap + glyph.bitmapOffset + (pixel >> 1));
      const uint8_t alpha = (pixel & 1) ? (packed & 0x0F) : (packed >> 4);
      if (alpha == 0) continue;
      const int16_t dx = cursorX + glyph.xOffset + (pixel % glyph.width);
      const int16_t dy = baseline + glyph.yOffset + (pixel / glyph.width);
      if (dx < 0 || dx >= LCD_WIDTH || dy < 0 || dy >= LCD_HEIGHT) continue;
      if (clip && !clip->contains(dx, dy)) continue;
      uint16_t& target = framebufferPixel(framebuffer, dx, dy);
      target = blend565(target, color, alpha);
    }
    cursorX += glyph.xAdvance;
  }
}

void drawSVGIcon(const SVGIcon* icon, int16_t x, int16_t y,
                 uint16_t color, uint8_t opacity = 15) {
  SVGIcon loaded;
  memcpy_P(&loaded, icon, sizeof(loaded));
  uint16_t* framebuffer = canvas->getFramebuffer();
  const uint32_t pixelCount =
      static_cast<uint32_t>(loaded.width) * loaded.height;
  for (uint32_t pixel = 0; pixel < pixelCount; ++pixel) {
    const uint8_t packed = pgm_read_byte(loaded.bitmap + (pixel >> 1));
    uint8_t alpha = (pixel & 1) ? (packed & 0x0F) : (packed >> 4);
    alpha = static_cast<uint8_t>((alpha * opacity + 7u) / 15u);
    if (alpha == 0) continue;
    const int16_t dx = x + pixel % loaded.width;
    const int16_t dy = y + pixel / loaded.width;
    if (dx < 0 || dx >= LCD_WIDTH || dy < 0 || dy >= LCD_HEIGHT) continue;
    uint16_t& target = framebufferPixel(framebuffer, dx, dy);
    target = blend565(target, color, alpha);
  }
}

uint16_t textWidth(const String& text, const AA4Font* font) {
  AA4Font loadedFont;
  memcpy_P(&loadedFont, font, sizeof(loadedFont));
  uint16_t width = 0;
  for (size_t i = 0; i < text.length(); ++i) {
    width += readGlyph(font, text[i], &loadedFont).xAdvance;
  }
  return width;
}

void drawTextCenteredInRect(const String& text, int16_t x, int16_t y,
                            int16_t width, int16_t height, uint16_t color,
                            const AA4Font* font, bool cacheStatusPlacement = false) {
  AA4Font loadedFont;
  memcpy_P(&loadedFont, font, sizeof(loadedFont));
  int16_t cursorX = 0;
  int16_t left = 0;
  int16_t top = 0;
  int16_t right = 0;
  int16_t bottom = 0;
  bool hasInk = false;
  for (size_t i = 0; i < text.length(); ++i) {
    const AA4Glyph glyph = readGlyph(font, text[i], &loadedFont);
    if (glyph.width > 0 && glyph.height > 0) {
      const int16_t glyphLeft = cursorX + glyph.xOffset;
      const int16_t glyphTop = glyph.yOffset;
      const int16_t glyphRight = glyphLeft + glyph.width;
      const int16_t glyphBottom = glyphTop + glyph.height;
      if (!hasInk) {
        left = glyphLeft;
        top = glyphTop;
        right = glyphRight;
        bottom = glyphBottom;
        hasInk = true;
      } else {
        left = min(left, glyphLeft);
        top = min(top, glyphTop);
        right = max(right, glyphRight);
        bottom = max(bottom, glyphBottom);
      }
    }
    cursorX += glyph.xAdvance;
  }
  if (!hasInk) return;
  const int16_t drawX = x + (width - (right - left)) / 2 - left;
  const int16_t baseline = y + (height - (bottom - top)) / 2 - top;
  if (cacheStatusPlacement) {
    cachedStatusTextX = drawX;
    cachedStatusBaseline = baseline;
    cachedStatusForeground = color;
    cachedStatusFont = font;
  }
  drawText(text, drawX, baseline, color, font);
}

void drawRight(const String& text, int16_t right, int16_t baseline,
               uint16_t color, const AA4Font* font) {
  drawText(text, right - static_cast<int16_t>(textWidth(text, font)),
           baseline, color, font);
}

String commaNumber(uint64_t value) {
  char raw[24];
  snprintf(raw, sizeof(raw), "%llu", static_cast<unsigned long long>(value));
  String result;
  const int length = strlen(raw);
  for (int i = 0; i < length; ++i) {
    if (i > 0 && (length - i) % 3 == 0) result += ',';
    result += raw[i];
  }
  return result;
}

String formatDuration(uint32_t seconds) {
  if (seconds == 0) return "resetting";
  const uint32_t days = seconds / 86400u;
  const uint32_t hours = (seconds % 86400u) / 3600u;
  const uint32_t minutes = (seconds % 3600u) / 60u;
  if (days > 0) return String(days) + "d " + String(hours) + "h";
  if (hours > 0) return String(hours) + "h " + String(minutes) + "m";
  return String(minutes) + "m";
}

bool updateStatusEquals(const char* value) {
  return currentUpdateStatus.equalsIgnoreCase(value);
}

bool updateOverlayVisible() {
  return updateStatusEquals("UPDATE AVAILABLE") ||
         updateStatusEquals("DOWNLOADING");
}

bool updateDotsVisible() {
  return updateStatusEquals("INSTALLING") ||
         updateStatusEquals("VERIFYING") ||
         updateStatusEquals("RESTARTING");
}

String formatResetDate(uint32_t epoch) {
  if (epoch == 0) return "--/--";
  time_t value = static_cast<time_t>(epoch);
  struct tm result {};
  gmtime_r(&value, &result);
  return String(result.tm_mon + 1) + "/" + String(result.tm_mday);
}

String windowLabel(uint16_t minutes) {
  if (minutes >= 10080 && minutes % 10080 == 0) {
    return minutes == 10080 ? "WEEK" : String(minutes / 10080) + " WEEKS";
  }
  if (minutes >= 60 && minutes % 60 == 0) {
    return String(minutes / 60) + "H";
  }
  return String(minutes) + "M";
}

uint32_t remainingSeconds(const codex_meter::CodexPacket& packet,
                          const codex_meter::UsageWindow& window,
                          uint32_t receivedAtMs) {
  if (window.resets_at <= packet.generated_at) return 0;
  const uint32_t original = window.resets_at - packet.generated_at;
  const uint32_t elapsed = (millis() - receivedAtMs) / 1000u;
  return elapsed >= original ? 0 : original - elapsed;
}

void drawCost(const codex_meter::CodexPacket* packet) {
  String amount = "--";
  if (packet && packet->cost_cny_cents >= 0) {
    const uint32_t cents = packet->cost_cny_cents;
    amount = String(cents / 100) + ".";
    if (cents % 100 < 10) amount += '0';
    amount += String(cents % 100);
  }
  const int16_t baseline = skin::layout(skin_format::CostBaseline);
  const int16_t costX = skin::layout(skin_format::CostX);
  drawText("RMB", costX, baseline, theme().colors.secondaryText, theme().type.body26);
  drawText(amount, costX + textWidth("RMB", theme().type.body26) + 6, baseline,
           theme().colors.secondaryText, theme().type.body32);
}

void drawBrand(const codex_meter::CodexPacket* packet) {
  const char* providerId =
      packet ? packet->provider : currentProviderId.c_str();
  const String provider = ai_model::providerLabel(providerId);
  const String plan =
      packet
          ? ((packet->flags & codex_meter::kPlanValid)
                 ? ai_model::planLabel(packet->plan)
                 : "")
          : currentPlanLabel;
  const SVGIcon* icon = ai_model::providerIcon(providerId);
  const int16_t providerX = icon ? 71 : 30;
  if (icon) drawSVGIcon(icon, 30, 25, theme().colors.primaryText);
  drawText(provider, providerX, 56, theme().colors.primaryText,
           theme().type.display40);
  const int16_t planX = providerX +
      textWidth(provider, theme().type.display40) + 14;
  if (plan.length() > 0) {
    const AA4Font* planFont = theme().type.body32;
    if (planX + textWidth(plan, planFont) > 350) planFont = theme().type.body24;
    if (planX + textWidth(plan, planFont) <= 350) {
      drawText(plan, planX, 55, theme().colors.primaryText, planFont);
    }
  }
}

void drawUpdateIndicator() {
  constexpr int16_t x = 348;
  constexpr int16_t y = 29;
  if (updateStatusEquals("UPDATE AVAILABLE")) {
    drawSVGIcon(&UpdateAvailable32, x, y, theme().colors.chargingText);
  } else if (updateStatusEquals("DOWNLOADING")) {
    drawSVGIcon(&DownloadRing32, x, y, theme().colors.primaryText);
    drawSVGIcon(&DownloadSweep32, x, y, theme().colors.chargingText);
    drawSVGIcon(&DownloadArrow14x16, x + 9, y + 8,
                theme().colors.chargingText);
  }
}

void drawBattery(int batteryPercent, bool charging) {
  constexpr int16_t x = 399;
  constexpr int16_t y = 32;
  const bool lowBattery = batteryPercent >= 0 && batteryPercent <= 15;
  const uint16_t fillColor =
      charging ? theme().colors.chargingText
               : (lowBattery ? theme().colors.lowBatteryFill
                             : theme().colors.primaryText);

  if (charging) {
    drawSVGIcon(&Charging10x18, 384, 36, theme().colors.chargingText);
  }
  drawSVGIcon(&Battery49x26, x, y, theme().colors.primaryText);

  int16_t fillWidth = 36;
  if (charging) {
    const uint32_t cycle = max<uint16_t>(1, theme().motion.chargingCycleMs);
    const float phase = static_cast<float>(millis() % cycle) / cycle;
    fillWidth = static_cast<int16_t>(
        fminf(1.0f, phase / 0.88f) * 36.0f + 0.5f);
    fillAARoundedRect(x + 5, y + 5, 36, 16, 4.0f,
                      theme().colors.track);
  }
  if (fillWidth > 0) {
    fillAARoundedRect(x + 5, y + 5, fillWidth, 16, 4.0f, fillColor);
  }

  const String value = batteryPercent >= 0 ? String(batteryPercent) : "--";
  const uint16_t textColor =
      lowBattery && !charging ? theme().colors.primaryText
                              : theme().colors.background;
  drawText(value,
           422 - static_cast<int16_t>(
                     textWidth(value, theme().type.strong20) / 2),
           52, textColor, theme().type.strong20);
}

void drawDashboardHeader(const codex_meter::CodexPacket* packet,
                         int batteryPercent, bool batteryCharging) {
  drawBrand(packet);
  if (updateOverlayVisible()) drawUpdateIndicator();
  drawBattery(batteryPercent, batteryCharging);
}

void drawCard(int16_t y, const char* fallbackLabel,
              const codex_meter::CodexPacket* packet,
              const codex_meter::UsageWindow& window, bool valid,
              uint32_t receivedAtMs) {
  const uint8_t usedPercent = valid ? min<uint8_t>(window.used_percent, 100) : 0;
  const uint8_t remainingPercent = valid ? 100 - usedPercent : 0;
  const bool isPrimary = packet && (&window == &packet->primary);
  const bool primaryValid =
      packet && (packet->flags & codex_meter::kPrimaryValid);
  const bool secondaryValid =
      packet && (packet->flags & codex_meter::kSecondaryValid);
  const bool singleValidWindow =
      packet && primaryValid != secondaryValid;
  const bool primaryLow =
      primaryValid && (100 - min<uint8_t>(packet->primary.used_percent, 100) < 10);
  const bool secondaryLow =
      secondaryValid &&
      (100 - min<uint8_t>(packet->secondary.used_percent, 100) < 10);
  const bool windowLow = valid && remainingPercent < 10;
  // The desktop flag preserves an explicit server-side limit result. When its
  // percentage does not identify a window, highlight the primary/only bucket
  // as a deterministic fallback. Otherwise only the low bucket turns red.
  const bool fallbackLimit =
      valid && packet && (packet->flags & codex_meter::kRateLimitReached) &&
      !primaryLow && !secondaryLow && (isPrimary || singleValidWindow);
  const bool exhausted = windowLow || fallbackLimit;
  fillAARoundedRect(theme().metrics.cardX, y, theme().metrics.cardWidth, 90,
                    theme().metrics.cardRadius,
                    exhausted ? theme().colors.exhaustedCard : theme().colors.card);

  const String label = valid ? windowLabel(window.window_minutes) : fallbackLabel;
  const uint16_t labelColor = exhausted ? theme().colors.exhaustedLabel : theme().colors.cardLabel;
  const uint16_t hintColor = exhausted ? theme().colors.exhaustedHint : theme().colors.cardHint;
  const int16_t labelX = theme().metrics.cardX + 25;
  drawText(label, labelX, y + 39, labelColor, theme().type.strong32);
  drawRight(valid ? String(remainingPercent) + "%" : "--", theme().metrics.cardX + theme().metrics.cardWidth - 20, y + 39,
            theme().colors.primaryText, theme().type.strong32);

  const String reset = valid
                           ? "Resets in " + formatDuration(
                                 remainingSeconds(*packet, window, receivedAtMs))
                           : "Waiting for data";
  const int16_t resetX = labelX + textWidth(label, theme().type.strong32) + 8;
  drawText(reset, resetX, y + 40, hintColor, theme().type.strong20);

  const int16_t trackY = y + skin::layout(skin_format::ProgressY) - (exhausted ? 2 : 0);
  const float trackRadius = exhausted ? 6.0f : 8.0f;
  fillAARoundedRect(theme().metrics.progressX, trackY,
                    theme().metrics.progressWidth,
                    theme().metrics.progressHeight, trackRadius,
                    exhausted ? theme().colors.exhaustedTrack
                              : theme().colors.track);
  if (valid && remainingPercent > 0 && !exhausted) {
    const int16_t progressWidth = max<int16_t>(theme().metrics.progressHeight, remainingPercent * theme().metrics.progressWidth / 100);
    drawNineSliceGlow(theme().assets.progressGlow,
                      theme().metrics.progressX, trackY,
                      progressWidth, theme().metrics.progressHeight,
                      theme().colors.accentGlow, 128);
    fillAARoundedRect(theme().metrics.progressX, trackY, progressWidth,
                      theme().metrics.progressHeight,
                      theme().metrics.progressRadius, theme().colors.accent);
  }
}

status_motion::State resolveStatus(const codex_meter::CodexPacket* packet,
                                    bool fresh) {
  using status_motion::State;
  if (!packet) return State::Waiting;
  // Test selection is authoritative. A live approval bit left in a test
  // snapshot must not make the full and partial render paths disagree.
  if (packet->test_state != codex_meter::kTestNone) {
    switch (packet->test_state) {
      case codex_meter::kTestThinking: return State::Thinking;
      case codex_meter::kTestNeedApproval: return State::Approval;
      case codex_meter::kTestDataStale: return State::Stale;
      case codex_meter::kTestWaitingForSync: return State::Waiting;
      default: return State::Idle;
    }
  }
  return status_motion::resolve(
      true, false,
      packet->needs_approval || strcmp(packet->status, "need approval") == 0 ||
          strcmp(packet->status, "needs approval") == 0,
      fresh, strcmp(packet->status, "thinking") == 0,
      packet->status[0] != '\0' && strcmp(packet->status, "idle") != 0);
}

bool statusHasBackplate(status_motion::State state) {
  if (skin::imported() && state != status_motion::State::Other && state != status_motion::State::Idle) {
    const int index = state == status_motion::State::Thinking ? 0 : state == status_motion::State::Approval ? 1 : state == status_motion::State::Stale ? 2 : 3;
    return skin::active()->animations[index].plate != 0;
  }
  return state == status_motion::State::Thinking ||
         state == status_motion::State::Approval ||
         state == status_motion::State::Other;
}

status_label::Look statusLight(status_motion::State state, uint32_t elapsed) {
  if (skin::imported() && state != status_motion::State::Other && state != status_motion::State::Idle) {
    if (!statusHasBackplate(state)) return {0,0};
    const auto mode = state == status_motion::State::Thinking ? status_pixel::Mode::Thinking : state == status_motion::State::Approval ? status_pixel::Mode::Approval : state == status_motion::State::Stale ? status_pixel::Mode::Stale : status_pixel::Mode::Waiting;
    return skin::light(mode, elapsed);
  }
  if (state == status_motion::State::Thinking || state == status_motion::State::Approval)
    return status_label::sample(state == status_motion::State::Approval, elapsed);
  return state == status_motion::State::Other ? status_label::Look{20,18}
                                             : status_label::Look{0,0};
}

status_tile::Backdrop statusBackdrop(status_motion::State state,
                                    const status_pixel::Frame& icon,
                                    status_label::Look light) {
  status_tile::Backdrop result;
  result.background = theme().colors.background;
  result.tint = state == status_motion::State::Other ? theme().colors.status
      : uint16_t(((icon.r & 0xF8) << 8) | ((icon.g & 0xFC) << 3) | (icon.b >> 3));
  if (!statusHasBackplate(state)) result.tint = 0;
  result.card = blend565_8(result.background, result.tint, light.card);
  result.edge = light.glow;
  return result;
}

struct PillBackend {
  int width(const char* s) { return textWidth(s, theme().type.strong24); }
  uint16_t blend(uint16_t bg, uint16_t ink, uint8_t alpha) { return blend565(bg, ink, alpha); }
  void glow(int x, int y, int w, int h, uint16_t color, uint8_t alpha) {
    drawNineSliceGlow(theme().assets.statusGlow, x,y,w,h,color,alpha);
  }
  void rect(int x, int y, int w, int h, float radius, uint16_t color) {
    fillAARoundedRect(x,y,w,h,radius,color);
  }
  void text(const char* s, int x, int baseline, uint16_t color) {
    drawText(s,x,baseline,color,theme().type.strong24);
  }
  void center(const char* s, int x, int y, int w, int h, uint16_t color) {
    drawTextCenteredInRect(s,x,y,w,h,color,theme().type.strong24);
  }
};

bool drawStatus(const codex_meter::CodexPacket* packet, bool fresh) {
  using status_motion::State;
  statusBackground.invalidate();
  const State state = resolveStatus(packet, fresh);
  const uint32_t now = millis();
  String label;
  status_pixel::Mode iconMode = status_pixel::Mode::Thinking;
  const AA4Font* font = theme().type.strong32;
  switch (state) {
    case State::Approval:
      label = "NEED APPROVAL";
      iconMode = status_pixel::Mode::Approval;
      break;
    case State::Stale:
      label = "DATA STALE";
      iconMode = status_pixel::Mode::Stale;
      break;
    case State::Waiting:
      label = "WAITING FOR SYNC";
      iconMode = status_pixel::Mode::Waiting;
      font = &SofiaBold28;
      break;
    case State::Thinking: label = "THINKING"; break;
    case State::Other:
      label = packet->status;
      label.toUpperCase();
      font = theme().type.body28;
      break;
    case State::Idle:
      statusTransition.update(state, 0, now);
      currentStatusLabel = "";
      statusTransitionNeedsFrame = false;
      portENTER_CRITICAL(&statusStatsMux);
      statusStats = {};
      statusStatsState = static_cast<uint8_t>(State::Idle);
      statusTotalUs = 0;
      statusFirstFrameMs = statusLastFrameMs = now;
      portEXIT_CRITICAL(&statusStatsMux);
      return false;
  }

  if (skin::imported() && skin::active()->schema == 2 && state != State::Other) {
    const unsigned index = static_cast<unsigned>(iconMode);
    const auto& p = skin_format::pills(skin::active())[index];
    if (statusTransition.update(state, p.width, now, label != currentStatusLabel)) {
      statusLoopStartedMs = now;
      currentStatusLabel = label;
    }
    // The classic style has no pixel entrance or tile cache; each tick rebuilds
    // its bounded header so pulsing text/glow never accumulates old pixels.
    statusTransitionNeedsFrame = false;
    PillBackend backend;
    skin_pill::draw(skin::active(), index, now-statusLoopStartedMs, backend);
    return true;
  }
  const bool showIcon = state != State::Other;
  const auto* skinAnimation = showIcon ? skin::animation(iconMode) : nullptr;
  if (skinAnimation) font = skinAnimation->font == 28 ? &SofiaBold28 : theme().type.strong32;
  // The chosen preview separates the glyph from the R8 text-only plate.
  // Bare sync states keep the glyph position but bring the words closer.
  constexpr int16_t kIconBodyWidth = 36;
  const int16_t kPadding = skin::layout(skin_format::PlatePadding);
  const bool hasPlate = statusHasBackplate(state);
  const int16_t x = theme().metrics.pageMargin;
  const int16_t y = theme().metrics.statusY;
  const int16_t height = theme().metrics.statusHeight;
  const int16_t groupX = skin::layout(skin_format::StatusBodyX);
  const int16_t cardX = showIcon ? groupX + kIconBodyWidth + skin::layout(skin_format::PlateGap) : x;
  const int16_t textX = hasPlate ? cardX + kPadding : groupX + kIconBodyWidth + skin::layout(skin_format::BareGap);
  const int16_t maxTextWidth = kStatusRegionWidth - kStatusRightPadding - textX -
                               (hasPlate ? kPadding : 0);
  if (textWidth(label, font) > maxTextWidth) {
    while (label.length() > 0 &&
           textWidth(label + "...", font) > maxTextWidth) {
      label.remove(label.length() - 1);
    }
    label += "...";
  }
  const int16_t labelWidth = textWidth(label, font);
  const int16_t cardWidth = hasPlate ? labelWidth + 2 * kPadding : 0;
  const int16_t targetWidth = textX + labelWidth + (hasPlate ? kPadding : 0) - x;
  if (statusTransition.update(state, targetWidth, now,
                              label != currentStatusLabel)) {
    statusLoopStartedMs = now;
    currentStatusLabel = label;
  }
  const float progress = statusTransition.progress(now);
  // Schedule the terminal frame too, even if the previous sample was just
  // before 240 ms and the new state does not have a continuous glyph loop.
  statusTransitionNeedsFrame = statusTransition.active(now);
  const uint32_t elapsed = now - statusLoopStartedMs;
  const auto icon = skin::sample(iconMode, elapsed);
  const auto light = statusLight(state, elapsed);
  const auto backdrop = statusBackdrop(state, icon, light);
  const uint16_t cardColor = backdrop.card;
  // Fixed layout during the short entrance avoids rebuilding the geometry
  // mask every animation tick. Opacity/vertical easing are retained below.
  if (hasPlate) {
    if (statusBackplate.configure(cardX, y, cardWidth, height, theme().metrics.statusRadius)) {
      statusBackplate.draw(canvas->getFramebuffer(), LCD_WIDTH,
                          backdrop.background, backdrop.tint, light);
    }
  }

  // Warnings bypass this entrance in Transition::update. Normal work states
  // settle over 240 ms; repeated heartbeats keep their existing phase.
  const float opacity = 0.45f + 0.55f * progress;
  const int16_t offsetY = static_cast<int16_t>((1.0f - progress) * 4.0f + 0.5f);
  if (showIcon) {
    const int16_t iconX = groupX + (kIconBodyWidth - status_pixel::kSize) / 2;
    const int16_t iconY = y + (height - status_pixel::kSize) / 2 + offsetY;
    if (!statusTransitionNeedsFrame &&
        statusBackground.capture(canvas->getFramebuffer(), LCD_WIDTH,
                                 LCD_WIDTH, LCD_HEIGHT, iconX, iconY)) {
      cachedStatusBackdrop = backdrop;
      cachedStatusLight = light;
      cachedStatusRight = hasPlate ? cardX + cardWidth : textX + labelWidth;
      cachedStatusState = state;
      cachedIconMode = iconMode;
      cachedStatusTheme = &theme();
    }
    status_pixel::draw(canvas->getFramebuffer() + iconY * LCD_WIDTH + iconX,
                       LCD_WIDTH, icon, opacity);
  }
  const uint32_t ink = skinAnimation ? skinAnimation->textColor : 0;
  const uint16_t textColor = skinAnimation ? uint16_t(((ink >> 8) & 0xF800) | ((ink >> 5) & 0x07E0) | ((ink >> 3) & 31)) : state == State::Stale
      ? uint16_t(((status_pixel::kGoldR & 0xF8) << 8) |
                 ((status_pixel::kGoldG & 0xFC) << 3) | (status_pixel::kGoldB >> 3))
      : theme().colors.primaryText;
  const uint16_t foreground = blend565_8(cardColor, textColor,
      static_cast<uint8_t>(opacity * 255.0f + 0.5f));
  drawTextCenteredInRect(label, textX, y + offsetY,
                         labelWidth, height, foreground, font,
                         statusBackground.valid());
  return true;
}

void drawTokenTotal(uint64_t value) {
  const String total = commaNumber(value);
  const AA4Font* totalFont = theme().type.total64;
  if (textWidth(total, totalFont) > 421) totalFont = theme().type.total48;
  drawText(total, skin::layout(skin_format::TotalX), skin::layout(skin_format::TotalBaseline), theme().colors.primaryText, totalFont);
}

uint64_t sampleTotalAnimation(uint32_t now) {
  if (!totalAnimationRunning) return displayedTotal;
  const uint32_t kDurationMs = theme().motion.tokenRollMs;
  const uint32_t elapsed = now - totalAnimationStartMs;
  const float progress = min(
      1.0f, static_cast<float>(elapsed) / kDurationMs);
  const float inverse = 1.0f - progress;
  const float eased = 1.0f - inverse * inverse * inverse * inverse;
  const double value = static_cast<double>(totalAnimationStartValue) +
      (static_cast<double>(totalAnimationTarget) -
       static_cast<double>(totalAnimationStartValue)) * eased;
  displayedTotal = value <= 0.0
                       ? 0
                       : static_cast<uint64_t>(value + 0.5);
  if (progress >= 1.0f) {
    displayedTotal = totalAnimationTarget;
    totalAnimationRunning = false;
  }
  return displayedTotal;
}

void updateTotalAnimationTarget(uint64_t target, uint32_t now) {
  if (!totalAnimationInitialized) {
    totalAnimationInitialized = true;
    displayedTotal = target;
    totalAnimationStartValue = target;
    totalAnimationTarget = target;
    totalAnimationRunning = false;
    return;
  }
  if (target == totalAnimationTarget) return;
  sampleTotalAnimation(now);
  totalAnimationStartValue = displayedTotal;
  totalAnimationTarget = target;
  totalAnimationStartMs = now;
  totalAnimationRunning = totalAnimationStartValue != totalAnimationTarget;
}

String resetCountValue(const codex_meter::CodexPacket* packet) {
  if (!packet || !(packet->flags & codex_meter::kResetCountValid)) {
    return "--";
  }
  return String(packet->reset_count);
}

void renderFrame(const codex_meter::CodexPacket* packet, bool fresh,
                 int batteryPercent, bool batteryCharging,
                 uint32_t receivedAtMs, uint64_t displayedTotal) {
  statusBackground.invalidate();
  gfx->fillScreen(theme().colors.background);
  const bool statusVisible =
      updateOverlayVisible() ? false : drawStatus(packet, fresh);
  if (!packet) {
    drawBattery(batteryPercent, batteryCharging);
    flushFrame();
    return;
  }
  if (!statusVisible) drawBrand(packet);
  if (updateOverlayVisible()) drawUpdateIndicator();
  drawBattery(batteryPercent, batteryCharging);

  const bool primaryValid =
      packet && (packet->flags & codex_meter::kPrimaryValid);
  const bool secondaryValid =
      packet && (packet->flags & codex_meter::kSecondaryValid);
  const bool singleQuota = useSingleQuotaLayout(
      primaryValid, secondaryValid);
  const char* tokenLabel =
      packet->flags & codex_meter::kLifetimeTokenTotal
          ? "TOTAL TOKENS"
          : "TODAY TOKENS";
  drawText(tokenLabel, skin::layout(skin_format::LabelX), skin::layout(skin_format::LabelBaseline), theme().colors.muted,
           theme().type.body24);
  if (!singleQuota) {
    drawRight("Resets:  " + resetCountValue(packet),
              skin::layout(skin_format::ResetRight), skin::layout(skin_format::LabelBaseline), theme().colors.muted, theme().type.body24);
  }

  drawTokenTotal(displayedTotal);
  drawCost(packet);

  const codex_meter::UsageWindow empty{};
  if (singleQuota) {
    const codex_meter::UsageWindow& onlyWindow =
        primaryValid ? packet->primary : packet->secondary;
    drawCard(skin::layout(skin_format::SingleY), primaryValid ? "5H" : "WEEK", packet, onlyWindow,
             true, receivedAtMs);
    drawText("Resets:  " + resetCountValue(packet), skin::layout(skin_format::LabelX), skin::layout(skin_format::SingleResetY),
             theme().colors.muted, theme().type.body32);
    drawText("The full reset will expire on " +
             formatResetDate(onlyWindow.resets_at),
             skin::layout(skin_format::LabelX), skin::layout(skin_format::SingleFooterY), theme().colors.muted, theme().type.body28);
  } else {
    drawCard(skin::layout(skin_format::PrimaryY), "5H", packet,
             primaryValid ? packet->primary : empty,
             primaryValid, receivedAtMs);
    drawCard(skin::layout(skin_format::SecondaryY), "WEEK", packet,
             secondaryValid ? packet->secondary : empty,
             secondaryValid, receivedAtMs);
  }
  flushFrame();
}

void drawBackFooter() {
  gfx->fillRect(0, 410, 480, 70, theme().colors.card);
  const String label = "BACK";
  drawText(label, (LCD_WIDTH - textWidth(label, theme().type.strong32)) / 2,
           457, theme().colors.primaryText, theme().type.strong32);
}

void drawSettingsCard(int16_t x, bool brightness) {
  // Match Figma's 200x142 card and keep the 20 px corners smooth on AMOLED.
  fillAARoundedRect(x, 120, 200, 142, 20.0f, theme().colors.card);
  if (brightness) {
    drawSVGIcon(&Brightness64, x + 30, 140, theme().colors.primaryText);
    drawRight(String(currentBrightness) + "%", x + 169, 185,
              theme().colors.primaryText, theme().type.strong24);
  } else {
    drawSVGIcon(&Volume64, x + 30, 140, theme().colors.primaryText);
    drawRight(String(currentVolume) + "%", x + 169, 185,
              theme().colors.primaryText, theme().type.strong24);
  }
  const String label = brightness ? "BRIGHTNESS" : "VOLUME";
  drawText(label, x + (200 - textWidth(label, theme().type.strong24)) / 2,
           244, theme().colors.primaryText, theme().type.strong24);
}

void renderSettingsHome(int batteryPercent, bool batteryCharging) {
  gfx->fillScreen(theme().colors.background);
  drawBrand(nullptr);
  drawBattery(batteryPercent, batteryCharging);
  drawText(currentDeviceName, 30, 94, theme().colors.muted,
           theme().type.strong24);
  drawSettingsCard(30, true);
  drawSettingsCard(250, false);
  drawBackFooter();
  flushFrame();
}

void renderControlPage(bool brightness, int batteryPercent,
                       bool batteryCharging) {
  gfx->fillScreen(theme().colors.background);
  drawBrand(nullptr);
  drawBattery(batteryPercent, batteryCharging);
  const uint8_t value = brightness ? currentBrightness : currentVolume;
  if (brightness) {
    drawSVGIcon(&Brightness40, 30, 121, theme().colors.primaryText);
  } else {
    drawSVGIcon(&Volume40, 30, 121, theme().colors.primaryText);
  }
  const String label = brightness ? "BRIGHTNESS" : "VOLUME";
  drawText(label, 81, 155, theme().colors.primaryText, theme().type.strong32);
  drawRight(String(value) + "%", 450, 155, theme().colors.primaryText, theme().type.strong32);

  fillAARoundedRect(30, 218, 420, 80, 20.0f, theme().colors.card);
  // The Figma 60% sample is x=31, width=258. Preserve that optical scale,
  // while clamping the 100% state exactly inside the 420 px track.
  const int16_t fillWidth = value == 0
                                ? 0
                                : min<int16_t>(419,
                                    (static_cast<int16_t>(value) * 43 + 5) / 10);
  if (fillWidth > 0) {
    fillAARoundedRect(31, 218, max<int16_t>(40, fillWidth), 80,
                      20.0f, theme().colors.primaryText);
  }
  drawBackFooter();
  flushFrame();
}

void drawLoopingUpdateLabel() {
  String word = currentUpdateStatus;
  word.toUpperCase();
  const bool installing = updateStatusEquals("INSTALLING");
  const int16_t baseline = installing ? 185 : 250;
  const int16_t dotWidth = textWidth(".", theme().type.strong32);
  const int16_t wordWidth = textWidth(word, theme().type.strong32);
  const String percent = installing
      ? String(currentUpdateProgress) + "%"
      : "";
  const int16_t percentGap = installing ? 8 : 0;
  const int16_t totalWidth =
      wordWidth + dotWidth * 3 + percentGap +
      textWidth(percent, theme().type.strong32);
  const int16_t startX = (LCD_WIDTH - totalWidth) / 2;
  drawText(word, startX, baseline, theme().colors.primaryText,
           theme().type.strong32);

  const uint32_t cycle =
      max<uint16_t>(1, theme().motion.updateDotsCycleMs);
  const float phase = static_cast<float>(millis() % cycle) / cycle;
  constexpr float kTau = 6.28318530718f;
  for (uint8_t dot = 0; dot < 3; ++dot) {
    const float wave =
        0.5f + 0.5f * sinf(kTau * (phase - dot / 3.0f));
    const uint8_t opacity = static_cast<uint8_t>(
        (0.25f + 0.75f * wave) * 15.0f + 0.5f);
    const uint16_t dotColor = blend565(
        theme().colors.background, theme().colors.primaryText, opacity);
    drawText(".", startX + wordWidth + dot * dotWidth, baseline,
             dotColor, theme().type.strong32);
  }
  if (installing) {
    drawText(percent, startX + wordWidth + dotWidth * 3 + percentGap,
             baseline, theme().colors.primaryText, theme().type.strong32);
  }
}

void renderUpdatePage(int batteryPercent, bool batteryCharging) {
  gfx->fillScreen(theme().colors.background);
  const bool showBattery =
      updateStatusEquals("INSTALLING") ||
      updateStatusEquals("VERIFYING") ||
      updateStatusEquals("RESTARTING");
  if (showBattery) {
    drawBattery(batteryPercent, batteryCharging);
  }

  if (updateStatusEquals("INSTALLING")) {
    drawLoopingUpdateLabel();
    fillAARoundedRect(30, 220, 420, 40, 12.0f, theme().colors.card);
    const int16_t fillWidth = static_cast<int16_t>(
        static_cast<uint32_t>(currentUpdateProgress) * 420u / 100u);
    if (fillWidth > 0) {
      fillAARoundedRect(30, 220, max<int16_t>(24, fillWidth), 40,
                        12.0f, theme().colors.chargingText);
    }
  } else if (updateStatusEquals("VERIFYING") ||
             updateStatusEquals("RESTARTING")) {
    drawLoopingUpdateLabel();
  } else if (updateStatusEquals("UPDATE FAILED")) {
    drawSVGIcon(&UpdateFailed110x100, 185, 113, theme().colors.error);
    const String label = "UPDATE FAILED";
    drawText(label,
             (LCD_WIDTH - textWidth(label, theme().type.strong32)) / 2,
             261, theme().colors.primaryText, theme().type.strong32);
    fillAARoundedRect(30, 323, 200, 110, 20.0f, theme().colors.card);
    fillAARoundedRect(250, 323, 200, 110, 20.0f, theme().colors.card);
    const String retry = "RETRY";
    const String rolledBack = "ROLLED BACK";
    drawText(retry,
             30 + (200 - textWidth(retry, theme().type.strong24)) / 2,
             391, theme().colors.primaryText, theme().type.strong24);
    drawText(rolledBack,
             250 + (200 - textWidth(rolledBack, theme().type.strong24)) / 2,
             391, theme().colors.primaryText, theme().type.strong24);
  } else if (updateStatusEquals("ROLLED BACK")) {
    drawSVGIcon(&RolledBack64, 208, 132, theme().colors.warning);
    const String label = "ROLLED BACK";
    drawText(label,
             (LCD_WIDTH - textWidth(label, theme().type.strong32)) / 2,
             260, theme().colors.primaryText, theme().type.strong32);
  } else {
    String label = currentUpdateStatus;
    label.toUpperCase();
    drawText(label,
             (LCD_WIDTH - textWidth(label, theme().type.strong32)) / 2,
             250, theme().colors.primaryText, theme().type.strong32);
  }
  flushFrame();
}

}  // namespace

namespace ui {

bool begin() {
  bus = new Arduino_ESP32QSPI(
      LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
  panel = new Arduino_CO5300(
      bus, LCD_RESET, 0, LCD_WIDTH, LCD_HEIGHT, 0, 0, 0, 0);
  if (!panel->begin()) return false;
  panel->setBrightness(map(currentBrightness, 0, 100, 0, 255));

  canvas = new Arduino_Canvas(LCD_WIDTH, LCD_HEIGHT, panel);
  if (!canvas->begin(GFX_SKIP_OUTPUT_BEGIN)) return false;
  rotatedFramebuffer = static_cast<uint16_t*>(heap_caps_malloc(
      static_cast<size_t>(LCD_WIDTH) * LCD_HEIGHT * sizeof(uint16_t),
      MALLOC_CAP_SPIRAM));
  if (!rotatedFramebuffer) {
    Serial.println("Rotation buffer unavailable; using rotation 0");
  }
  partialFramebuffer = static_cast<uint16_t*>(heap_caps_malloc(
      static_cast<size_t>(kPartialFramebufferPixels) * sizeof(uint16_t),
      MALLOC_CAP_SPIRAM));
  if (!partialFramebuffer) {
    Serial.println("Partial animation buffer unavailable; using full-frame flushes");
  }
  gfx = canvas;
  gfx->fillScreen(theme().colors.background);
  flushFrame();
  return true;
}

void setPage(Page value) {
  if (currentPage != value) statusBackground.invalidate();
  currentPage = value;
}

Page page() {
  return currentPage;
}

void setControls(uint8_t brightnessPercent, uint8_t volumePercent) {
  const uint8_t nextBrightness = constrain(brightnessPercent, 10, 100);
  currentVolume = constrain(volumePercent, 0, 100);
  if (nextBrightness != currentBrightness) {
    setBrightness(nextBrightness);
  }
}

void setDeviceName(const String& deviceName) {
  currentDeviceName = deviceName.length() ? deviceName : codex_meter::kDeviceName;
}

void setUpdateState(const String& status, uint8_t progressPercent) {
  statusBackground.invalidate();
  currentUpdateStatus = status;
  currentUpdateStatus.trim();
  currentUpdateStatus.toUpperCase();
  currentUpdateProgress = min<uint8_t>(progressPercent, 100);
  if (currentUpdateStatus.isEmpty() || updateStatusEquals("IDLE")) {
    currentUpdateStatus = "";
    if (currentPage == Page::Update) currentPage = Page::Dashboard;
  } else if (updateOverlayVisible()) {
    currentPage = Page::Dashboard;
  } else {
    currentPage = Page::Update;
  }
}

void setBrightness(uint8_t brightnessPercent) {
  currentBrightness = constrain(brightnessPercent, 10, 100);
  if (panel) {
    panel->setBrightness(map(currentBrightness, 0, 100, 0, 255));
  }
}

void setRotation(uint8_t value) {
  value &= 0x03;
  if (value == currentRotation) return;
  statusBackground.invalidate();
  currentRotation = value;
  // Compose each page in a stable rotation-0 Canvas. flushFrame() rotates the
  // completed bitmap, keeping layout and antialiased pixel writes consistent.
  if (panel) panel->setRotation(0);
  if (canvas) canvas->setRotation(0);
}

uint8_t rotation() {
  return currentRotation;
}

void sleepDisplay() {
  if (!panel || isDisplaySleeping) return;
  statusBackground.invalidate();
  panel->setBrightness(0);
  panel->displayOff();
  isDisplaySleeping = true;
}

void wakeDisplay() {
  if (!panel || !isDisplaySleeping) return;
  statusBackground.invalidate();
  panel->displayOn();
  panel->setBrightness(map(currentBrightness, 0, 100, 0, 255));
  isDisplaySleeping = false;
}

bool displaySleeping() {
  return isDisplaySleeping;
}

void render(const codex_meter::CodexPacket* packet, bool fresh,
            bool, int batteryPercent, bool batteryCharging,
            uint32_t receivedAtMs) {
  codex_meter::CodexPacket testPacket;
  if (packet) {
    if (packet->test_state != codex_meter::kTestNone) {
      testPacket = *packet;
      testPacket.flags &= ~codex_meter::kRateLimitReached;
      strlcpy(testPacket.status, "idle", sizeof(testPacket.status));
      if (testPacket.test_state == codex_meter::kTestThinking) {
        strlcpy(testPacket.status, "thinking", sizeof(testPacket.status));
      } else if (testPacket.test_state ==
                 codex_meter::kTestNeedApproval) {
        strlcpy(testPacket.status, "need approval",
                sizeof(testPacket.status));
      } else if (testPacket.test_state == codex_meter::kTestLimitReached) {
        testPacket.flags |= codex_meter::kRateLimitReached;
        testPacket.primary.used_percent = 91;
      }
      switch (testPacket.test_state) {
        case codex_meter::kTestNeedApproval:
          setUpdateState("IDLE", 0);
          break;
        case codex_meter::kTestUpdateAvailable:
          setUpdateState("UPDATE AVAILABLE", 0);
          break;
        case codex_meter::kTestDownloading:
          setUpdateState("DOWNLOADING", 35);
          break;
        case codex_meter::kTestInstalling:
          setUpdateState("INSTALLING", 60);
          break;
        case codex_meter::kTestVerifying:
          setUpdateState("VERIFYING", 100);
          break;
        case codex_meter::kTestRestarting:
          setUpdateState("RESTARTING", 100);
          break;
        case codex_meter::kTestUpdateFailed:
          setUpdateState("UPDATE FAILED", 63);
          break;
        case codex_meter::kTestRolledBack:
          setUpdateState("ROLLED BACK", 0);
          break;
        case codex_meter::kTestSingleQuota:
          testPacket.flags &= ~codex_meter::kPrimaryValid;
          testPacket.flags |= codex_meter::kSecondaryValid;
          setUpdateState("IDLE", 0);
          break;
        default:
          if (testPacket.test_state <= codex_meter::kTestLowBattery) {
            setUpdateState("IDLE", 0);
          }
          break;
      }
      packet = &testPacket;
    }
    if (packet->test_state == codex_meter::kTestCharging) {
      batteryPercent = 85;
      batteryCharging = true;
    } else if (packet->test_state == codex_meter::kTestLowBattery) {
      batteryPercent = 10;
      batteryCharging = false;
    }
    if (packet->test_state == codex_meter::kTestDataStale) {
      fresh = false;
    }
  }
  if (packet) {
    currentProviderId =
        packet->provider[0] != '\0' ? packet->provider : "openai";
    currentPlanLabel =
        (packet->flags & codex_meter::kPlanValid)
            ? ai_model::planLabel(packet->plan)
            : "";
  }
  if (currentPage == Page::Settings) {
    renderSettingsHome(batteryPercent, batteryCharging);
    return;
  }
  if (currentPage == Page::Brightness) {
    renderControlPage(true, batteryPercent, batteryCharging);
    return;
  }
  if (currentPage == Page::Volume) {
    renderControlPage(false, batteryPercent, batteryCharging);
    return;
  }
  if (currentPage == Page::Update) {
    renderUpdatePage(batteryPercent, batteryCharging);
    return;
  }

  if (!packet) {
    renderFrame(nullptr, fresh, batteryPercent, batteryCharging,
                receivedAtMs, 0);
    return;
  }

  const uint32_t now = millis();
  updateTotalAnimationTarget(packet->total_tokens, now);
  renderFrame(packet, fresh, batteryPercent, batteryCharging,
              receivedAtMs, sampleTotalAnimation(now));
}

bool tokenAnimationActive() {
  return currentPage == Page::Dashboard && totalAnimationRunning;
}

bool updateAnimationActive() {
  return currentPage == Page::Update && updateDotsVisible();
}

bool statusAnimationActive(const codex_meter::CodexPacket* packet, bool fresh) {
  if (currentPage != Page::Dashboard || isDisplaySleeping ||
      updateOverlayVisible()) return false;
  const auto state = resolveStatus(packet, fresh);
  return !statusTransition.initialized() ||
      state != statusTransition.state() || status_motion::loops(state) ||
      statusTransitionNeedsFrame || statusTransition.active(millis());
}

void renderTokenAnimationFrame() {
  if (!tokenAnimationActive() || !gfx) return;
  constexpr int16_t kRegionX = 20;
  const int16_t kRegionY = skin::layout(skin_format::TotalBaseline) - 65;
  constexpr int16_t kRegionWidth = 440;
  constexpr int16_t kRegionHeight = 76;
  gfx->fillRect(kRegionX, kRegionY, kRegionWidth, kRegionHeight, theme().colors.background);
  drawTokenTotal(sampleTotalAnimation(millis()));
  flushRegion(kRegionX, kRegionY, kRegionWidth, kRegionHeight);
}

void renderUpdateAnimationFrame() {
  if (!updateAnimationActive() || !gfx || isDisplaySleeping) return;
  const int16_t regionY = updateStatusEquals("INSTALLING") ? 145 : 210;
  constexpr int16_t regionX = 70;
  constexpr int16_t regionWidth = 340;
  constexpr int16_t regionHeight = 70;
  gfx->fillRect(regionX, regionY, regionWidth, regionHeight,
                theme().colors.background);
  drawLoopingUpdateLabel();
  flushRegion(regionX, regionY, regionWidth, regionHeight);
}

void renderStatusAnimationFrame(const codex_meter::CodexPacket* packet,
                                bool fresh) {
  if (!gfx || !statusAnimationActive(packet, fresh)) return;

  // Bare labels normally use the 64px tile. Breathing plates redraw the
  // necessary header area when their palette changes, then recapture a clean
  // tile so neither the background nor text AA accumulates between frames.
  const auto state = resolveStatus(packet, fresh);
  const StatusFrameTimer timer(state);
  const uint32_t now = millis();
  const bool settledSame = statusBackground.valid() && state == cachedStatusState &&
      cachedStatusTheme == &theme() && !statusTransitionNeedsFrame &&
      !statusTransition.active(now);
  if (settledSame) {
    const auto icon = skin::sample(cachedIconMode, now - statusLoopStartedMs);
    const auto light = statusLight(state, now - statusLoopStartedMs);
    if (cachedStatusLight.card == light.card && cachedStatusLight.glow == light.glow &&
        cachedStatusBackdrop.samePixels(statusBackdrop(state, icon, light)) &&
        statusBackground.restore(canvas->getFramebuffer(), LCD_WIDTH,
                                 LCD_WIDTH, LCD_HEIGHT)) {
      const auto& tile = statusBackground.rect;
      status_pixel::draw(canvas->getFramebuffer() + tile.y * LCD_WIDTH + tile.x,
                         LCD_WIDTH, icon);
      // The halo overlaps up to 4px of the bare sync label. Redraw only
      // that intersection, never accumulate AA on the untouched text.
      drawText(currentStatusLabel, cachedStatusTextX, cachedStatusBaseline,
                cachedStatusForeground, cachedStatusFont, &tile);
      flushRegion(tile.x, tile.y, tile.width, tile.height);
      return;
    }
  }

  constexpr int16_t kRegionX = 0;
  constexpr int16_t kRegionY = 0;
  const int16_t kRegionWidth = settledSame
      ? min<int16_t>(kStatusRegionWidth, cachedStatusRight + 30) : kStatusRegionWidth;
  constexpr int16_t kRegionHeight = kTodayTokensGlyphTop;
  gfx->fillRect(kRegionX, kRegionY, kRegionWidth, kRegionHeight, theme().colors.background);
  if (!drawStatus(packet, fresh) && packet) drawBrand(packet);
  flushRegion(kRegionX, kRegionY, kRegionWidth, kRegionHeight);
}

StatusRenderStats statusRenderStats() {
  portENTER_CRITICAL(&statusStatsMux);
  const StatusRenderStats result = statusStats;
  portEXIT_CRITICAL(&statusStatsMux);
  return result;
}

void skinChanged() {
  statusBackground.invalidate();
  statusBackplate.invalidate();
  statusTransition = status_motion::Transition{};
  cachedStatusTheme = nullptr;
}

void renderBatteryAnimationFrame(const codex_meter::CodexPacket* packet,
                                 bool fresh, int batteryPercent,
                                 bool batteryCharging) {
  if (!batteryCharging || !gfx || isDisplaySleeping) return;

  // This rectangle is disjoint from the status and token animation regions,
  // allowing all three animations to run in the same frame without erasing
  // or throttling each other.
  constexpr int16_t kRegionX = 370;
  constexpr int16_t kRegionY = 20;
  constexpr int16_t kRegionWidth = 110;
  constexpr int16_t kRegionHeight = 50;
  gfx->fillRect(kRegionX, kRegionY, kRegionWidth, kRegionHeight,
                theme().colors.background);
  drawBattery(batteryPercent, true);
  flushRegion(kRegionX, kRegionY, kRegionWidth, kRegionHeight);
}

}  // namespace ui
