#pragma once
#include "skin_format.h"

// Shared production/host renderer. Geometry and six opacity tracks come from
// the package, not a skin-id branch. Text remains firmware-owned for safety.
namespace skin_pill {
inline uint16_t rgb565(uint32_t rgb) {
  return uint16_t(((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb >> 3) & 31));
}
struct Frame { uint8_t values[6]; };
inline Frame sample(const uint8_t* bytes, const skin_format::Animation& a, uint32_t elapsed) {
  const uint8_t* ends = bytes + a.offset;
  const uint32_t phase = elapsed % a.cycle;
  uint32_t index = 0;
  while (index + 1 < a.count && phase >= skin_format::word(ends + index*4)) ++index;
  const uint32_t start = index ? skin_format::word(ends + (index-1)*4) : 0;
  float mix = a.smooth == 2 ? 0.0f : float(phase-start) / (skin_format::word(ends + index*4)-start);
  if (a.smooth == 1) mix = mix*mix*(3.0f-2.0f*mix);
  const uint8_t* pixels = ends + a.count*4;
  const uint8_t* first = pixels + index*6;
  const uint8_t* next = pixels + ((index+1)%a.count)*6;
  Frame f{};
  for (int i=0; i<6; ++i) f.values[i] = uint8_t(first[i] + (float(next[i])-first[i])*mix + 0.5f);
  return f;
}
template <class Backend>
inline void draw(const skin_format::Header* h, unsigned state, uint32_t elapsed, Backend& b) {
  if (!h || h->schema != 2 || state >= 4) return;
  const auto& p = skin_format::pills(h)[state];
  const auto& a = h->animations[state];
  const auto f = sample(reinterpret_cast<const uint8_t*>(h), a, elapsed);
  const char* labels[] = {"THINKING", "NEED APPROVAL", "DATA STALE", "WAITING FOR SYNC"};
  const char* label = labels[state];
  const int wordWidth = b.width(label), dotWidth = b.width(".");
  const int fullWidth = wordWidth + (p.dots ? 3*dotWidth : 0);
  const int textX = p.x + (p.textMode == 2 ? int(p.textInset) : (int(p.width)-fullWidth)/2);
  const uint16_t background = rgb565(h->colors[0]), ink = rgb565(a.textColor);
  const uint16_t foreground = b.blend(background, ink, f.values[1]);
  b.glow(p.x, p.y, p.width, p.height, rgb565(p.glowColor), f.values[2]);
  b.rect(p.x, p.y, p.width, p.height, float(p.radius), b.blend(background, rgb565(a.colors[0]), f.values[0]));
  if (p.dotSize) b.rect(p.x+p.dotX, p.y+p.dotY, p.dotSize, p.dotSize, float(p.dotSize)/2.0f, foreground);
  if (p.textMode == 1) b.center(label, p.x, p.y, p.width, p.height, foreground);
  else b.text(label, textX, p.y+p.baseline, foreground);
  if (p.dots) for (int i=0; i<3; ++i)
    b.text(".", textX+wordWidth+i*dotWidth, p.y+p.baseline, b.blend(background, ink, f.values[3+i]));
}
} // namespace skin_pill
