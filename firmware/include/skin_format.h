#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// Little-endian declarative format compiled from .meterskin JSON by the client.
// Every count/range/offset is revalidated on the device before activation.
namespace skin_format {
constexpr uint32_t kMaxBytes = 384u * 1024u;
// Read-only compatibility for already committed skins in the old head slots.
constexpr uint32_t kLegacyMaxBytes = 512u * 1024u;
constexpr uint32_t kColorCount = 25;
enum Layout : uint8_t {
  StatusBodyX, StatusY, PlateGap, BareGap, PlatePadding, StatusRadius,
  TotalX, TotalBaseline, CostX, CostBaseline, LabelX, LabelBaseline,
  ResetRight, CardX, CardWidth, PrimaryY, SecondaryY, SingleY, CardRadius,
  ProgressInset, ProgressY, ProgressHeight, ProgressRadius, SingleResetY,
  SingleFooterY, LayoutCount
};
constexpr uint32_t kDefaults[LayoutCount] = {
  34,24,12,10,10,8,30,181,30,222,30,115,450,20,440,251,361,261,20,20,55,20,8,407,447
};
constexpr uint32_t kMinimum[LayoutCount] = {
  14,10,0,0,0,0,20,180,20,218,20,115,430,10,400,250,250,250,0,15,50,10,0,395,440
};
constexpr uint32_t kMaximum[LayoutCount] = {
  34,36,12,12,10,20,40,205,40,245,40,125,460,30,450,375,375,280,30,25,60,25,12,415,455
};
struct Animation {
  uint32_t kind, smooth, exact, count, cycle, offset, stride, colorCount, colorCycle;
  uint32_t colors[8];
  uint32_t glowMin, glowMax, glowCycle, plate, plateCycle;
  uint32_t cardMin, cardMax, haloMin, haloMax, font, textColor;
};
struct Header {
  char magic[4];
  uint32_t schema, size;
  char id[32];
  uint32_t colors[kColorCount];
  uint32_t layout[LayoutCount];
  Animation animations[4]; // thinking, approval, stale, waiting
};
static_assert(sizeof(Animation) == 112, "skin animation ABI");
static_assert(sizeof(Header) == 692, "skin header ABI");
// Schema 2 adds four declarative pill layouts after the unchanged base header.
// Schema 1 bytes and offsets remain identical; no behavior is keyed by skin id.
struct Pill {
  uint32_t x, y, width, height, radius, textMode, textInset, baseline;
  uint32_t dotX, dotY, dotSize, dots, glowColor;
};
static_assert(sizeof(Pill) == 52, "pill layout ABI");
inline const Pill* pills(const Header* h) {
  return h && h->schema == 2 ? reinterpret_cast<const Pill*>(h + 1) : nullptr;
}
inline bool validPill(const Pill& p, unsigned state) {
  const uint32_t widths[] = {109,189,139,220}; // Sofia Bold 24 advances.
  if (p.x < 30 || p.x > 40 || p.y < 10 || p.y > 36 ||
      p.width < 160 || p.width > 298 || p.height < 36 || p.height > 44 ||
      p.x + p.width > 338 || p.y + p.height > 69 || p.radius != 18 ||
      p.textMode > 2 || p.textInset > 48 || p.baseline < 24 || p.baseline > 30 ||
      p.dotX > 36 || p.dotY > 28 || p.dotSize > 16 || p.dots > 1 ||
      p.glowColor > 0xFFFFFF || (p.dots && (state != 0 || p.textMode != 2))) return false;
  const uint32_t text = widths[state] + p.dots * 18;
  if (p.textMode == 2 ? p.textInset + text + 8 > p.width : text + 16 > p.width) return false;
  return !p.dotSize || (p.textMode == 2 && p.dotX + p.dotSize + 4 <= p.textInset &&
      p.dotY + p.dotSize <= p.height);
}
inline uint32_t word(const uint8_t* bytes) {
  return uint32_t(bytes[0]) | uint32_t(bytes[1]) << 8 |
      uint32_t(bytes[2]) << 16 | uint32_t(bytes[3]) << 24;
}
inline uint32_t peak(uint32_t rgb) {
  uint32_t a = rgb >> 16, b = (rgb >> 8) & 255, c = rgb & 255;
  return a > b ? (a > c ? a : c) : (b > c ? b : c);
}
inline bool period(uint32_t value) { return value >= 200 && value <= 30000; }
inline const char* validate(const uint8_t* bytes, size_t size, size_t limit = kMaxBytes) {
  if (!bytes || size < sizeof(Header) || size > limit) return "skin size";
  Header h;
  memcpy(&h, bytes, sizeof(h));
  if ((h.schema != 1 && h.schema != 2) ||
      memcmp(h.magic, h.schema == 1 ? "TMS1" : "TMS2", 4) || h.size != size) return "skin format";
  size_t idLength = 0;
  for (; idLength < sizeof(h.id) && h.id[idLength]; ++idLength) {
    char c = h.id[idLength];
    if (!(c >= 'a' && c <= 'z') && !(c >= '0' && c <= '9') &&
        !(idLength && c == '-')) return "skin id";
  }
  if (idLength == 0 || idLength == sizeof(h.id)) return "skin id length";
  for (uint32_t c : h.colors) if (c > 0xFFFFFF) return "color range";
  if (peak(h.colors[0]) > 48 || (h.colors[2] >> 16) < 192 ||
      ((h.colors[2] >> 8) & 255) < 192 || (h.colors[2] & 255) < 192) return "text contrast";
  const auto* l = h.layout;
  for (uint8_t i = 0; i < LayoutCount; ++i)
    if (l[i] < kMinimum[i] || l[i] > kMaximum[i]) return "layout range";
  const uint32_t distance = l[PrimaryY] > l[SecondaryY]
      ? l[PrimaryY] - l[SecondaryY] : l[SecondaryY] - l[PrimaryY];
  if (l[CardX] + l[CardWidth] > 470 || distance < 100 ||
      l[TotalBaseline] < l[LabelBaseline] + 66 ||
      l[CostBaseline] < l[TotalBaseline] + 41 ||
      l[PrimaryY] < l[CostBaseline] + 15 || l[SecondaryY] < l[CostBaseline] + 15 ||
      l[SingleY] < l[CostBaseline] + 15) return "layout overlap";
  uint32_t end = sizeof(Header);
  if (h.schema == 2) {
    end += 4 * sizeof(Pill);
    if (size < end) return "pill header truncated";
    for (unsigned i=0; i<4; ++i) {
      Pill p;
      memcpy(&p, bytes + sizeof(Header) + i*sizeof(Pill), sizeof(p));
      if (!validPill(p, i)) return "pill layout";
    }
  }
  for (uint8_t state = 0; state < 4; ++state) {
    const Animation& a = h.animations[state];
    const bool pill = h.schema == 2;
    if ((pill ? a.kind != 3 : a.kind > 2) || a.smooth > (pill ? 2u : 1u) || a.exact > 1 || a.count < 2 || a.count > 128 ||
        !period(a.cycle) || a.offset != end || a.colorCount < 1 || a.colorCount > 8 ||
        !period(a.colorCycle) || !period(a.glowCycle) || !period(a.plateCycle) ||
        a.glowMin > a.glowMax || a.glowMax > 255 || a.plate > 1 ||
        a.cardMin > a.cardMax || a.cardMax > 255 || a.haloMin > a.haloMax || a.haloMax > 255 ||
        (pill ? a.font != 24 : ((a.font != 28 && a.font != 32) || (state == 3 && a.font != 28))) ||
        a.textColor > 0xFFFFFF || peak(a.textColor) < 192) return "animation range";
    const uint32_t stride = pill ? 6 : a.kind == 0 ? 4096 : (a.kind == 1 ? 9 : 1);
    if (a.stride != stride) return "animation stride";
    const uint32_t length = a.count * (4 + stride);
    if (end > size || length > size - end) return "animation bounds";
    uint32_t previous = 0;
    for (uint32_t i = 0; i < a.count; ++i) {
      uint32_t next = word(bytes + end + 4 * i);
      if (next < previous || next - previous < 10 || next - previous > 5000) return "frame duration";
      previous = next;
    }
    if (previous != a.cycle) return "animation cycle";
    if (pill) {
      if (a.colorCount != 1) return "pill palette";
      const auto* frames = bytes + end + a.count * 4;
      for (uint32_t i=0; i<a.count; ++i) {
        const auto* f = frames + i*6;
        if (f[0] > 15 || f[1] < 5 || f[1] > 15 ||
            f[3] < 5 || f[3] > 15 || f[4] < 5 || f[4] > 15 || f[5] < 5 || f[5] > 15)
          return "pill visibility";
      }
    }
    for (uint32_t i = 0; i < a.colorCount; ++i)
      if (a.colors[i] > 0xFFFFFF || peak(a.colors[i]) < 96) return "animation color";
    bool light = false;
    for (uint32_t i = end + a.count * 4; i < end + length; ++i) light |= bytes[i] != 0;
    if (!light) return "blank animation";
    end += length;
  }
  return end == size ? nullptr : "unexpected trailing data";
}
} // namespace skin_format
