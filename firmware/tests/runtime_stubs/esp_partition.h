#pragma once
#include "Arduino.h"
constexpr int ESP_OK = 0, ESP_PARTITION_TYPE_DATA = 1, ESP_PARTITION_SUBTYPE_DATA_SPIFFS = 2;
struct esp_partition_t { uint32_t size; };
static esp_partition_t simulatedPartition{0x7E0000};
static std::vector<uint8_t> simulatedFlash(simulatedPartition.size, 255);
static int failFlashWriteAfter = -1;
static bool missingPartition = false, failFlashRead = false;
static int eraseCount = 0;
inline const esp_partition_t* esp_partition_find_first(int, int, const char*) { return missingPartition ? nullptr : &simulatedPartition; }
inline int esp_partition_read(const esp_partition_t*, size_t offset, void* target, size_t length) {
  if (failFlashRead || offset > simulatedFlash.size() || length > simulatedFlash.size()-offset) return -1;
  memcpy(target, simulatedFlash.data()+offset, length); return 0;
}
inline int esp_partition_write(const esp_partition_t*, size_t offset, const void* source, size_t length) {
  if (offset > simulatedFlash.size() || length > simulatedFlash.size()-offset) return -1;
  const uint8_t* bytes = static_cast<const uint8_t*>(source);
  for (size_t i=0; i<length; ++i) {
    if (failFlashWriteAfter == 0) return -1;
    if (failFlashWriteAfter > 0) --failFlashWriteAfter;
    if ((simulatedFlash[offset+i] & bytes[i]) != bytes[i]) return -1;
    simulatedFlash[offset+i] &= bytes[i];
  }
  return 0;
}
inline int esp_partition_erase_range(const esp_partition_t*, size_t offset, size_t length) {
  if (offset % 4096 || length % 4096 || offset > simulatedFlash.size() || length > simulatedFlash.size()-offset) return -1;
  ++eraseCount;
  memset(simulatedFlash.data()+offset, 255, length); return 0;
}
