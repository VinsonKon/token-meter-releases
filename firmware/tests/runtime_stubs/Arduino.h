#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <deque>
#include <vector>
using String = std::string;
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define PROGMEM
static uint32_t simulatedMillis = 1;
inline uint32_t millis() { return simulatedMillis; }
inline size_t strlcpy(char* target, const char* source, size_t capacity) {
  size_t length = strlen(source);
  if (capacity) { size_t count = length < capacity-1 ? length : capacity-1; memcpy(target, source, count); target[count] = 0; }
  return length;
}
struct FakeQueue { size_t limit, itemSize; std::deque<std::vector<uint8_t>> items; };
using QueueHandle_t = FakeQueue*;
constexpr int pdTRUE = 1;
inline QueueHandle_t xQueueCreate(size_t count, size_t size) { return new FakeQueue{count,size,{}}; }
inline int xQueueSend(QueueHandle_t queue, const void* value, int) {
  if (queue->items.size() == queue->limit) return 0;
  const auto* bytes = static_cast<const uint8_t*>(value);
  queue->items.emplace_back(bytes, bytes + queue->itemSize); return 1;
}
inline int xQueueReceive(QueueHandle_t queue, void* value, int) {
  if (queue->items.empty()) return 0;
  memcpy(value, queue->items.front().data(), queue->itemSize); queue->items.pop_front(); return 1;
}
inline void xQueueReset(QueueHandle_t queue) { queue->items.clear(); }
