#include "skin_sampler.h"
#include "sofia_bold28.h"
#include <algorithm>
#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <random>
#include <vector>

uint64_t checks = 0;
void require(bool condition, const char* reason) {
  ++checks;
  if (!condition) { std::cerr << reason << '\n'; std::exit(1); }
}
int textWidth(const char* text, const AA4Font& font) {
  int width = 0;
  for (; *text; ++text) width += font.glyphs[uint8_t(*text)-font.first].xAdvance;
  return width;
}
int main(int argc, char** argv) {
  require(argc == 2, "skin_host requires a compiled skinbin");
  std::ifstream stream(argv[1], std::ios::binary);
  std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(stream)), {});
  require(!skin_format::validate(bytes.data(), bytes.size()), "compiled skin validation");
  skin_format::Header h;
  memcpy(&h, bytes.data(), sizeof(h));
  for (size_t size = 0; size < bytes.size(); ++size)
    require(skin_format::validate(bytes.data(), size) != nullptr, "truncated skin accepted");
  const char* labels[] = {"THINKING", "NEED APPROVAL", "DATA STALE", "WAITING FOR SYNC"};
  for (int state = 0; state < 4; ++state) {
    const auto& font = h.animations[state].font == 28 ? SofiaBold28 : SofiaBold32;
    const int width = textWidth(labels[state], font);
    // Most space-consuming supported layout, including optional sync backplates.
    require(34 + 36 + 12 + 10 + width + 10 <= 356, "status label clipped by allowed geometry");
    std::cout << labels[state] << " width=" << width << '\n';
  }
  std::array<uint16_t, 66*66> original{}, imported{};
  unsigned maxDelta = 0;
  for (uint32_t state = 0; state < 4; ++state) {
    for (uint32_t elapsed = 0; elapsed < 12001; elapsed += 37) {
      original.fill(0x1234); imported.fill(0x1234);
      auto native = status_pixel::sample(static_cast<status_pixel::Mode>(state), elapsed);
      auto external = skin_sampler::sample(bytes.data(), h.animations[state], elapsed);
      status_pixel::draw(original.data()+67, 66, native);
      status_pixel::draw(imported.data()+67, 66, external);
      for (int y = 0; y < 66; ++y) for (int x = 0; x < 66; ++x) {
        const auto a = original[y*66+x], b = imported[y*66+x];
        if (x == 0 || x == 65 || y == 0 || y == 65) require(b == 0x1234, "skin writes outside tile");
        for (int shift : {0,5,11}) {
          const int mask = shift == 5 ? 63 : 31;
          const unsigned delta = std::abs(int((a>>shift)&mask)-int((b>>shift)&mask));
          maxDelta = std::max(maxDelta, delta);
          require(delta <= 2, "imported animation differs from approved core beyond quantization tolerance");
        }
      }
      if (state < 2) {
        auto oldLight = status_label::sample(state == 1, elapsed);
        auto newLight = skin_sampler::light(h.animations[state], elapsed);
        require(oldLight.card == newLight.card && oldLight.glow == newLight.glow, "backplate breath changed");
      }
    }
    // Shape interpolation closes onto frame zero independently of color timing.
    auto last = skin_sampler::sample(bytes.data(), h.animations[state], h.animations[state].cycle-1);
    auto first = skin_sampler::sample(bytes.data(), h.animations[state], 0);
    if (last.reference) require(last.referenceMix > 230, "bitmap loop seam not interpolated");
    else for (int i=0;i<9;++i) require(std::abs(last.cells[i]-first.cells[i]) < 0.03f, "cell loop seam");
  }
  std::mt19937 random(0x51a1);
  unsigned accepted = 0;
  for (int test = 0; test < 3000; ++test) {
    auto mutated = bytes;
    for (unsigned count=0; count < 1+random()%4; ++count) {
      const size_t at = random() % sizeof(h);
      mutated[at] = uint8_t(random());
    }
    if (skin_format::validate(mutated.data(), mutated.size())) continue;
    ++accepted;
    skin_format::Header modified;
    memcpy(&modified, mutated.data(), sizeof(modified));
    for (auto& a : modified.animations) {
      auto frame = skin_sampler::sample(mutated.data(), a, UINT32_MAX);
      imported.fill(0x1234);
      status_pixel::draw(imported.data()+67, 66, frame);
      for (int p=0;p<66;++p) {
        require(imported[p] == 0x1234 && imported[65*66+p] == 0x1234 &&
          imported[p*66] == 0x1234 && imported[p*66+65] == 0x1234, "fuzz canary damaged");
      }
    }
  }
  std::cout << "Skin host: " << checks << " checks; max RGB565 channel delta=" << maxDelta
            << "; valid mutated headers=" << accepted << '\n';
}
