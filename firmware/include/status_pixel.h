#pragma once

#include <math.h>
#include <stdint.h>

#include "thinking_reference.h"
#include "approval_reference.h"
#include "reading_reference.h"

// Portable RGB565 status glyph: the firmware and host preview use this same
// sampler and renderer. Callers provide a freshly restored 64 x 64 background.
namespace status_pixel {

enum class Mode { Thinking, Approval, Stale, Waiting };

constexpr int kSize = 64;
constexpr uint32_t kThinkingColorCycleMs = 12000;
constexpr uint32_t kApprovalCycleMs = approval_reference::kCycleMs;
constexpr uint32_t kApprovalBreathMs = 3200;
constexpr uint32_t kStaleCycleMs = reading_reference::kCycleMs;
constexpr uint8_t kGoldR = 255;
constexpr uint8_t kGoldG = 210;
constexpr uint8_t kGoldB = 128;
static_assert(kSize == thinking_reference::kSize, "reference tile size mismatch");

struct Frame {
  float cells[9];
  uint8_t r;
  uint8_t g;
  uint8_t b;
  float glow;
  bool solidSquare;
  // Selected status accents keep their approved RGB color at full intensity.
  // Other glyphs retain the existing 12% white highlight on their sharp cells.
  bool exactColor;
  bool reference;
  uint16_t referenceFrame;
  uint16_t referenceNextFrame;
  uint8_t referenceMix;
  // Imported skin frame data lives in validated PSRAM, not executable code.
  const uint8_t* externalCurrent = nullptr;
  const uint8_t* externalNext = nullptr;
};

namespace detail {

inline constexpr float clamp01(float value) {
  return value > 0.0f ? (value < 1.0f ? value : 1.0f) : 0.0f;
}

inline constexpr float smooth(float value) {
  const float t = clamp01(value);
  return t * t * (3.0f - 2.0f * t);
}

inline float breath(uint32_t elapsedMs, uint32_t periodMs) {
  constexpr float kTau = 6.28318530718f;
  return 0.5f + 0.5f * cosf(kTau * float(elapsedMs % periodMs) /
                          float(periodMs));
}

inline void fillMask(Frame& frame, uint16_t mask, float brightness) {
  for (int i = 0; i < 9; ++i) {
    frame.cells[i] = (mask & (uint16_t(1) << i)) ? brightness : 0.0f;
  }
}

inline void morph(Frame& frame, const uint16_t* masks, int count,
                  uint32_t elapsedMs, uint32_t stepMs, float brightness) {
  const uint32_t phase = elapsedMs % (stepMs * uint32_t(count));
  const int index = int(phase / stepMs);
  // Hold each recognizable silhouette briefly, then ease into the next one.
  const float t = smooth((float(phase % stepMs) / float(stepMs) - 0.35f) /
                         0.65f);
  const uint16_t from = masks[index];
  const uint16_t to = masks[(index + 1) % count];
  for (int i = 0; i < 9; ++i) {
    const float a = (from & (uint16_t(1) << i)) ? 1.0f : 0.0f;
    const float b = (to & (uint16_t(1) << i)) ? 1.0f : 0.0f;
    frame.cells[i] = (a + (b - a) * t) * brightness;
  }
}

inline uint8_t channel(float value) {
  return uint8_t(value < 0.0f ? 0.0f : (value > 255.0f ? 255.0f : value));
}

inline void blend(uint16_t& dst, uint8_t r, uint8_t g, uint8_t b,
                  float opacity) {
  const int a = int(clamp01(opacity) * 256.0f + 0.5f);
  if (a == 0) return;
  const int inverse = 256 - a;
  const uint16_t red = uint16_t((((dst >> 11) & 31) * inverse +
                                (r >> 3) * a + 128) >> 8);
  const uint16_t green = uint16_t((((dst >> 5) & 63) * inverse +
                                  (g >> 2) * a + 128) >> 8);
  const uint16_t blue = uint16_t(((dst & 31) * inverse +
                                 (b >> 3) * a + 128) >> 8);
  dst = uint16_t((red << 11) | (green << 5) | blue);
}

inline constexpr float haloAxis(float distance) {
  // Compact separable blur: no heap, textures, per-pixel sqrt or exp needed.
  return 1.0f - smooth((distance - 5.3f) / 7.7f);
}

inline constexpr float solidHaloAxis(float distance) {
  // One soft halo around the entire 36px square, not nine cell halos.
  return 1.0f - smooth((distance - 15.5f) / 13.5f);
}

inline void drawSolidSquare(uint16_t* topLeft, int stride, const Frame& frame,
                            float opacity) {
  const float strength = clamp01(frame.cells[4]) * opacity;
  if (!(strength > 0.0f)) return;
  static constexpr float kAxis[32] = {
      solidHaloAxis(0), solidHaloAxis(1), solidHaloAxis(2), solidHaloAxis(3),
      solidHaloAxis(4), solidHaloAxis(5), solidHaloAxis(6), solidHaloAxis(7),
      solidHaloAxis(8), solidHaloAxis(9), solidHaloAxis(10), solidHaloAxis(11),
      solidHaloAxis(12), solidHaloAxis(13), solidHaloAxis(14), solidHaloAxis(15),
      solidHaloAxis(16), solidHaloAxis(17), solidHaloAxis(18), solidHaloAxis(19),
      solidHaloAxis(20), solidHaloAxis(21), solidHaloAxis(22), solidHaloAxis(23),
      solidHaloAxis(24), solidHaloAxis(25), solidHaloAxis(26), solidHaloAxis(27),
      solidHaloAxis(28), solidHaloAxis(29), solidHaloAxis(30), solidHaloAxis(31)};
  const float halo = strength * clamp01(frame.glow) * 0.36f;
  for (int y = 0; y < kSize; ++y) {
    const int dy = y > 31 ? y - 31 : 31 - y;
    if (dy >= 32 || kAxis[dy] <= 0.0f) continue;
    for (int x = 0; x < kSize; ++x) {
      const int dx = x > 31 ? x - 31 : 31 - x;
      if (dx < 32) blend(topLeft[y * stride + x], frame.r, frame.g, frame.b,
                         kAxis[dx] * kAxis[dy] * halo);
    }
  }
  const uint8_t red = frame.exactColor ? frame.r : channel(frame.r + (255.0f - frame.r) * 0.12f);
  const uint8_t green = frame.exactColor ? frame.g : channel(frame.g + (255.0f - frame.g) * 0.12f);
  const uint8_t blue = frame.exactColor ? frame.b : channel(frame.b + (255.0f - frame.b) * 0.12f);
  for (int y = 13; y <= 49; ++y) {
    const float vertical = clamp01(18.0f - float(y > 31 ? y - 31 : 31 - y));
    for (int x = 13; x <= 49; ++x) {
      const float horizontal = clamp01(18.0f - float(x > 31 ? x - 31 : 31 - x));
      blend(topLeft[y * stride + x], red, green, blue,
            horizontal * vertical * strength);
    }
  }
}

inline void thinkingColor(Frame& frame, uint32_t elapsedMs) {
  static const uint8_t kColors[3][3] = {
      {kGoldR, kGoldG, kGoldB}, {120, 232, 158}, {106, 184, 255}};
  constexpr uint32_t kStepMs = kThinkingColorCycleMs / 3;
  const uint32_t phase = elapsedMs % kThinkingColorCycleMs;
  const int index = int(phase / kStepMs);
  const int next = (index + 1) % 3;
  const float t = smooth(float(phase % kStepMs) / float(kStepMs));
  frame.r = channel(kColors[index][0] + (float(kColors[next][0]) - kColors[index][0]) * t + 0.5f);
  frame.g = channel(kColors[index][1] + (float(kColors[next][1]) - kColors[index][1]) * t + 0.5f);
  frame.b = channel(kColors[index][2] + (float(kColors[next][2]) - kColors[index][2]) * t + 0.5f);
}

// One small lookup table is rebuilt for the tint/entrance opacity of each
// rendered frame. Pixel work then contains no floating-point conversions.
struct ReferenceBlend {
  uint16_t red;
  uint16_t green;
  uint16_t blue;
  uint16_t inverse;
};

inline uint16_t tintLightColor(uint8_t light, const Frame& frame) {
  const uint16_t red = (uint16_t(light) * frame.r + 127u) / 255u;
  const uint16_t green = (uint16_t(light) * frame.g + 127u) / 255u;
  const uint16_t blue = (uint16_t(light) * frame.b + 127u) / 255u;
  return uint16_t(((red & 248u) << 8) | ((green & 252u) << 3) | (blue >> 3));
}

inline ReferenceBlend referenceBlendEntry(uint16_t source,
                                          uint16_t opacity256) {
  const uint32_t red = (source >> 11) & 31u;
  const uint32_t green = (source >> 5) & 63u;
  const uint32_t blue = source & 31u;
  const uint16_t redAlpha = uint16_t((red * opacity256 + 15u) / 31u);
  const uint16_t greenAlpha = uint16_t((green * opacity256 + 31u) / 63u);
  const uint16_t blueAlpha = uint16_t((blue * opacity256 + 15u) / 31u);
  const uint16_t alpha = redAlpha > greenAlpha
      ? (redAlpha > blueAlpha ? redAlpha : blueAlpha)
      : (greenAlpha > blueAlpha ? greenAlpha : blueAlpha);
  // Preserve the old premultiplied-light semantics: alpha comes from the
  // tinted RGB565 peak, not from the untinted light. Values use 8 fraction bits.
  return {
      uint16_t((red * 255u * opacity256 + 15u) / 31u),
      uint16_t((green * 255u * opacity256 + 31u) / 63u),
      uint16_t((blue * 255u * opacity256 + 15u) / 31u),
      uint16_t(256u - alpha)};
}

inline void blendReferenceInteger(uint16_t& dst, const ReferenceBlend& source) {
  const uint32_t red = ((((dst >> 11) & 31u) * 255u + 15u) / 31u);
  const uint32_t green = ((((dst >> 5) & 63u) * 255u + 31u) / 63u);
  const uint32_t blue = (((dst & 31u) * 255u + 15u) / 31u);
  const uint16_t outRed = uint16_t((red * source.inverse + source.red + 128u) >> 8);
  const uint16_t outGreen = uint16_t((green * source.inverse + source.green + 128u) >> 8);
  const uint16_t outBlue = uint16_t((blue * source.inverse + source.blue + 128u) >> 8);
  dst = uint16_t(((outRed & 248u) << 8) | ((outGreen & 252u) << 3) | (outBlue >> 3));
}

}  // namespace detail

inline Frame sample(Mode mode, uint32_t elapsedMs) {
  Frame frame = {};
  if (mode == Mode::Thinking) {
    const uint32_t phase = elapsedMs % thinking_reference::kDurationMs;
    uint16_t index = 0;
    while (index + 1 < thinking_reference::kFrameCount &&
           phase >= thinking_reference::kFrameEnds[index]) ++index;
    frame.reference = true;
    frame.referenceFrame = index;
    frame.referenceNextFrame = (index + 1) % thinking_reference::kFrameCount;
    const uint32_t start = index ? thinking_reference::kFrameEnds[index - 1] : 0;
    const uint32_t duration = thinking_reference::kFrameEnds[index] - start;
    frame.referenceMix = uint8_t(((phase - start) * 255u + duration / 2u) / duration);
    // Do not derive color phase from the shape loop: the 12s color
    // loop must remain continuous when the reference goes back to frame 0.
    detail::thinkingColor(frame, elapsedMs);
    frame.glow = 0.55f;
    return frame;
  }
  if (mode == Mode::Waiting) {
    static const uint8_t kRing[] = {0, 1, 2, 5, 8, 7, 6, 3};
    detail::fillMask(frame, 0x1EF, 0.30f);
    const uint32_t phase = elapsedMs % 3200;
    const int index = int(phase / 400);
    const float t = detail::smooth(float(phase % 400) / 400.0f);
    frame.cells[kRing[index]] += 0.65f * (1.0f - t);
    frame.cells[kRing[(index + 1) % 8]] += 0.65f * t;
    frame.r = 144;
    frame.g = 187;
    frame.b = 227;
    frame.glow = 0.40f + 0.20f * detail::breath(elapsedMs, 3200);
    return frame;
  }
  if (mode == Mode::Stale) {
    // Reading file in the requested 11.7-14.9s clip is one fixed solid
    // square breathing almost to black, not a rotating or segmented glyph.
    const uint32_t phase = elapsedMs % kStaleCycleMs;
    const int index = int(phase / reading_reference::kStepMs);
    const int next = (index + 1) % reading_reference::kSampleCount;
    const float t = float(phase % reading_reference::kStepMs) /
                    float(reading_reference::kStepMs);
    const float a = reading_reference::kLightSamples[index];
    const float b = reading_reference::kLightSamples[next];
    detail::fillMask(frame, 0x1FF, (a + (b - a) * t) / 255.0f);
    frame.solidSquare = true;
    frame.exactColor = true;
    frame.r = 230;
    frame.g = 172;
    frame.b = 56;
    // The body and halo share the same pulse and #E6AC38 accent. The renderer
    // keeps the persistent label's original gold separate from this icon tint.
    frame.glow = 0.64f;
    return frame;
  }
  if (mode == Mode::Approval) {
    // The requested video's 14.9-18.3s section uses three phase-shifted
    // brightness tracks, not four held masks. Reproduce its 700ms movement
    // with tiny measured samples, interpolated at the existing display rate.
    const uint32_t phase = elapsedMs % kApprovalCycleMs;
    const int index = int(phase / approval_reference::kStepMs);
    const int next = (index + 1) % approval_reference::kSampleCount;
    const float t = float(phase % approval_reference::kStepMs) /
                    float(approval_reference::kStepMs);
    float groups[3];
    for (int group = 0; group < 3; ++group) {
      const float a = approval_reference::kSamples[index][group];
      const float b = approval_reference::kSamples[next][group];
      groups[group] = (a + (b - a) * t) / 255.0f;
    }
    for (int cell = 0; cell < 9; ++cell) {
      frame.cells[cell] = groups[cell == 4 ? 2 : (cell % 2 == 0 ? 0 : 1)];
    }
    // Keep the already-approved slow pill/glow breath independent of the
    // faster shape. This also preserves the settled header-cache cadence.
    const float pulse = detail::breath(elapsedMs, kApprovalBreathMs);
    frame.exactColor = true;
    frame.r = 255;
    frame.g = 107;
    frame.b = 107;
    frame.glow = 0.44f + 0.22f * pulse;
    return frame;
  }

  return frame;
}

inline void draw(uint16_t* topLeft, int stride, const Frame& frame,
                 float opacity = 1.0f) {
  if (topLeft == nullptr || stride < kSize || !(opacity > 0.0f)) return;
  opacity = detail::clamp01(opacity);
  if (frame.solidSquare) {
    detail::drawSolidSquare(topLeft, stride, frame, opacity);
    return;
  }
  if (frame.reference) {
    if (!frame.externalCurrent && (frame.referenceFrame >= thinking_reference::kFrameCount ||
        frame.referenceNextFrame >= thinking_reference::kFrameCount)) return;
    const uint16_t opacity256 = uint16_t(opacity * 256.0f + 0.5f);
    if (opacity256 == 0) return;
    // Direct frame-major bytes avoid per-run dispatch and permit smooth
    // interpolation, including the last-to-first seam, at the display cadence.
    // The table supports any restored background, including pill/glow edges.
    detail::ReferenceBlend palette[256];
    for (int index = 0; index < 256; ++index) {
      palette[index] = detail::referenceBlendEntry(
          detail::tintLightColor(uint8_t(index), frame), opacity256);
    }
    const uint8_t* current = frame.externalCurrent ? frame.externalCurrent : thinking_reference::kLightFrames +
        uint32_t(frame.referenceFrame) * thinking_reference::kFramePixels;
    const uint8_t* next = frame.externalNext ? frame.externalNext : thinking_reference::kLightFrames +
        uint32_t(frame.referenceNextFrame) * thinking_reference::kFramePixels;
    const uint16_t mix = frame.referenceMix;
    const uint16_t inverseMix = 255u - mix;
    for (int y = 0; y < kSize; ++y) {
      uint16_t* destination = topLeft + y * stride;
      for (int x = 0; x < kSize; ++x, ++current, ++next) {
        const uint8_t light = uint8_t(
            (uint16_t(*current) * inverseMix + uint16_t(*next) * mix + 127u) / 255u);
        if (light != 0) detail::blendReferenceInteger(destination[x], palette[light]);
      }
    }
    return;
  }
  const float glow = detail::clamp01(frame.glow);
  static constexpr float kHaloAxis[14] = {
      detail::haloAxis(0), detail::haloAxis(1), detail::haloAxis(2),
      detail::haloAxis(3), detail::haloAxis(4), detail::haloAxis(5),
      detail::haloAxis(6), detail::haloAxis(7), detail::haloAxis(8),
      detail::haloAxis(9), detail::haloAxis(10), detail::haloAxis(11),
      detail::haloAxis(12), detail::haloAxis(13)};

  // Nine 12 px cells form a 36 px body inside a 64 px halo tile.
  // Draw every halo before the sharp cells to preserve clean pixel edges.
  for (int cell = 0; cell < 9; ++cell) {
    const float strength = detail::clamp01(frame.cells[cell]) * opacity;
    if (strength <= 0.0f || glow <= 0.0f) continue;
    const int cx = 19 + (cell % 3) * 12;
    const int cy = 19 + (cell / 3) * 12;
    for (int y = cy - 13; y <= cy + 13; ++y) {
      const float vertical = kHaloAxis[y > cy ? y - cy : cy - y];
      if (vertical <= 0.0f) continue;
      for (int x = cx - 13; x <= cx + 13; ++x) {
        const float horizontal = kHaloAxis[x > cx ? x - cx : cx - x];
        const float alpha = horizontal * vertical * strength * glow * 0.24f;
        detail::blend(topLeft[y * stride + x], frame.r, frame.g, frame.b, alpha);
      }
    }
  }

  const uint8_t red = frame.exactColor ? frame.r : detail::channel(frame.r + (255.0f - frame.r) * 0.12f);
  const uint8_t green = frame.exactColor ? frame.g : detail::channel(frame.g + (255.0f - frame.g) * 0.12f);
  const uint8_t blue = frame.exactColor ? frame.b : detail::channel(frame.b + (255.0f - frame.b) * 0.12f);
  for (int cell = 0; cell < 9; ++cell) {
    const float strength = detail::clamp01(frame.cells[cell]) * opacity;
    if (strength <= 0.0f) continue;
    const int cx = 19 + (cell % 3) * 12;
    const int cy = 19 + (cell / 3) * 12;
    for (int y = cy - 6; y <= cy + 6; ++y) {
      const float vertical = detail::clamp01(6.0f - float(y > cy ? y - cy : cy - y));
      for (int x = cx - 6; x <= cx + 6; ++x) {
        const float horizontal = detail::clamp01(6.0f - float(x > cx ? x - cx : cx - x));
        detail::blend(topLeft[y * stride + x], red, green, blue,
                      horizontal * vertical * strength);
      }
    }
  }
}

}  // namespace status_pixel
