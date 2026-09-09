#pragma once

#include <math.h>
#include <stdint.h>
#include <string.h>

// Fixed-memory text-only R8 backplate. Geometry is rasterized once; animated
// frames only rebuild a small RGB565 palette and copy indexed pixels.
namespace status_label {

constexpr int kRegionWidth = 368;
constexpr int kRegionHeight = 98;
constexpr int kSpread = 30;

struct Look {
  uint8_t card;
  uint8_t glow;
};

inline Look sample(bool approval, uint32_t elapsed) {
  const uint32_t period = approval ? 1400u : 1500u;
  const float wave = 0.5f - 0.5f * cosf(
      6.28318530718f * float(elapsed % period) / float(period));
  const float pulse = wave * wave * (3.0f - 2.0f * wave);
  return approval
      ? Look{uint8_t(65.0f + 65.0f * pulse + 0.5f),
             uint8_t(85.0f + 125.0f * pulse + 0.5f)}
      : Look{uint8_t(45.0f + 60.0f * pulse + 0.5f),
             uint8_t(60.0f + 105.0f * pulse + 0.5f)};
}

namespace detail {

constexpr uint8_t kBayer[16] = {
    0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5};
constexpr float kHaloAtEdge = 0.99845798026f;  // exp(-0.5^2 / (2 * 9^2))

inline float distance(float px, float py, int x, int y, int width, int height, float corner = 8.0f) {
  const float radius = fminf(corner, 0.5f * fminf(float(width), float(height)));
  const float qx = fabsf(px - (float(x) + width * 0.5f)) - (width * 0.5f - radius);
  const float qy = fabsf(py - (float(y) + height * 0.5f)) - (height * 0.5f - radius);
  const float ox = fmaxf(qx, 0.0f);
  const float oy = fmaxf(qy, 0.0f);
  return sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0.0f) - radius;
}

// 0 is clear; 1..239 encode the Gaussian profile; 240..254 encode 4-bit
// edge coverage; 255 is the full fill. The Gaussian remains sigma=9.
// This function is called only when geometry changes, never by draw().
inline uint8_t encodeDistance(float value) {
  if (value <= -0.5f) return 255;
  if (value <= 0.5f) return uint8_t(240 + int((0.5f - value) * 15.0f + 0.5f));
  if (value >= float(kSpread)) return 0;
  return uint8_t(239.0f * expf(-(value * value - 0.25f) / 162.0f) + 0.5f);
}

inline float nominalAlpha(uint8_t index, Look light) {
  if (index == 0) return 0.0f;
  if (index == 255) return float(light.card);
  const float peak = fminf(float(light.glow) * 0.32f, float(light.card) * 0.65f);
  if (index < 240) return peak * (float(index) / 239.0f) * kHaloAtEdge;
  const float coverage = float(index - 240) / 15.0f;
  const float outside = fmaxf(0.0f, 0.5f - coverage);
  // On the AA strip outside<=0.5. This Taylor approximation differs from
  // exp by <0.0000012, well below one 8-bit alpha step, and needs no exp().
  const float halo = peak * (1.0f - outside * outside / 162.0f);
  return coverage * light.card + (1.0f - coverage) * halo;
}

inline uint8_t alpha(uint8_t index, Look light, int bayerPosition) {
  float result = nominalAlpha(index, light);
  if (index > 0 && index < 240) {
    const float bias = (float(kBayer[bayerPosition & 15]) - 7.5f) * 0.5f;
    result += bias * fminf(1.0f, result / 4.0f);
  }
  return uint8_t(fmaxf(0.0f, fminf(float(light.card), result)) + 0.5f);
}

inline uint16_t blend(uint16_t background, uint16_t tint, uint8_t alpha) {
  const unsigned inverse = 255u - alpha;
  const unsigned r = (((background >> 11) & 31u) * inverse +
                       ((tint >> 11) & 31u) * alpha + 127u) / 255u;
  const unsigned g = (((background >> 5) & 63u) * inverse +
                       ((tint >> 5) & 63u) * alpha + 127u) / 255u;
  const unsigned b = ((background & 31u) * inverse +
                       (tint & 31u) * alpha + 127u) / 255u;
  return uint16_t((r << 11) | (g << 5) | b);
}

}  // namespace detail

class Backplate {
 public:
  bool configure(int x, int y, int width, int height = 44, float radius = 8.0f) {
    if (x < 0 || y < 0 || x >= kRegionWidth || y >= kRegionHeight ||
        width <= 0 || height <= 0 || width > kRegionWidth - x ||
        height > kRegionHeight - y || !(radius >= 0.0f && radius <= 22.0f)) {
      invalidate();
      return false;
    }
    if (valid_ && x == x_ && y == y_ && width == width_ && height == height_ && radius == radius_) return true;
    radius_ = radius;
    x_ = x;
    y_ = y;
    width_ = width;
    height_ = height;
    left_ = x > kSpread ? x - kSpread : 0;
    top_ = y > kSpread ? y - kSpread : 0;
    right_ = x + width + kSpread < kRegionWidth ? x + width + kSpread : kRegionWidth;
    bottom_ = y + height + kSpread < kRegionHeight ? y + height + kSpread : kRegionHeight;
    memset(mask_, 0, sizeof(mask_));
    const int rightSpace = kRegionWidth - (x + width);
    for (int py = top_; py < bottom_; ++py) {
      for (int px = left_; px < right_; ++px) {
        float value = detail::distance(px + 0.5f, py + 0.5f, x, y, width, height, radius);
        // Leave the fill/AA geometry untouched. Compress only the exterior
        // right tail into the available header margin, so a plate ending at
        // x=356 fades naturally before the independent battery at x=370.
        if (value > 0.5f && rightSpace > 0 && rightSpace < kSpread && px >= x + width) {
          value = fmaxf(value, (px + 0.5f - (x + width)) * kSpread / rightSpace);
        }
        mask_[py * kRegionWidth + px] = detail::encodeDistance(value);
      }
    }
    valid_ = true;
    ++revision_;
    return true;
  }

  // Replace only the clipped plate+halo bounds, composing from the supplied
  // clean background (not from the last frame). Draw glyph/text afterwards.
  // The caller must clear the union of old/new bounds on a layout change.
  // fb must have at least kRegionHeight rows and stride>=kRegionWidth.
  void draw(uint16_t* fb, int stride, uint16_t background, uint16_t tint, Look light) {
    if (!valid_ || !fb || stride < kRegionWidth) return;
    if (!paletteValid_ || background != background_ || tint != tint_ ||
        light.card != light_.card || light.glow != light_.glow) {
      for (int index = 0; index < 256; ++index) {
        for (int position = 0; position < 16; ++position) {
          palette_[index * 16 + position] = detail::blend(
              background, tint, detail::alpha(uint8_t(index), light, position));
        }
      }
      background_ = background;
      tint_ = tint;
      light_ = light;
      paletteValid_ = true;
    }
    for (int py = top_; py < bottom_; ++py) {
      const uint8_t* row = mask_ + py * kRegionWidth;
      uint16_t* output = fb + py * stride;
      const int phase = (py & 3) * 4;
      for (int px = left_; px < right_; ++px) {
        output[px] = palette_[unsigned(row[px]) * 16u + unsigned(phase + (px & 3))];
      }
    }
  }

  void invalidate() { valid_ = false; }
  bool valid() const { return valid_; }
  int left() const { return left_; }
  int top() const { return top_; }
  int right() const { return right_; }    // Exclusive.
  int bottom() const { return bottom_; }  // Exclusive.
  uint32_t revision() const { return revision_; }

 private:
  uint8_t mask_[kRegionWidth * kRegionHeight] = {};
  uint16_t palette_[256 * 16] = {};
  int x_ = 0, y_ = 0, width_ = 0, height_ = 0;
  float radius_ = 8.0f;
  int left_ = 0, top_ = 0, right_ = 0, bottom_ = 0;
  uint32_t revision_ = 0;
  uint16_t background_ = 0, tint_ = 0;
  Look light_ = {0, 0};
  bool valid_ = false;
  bool paletteValid_ = false;
};

static_assert(sizeof(Backplate) <= 64u * 1024u, "status label exceeds fixed memory budget");

}  // namespace status_label
