#include "skin_runtime.h"
#include "skin_sampler.h"
#include <ArduinoJson.h>
#include <Preferences.h>
#include <esp_partition.h>
#include <esp_heap_caps.h>
#include <mbedtls/sha256.h>
#include <atomic>

namespace {
constexpr uint32_t kLegacySlotSize = 0x90000;
constexpr uint32_t kSlotSize = 0x68000;
constexpr uint32_t kPayloadOffset = 4096;
static_assert(kPayloadOffset + skin_format::kMaxBytes <= kSlotSize, "skin slot capacity");
// NVS active 0/1 retain their old meaning; 2/3 identify tail slots atomically.
// Old slots are read-only. No migration erases or copies unknown partition data.
struct Stored {
  char magic[8];
  uint32_t size;
  uint8_t hash[32];
  uint32_t pending; // FFFFFFFF while writing; zero committed last.
};
struct Event { uint16_t length; bool control; uint8_t data[240]; };
Preferences preferences;
const esp_partition_t* partition = nullptr;
const char* storageError = "skin storage not initialized";
uint32_t slotOffset(int slot) {
  return slot < 2 ? uint32_t(slot) * kLegacySlotSize
      : partition->size - 2 * kSlotSize + uint32_t(slot - 2) * kSlotSize;
}
uint8_t* currentBytes = nullptr;
int currentSlot = -1, incomingSlot = -1;
uint32_t generation = 0, expected = 0, received = 0, lastWrite = 0;
uint8_t expectedHash[32] = {};
char transaction[17] = "", currentHash[65] = "";
std::atomic<bool> receiving{false};
QueueHandle_t events = nullptr;
portMUX_TYPE statusMux = portMUX_INITIALIZER_UNLOCKED;
char statusJson[384] = "{\"schema\":1,\"state\":\"idle\",\"id\":\"builtin\"}";

void hex(const uint8_t* bytes, char* text) {
  static const char chars[] = "0123456789abcdef";
  for (int i = 0; i < 32; ++i) { text[i*2] = chars[bytes[i] >> 4]; text[i*2+1] = chars[bytes[i] & 15]; }
  text[64] = 0;
}
bool unhex(const char* text, uint8_t* output) {
  if (!text || strlen(text) != 64) return false;
  for (int i = 0; i < 32; ++i) {
    uint8_t value = 0;
    for (int j = 0; j < 2; ++j) {
      char c = text[i*2+j];
      if (!(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f')) return false;
      value = value * 16 + (c <= '9' ? c-'0' : c-'a'+10);
    }
    output[i] = value;
  }
  return true;
}
void report(const char* state, const char* error = "") {
  char json[384] = {};
  snprintf(json, sizeof(json),
      "{\"schema\":1,\"schemas\":[1,2],\"storage\":2,\"max_bytes\":%u,\"state\":\"%s\",\"tx\":\"%s\",\"offset\":%u,\"id\":\"%s\",\"sha256\":\"%s\",\"error\":\"%s\"}",
      skin_format::kMaxBytes, state, transaction, received, skin::id(), currentHash, error);
  portENTER_CRITICAL(&statusMux);
  memcpy(statusJson, json, sizeof(json));
  portEXIT_CRITICAL(&statusMux);
}
void fail(const char* error) { receiving = false; incomingSlot = -1; report("error", error); }
bool sameMagic(const Stored& stored, int slot) {
  return memcmp(stored.magic, slot < 2 ? "TMSLOT1" : "TMSLOT2", 8) == 0;
}
uint8_t* readSlot(int slot, bool requireCommitted) {
  if (!partition || slot < 0 || slot > 3) return nullptr;
  Stored stored;
  const uint32_t offset = slotOffset(slot);
  const uint32_t limit = slot < 2 ? skin_format::kLegacyMaxBytes : skin_format::kMaxBytes;
  if (esp_partition_read(partition, offset, &stored, sizeof(stored)) != ESP_OK ||
      !sameMagic(stored, slot) || (requireCommitted && stored.pending != 0) ||
      stored.size < sizeof(skin_format::Header) || stored.size > limit) return nullptr;
  uint8_t* bytes = static_cast<uint8_t*>(heap_caps_malloc(stored.size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!bytes) return nullptr;
  uint8_t digest[32];
  if (esp_partition_read(partition, offset + kPayloadOffset, bytes, stored.size) != ESP_OK ||
      mbedtls_sha256(bytes, stored.size, digest, 0) != 0 || memcmp(digest, stored.hash, 32) ||
      skin_format::validate(bytes, stored.size, limit)) { free(bytes); return nullptr; }
  return bytes;
}
const char* writableSlot(int slot) {
  if (slot < 2 || slot > 3 || slot == currentSlot) return "skin slot protected";
  const uint32_t offset = slotOffset(slot);
  Stored stored;
  if (esp_partition_read(partition, offset, &stored, sizeof(stored)) != ESP_OK) return "skin storage read failed";
  if (sameMagic(stored, slot) && stored.size >= sizeof(skin_format::Header) &&
      stored.size <= skin_format::kMaxBytes && (stored.pending == 0 || stored.pending == 0xFFFFFFFFu)) return nullptr;
  // Never format a filesystem or erase unidentified user data in the partition.
  uint8_t buffer[512];
  for (uint32_t at = 0; at < kSlotSize; at += sizeof(buffer)) {
    if (esp_partition_read(partition, offset + at, buffer, sizeof(buffer)) != ESP_OK) return "skin storage read failed";
    for (uint8_t byte : buffer) if (byte != 255) return "skin slot contains unrelated data";
  }
  return nullptr;
}
class Input final : public BLECharacteristicCallbacks {
 public:
  explicit Input(bool control) : control_(control) {}
  void onWrite(BLECharacteristic* c) override {
    const String value = c->getValue();
    if (!events || value.length() == 0 || value.length() > 240) return;
    Event e{}; e.control = control_; e.length = static_cast<uint16_t>(value.length());
    memcpy(e.data, value.c_str(), e.length);
    xQueueSend(events, &e, 0); // Flash work and renderer mutations stay on the main loop.
  }
 private: bool control_;
};
class Status final : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic* c) override {
    char json[384];
    portENTER_CRITICAL(&statusMux);
    memcpy(json, statusJson, sizeof(json));
    portEXIT_CRITICAL(&statusMux);
    c->setValue(reinterpret_cast<uint8_t*>(json), strlen(json));
  }
};
} // namespace

namespace skin {
void begin() {
  events = xQueueCreate(8, sizeof(Event));
  storageError = "skin preferences unavailable";
  if (!preferences.begin("meter-skin", false)) { report("error", storageError); return; }
  partition = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "spiffs");
  storageError = !partition ? "skin partition missing" :
      (partition->size % 4096 || partition->size < 2 * (kLegacySlotSize + kSlotSize))
      ? "skin partition size invalid" : nullptr;
  if (storageError) { partition = nullptr; report("error", storageError); return; }
  const int slot = preferences.getChar("active", -1);
  // Reserve the persisted selection even if boot-time PSRAM allocation fails.
  // A subsequent upload must never stage over that recoverable active skin.
  currentSlot = slot >= 0 && slot <= 3 ? slot : -1;
  currentBytes = readSlot(slot, true);
  if (currentBytes) {
    currentSlot = slot;
    uint8_t digest[32];
    mbedtls_sha256(currentBytes, active()->size, digest, 0);
    hex(digest, currentHash);
  }
  report("active");
}
void attachBle(BLEService* service) {
  auto* control = service->createCharacteristic("7f520008-1b15-4f9f-8bd8-29dbe9e681a1", BLECharacteristic::PROPERTY_WRITE);
  auto* data = service->createCharacteristic("7f520009-1b15-4f9f-8bd8-29dbe9e681a1", BLECharacteristic::PROPERTY_WRITE);
  auto* status = service->createCharacteristic("7f52000a-1b15-4f9f-8bd8-29dbe9e681a1", BLECharacteristic::PROPERTY_READ);
  static Input controlCallbacks(true), dataCallbacks(false);
  static Status statusCallbacks;
  control->setCallbacks(&controlCallbacks); data->setCallbacks(&dataCallbacks);
  status->setCallbacks(&statusCallbacks);
}
bool poll(bool connected, bool otaBusy) {
  if (!events) return false;
  if (!connected) {
    if (receiving) fail("disconnected; previous skin retained");
    xQueueReset(events);
    return false;
  }
  if (receiving && (otaBusy || millis() - lastWrite > 30000u)) fail("transfer interrupted");
  Event e;
  if (xQueueReceive(events, &e, 0) != pdTRUE) return false;
  if (!e.control) {
    if (!receiving || e.length < 5 || e.length > 180) return false;
    const uint32_t offset = skin_format::word(e.data);
    const uint32_t length = e.length - 4;
    if (offset != received || length > expected - received) { fail("chunk offset"); return false; }
    if (esp_partition_write(partition, slotOffset(incomingSlot) + kPayloadOffset + received,
                            e.data + 4, length) != ESP_OK) { fail("storage write failed"); return false; }
    received += length; lastWrite = millis(); report("receiving");
    return false;
  }
  JsonDocument doc;
  if (deserializeJson(doc, e.data, e.length, DeserializationOption::NestingLimit(3))) return false;
  const char* op = doc["op"] | "";
  const char* tx = doc["tx"] | "";
  const size_t txLength = strlen(tx);
  if (txLength == 0 || txLength > 16) return false;
  for (size_t i = 0; i < txLength; ++i) if (!(tx[i] >= '0' && tx[i] <= '9') && !(tx[i] >= 'a' && tx[i] <= 'f')) return false;
  if (strcmp(op, "abort") == 0) {
    if (strcmp(tx, transaction) == 0 && receiving) fail("transfer cancelled");
    return false;
  }
  if (strcmp(op, "commit") == 0) {
    if (!receiving || strcmp(tx, transaction)) return false;
    if (received != expected || otaBusy) { fail("incomplete skin"); return false; }
    uint8_t* candidate = readSlot(incomingSlot, false);
    if (!candidate) { fail("skin validation failed"); return false; }
    uint32_t committed = 0;
    if (esp_partition_write(partition, slotOffset(incomingSlot) + offsetof(Stored, pending), &committed, 4) != ESP_OK ||
        preferences.putChar("active", static_cast<int8_t>(incomingSlot)) != 1) {
      free(candidate); fail("skin commit failed"); return false;
    }
    uint8_t* old = currentBytes;
    currentBytes = candidate; currentSlot = incomingSlot; incomingSlot = -1;
    receiving = false; ++generation; hex(expectedHash, currentHash);
    free(old); report("active"); return true;
  }
  // A second begin/default must not replace another active transaction.
  if (receiving) return false;
  strlcpy(transaction, tx, sizeof(transaction));
  if (otaBusy) { fail("firmware update in progress"); return false; }
  if (strcmp(op, "default") == 0) {
    if (preferences.putChar("active", -1) != 1) { fail("skin preference failed"); return false; }
    free(currentBytes); currentBytes = nullptr; currentSlot = -1; currentHash[0] = 0;
    received = 0; ++generation; report("active"); return true;
  }
  if (strcmp(op, "begin")) { fail("unknown skin command"); return false; }
  if (!doc["size"].is<uint32_t>()) { fail("skin size"); return false; }
  expected = doc["size"].as<uint32_t>();
  if (expected < sizeof(skin_format::Header) || expected > skin_format::kMaxBytes ||
      !unhex(doc["sha256"] | "", expectedHash)) { fail("skin metadata"); return false; }
  if (storageError) { fail(storageError); return false; }
  incomingSlot = currentSlot == 2 ? 3 : 2;
  const char* error = writableSlot(incomingSlot);
  // When builtin/legacy is active, either tail slot can stage the first skin.
  if (error && currentSlot < 2 && strcmp(error, "skin slot contains unrelated data") == 0) {
    incomingSlot = 3;
    error = writableSlot(incomingSlot);
  }
  if (error) { fail(error); return false; }
  Stored stored{};
  memcpy(stored.magic, "TMSLOT2", 8); stored.size = expected;
  memcpy(stored.hash, expectedHash, 32); stored.pending = 0xFFFFFFFFu;
  const uint32_t offset = slotOffset(incomingSlot);
  if (esp_partition_erase_range(partition, offset, kSlotSize) != ESP_OK ||
      esp_partition_write(partition, offset, &stored, sizeof(stored)) != ESP_OK) { fail("skin storage preparation failed"); return false; }
  received = 0; receiving = true; lastWrite = millis(); report("receiving");
  return false;
}
bool busy() { return receiving; }
bool imported() { return currentBytes != nullptr; }
const skin_format::Header* active() { return reinterpret_cast<const skin_format::Header*>(currentBytes); }
const char* id() { return imported() ? active()->id : "builtin"; }
int16_t layout(skin_format::Layout field) { return static_cast<int16_t>(imported() ? active()->layout[field] : skin_format::kDefaults[field]); }
uint32_t revision() { return generation; }
const skin_format::Animation* animation(status_pixel::Mode mode) {
  return imported() ? &active()->animations[static_cast<uint8_t>(mode)] : nullptr;
}
status_pixel::Frame sample(status_pixel::Mode mode, uint32_t elapsed) {
  const auto* a = animation(mode);
  return a ? skin_sampler::sample(currentBytes, *a, elapsed) : status_pixel::sample(mode, elapsed);
}
status_label::Look light(status_pixel::Mode mode, uint32_t elapsed) {
  const auto* a = animation(mode);
  if (!a) return status_label::sample(mode == status_pixel::Mode::Approval, elapsed);
  return skin_sampler::light(*a, elapsed);
}
} // namespace skin
