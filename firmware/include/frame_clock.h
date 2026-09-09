#pragma once
#include <stdint.h>

namespace frame_clock {
constexpr uint32_t kAnimationFps = 60u;
constexpr uint32_t kAnimationFrameUs =
    (1000000u + kAnimationFps - 1u) / kAnimationFps;

// Preserve phase despite polling jitter; skip missed frames rather than
// queuing a burst that could starve BLE. Unsigned subtraction handles wrap.
inline bool due(uint32_t nowUs, uint32_t intervalUs, uint32_t& lastUs) {
  if (intervalUs == 0) return false;
  const uint32_t elapsed = nowUs - lastUs;
  if (elapsed < intervalUs) return false;
  lastUs += (elapsed / intervalUs) * intervalUs;
  return true;
}
}  // namespace frame_clock
