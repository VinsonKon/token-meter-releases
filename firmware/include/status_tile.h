#pragma once

#include <stdint.h>
#include <string.h>

// The settled status header only needs a small moving tile. Keep its clean
// background (without the previous glyph or text) so no light/AA can accumulate.
namespace status_tile {

constexpr int kSize = 64;

struct Rect {
  int x = 0;
  int y = 0;
  int width = kSize;
  int height = kSize;
  bool contains(int px, int py) const {
    return px >= x && px < x + width && py >= y && py < y + height;
  }
};

class Background {
 public:
  Rect rect;
  bool valid() const { return valid_; }
  void invalidate() { valid_ = false; }
  bool capture(const uint16_t* source, int stride, int width, int height,
               int x, int y) {
    valid_ = false;
    if (!source || stride < width || x < 0 || y < 0 ||
        width < kSize || height < kSize || x > width - kSize ||
        y > height - kSize) return false;
    rect.x = x;
    rect.y = y;
    rect.width = kSize;
    rect.height = kSize;
    for (int row = 0; row < kSize; ++row) {
      memcpy(pixels_ + row * kSize, source + (y + row) * stride + x,
             kSize * sizeof(uint16_t));
    }
    valid_ = true;
    return true;
  }
  bool restore(uint16_t* destination, int stride, int width, int height) const {
    if (!valid_ || !destination || stride < width || rect.x < 0 || rect.y < 0 ||
        rect.width != kSize || rect.height != kSize ||
        width < kSize || height < kSize || rect.x > width - kSize ||
        rect.y > height - kSize) return false;
    for (int row = 0; row < kSize; ++row) {
      memcpy(destination + (rect.y + row) * stride + rect.x,
             pixels_ + row * kSize, kSize * sizeof(uint16_t));
    }
    return true;
  }

 private:
  bool valid_ = false;
  uint16_t pixels_[kSize * kSize] = {};
};

inline uint16_t blend(uint16_t background, uint16_t foreground, unsigned alpha) {
  const unsigned inverse = 255 - alpha;
  const unsigned r = (((background >> 11) & 31) * inverse +
                      ((foreground >> 11) & 31) * alpha + 127) / 255;
  const unsigned g = (((background >> 5) & 63) * inverse +
                      ((foreground >> 5) & 63) * alpha + 127) / 255;
  const unsigned b = ((background & 31) * inverse +
                      (foreground & 31) * alpha + 127) / 255;
  return uint16_t((r << 11) | (g << 5) | b);
}

struct Backdrop {
  uint16_t background = 0;
  uint16_t tint = 0;
  uint16_t card = 0;
  uint8_t edge = 0;

  bool samePixels(const Backdrop& other) const {
    if (background != other.background || card != other.card ||
        edge != other.edge) return false;
    if (tint == other.tint) return true;
    // Glow masks produce alpha in [0, edge], with ordered dither adding at
    // most 4. Comparing every possible RGB565 result is conservative and
    // exact: reuse only while the visible background is genuinely unchanged.
    // This preserves the full color cycle and approval's pill breath.
    const unsigned maximum = unsigned(edge) + 4u < 255u ? unsigned(edge) + 4u : 255u;
    for (unsigned alpha = 0; alpha <= maximum; ++alpha) {
      if (blend(background, tint, alpha) !=
          blend(background, other.tint, alpha)) return false;
    }
    return true;
  }
};

}  // namespace status_tile
