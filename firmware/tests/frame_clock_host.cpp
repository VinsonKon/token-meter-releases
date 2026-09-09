#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "frame_clock.h"

static int checks = 0;
static void check(bool result, const char* label) {
  ++checks;
  if (!result) { fprintf(stderr, "FAIL: %s\n", label); exit(1); }
}
int main() {
  const uint32_t interval = frame_clock::kAnimationFrameUs;
  check(interval == 16667u, "60 FPS microsecond budget");
  uint32_t last = 0;
  check(!frame_clock::due(interval - 1, interval, last), "not early");
  check(last == 0, "early check preserves schedule");
  check(frame_clock::due(interval, interval, last), "exact boundary");
  check(!frame_clock::due(interval, interval, last), "no duplicate frame");
  last = 0;
  uint32_t count = 0;
  for (uint32_t now = 0; now <= 100000000u; now += 1000u) {
    if (frame_clock::due(now, interval, last)) ++count;
    check(now - last < interval, "polling jitter never accumulates");
  }
  check(count == 5999u, "1ms polling stays at 60 FPS, not 58.8 FPS");
  last = 0;
  const uint32_t late = interval * 20u + 1000u;
  check(frame_clock::due(late, interval, last), "late frame allowed");
  check(last == interval * 20u, "missed frames skipped");
  check(!frame_clock::due(late, interval, last), "no catch-up burst");
  check(!frame_clock::due(late + 1000u, interval, last), "wait for next slot");
  last = UINT32_MAX - 999u;
  const uint32_t wrapped = last + interval;
  check(!frame_clock::due(wrapped - 1, interval, last), "wrap not early");
  check(frame_clock::due(wrapped, interval, last), "micros wrap boundary");
  check(last == wrapped, "wrap schedule advances");
  last = 123u;
  check(!frame_clock::due(500u, 0, last), "zero interval safe");
  check(last == 123u, "invalid interval does not change schedule");
  check(!frame_clock::due(last + 9999999u, 10000000u, last), "idle 10s not early");
  check(frame_clock::due(last + 10000000u, 10000000u, last), "idle interval kept");
  last = 30000000u;
  check(!frame_clock::due(last, interval, last), "full render resets baseline");
  check(frame_clock::due(last + interval, interval, last), "resume animation");
  printf("Frame clock: %d checks passed\n", checks);
}
