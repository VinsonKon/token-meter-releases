#pragma once
#include "skin_format.h"
#include "status_pixel.h"
#include "status_label.h"

// Pure portable sampling, shared by actual firmware and host regression tests.
namespace skin_sampler {
inline status_pixel::Frame sample(const uint8_t* bytes,
                                 const skin_format::Animation& a, uint32_t elapsed) {
  status_pixel::Frame f{};
  if (a.kind == 3) return f; // Pill opacity tracks are not nine-cell image data.
  const uint8_t* ends = bytes + a.offset;
  const uint8_t* pixels = ends + a.count * 4;
  const uint32_t phase = elapsed % a.cycle;
  uint32_t index = 0;
  while (index + 1 < a.count && phase >= skin_format::word(ends + index*4)) ++index;
  const uint32_t start = index ? skin_format::word(ends + (index-1)*4) : 0;
  const uint32_t duration = skin_format::word(ends + index*4) - start;
  float mix = float(phase-start) / duration;
  if (a.smooth) mix = status_pixel::detail::smooth(mix);
  const uint8_t* first = pixels + index * a.stride;
  const uint8_t* next = pixels + ((index+1) % a.count) * a.stride;
  if (a.kind == 0) {
    f.reference = true; f.externalCurrent = first; f.externalNext = next;
    f.referenceMix = uint8_t(mix*255.0f+0.5f);
  } else {
    for (int i = 0; i < 9; ++i) {
      const int cell = a.kind == 2 ? 0 : i;
      f.cells[i] = (first[cell] + (float(next[cell])-first[cell])*mix) / 255.0f;
    }
    f.solidSquare = a.kind == 2;
  }
  const float colorPhase = float(elapsed % a.colorCycle) * a.colorCount / a.colorCycle;
  const uint32_t ci = uint32_t(colorPhase);
  const float t = status_pixel::detail::smooth(colorPhase-ci);
  const uint32_t c0 = a.colors[ci], c1 = a.colors[(ci+1) % a.colorCount];
  auto channel = [&](int shift) { return uint8_t(float((c0 >> shift)&255) +
      (float((c1 >> shift)&255)-float((c0 >> shift)&255))*t+0.5f); };
  f.r = channel(16); f.g = channel(8); f.b = channel(0);
  f.glow = (a.glowMin + (a.glowMax-a.glowMin)*status_pixel::detail::breath(elapsed, a.glowCycle))/255.0f;
  f.exactColor = a.exact;
  return f;
}
inline status_label::Look light(const skin_format::Animation& a, uint32_t elapsed) {
  const float wave = 1.0f - status_pixel::detail::breath(elapsed, a.plateCycle);
  const float p = status_pixel::detail::smooth(wave);
  return {uint8_t(a.cardMin + (a.cardMax-a.cardMin)*p+0.5f),
          uint8_t(a.haloMin + (a.haloMax-a.haloMin)*p+0.5f)};
}
} // namespace skin_sampler
