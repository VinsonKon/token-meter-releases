#include "status_label.h"

#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>

namespace {
unsigned checks = 0;
void require(bool value, const char* message) {
  ++checks;
  if (!value) {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}
constexpr int kStride = 480;
constexpr int kRows = 112;
constexpr int kGuard = 32;
constexpr uint16_t kSentinel = 0xA55A;
std::array<uint16_t, kStride * kRows + 2 * kGuard> buffer;
status_label::Backplate plate;

void checkGuards() {
  for (int i = 0; i < kGuard; ++i) {
    require(buffer[i] == kSentinel, "leading guard changed");
    require(buffer[buffer.size() - 1 - i] == kSentinel, "trailing guard changed");
  }
  for (int y = 0; y < kRows; ++y) {
    for (int x = 0; x < kStride; ++x) {
      if (x >= 368 || y >= 98 || x < plate.left() || x >= plate.right() ||
          y < plate.top() || y >= plate.bottom()) {
        require(buffer[kGuard + y * kStride + x] == kSentinel,
                "draw touched outside clipped plate bounds");
      }
    }
  }
}

void drawGuarded(int x, int y, int width, int height, uint16_t background,
                 uint16_t tint, status_label::Look light) {
  require(plate.configure(x, y, width, height), "valid geometry rejected");
  buffer.fill(kSentinel);
  plate.draw(buffer.data() + kGuard, kStride, background, tint, light);
  checkGuards();
  const int cx = x + width / 2;
  const int cy = y + height / 2;
  if (width >= 4 && height >= 4) {
    require(buffer[kGuard + cy * kStride + cx] ==
                status_label::detail::blend(background, tint, light.card),
            "full fill does not have exact requested card alpha");
  }
}
}  // namespace

int main() {
  using namespace status_label;
  require(sizeof(Backplate) <= 65536, "fixed memory exceeds 64KB");
  for (bool approval : {false, true}) {
    const uint32_t period = approval ? 1400u : 1500u;
    require(sample(approval, 0).card == (approval ? 65 : 45), "wrong card minimum");
    require(sample(approval, 0).glow == (approval ? 85 : 60), "wrong glow minimum");
    require(sample(approval, period / 2).card == (approval ? 130 : 105), "wrong card peak");
    require(sample(approval, period / 2).glow == (approval ? 210 : 165), "wrong glow peak");
    for (uint32_t time = 0; time < period; time += 10) {
      const auto light = sample(approval, time);
      const auto repeated = sample(approval, time + period);
      require(light.card == repeated.card && light.glow == repeated.glow, "cycle seam differs");
      float previous = float(light.card);
      for (int step = 0; step <= 3200; ++step) {
        const float distance = -1.0f + step * 0.01f;
        const uint8_t index = detail::encodeDistance(distance);
        const float nominal = detail::nominalAlpha(index, light);
        require(nominal <= previous + 0.0001f, "nominal profile has a bright edge");
        require(nominal <= light.card + 0.0001f, "halo exceeds fill");
        previous = nominal;
      }
      for (int index = 0; index < 256; ++index) {
        for (int position = 0; position < 16; ++position) {
          require(detail::alpha(uint8_t(index), light, position) <= light.card,
                  "dither creates a brighter outline");
        }
      }
    }
  }
  require(plate.configure(82, 24, 274), "approval geometry rejected");
  const uint32_t firstRevision = plate.revision();
  require(plate.configure(82, 24, 274) && plate.revision() == firstRevision,
          "same geometry recomputes mask");
  require(plate.configure(82, 24, 176) && plate.revision() == firstRevision + 1,
          "layout change did not rebuild mask");
  plate.invalidate();
  require(!plate.valid(), "invalidation ignored");
  require(plate.configure(82, 24, 176) && plate.revision() == firstRevision + 2,
          "invalidate did not require rebuild");

  for (uint16_t background : {uint16_t(0), uint16_t(0xFFFF), uint16_t(0x18E3)}) {
    for (uint16_t tint : {uint16_t(0xFB4D), uint16_t(0xFED0), uint16_t(0xFFFF)}) {
      for (uint32_t time : {0u, 350u, 700u, 1399u}) {
        drawGuarded(82, 24, 274, 44, background, tint, sample(true, time));
        for (int y = 24; y < 68; ++y) {
          require(buffer[kGuard + y * kStride + 367] == background,
                  "compressed right halo ends in a visible clipped column");
        }
        drawGuarded(82, 24, 176, 44, background, tint, sample(false, time));
      }
    }
  }
  for (const auto& geometry : {
      std::array<int, 4>{{0, 0, 368, 98}}, {{0, 24, 100, 44}},
      {{267, 0, 100, 44}}, {{367, 97, 1, 1}}, {{0, 0, 1, 1}},
      {{82, 24, 274, 44}}, {{82, 24, 176, 44}}}) {
    drawGuarded(geometry[0], geometry[1], geometry[2], geometry[3],
                0, 0xFB4D, sample(true, 700));
  }
  for (const auto& geometry : {
      std::array<int, 4>{{-1, 24, 10, 44}}, {{0, -1, 10, 44}},
      {{368, 0, 1, 1}}, {{0, 98, 1, 1}}, {{0, 0, 0, 1}},
      {{0, 0, 1, 0}}, {{82, 24, 287, 44}}, {{0, 0, 2147483647, 1}}}) {
    require(!plate.configure(geometry[0], geometry[1], geometry[2], geometry[3]),
            "illegal geometry accepted");
    buffer.fill(kSentinel);
    plate.draw(buffer.data() + kGuard, kStride, 0, 0xFFFF, {255, 255});
    for (uint16_t pixel : buffer) require(pixel == kSentinel, "invalid draw wrote framebuffer");
  }
  require(plate.configure(82, 24, 274), "final configure failed");
  buffer.fill(kSentinel);
  plate.draw(nullptr, kStride, 0, 0xFFFF, {255, 255});
  plate.draw(buffer.data() + kGuard, 367, 0, 0xFFFF, {255, 255});
  for (uint16_t pixel : buffer) require(pixel == kSentinel, "invalid destination was written");
  std::cout << "PASS: " << checks << " checks; Backplate=" << sizeof(Backplate)
            << " bytes. Timing, monotonic no-outline profile, geometry reuse, AA fill, "
               "compressed tail and framebuffer guards passed.\n";
}
