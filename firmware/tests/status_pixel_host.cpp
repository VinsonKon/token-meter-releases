#include "status_pixel.h"
#include "status_motion.h"
#include "status_tile.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

namespace {
using status_pixel::Frame;
using status_pixel::Mode;
constexpr int kIcon = status_pixel::kSize;
constexpr int kWidth = 832;
constexpr int kHeight = 400;
int checks = 0;

void require(bool condition, const char* message) {
  ++checks;
  if (!condition) {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}

int red(uint16_t color) { return ((color >> 11) & 31) * 255 / 31; }
int green(uint16_t color) { return ((color >> 5) & 63) * 255 / 63; }
int blue(uint16_t color) { return (color & 31) * 255 / 31; }

bool equalFrame(const Frame& left, const Frame& right) {
  if (left.solidSquare != right.solidSquare) return false;
  if (left.exactColor != right.exactColor) return false;
  if (left.reference != right.reference || left.referenceFrame != right.referenceFrame) return false;
  if (left.referenceNextFrame != right.referenceNextFrame || left.referenceMix != right.referenceMix) return false;
  if (left.r != right.r || left.g != right.g || left.b != right.b || left.glow != right.glow) return false;
  for (int i = 0; i < 9; ++i) if (left.cells[i] != right.cells[i]) return false;
  return true;
}

bool equalColor(const Frame& left, const Frame& right) {
  return left.r == right.r && left.g == right.g && left.b == right.b;
}

uint16_t expectedReferencePixel(uint8_t light, const Frame& tint,
                                float opacity = 1.0f, uint16_t background = 0) {
  // Independent double-precision reference for the premultiplied-light
  // contract. The firmware's integer path may differ by one RGB565 step.
  if (light == 0 || opacity <= 0.0f) return background;
  const double strength = double(light) / 255.0;
  const int packedR = int(std::floor(double(tint.r) * strength + 0.5)) >> 3;
  const int packedG = int(std::floor(double(tint.g) * strength + 0.5)) >> 2;
  const int packedB = int(std::floor(double(tint.b) * strength + 0.5)) >> 3;
  const double r = packedR * 255.0 / 31.0;
  const double g = packedG * 255.0 / 63.0;
  const double b = packedB * 255.0 / 31.0;
  const double alpha = std::max({r, g, b}) * double(opacity) / 255.0;
  const int resultR = int(std::floor(((background >> 11) & 31) * 255.0 / 31.0 * (1.0 - alpha) + r * opacity + 0.5));
  const int resultG = int(std::floor(((background >> 5) & 63) * 255.0 / 63.0 * (1.0 - alpha) + g * opacity + 0.5));
  const int resultB = int(std::floor((background & 31) * 255.0 / 31.0 * (1.0 - alpha) + b * opacity + 0.5));
  return uint16_t(((std::min(resultR, 255) >> 3) << 11) | ((std::min(resultG, 255) >> 2) << 5) | (std::min(resultB, 255) >> 3));
}

bool withinOneRgb565Step(uint16_t actual, uint16_t expected) {
  return std::abs(int((actual >> 11) & 31) - int((expected >> 11) & 31)) <= 1 &&
         std::abs(int((actual >> 5) & 63) - int((expected >> 5) & 63)) <= 1 &&
         std::abs(int(actual & 31) - int(expected & 31)) <= 1;
}

std::array<uint16_t, kIcon * kIcon> raster(const Frame& frame, float opacity = 1.0f, uint16_t background = 0) {
  std::array<uint16_t, kIcon * kIcon> pixels;
  pixels.fill(background);
  status_pixel::draw(pixels.data(), kIcon, frame, opacity);
  return pixels;
}

uint16_t expectedAccentBlend(uint16_t background, int r, int g, int b, double opacity) {
  // Independent packed-channel oracle: the display uses RGB565, so an exact
  // requested accent means its native 5/6/5 quantization, not a whitened core.
  const int alpha = int(std::floor(std::max(0.0, std::min(1.0, opacity)) * 256.0 + 0.5));
  const int source[] = {r >> 3, g >> 2, b >> 3};
  const int destination[] = {(background >> 11) & 31, (background >> 5) & 63, background & 31};
  int result[3];
  for (int channel = 0; channel < 3; ++channel) {
    result[channel] = int(std::floor((destination[channel] * (256 - alpha) + source[channel] * alpha) / 256.0 + 0.5));
  }
  return uint16_t((result[0] << 11) | (result[1] << 5) | result[2]);
}

void testExactStatusAccents() {
  require(!Frame{}.exactColor, "default frame unexpectedly enables exact accent coloring");
  for (Mode mode : {Mode::Thinking, Mode::Approval, Mode::Stale, Mode::Waiting}) {
    for (uint32_t time : {0u, 337u, 699u, 1199u, 1500u, 4000u, 12000u}) {
      require(status_pixel::sample(mode, time).exactColor == (mode == Mode::Approval || mode == Mode::Stale), "exact accent mode leaks into an unchanged status");
    }
  }
  for (Mode mode : {Mode::Approval, Mode::Stale, Mode::Waiting}) {
    Frame frame = status_pixel::sample(mode, 0);
    const bool exact = mode != Mode::Waiting;
    const int requestedR = mode == Mode::Approval ? 255 : mode == Mode::Stale ? 230 : 144;
    const int requestedG = mode == Mode::Approval ? 107 : mode == Mode::Stale ? 172 : 187;
    const int requestedB = mode == Mode::Approval ? 107 : mode == Mode::Stale ? 56 : 227;
    require(frame.r == requestedR && frame.g == requestedG && frame.b == requestedB, "status accent changed from the requested RGB color");
    const int bodyR = exact ? requestedR : int(requestedR + (255 - requestedR) * 0.12);
    const int bodyG = exact ? requestedG : int(requestedG + (255 - requestedG) * 0.12);
    const int bodyB = exact ? requestedB : int(requestedB + (255 - requestedB) * 0.12);
    const double haloFactor = mode == Mode::Stale ? 0.36 : 0.24;
    const double haloCoordinate = mode == Mode::Stale ? (22.0 - 15.5) / 13.5 : (10.0 - 5.3) / 7.7;
    const double haloCoverage = 1.0 - haloCoordinate * haloCoordinate * (3.0 - 2.0 * haloCoordinate);
    const int haloX = mode == Mode::Stale ? 9 : 41;
    for (float& cell : frame.cells) cell = 0.0f;
    for (float strength : {0.0f, 0.25f, 0.5f, 1.0f}) {
      frame.cells[4] = strength;
      for (float opacity : {0.0f, 0.125f, 0.5f, 1.0f}) {
        for (uint16_t background : {uint16_t(0), uint16_t(0xffff), uint16_t(0x18c6), uint16_t(0xbda7)}) {
          const auto pixels = raster(frame, opacity, background);
          const double level = double(strength) * opacity;
          const uint16_t base = expectedAccentBlend(background, requestedR, requestedG, requestedB, level * frame.glow * haloFactor);
          const uint16_t expectedBody = expectedAccentBlend(base, bodyR, bodyG, bodyB, level);
          const uint16_t expectedHalo = expectedAccentBlend(background, requestedR, requestedG, requestedB, level * frame.glow * haloFactor * haloCoverage);
          require(pixels[31 * kIcon + 31] == expectedBody, "status body does not blend its exact accent (or unchanged waiting highlight) with the actual background");
          require(pixels[31 * kIcon + haloX] == expectedHalo, "status halo is not drawn directly in the same unwhitened accent");
          if (exact && strength == 1.0f && opacity == 1.0f) {
            const uint16_t target = uint16_t(((requestedR >> 3) << 11) | ((requestedG >> 2) << 5) | (requestedB >> 3));
            require(pixels[31 * kIcon + 31] == target, "full-intensity status body differs from its requested RGB565 color");
          }
        }
      }
    }
    if (exact) {
      frame.cells[4] = 1.0f;
      const auto direct = raster(frame);
      frame.exactColor = false;
      require(direct != raster(frame), "exact accent flag does not remove the legacy 12-percent white highlight");
    }
  }
  std::cout << "Exact accents: Approval #FF6B6B, Stale #E6AC38; full RGB565 body, same-color halo, background/opacity fidelity; Waiting highlight unchanged.\n";
}

void testBoundedDrawing() {
  constexpr int stride = kIcon + 13;
  constexpr int rows = kIcon + 6;
  constexpr int originRow = 3;
  constexpr int originColumn = 5;
  constexpr uint16_t sentinel = 0x5a2d;
  for (Mode mode : {Mode::Thinking, Mode::Approval, Mode::Stale, Mode::Waiting}) {
    std::vector<uint32_t> times = {0u, 777u, 3199u, 5759u, 11519u, 4294967295u};
    if (mode == Mode::Thinking) {
      for (int i = 0; i + 1 < thinking_reference::kFrameCount; ++i) times.push_back(thinking_reference::kFrameEnds[i]);
    }
    for (uint32_t time : times) {
      std::array<uint16_t, stride * rows> pixels;
      pixels.fill(sentinel);
      status_pixel::draw(pixels.data() + originRow * stride + originColumn, stride, status_pixel::sample(mode, time));
      for (int y = 0; y < rows; ++y) {
        for (int x = 0; x < stride; ++x) {
          const bool inside = y >= originRow && y < originRow + kIcon && x >= originColumn && x < originColumn + kIcon;
          if (!inside) require(pixels[y * stride + x] == sentinel, "draw wrote outside its 64x64 destination rectangle");
        }
      }
      pixels.fill(sentinel);
      status_pixel::draw(pixels.data() + originRow * stride + originColumn, stride, status_pixel::sample(mode, time), 0.0f);
      for (uint16_t pixel : pixels) require(pixel == sentinel, "zero opacity changed the background");
    }
  }
}

void testAnimationContract() {
  float approvalSlopePerMs = 0.0f;
  for (int sample = 0; sample < approval_reference::kSampleCount; ++sample) {
    const int next = (sample + 1) % approval_reference::kSampleCount;
    for (int group = 0; group < 3; ++group) approvalSlopePerMs = std::max(approvalSlopePerMs, float(std::abs(int(approval_reference::kSamples[next][group]) - int(approval_reference::kSamples[sample][group]))) / (255.0f * approval_reference::kStepMs));
  }
  float readingSlopePerMs = 0.0f;
  for (int sample = 0; sample < reading_reference::kSampleCount; ++sample) {
    const int next = (sample + 1) % reading_reference::kSampleCount;
    readingSlopePerMs = std::max(readingSlopePerMs, float(std::abs(int(reading_reference::kLightSamples[next]) - int(reading_reference::kLightSamples[sample]))) / (255.0f * reading_reference::kStepMs));
  }
  for (Mode mode : {Mode::Approval, Mode::Stale, Mode::Waiting}) {
    Frame previous = status_pixel::sample(mode, 0);
    bool changed = false;
    bool rasterChanged = false;
    bool highlighted[9] = {};
    const auto initialRaster = raster(previous);
    for (uint32_t time = 0; time <= 12800; time += 40) {
      const Frame frame = status_pixel::sample(mode, time);
      require(!frame.reference, "a non-thinking state uses the thinking reference animation");
      require(frame.solidSquare == (mode == Mode::Stale), "solid-square drawing is selected for the wrong status");
      require(frame.exactColor == (mode == Mode::Approval || mode == Mode::Stale), "exact accent coloring is selected for the wrong status");
      for (float cell : frame.cells) require(std::isfinite(cell) && cell >= 0.0f && cell <= 1.0f, "cell intensity outside [0,1]");
      require(std::isfinite(frame.glow) && frame.glow >= 0.0f && frame.glow <= 1.0f, "glow intensity outside [0,1]");
      const float stepLimit = mode == Mode::Approval ? approvalSlopePerMs * 40.0f + 0.000001f : mode == Mode::Stale ? readingSlopePerMs * 40.0f + 0.000001f : 0.21f;
      for (int i = 0; i < 9; ++i) require(std::fabs(frame.cells[i] - previous.cells[i]) <= stepLimit, "shape moves faster than its permitted source-curve slope between adjacent frames");
      require(std::abs(int(frame.r) - int(previous.r)) <= 4 && std::abs(int(frame.g) - int(previous.g)) <= 4 && std::abs(int(frame.b) - int(previous.b)) <= 4, "palette jumps between adjacent 25 FPS frames");
      changed = changed || !equalFrame(frame, previous);
      const auto pixels = raster(frame);
      rasterChanged = rasterChanged || pixels != initialRaster;
      if (mode == Mode::Approval) {
        require(frame.r > frame.g && frame.r > frame.b, "approval palette stopped being warm red");
        int peakRed = 0;
        for (uint16_t pixel : pixels) peakRed = std::max(peakRed, red(pixel));
        require(peakRed >= 160, "approval disappears during the dimmest breathing phase");
      }
      if (mode == Mode::Stale) {
        require(frame.r == 230 && frame.g == 172 && frame.b == 56, "stale icon does not retain its fixed #E6AC38 accent");
      }
      if (mode == Mode::Waiting) {
        require(frame.b > frame.g && frame.g > frame.r, "waiting animation stopped being blue");
        require(frame.cells[4] == 0.0f, "waiting orbit fills its hollow center");
        const float brightest = *std::max_element(frame.cells, frame.cells + 9);
        const float dimmest = *std::min_element(frame.cells, frame.cells + 9);
        require(brightest >= 0.4f && brightest > dimmest, "waiting light disappears or ceases to have a highlighted cell");
        if (time < 3200) highlighted[std::max_element(frame.cells, frame.cells + 9) - frame.cells] = true;
      }
      previous = frame;
    }
    require(changed && rasterChanged, "a requested animated state remains visually static");
    if (mode == Mode::Waiting) {
      for (int i = 0; i < 9; ++i) require(highlighted[i] == (i != 4), "waiting highlight does not travel through all eight perimeter cells");
    }
  }
  for (const auto& entry : {std::pair<Mode, uint32_t>(Mode::Stale, status_pixel::kStaleCycleMs), std::pair<Mode, uint32_t>(Mode::Waiting, 3200u)}) {
    const Frame start = status_pixel::sample(entry.first, 0);
    const Frame end = status_pixel::sample(entry.first, entry.second - 1);
    require(equalFrame(start, status_pixel::sample(entry.first, entry.second)), "animation does not repeat at its intended period");
    for (int i = 0; i < 9; ++i) require(std::fabs(start.cells[i] - end.cells[i]) < 0.01f, "shape has a discontinuity across the loop seam");
    require(std::fabs(start.glow - end.glow) < 0.01f, "breathing has a discontinuity across the loop seam");
    require(std::abs(int(start.r) - int(end.r)) <= 1 && std::abs(int(start.g) - int(end.g)) <= 1 && std::abs(int(start.b) - int(end.b)) <= 1, "color has a discontinuity across the loop seam");
  }
}

void testReadingReferenceMotion() {
  using namespace reading_reference;
  require(kCycleMs == 1200 && kStepMs == 20 && kSampleCount == 60 && kCycleMs == kStepMs * kSampleCount, "reading-file light curve does not match the measured 1.2-second/60-sample cycle");
  require(status_pixel::kStaleCycleMs == kCycleMs, "stale does not use the reading-file light cycle");
  float minimum = 1.0f, maximum = 0.0f;
  Frame brightest = status_pixel::sample(Mode::Stale, 0);
  for (uint32_t time = 0; time < kCycleMs * 3; ++time) {
    const uint32_t phase = time % kCycleMs;
    const int index = phase / kStepMs;
    const int next = (index + 1) % kSampleCount;
    const double fraction = double(phase % kStepMs) / kStepMs;
    const double expected = (kLightSamples[index] * (1.0 - fraction) + kLightSamples[next] * fraction) / 255.0;
    const Frame frame = status_pixel::sample(Mode::Stale, time);
    require(frame.solidSquare && !frame.reference, "reading-file motion does not select a single solid square");
    for (float cell : frame.cells) require(std::fabs(cell - expected) <= 0.000001, "stale square does not follow the independently interpolated scalar source light curve");
    require(frame.r == 230 && frame.g == 172 && frame.b == 56 && frame.glow == 0.64f, "stale light curve changes its #E6AC38 tint or fixed halo scale");
    require(equalFrame(frame, status_pixel::sample(Mode::Stale, time + kCycleMs)), "reading-file motion does not repeat at the exact reference period");
    minimum = std::min(minimum, frame.cells[4]);
    if (frame.cells[4] > maximum) { maximum = frame.cells[4]; brightest = frame; }
  }
  require(maximum >= 0.75f && minimum <= maximum * 0.35f, "reading-file square does not visibly alternate between bright and dark phases");
  const auto brightPixels = raster(brightest);
  const uint16_t center = brightPixels[31 * kIcon + 31];
  require(red(center) >= 160, "reading-file square stays invisible for its entire cycle");
  for (int y = 18; y <= 44; ++y) for (int x = 18; x <= 44; ++x) require(withinOneRgb565Step(brightPixels[y * kIcon + x], center), "solid reading-file square retains a nine-cell grid or dark internal seams");
  Frame perturbed = brightest;
  for (int cell = 0; cell < 9; ++cell) if (cell != 4) perturbed.cells[cell] = cell % 2 ? 0.0f : 1.0f;
  require(raster(perturbed) == brightPixels, "solid-square drawing still depends on the eight unused grid cells");

  int previousBody = -1, previousHalo = -1;
  for (float level : {0.0f, 0.25f, 0.5f, 0.75f, 1.0f}) {
    Frame frame = brightest;
    for (float& cell : frame.cells) cell = level;
    const auto pixels = raster(frame);
    const int body = red(pixels[31 * kIcon + 31]);
    const int halo = red(pixels[31 * kIcon + 9]);
    require(body >= previousBody && halo >= previousHalo, "reading-file body and halo do not brighten together");
    if (level == 0.0f) {
      for (uint16_t background : {uint16_t(0), uint16_t(0xffff), uint16_t(0x18c6)}) {
        const auto dark = raster(frame, 1.0f, background);
        for (uint16_t pixel : dark) require(pixel == background, "a dark reading-file square leaves its body/halo painted on the background");
      }
    }
    if (level == 1.0f) require(body > 0 && halo > 0, "reading-file light lacks either its square body or halo");
    previousBody = body;
    previousHalo = halo;
  }
  for (uint16_t background : {uint16_t(0), uint16_t(0xffff), uint16_t(0x18c6), uint16_t(0xbda7)}) {
    const auto invisible = raster(brightest, 0.0f, background);
    const auto full = raster(brightest, 1.0f, background);
    const auto half = raster(brightest, 0.5f, background);
    for (size_t pixel = 0; pixel < full.size(); ++pixel) {
      require(invisible[pixel] == background, "zero-opacity reading-file drawing modifies an arbitrary background");
      for (auto channel : {red, green, blue}) require(channel(half[pixel]) >= std::min(channel(background), channel(full[pixel])) - 8 && channel(half[pixel]) <= std::max(channel(background), channel(full[pixel])) + 8, "reading-file partial opacity does not blend with its actual background");
    }
    require(full != half, "reading-file opacity has no visible effect");
  }
  std::cout << "Reading-file square: " << kCycleMs << " ms, normalized source light " << minimum << " to " << maximum << "; solid body, synchronized halo, fixed gold.\n";
}

void testApprovalReferenceMotion() {
  using namespace approval_reference;
  require(kCycleMs == 700 && kStepMs == 20 && kSampleCount == 35 && kCycleMs == kStepMs * kSampleCount, "approval does not use the measured 700ms/35-sample source cycle");
  require(status_pixel::kApprovalCycleMs == kCycleMs && status_pixel::kApprovalBreathMs == 3200, "approval shape and glow cycles are not independent");
  for (uint32_t time = 0; time < kCycleMs * 3; time += 3) {
    const uint32_t phase = time % kCycleMs;
    const int index = phase / kStepMs;
    const int next = (index + 1) % kSampleCount;
    const double fraction = double(phase % kStepMs) / kStepMs;
    const Frame frame = status_pixel::sample(Mode::Approval, time);
    const Frame repeated = status_pixel::sample(Mode::Approval, time + kCycleMs);
    for (int cell = 0; cell < 9; ++cell) {
      const int group = cell == 4 ? 2 : cell % 2 == 0 ? 0 : 1;
      const double expected = (kSamples[index][group] * (1.0 - fraction) + kSamples[next][group] * fraction) / 255.0;
      require(std::fabs(frame.cells[cell] - expected) <= 0.000001, "approval differs from independently interpolated corner/edge/center source tracks");
      require(frame.cells[cell] == repeated.cells[cell], "approval shape does not repeat after exactly 700ms");
    }
    require(frame.cells[0] == frame.cells[2] && frame.cells[2] == frame.cells[6] && frame.cells[6] == frame.cells[8], "approval corners are not symmetrical");
    require(frame.cells[1] == frame.cells[3] && frame.cells[3] == frame.cells[5] && frame.cells[5] == frame.cells[7], "approval edge middles are not symmetrical");
    require(frame.r == 255 && frame.g == 107 && frame.b == 107, "approval loses its exact #FF6B6B accent");
  }
  const Frame ring = status_pixel::sample(Mode::Approval, 0);
  const Frame corners = status_pixel::sample(Mode::Approval, 220);
  const Frame center = status_pixel::sample(Mode::Approval, 420);
  const Frame cross = status_pixel::sample(Mode::Approval, 540);
  require(ring.cells[0] > 0.85f && ring.cells[1] > 0.9f && ring.cells[4] < std::min(ring.cells[0], ring.cells[1]), "approval's ring phase is not visibly present");
  require(corners.cells[0] > 0.9f && corners.cells[1] < 0.5f && corners.cells[4] < 0.5f, "approval's four-corner phase is not visibly present");
  require(center.cells[4] > 0.95f && center.cells[0] < 0.35f && center.cells[1] < 0.4f, "approval's center phase is not visibly present");
  require(cross.cells[4] > 0.9f && cross.cells[1] > 0.9f && cross.cells[0] < 0.3f, "approval's cross phase is not visibly present");
  for (uint32_t time = 0; time < 3200; time += 33) {
    const Frame first = status_pixel::sample(Mode::Approval, time);
    const Frame nextBreath = status_pixel::sample(Mode::Approval, time + 3200);
    require(first.glow == nextBreath.glow, "approval glow is reset by the faster shape loop");
  }
  require(status_pixel::sample(Mode::Approval, 0).glow != status_pixel::sample(Mode::Approval, 700).glow, "approval glow incorrectly restarts every 700ms");
  const Frame tail = status_pixel::sample(Mode::Approval, kCycleMs - 1);
  for (int cell = 0; cell < 9; ++cell) require(std::fabs(tail.cells[cell] - ring.cells[cell]) < 0.01f, "approval shape jumps at its source-curve seam");
}

void testThinkingColorLoop() {
  const Frame yellow = status_pixel::sample(Mode::Thinking, 0);
  const Frame greenFrame = status_pixel::sample(Mode::Thinking, 4000);
  const Frame blueFrame = status_pixel::sample(Mode::Thinking, 8000);
  require(yellow.r == 255 && yellow.g == 210 && yellow.b == 128, "thinking color loop does not begin in yellow");
  require(greenFrame.r == 120 && greenFrame.g == 232 && greenFrame.b == 158, "thinking does not reach green after yellow");
  require(blueFrame.r == 106 && blueFrame.g == 184 && blueFrame.b == 255, "thinking does not reach blue after green");
  require(equalColor(yellow, status_pixel::sample(Mode::Thinking, 12000)), "thinking color loop does not return to yellow after 12 seconds");
  Frame previous = yellow;
  for (uint32_t time = 0; time < thinking_reference::kDurationMs * 3; time += 40) {
    const Frame frame = status_pixel::sample(Mode::Thinking, time);
    require(std::abs(int(frame.r) - int(previous.r)) <= 4 && std::abs(int(frame.g) - int(previous.g)) <= 4 && std::abs(int(frame.b) - int(previous.b)) <= 4, "thinking color jumps during its gradient or at a shape loop boundary");
    require(equalColor(frame, status_pixel::sample(Mode::Thinking, time + 12000)), "thinking gradient phase depends on the GIF frame loop");
    previous = frame;
  }
  for (uint32_t seam : {12000u, uint32_t(thinking_reference::kDurationMs)}) {
    const Frame before = status_pixel::sample(Mode::Thinking, seam - 1);
    const Frame after = status_pixel::sample(Mode::Thinking, seam);
    require(std::abs(int(before.r) - int(after.r)) <= 1 && std::abs(int(before.g) - int(after.g)) <= 1 && std::abs(int(before.b) - int(after.b)) <= 1, "thinking tint is discontinuous at its own or the GIF loop seam");
  }
  require(!equalColor(yellow, status_pixel::sample(Mode::Thinking, thinking_reference::kDurationMs)), "thinking tint incorrectly restarts when the GIF restarts");
  // A 12-second tint loop now equals exactly eight 1.5-second shape loops.
  // Check each independent clock at off-boundary times instead of treating
  // their legitimate simultaneous restart as evidence of accidental coupling.
  constexpr int tintKeys[3][3] = {{255, 210, 128}, {120, 232, 158}, {106, 184, 255}};
  for (uint32_t time : {117u, 733u, 1607u, 2789u, 4301u, 6593u, 8013u, 10037u, 11917u, 12531u}) {
    const Frame frame = status_pixel::sample(Mode::Thinking, time);
    const uint32_t colorPhase = time % 12000u;
    const int key = colorPhase / 4000u;
    const int nextKey = (key + 1) % 3;
    const double linear = double(colorPhase % 4000u) / 4000.0;
    const double eased = linear * linear * (3.0 - 2.0 * linear);
    const int actualChannels[] = {frame.r, frame.g, frame.b};
    for (int channel = 0; channel < 3; ++channel) {
      const int expected = int(std::floor(tintKeys[key][channel] + (tintKeys[nextKey][channel] - tintKeys[key][channel]) * eased + 0.5));
      require(std::abs(actualChannels[channel] - expected) <= 1, "thinking color phase is not the independent 12-second gradient at a non-boundary time");
    }
    const uint32_t shapePhase = time % thinking_reference::kDurationMs;
    const auto* expectedEnd = std::upper_bound(thinking_reference::kFrameEnds, thinking_reference::kFrameEnds + thinking_reference::kFrameCount, shapePhase);
    const int expectedIndex = int(expectedEnd - thinking_reference::kFrameEnds);
    require(frame.referenceFrame == expectedIndex, "thinking shape phase depends on the color clock at a non-boundary time");
    const Frame nextShapeLoop = status_pixel::sample(Mode::Thinking, time + thinking_reference::kDurationMs);
    require(frame.referenceFrame == nextShapeLoop.referenceFrame && frame.referenceNextFrame == nextShapeLoop.referenceNextFrame && frame.referenceMix == nextShapeLoop.referenceMix, "thinking does not repeat its shape independently every 1.5 seconds");
    require(!equalColor(frame, nextShapeLoop), "thinking color is incorrectly held/restarted at the same shape phase one loop later");
  }
  require(!yellow.exactColor && status_pixel::kGoldR == 255 && status_pixel::kGoldG == 210 && status_pixel::kGoldB == 128, "stale accent update changed the original thinking yellow or its rendering path");
  require(!equalColor(yellow, status_pixel::sample(Mode::Stale, 11999)), "stale icon tint is still incorrectly coupled to the thinking yellow");
  int yellowPixels = 0, greenPixels = 0, bluePixels = 0;
  for (uint32_t time = 0; time < 12000; time += 100) {
    const auto pixels = raster(status_pixel::sample(Mode::Thinking, time));
    for (uint16_t pixel : pixels) {
      const int r = red(pixel), g = green(pixel), b = blue(pixel);
      if (r >= 160 && g >= 80 && r > g && g > b + 20) ++yellowPixels;
      if (g >= 160 && g > r + 20 && g > b + 20) ++greenPixels;
      if (b >= 160 && b > r + 20 && b > g + 20) ++bluePixels;
    }
  }
  require(yellowPixels > 16 && greenPixels > 16 && bluePixels > 16, "the independent 12-second render does not visibly traverse yellow, green, and blue");
  std::cout << "Independent color cycle coverage: yellow=" << yellowPixels << ", green=" << greenPixels << ", blue=" << bluePixels << " pixels.\n";
}

void testThinkingReference() {
  static_assert(kIcon == 64, "preview and reference contract expects 64x64 tiles");
  using namespace thinking_reference;
  require(kFrameCount > 1 && kDurationMs > 0, "thinking reference contains no animation");
  require(kSourceFirstFrame == 45 && kSourceCycleEndFrame == 86 && kFrameCount == 41, "thinking does not use the approved complete natural source cycle");
  require(kPlaybackSpeedNumerator == 323 && kPlaybackSpeedDenominator == 150 && kSourceCycleDurationMs == 3230, "thinking playback speed does not map the approved source cycle to exactly 1.5 seconds");
  require(kDurationMs == 1500, "thinking natural cycle is not exactly 1.5 seconds");
  require(kFramePixels == kIcon * kIcon, "thinking direct frame dimensions differ from the drawing tile");
  require(sizeof(kLightFrames) == size_t(kFrameCount) * kFramePixels, "thinking direct grayscale frames are incomplete");
  require(kFrameEnds[kFrameCount - 1] == kDurationMs, "thinking duration does not match its frame timings");
  std::array<uint8_t, kIcon * kIcon> previous = {};
  int changedFrames = 0;
  int visiblyInterpolatedFrames = 0;
  uint32_t frameStart = 0;
  for (int index = 0; index < kFrameCount; ++index) {
    require(kFrameEnds[index] > frameStart, "thinking frame has zero or negative duration");
    const uint32_t frameDuration = kFrameEnds[index] - frameStart;
    const int next = (index + 1) % kFrameCount;
    const uint8_t* source = kLightFrames + size_t(index) * kFramePixels;
    const uint8_t* following = kLightFrames + size_t(next) * kFramePixels;
    std::array<uint8_t, kIcon * kIcon> decoded;
    std::copy(source, source + kFramePixels, decoded.begin());
    const Frame frame = status_pixel::sample(Mode::Thinking, frameStart);
    require(frame.reference && frame.referenceFrame == index && frame.referenceNextFrame == next && frame.referenceMix == 0, "thinking source boundary does not start at the exact original frame");
    require(status_pixel::sample(Mode::Thinking, kFrameEnds[index] - 1).referenceFrame == index, "thinking frame duration differs from the reference");
    const auto pixels = raster(frame);
    for (size_t pixel = 0; pixel < pixels.size(); ++pixel) require(withinOneRgb565Step(pixels[pixel], expectedReferencePixel(decoded[pixel], frame)), "thinking source boundary lost original grayscale shape/light");
    if (index > 0 && decoded != previous) ++changedFrames;

    const Frame middle = status_pixel::sample(Mode::Thinking, frameStart + frameDuration / 2);
    require(middle.referenceFrame == index && middle.referenceNextFrame == next && middle.referenceMix > 0 && middle.referenceMix < 255, "thinking holds an entire source frame instead of interpolating toward the next");
    const double expectedMix = double(frameDuration / 2) * 255.0 / frameDuration;
    require(std::fabs(double(middle.referenceMix) - expectedMix) <= 0.5, "thinking interpolation fraction does not follow elapsed time");
    Frame left = middle;
    Frame right = middle;
    left.referenceMix = 0;
    right.referenceMix = 255;
    const auto middlePixels = raster(middle);
    if (middlePixels != raster(left) && middlePixels != raster(right)) ++visiblyInterpolatedFrames;

    // Test arbitrary RGB565 backgrounds and several opacities against a
    // separate floating-point compositor. Include a per-pixel patterned
    // background, not only a flat black/gray destination.
    for (int backgroundKind = 0; backgroundKind < 3; ++backgroundKind) {
      for (float opacity : {0.125f, 0.5f, 1.0f}) {
        std::array<uint16_t, kIcon * kIcon> background;
        for (size_t pixel = 0; pixel < background.size(); ++pixel) {
          background[pixel] = backgroundKind == 0 ? 0 : backgroundKind == 1 ? 0xffff : uint16_t(pixel * 977u + 0x18c6u);
        }
        auto result = background;
        status_pixel::draw(result.data(), kIcon, middle, opacity);
        for (size_t pixel = 0; pixel < result.size(); ++pixel) {
          const double fraction = double(middle.referenceMix) / 255.0;
          const uint8_t light = uint8_t(std::floor(double(source[pixel]) * (1.0 - fraction) + double(following[pixel]) * fraction + 0.5));
          const uint16_t expected = expectedReferencePixel(light, middle, opacity, background[pixel]);
          if (!withinOneRgb565Step(result[pixel], expected)) {
            std::cerr << "Integer compositor mismatch: frame=" << index << " pixel=" << pixel << " light=" << int(light) << " background=" << background[pixel] << " opacity=" << opacity << " actual=" << result[pixel] << " expected=" << expected << '\n';
          }
          require(withinOneRgb565Step(result[pixel], expected), "thinking integer compositor exceeds one RGB565 rounding step");
          if (light == 0) require(result[pixel] == background[pixel], "black reference pixels overwrite the existing background");
        }
      }
    }
    previous = decoded;
    frameStart = kFrameEnds[index];
  }
  require(changedFrames >= 2, "thinking reference frames are visually identical");
  require(visiblyInterpolatedFrames > kFrameCount / 2, "thinking intermediate rendering still holds source frames instead of visibly blending");
  const Frame restarted = status_pixel::sample(Mode::Thinking, kDurationMs);
  require(restarted.referenceFrame == 0 && restarted.referenceNextFrame == 1 && restarted.referenceMix == 0, "thinking does not restart at the original first shape frame");
  require(status_pixel::sample(Mode::Thinking, 4294967295u).referenceFrame < kFrameCount, "large elapsed time selects an invalid reference frame");
  Frame beforeSeam = status_pixel::sample(Mode::Thinking, kDurationMs - 1);
  Frame afterSeam = restarted;
  beforeSeam.r = beforeSeam.g = beforeSeam.b = 255;
  afterSeam.r = afterSeam.g = afterSeam.b = 255;
  const auto beforePixels = raster(beforeSeam);
  const auto afterPixels = raster(afterSeam);
  for (size_t pixel = 0; pixel < beforePixels.size(); ++pixel) require(withinOneRgb565Step(beforePixels[pixel], afterPixels[pixel]), "thinking visibly snaps when the final frame wraps to the first");
  std::cout << "Reference: " << kFrameCount << " grayscale frames, " << kDurationMs << " ms, " << changedFrames << " source changes, " << visiblyInterpolatedFrames << " visibly blended intervals.\n";
}

struct MotionImage {
  std::array<double, kIcon * kIcon> light;
  double total = 0.0;
  double centroidX = 0.0;
  double centroidY = 0.0;
};

MotionImage thinkingMotionImage(uint32_t time) {
  Frame frame = status_pixel::sample(Mode::Thinking, time);
  // Isolate geometry/light from the deliberately independent hue cycle.
  frame.r = frame.g = frame.b = 255;
  const auto pixels = raster(frame);
  MotionImage result;
  for (size_t i = 0; i < pixels.size(); ++i) {
    const double light = red(pixels[i]) * 0.2126 + green(pixels[i]) * 0.7152 + blue(pixels[i]) * 0.0722;
    result.light[i] = light;
    result.total += light;
    result.centroidX += double(i % kIcon) * light;
    result.centroidY += double(i / kIcon) * light;
  }
  require(result.total > 0.0, "thinking becomes completely dark during its cycle");
  result.centroidX /= result.total;
  result.centroidY /= result.total;
  return result;
}

struct MotionStep {
  double pixelMae = 0.0;
  double normalizedShapePercent = 0.0;
  double centroidPixels = 0.0;
};

MotionStep motionStep(const MotionImage& before, const MotionImage& after) {
  MotionStep result;
  for (size_t i = 0; i < before.light.size(); ++i) {
    result.pixelMae += std::fabs(before.light[i] - after.light[i]);
    result.normalizedShapePercent += std::fabs(before.light[i] / before.total - after.light[i] / after.total);
  }
  result.pixelMae /= before.light.size();
  result.normalizedShapePercent *= 50.0;  // Total variation, independent of uniform brightness.
  result.centroidPixels = std::hypot(before.centroidX - after.centroidX, before.centroidY - after.centroidY);
  return result;
}

double motionDirectionCosine(const MotionImage& before, const MotionImage& middle, const MotionImage& after) {
  double dot = 0.0, leftSquared = 0.0, rightSquared = 0.0;
  for (size_t i = 0; i < before.light.size(); ++i) {
    const double left = middle.light[i] - before.light[i];
    const double right = after.light[i] - middle.light[i];
    dot += left * right;
    leftSquared += left * left;
    rightSquared += right * right;
  }
  if (leftSquared < 0.000001 || rightSquared < 0.000001) return 1.0;
  return dot / std::sqrt(leftSquared * rightSquared);
}

double percentile(std::vector<double> values, double fraction) {
  require(!values.empty(), "motion analysis has no ordinary transitions to compare");
  std::sort(values.begin(), values.end());
  return values[size_t(std::ceil((values.size() - 1) * fraction))];
}

void testThinkingSeamMotion() {
  const uint32_t period = thinking_reference::kDurationMs;
  std::vector<double> ordinaryMae, ordinaryShape, ordinaryCentroid, ordinaryDirection;
  MotionImage older = thinkingMotionImage(0);
  MotionImage previous = thinkingMotionImage(33);
  uint32_t previousTime = 33;
  int ordinaryStillRun = 0, ordinaryLongestStillRun = 0;
  for (uint32_t frameIndex = 2; frameIndex * 1000u / 30u < period; ++frameIndex) {
    const uint32_t time = frameIndex * 1000u / 30u;
    const MotionImage current = thinkingMotionImage(time);
    if (previousTime >= 250 && time <= period - 250) {
      const auto step = motionStep(previous, current);
      ordinaryMae.push_back(step.pixelMae);
      ordinaryShape.push_back(step.normalizedShapePercent);
      ordinaryCentroid.push_back(step.centroidPixels);
      ordinaryDirection.push_back(motionDirectionCosine(older, previous, current));
      ordinaryStillRun = step.pixelMae < 0.05 ? ordinaryStillRun + 1 : 0;
      ordinaryLongestStillRun = std::max(ordinaryLongestStillRun, ordinaryStillRun);
    }
    older = previous;
    previous = current;
    previousTime = time;
  }
  MotionStep seamMaximum;
  previous = thinkingMotionImage(period - 250);
  int seamStillRun = 0, seamLongestStillRun = 0, seamSteps = 0;
  for (uint32_t frameIndex = 1; frameIndex * 1000u / 30u <= 500; ++frameIndex) {
    const uint32_t time = period - 250 + frameIndex * 1000u / 30u;
    const MotionImage current = thinkingMotionImage(time);
    const auto step = motionStep(previous, current);
    seamMaximum.pixelMae = std::max(seamMaximum.pixelMae, step.pixelMae);
    seamMaximum.normalizedShapePercent = std::max(seamMaximum.normalizedShapePercent, step.normalizedShapePercent);
    seamMaximum.centroidPixels = std::max(seamMaximum.centroidPixels, step.centroidPixels);
    seamStillRun = step.pixelMae < 0.05 ? seamStillRun + 1 : 0;
    seamLongestStillRun = std::max(seamLongestStillRun, seamStillRun);
    previous = current;
    ++seamSteps;
  }
  const double mae95 = percentile(ordinaryMae, 0.95);
  const double shape95 = percentile(ordinaryShape, 0.95);
  const double centroid95 = percentile(ordinaryCentroid, 0.95);
  const double seamDirection = motionDirectionCosine(thinkingMotionImage(period - 33), thinkingMotionImage(period), thinkingMotionImage(period + 33));
  const double direction05 = percentile(ordinaryDirection, 0.05);
  std::cout << std::fixed << std::setprecision(3)
            << "30 FPS seam +/-250ms (" << seamSteps << " adjacent steps), peak / ordinary P95 / ordinary max: light MAE "
            << seamMaximum.pixelMae << " / " << mae95 << " / " << percentile(ordinaryMae, 1.0)
            << "; normalized shape % " << seamMaximum.normalizedShapePercent << " / " << shape95 << " / " << percentile(ordinaryShape, 1.0)
            << "; centroid px " << seamMaximum.centroidPixels << " / " << centroid95 << " / " << percentile(ordinaryCentroid, 1.0)
            << "; boundary direction cosine=" << seamDirection << " (ordinary P05=" << direction05 << ")"
            << "; longest still run seam/ordinary=" << seamLongestStillRun << '/' << ordinaryLongestStillRun << ".\n";
  // Detect a seam-specific speed/shape outlier, reversal or hold. These checks
  // are supplemented by the exported seven-frame strip and continuous clip;
  // a one-millisecond equality alone cannot establish natural-looking motion.
  require(seamMaximum.pixelMae <= std::max(1.0, mae95 * 1.5), "thinking seam light/position changes unusually fast across consecutive display frames");
  require(seamMaximum.normalizedShapePercent <= std::max(0.5, shape95 * 1.5), "thinking seam contour changes unusually fast across consecutive display frames");
  require(seamMaximum.centroidPixels <= std::max(0.15, centroid95 * 1.5), "thinking seam centroid jumps unusually far across consecutive display frames");
  require(seamDirection >= std::max(-0.35, direction05 - 0.25), "thinking reverses its local motion direction at the loop boundary");
  require(seamLongestStillRun <= ordinaryLongestStillRun + 1, "thinking pauses unusually long around the loop boundary");
}

void testOpacityBlend() {
  constexpr uint16_t background = 0x18c6;
  const Frame frame = status_pixel::sample(Mode::Thinking, 1750);
  const auto full = raster(frame, 1.0f, background);
  const auto half = raster(frame, 0.5f, background);
  bool visiblyDifferent = false;
  for (size_t i = 0; i < full.size(); ++i) {
    for (auto channel : {red, green, blue}) {
      const int initial = channel(background);
      const int maximum = channel(full[i]);
      const int blended = channel(half[i]);
      require(blended >= std::min(initial, maximum) - 8 && blended <= std::max(initial, maximum) + 8, "partial opacity did not blend with the existing background");
    }
    visiblyDifferent = visiblyDifferent || full[i] != half[i];
  }
  require(visiblyDifferent, "partial opacity had no visible effect");
}

void testStatusSelectionAndTransitions() {
  using status_motion::State;
  using status_motion::Transition;
  require(status_motion::resolve(true, false, true, false, true, true) == State::Approval, "stale/thinking masks an approval");
  require(status_motion::resolve(true, false, true, true, true, false) == State::Approval, "thinking masks an approval");
  require(status_motion::resolve(true, false, false, false, true, false) == State::Stale, "thinking masks stale data");
  require(status_motion::resolve(true, false, false, true, true, false) == State::Thinking, "fresh running state not selected");
  require(status_motion::resolve(false, false, false, false, false, false) == State::Waiting, "missing snapshot is not waiting");
  require(status_motion::resolve(true, true, false, true, true, false) == State::Waiting, "waiting-for-sync state not selected");
  require(status_motion::resolve(true, false, false, true, false, false) == State::Idle, "idle state not selected");
  require(status_motion::loops(State::Approval) && status_motion::loops(State::Thinking) && status_motion::loops(State::Waiting) && status_motion::loops(State::Stale), "one of the four animated statuses is not scheduled");
  require(!status_motion::loops(State::Idle) && !status_motion::loops(State::Other), "a text-only status continually loops");

  Transition transition;
  require(transition.update(State::Other, 100.0f, 1000), "initial state ignored");
  require(!transition.active(1000) && transition.width(1000) == 100.0f, "first snapshot starts invisible");
  require(transition.update(State::Thinking, 170.0f, 1100), "thinking transition not started");
  require(transition.active(1100) && transition.progress(1100) == 0.0f, "normal transition not started at zero");
  require(transition.width(1100) == 100.0f, "normal transition does not preserve visible width");
  require(!transition.update(State::Thinking, 170.0f, 1220), "same-state heartbeat restarts transition");
  require(transition.progress(1220) == 0.5f && transition.width(1220) > 100.0f && transition.width(1220) < 170.0f, "heartbeat disrupted elapsed-time animation");
  require(transition.active(1339) && !transition.active(1340), "normal transition is not 240 ms");
  require(transition.width(1340) == 170.0f && transition.progress(1340) == 1.0f, "normal transition does not finish exactly");
  require(transition.update(State::Other, 140.0f, 1400), "second normal transition not started");
  require(transition.update(State::Approval, 220.0f, 1460), "approval did not interrupt transition");
  require(transition.state() == State::Approval && transition.width(1460) == 220.0f && transition.progress(1460) == 1.0f && !transition.active(1460), "approval is delayed or truncated by an in-progress animation");
  require(!transition.update(State::Approval, 220.0f, 1900), "approval heartbeat resets the animation clock");
  require(transition.elapsed(1900) > transition.elapsed(1460), "approval breathing clock does not advance through heartbeat");
  require(transition.update(State::Thinking, 170.0f, 2000), "resolved approval does not leave its state");
  require(transition.state() == State::Thinking && transition.width(2000) == 170.0f && transition.progress(2000) == 1.0f && !transition.active(2000), "resolved approval remains visible during transition");

  Transition rollover;
  constexpr uint32_t start = 0xfffffff0u;
  rollover.update(State::Other, 100.0f, start - 10u);
  rollover.update(State::Thinking, 170.0f, start);
  require(rollover.active(uint32_t(start + 120u)) && rollover.progress(uint32_t(start + 120u)) == 0.5f, "millis rollover breaks transition progress");
  require(!rollover.active(uint32_t(start + 240u)) && rollover.width(uint32_t(start + 240u)) == 170.0f, "millis rollover prevents transition completion");
}

uint16_t independentBackdropBlend(uint16_t background, uint16_t foreground, unsigned alpha) {
  const double amount = double(alpha) / 255.0;
  const int r = int(std::floor(((background >> 11) & 31) * (1.0 - amount) + ((foreground >> 11) & 31) * amount + 0.5));
  const int g = int(std::floor(((background >> 5) & 63) * (1.0 - amount) + ((foreground >> 5) & 63) * amount + 0.5));
  const int b = int(std::floor((background & 31) * (1.0 - amount) + (foreground & 31) * amount + 0.5));
  return uint16_t((r << 11) | (g << 5) | b);
}

uint16_t independentGlowPixel(const status_tile::Backdrop& backdrop, int normalized, int bias) {
  if (normalized == 0 || backdrop.edge == 0) return backdrop.background;
  int alpha = int(std::floor(double(normalized) * backdrop.edge / 255.0 + 0.5));
  if (normalized < 250) alpha = std::max(0, std::min(255, alpha + bias));
  return independentBackdropBlend(backdrop.background, backdrop.tint, unsigned(alpha));
}

void testBackgroundCaptureAndRestore() {
  constexpr int width = 84, height = 80, stride = 97;
  std::array<uint16_t, stride * height> clean;
  for (size_t pixel = 0; pixel < clean.size(); ++pixel) clean[pixel] = uint16_t(pixel * 977u + 0x18c6u);
  const auto original = clean;
  status_tile::Background cache;
  auto output = clean;
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "an uninitialized background cache writes pixels");
  for (const auto& position : {std::pair<int, int>(0, 0), std::pair<int, int>(width - kIcon, height - kIcon), std::pair<int, int>(8, 7)}) {
    require(cache.capture(clean.data(), stride, width, height, position.first, position.second), "valid background tile capture fails at an image edge");
    require(cache.valid() && clean == original, "background capture changes its source");
    output.fill(0x5a2d);
    require(cache.restore(output.data(), stride, width, height), "valid background tile restore fails");
    for (int y = 0; y < height; ++y) for (int x = 0; x < stride; ++x) {
      const bool inside = x >= position.first && x < position.first + kIcon && y >= position.second && y < position.second + kIcon;
      require(output[y * stride + x] == (inside ? clean[y * stride + x] : 0x5a2d), "background restore misreads stride or overwrites tile guards");
    }
  }
  for (const auto& position : {std::pair<int, int>(-1, 0), std::pair<int, int>(0, -1), std::pair<int, int>(width - kIcon + 1, 0), std::pair<int, int>(0, height - kIcon + 1)}) {
    require(!cache.capture(clean.data(), stride, width, height, position.first, position.second), "out-of-bounds tile capture succeeds");
    output = clean;
    require(!cache.restore(output.data(), stride, width, height) && output == clean, "failed capture leaves a writable old cache");
  }
  require(!cache.capture(nullptr, stride, width, height, 0, 0), "null background capture succeeds");
  require(!cache.capture(clean.data(), width - 1, width, height, 0, 0), "short source stride capture succeeds");
  require(!cache.capture(clean.data(), stride, kIcon - 1, height, 0, 0), "undersized source width capture succeeds");
  require(!cache.capture(clean.data(), stride, width, kIcon - 1, 0, 0), "undersized source height capture succeeds");
  require(cache.capture(clean.data(), stride, width, height, 8, 7), "background capture does not recover after an invalid request");
  output = clean;
  require(!cache.restore(output.data(), width - 1, width, height) && output == clean, "short destination stride permits a partial restore");
  require(!cache.restore(output.data(), stride, kIcon, kIcon) && output == clean, "smaller destination permits an out-of-bounds restore");
  require(!cache.restore(nullptr, stride, width, height), "null restore succeeds");
  cache.rect.x = -1;
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "negative cached x permits a restore before the destination");
  cache.rect.x = 8;
  cache.rect.y = -1;
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "negative cached y permits a restore before the destination");
  require(cache.capture(clean.data(), stride, width, height, 8, 7), "valid capture fails after invalid coordinates");
  cache.rect.width = kIcon - 1;
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "modified cache rectangle width permits an inconsistent restore");
  require(cache.capture(clean.data(), stride, width, height, 8, 7) && cache.rect.width == kIcon, "capture does not reset a modified rectangle width");
  cache.rect.height = kIcon - 1;
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "modified cache rectangle height permits an inconsistent restore");
  require(cache.capture(clean.data(), stride, width, height, 8, 7) && cache.rect.height == kIcon, "capture does not reset a modified rectangle height");
  cache.invalidate();
  require(!cache.restore(output.data(), stride, width, height) && output == clean, "invalidated cache restores old pixels");

  require(cache.capture(clean.data(), stride, width, height, 8, 7), "fresh background capture failed");
  output = clean;
  for (Mode mode : {Mode::Thinking, Mode::Approval, Mode::Stale, Mode::Waiting}) {
    for (uint32_t frameIndex = 0; frameIndex < 192; ++frameIndex) {
      const Frame frame = status_pixel::sample(mode, frameIndex * 1000u / 30u);
      require(cache.restore(output.data(), stride, width, height), "background restore fails during sustained animation");
      status_pixel::draw(output.data() + 7 * stride + 8, stride, frame);
      auto independentlyFresh = clean;
      status_pixel::draw(independentlyFresh.data() + 7 * stride + 8, stride, frame);
      require(output == independentlyFresh, "cached drawing accumulates light/trails or damages pixels outside the tile");
    }
  }
}

void testBackdropEquivalence() {
  int equivalentPairs = 0;
  int differentTintPairs = 0;
  uint32_t seed = 0x59a8271u;
  for (int sample = 0; sample < 96; ++sample) {
    seed = seed * 1664525u + 1013904223u;
    const uint16_t background = uint16_t(seed >> 16);
    seed = seed * 1664525u + 1013904223u;
    const uint16_t tint = uint16_t(seed >> 16);
    for (unsigned edge : {0u, 1u, 4u, 18u, 43u, 128u, 251u, 255u}) {
      status_tile::Backdrop left;
      left.background = background;
      left.tint = tint;
      left.card = independentBackdropBlend(background, tint, 20);
      left.edge = uint8_t(edge);
      for (uint16_t delta : {uint16_t(0), uint16_t(1), uint16_t(32), uint16_t(2048)}) {
        auto right = left;
        right.tint ^= delta;
        const bool equivalent = left.samePixels(right);
        require(equivalent == right.samePixels(left), "backdrop pixel equivalence is asymmetric");
        if (equivalent) {
          ++equivalentPairs;
          if (left.tint != right.tint) ++differentTintPairs;
          for (int normalized = 0; normalized <= 255; ++normalized) for (int bias = -3; bias <= 4; ++bias) {
            require(independentGlowPixel(left, normalized, bias) == independentGlowPixel(right, normalized, bias), "samePixels reuses a backdrop whose glow/dither pixels differ");
          }
        }
      }
      auto changed = left;
      changed.background ^= 1;
      require(!left.samePixels(changed), "changed background does not invalidate the tile cache");
      changed = left;
      changed.card ^= 1;
      require(!left.samePixels(changed), "changed card color does not invalidate the tile cache");
      changed = left;
      changed.edge ^= 1;
      require(!left.samePixels(changed), "changed glow edge alpha does not invalidate the tile cache");
    }
  }
  require(equivalentPairs > 0 && differentTintPairs > 0, "backdrop tests never exercise non-identical-but-equivalent tints");
}

status_tile::Backdrop measuredSettledBackdrop(Mode mode, const Frame& icon) {
  // Read-only algorithm estimate for the current Codex Dark renderer formula.
  // Excludes transition, theme and label invalidations; not a hardware FPS test.
  status_tile::Backdrop backdrop;
  backdrop.background = 0;
  backdrop.tint = uint16_t(((icon.r >> 3) << 11) | ((icon.g >> 2) << 5) | (icon.b >> 3));
  const unsigned alpha = mode == Mode::Approval ? unsigned(23 + 15 * icon.glow) : 20u;
  backdrop.card = independentBackdropBlend(backdrop.background, backdrop.tint, alpha);
  backdrop.edge = mode == Mode::Approval ? uint8_t(26 + 28 * icon.glow) : 18;
  return backdrop;
}

void reportSettledCacheRates() {
  const Mode modes[] = {Mode::Thinking, Mode::Approval, Mode::Stale, Mode::Waiting};
  const char* names[] = {"Thinking", "Approval", "Stale", "Waiting"};
  const uint32_t periods[] = {std::max(thinking_reference::kDurationMs, status_pixel::kThinkingColorCycleMs), std::max(status_pixel::kApprovalCycleMs, status_pixel::kApprovalBreathMs), status_pixel::kStaleCycleMs, 3200};
  for (int index = 0; index < 4; ++index) {
    auto cached = measuredSettledBackdrop(modes[index], status_pixel::sample(modes[index], 0));
    int hits = 0, attempts = 0;
    for (uint32_t frame = 1; frame * 1000u / 30u < periods[index] * 2u; ++frame) {
      const Frame icon = status_pixel::sample(modes[index], frame * 1000u / 30u);
      const auto current = measuredSettledBackdrop(modes[index], icon);
      ++attempts;
      if (cached.samePixels(current)) ++hits;
      else cached = current;
    }
    std::cout << "Settled cache estimate, " << names[index] << ": " << hits << '/' << attempts << " (" << std::fixed << std::setprecision(1) << 100.0 * hits / attempts << "%), 30 FPS over two cycles of the slowest component (" << periods[index] * 2u << " ms); not measured device performance.\n";
  }
}

// Compact raster labels keep this preview self-contained; icon pixels are drawn
// by the same status_pixel::draw function used by the firmware.
const char* glyph(char value) {
  switch (value) {
    case 'A': return "01110100011000111111100011000110001";
    case 'C': return "01111100001000010000100001000001111";
    case 'D': return "11110100011000110001100011000111110";
    case 'E': return "11111100001000011110100001000011111";
    case 'F': return "11111100001000011110100001000010000";
    case 'G': return "01111100001000010111100011000101111";
    case 'H': return "10001100011000111111100011000110001";
    case 'I': return "11111001000010000100001000010011111";
    case 'K': return "10001100101010011000101001001010001";
    case 'L': return "10000100001000010000100001000011111";
    case 'N': return "10001110011010110011100011000110001";
    case 'O': return "01110100011000110001100011000101110";
    case 'P': return "11110100011000111110100001000010000";
    case 'R': return "11110100011000111110101001001010001";
    case 'S': return "01111100001000001110000010000111110";
    case 'T': return "11111001000010000100001000010000100";
    case 'U': return "10001100011000110001100011000101110";
    case 'V': return "10001100011000110001100010101000100";
    case 'W': return "10001100011000110101101011101110001";
    case 'X': return "10001100010101000100010101000110001";
    case 'Z': return "11111000010001000100010001000011111";
    case '0': return "01110100011001110101110011000101110";
    case '2': return "01110100010000100010001000100011111";
    case '3': return "11110000010000101110000010000111110";
    case '5': return "11111100001000011110000010000111110";
    case '/': return "00001000100001000100010000100010000";
    default: return "00000000000000000000000000000000000";
  }
}

void put(std::vector<uint8_t>& output, int x, int y, int r, int g, int b) {
  if (x < 0 || y < 0 || x >= kWidth || y >= kHeight) return;
  const int index = (y * kWidth + x) * 3;
  output[index] = uint8_t(r); output[index + 1] = uint8_t(g); output[index + 2] = uint8_t(b);
}

void label(std::vector<uint8_t>& output, int center, int y, const std::string& text, int scale = 2) {
  int x = center - (int(text.size()) * 6 - 1) * scale / 2;
  for (char character : text) {
    const char* bits = glyph(character);
    for (int row = 0; row < 7; ++row) for (int col = 0; col < 5; ++col) if (bits[row * 5 + col] == '1') {
      for (int dy = 0; dy < scale; ++dy) for (int dx = 0; dx < scale; ++dx) put(output, x + col * scale + dx, y + row * scale + dy, 157, 166, 180);
    }
    x += 6 * scale;
  }
}

void preview() {
#ifdef _WIN32
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  const Mode modes[] = {Mode::Thinking, Mode::Approval, Mode::Stale, Mode::Waiting};
  const char* names[] = {"THINKING", "NEED APPROVAL", "DATA STALE", "WAITING"};
  const uint32_t duration = std::max(uint32_t(12800), uint32_t(thinking_reference::kDurationMs * 2));
  for (uint32_t frameIndex = 0; ; ++frameIndex) {
    const uint32_t time = frameIndex * 1000u / 30u;
    if (time >= duration) break;
    std::vector<uint8_t> output(kWidth * kHeight * 3, 0);
    label(output, kWidth / 2, 18, "STATUS PIXEL CORE / 30 FPS");
    for (int tile = 0; tile < 4; ++tile) {
      const int center = tile * 208 + 104;
      const auto pixels = raster(status_pixel::sample(modes[tile], time));
      for (int y = 0; y < kIcon; ++y) for (int x = 0; x < kIcon; ++x) {
        const uint16_t color = pixels[y * kIcon + x];
        for (int dy = 0; dy < 3; ++dy) for (int dx = 0; dx < 3; ++dx) put(output, center - kIcon * 3 / 2 + x * 3 + dx, 58 + y * 3 + dy, red(color), green(color), blue(color));
        put(output, center - kIcon / 2 + x, 298 + y, red(color), green(color), blue(color));
      }
      label(output, center, 266, names[tile]);
    }
    label(output, kWidth / 2, 380, "3X VIEW / NATIVE SIZE", 1);
    if (std::fwrite(output.data(), 1, output.size(), stdout) != output.size()) {
      std::cerr << "Could not write preview frame\n";
      std::exit(2);
    }
  }
}
}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::strcmp(argv[1], "--metadata") == 0) {
    std::cout << "{\"thinkingCycleMs\":" << thinking_reference::kDurationMs << ",\"approvalCycleMs\":" << status_pixel::kApprovalCycleMs << ",\"staleCycleMs\":" << status_pixel::kStaleCycleMs << ",\"fps\":30}\n";
    return 0;
  }
  if (argc == 2 && std::strcmp(argv[1], "--preview") == 0) {
    preview();
    return 0;
  }
  testBoundedDrawing();
  testExactStatusAccents();
  testAnimationContract();
  testReadingReferenceMotion();
  testApprovalReferenceMotion();
  testThinkingColorLoop();
  testThinkingReference();
  testThinkingSeamMotion();
  testOpacityBlend();
  testStatusSelectionAndTransitions();
  testBackgroundCaptureAndRestore();
  testBackdropEquivalence();
  reportSettledCacheRates();
  std::cout << "PASS: " << checks << " checks; bounded RGB565 writes, opacity/background fidelity, grayscale source timing and interpolation, continuous loop seam, independent color loop, reading-file solid square, four animated modes, visible approval, priority and elapsed-time transitions.\n";
  return 0;
}
