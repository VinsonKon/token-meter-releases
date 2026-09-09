#pragma once
static int8_t persistedActive = -1;
static bool failPreferenceWrite = false;
static bool failPreferenceBegin = false;
class Preferences {
 public:
  bool begin(const char*, bool) { return !failPreferenceBegin; }
  int8_t getChar(const char*, int8_t) { return persistedActive; }
  size_t putChar(const char*, int8_t value) {
    if (failPreferenceWrite) return 0;
    persistedActive = value; return 1;
  }
};
