#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLEDevice.h>
#include <Preferences.h>
#include <Update.h>
#include <SensorQMI8658.hpp>
#include <TouchDrv.hpp>
#include <Wire.h>
#include <XPowersLib.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>

#include "board_config.h"
#include "codex_packet.h"
#include "firmware_version.h"
#include "renderer.h"
#include "ui_theme.h"
#include "skin_runtime.h"

using codex_meter::CodexPacket;
namespace {

XPowersPMU power;
Preferences preferences;
QueueHandle_t packetQueue = nullptr;
BLECharacteristic* ackCharacteristic = nullptr;
BLECharacteristic* deviceInfoCharacteristic = nullptr;
BLECharacteristic* otaStatusCharacteristic = nullptr;
SensorQMI8658 imu;
TouchDrvCST92xx touch;

CodexPacket snapshot{};
bool hasSnapshot = false;
bool snapshotIsFresh = false;
bool bleConnected = false;
bool pmuAvailable = false;
bool imuAvailable = false;
bool touchAvailable = false;
volatile bool touchPending = false;
uint32_t snapshotReceivedMs = 0;
uint32_t lastDataActivityMs = 0;
uint32_t lastRenderMs = 0;
uint32_t lastBatteryReadMs = 0;
uint32_t lastImuReadMs = 0;
uint32_t lastTouchMs = 0;
uint32_t lastTouchLogMs = 0;
uint32_t lastNavigationMs = 0;
uint32_t lastControlRenderMs = 0;
uint32_t controlsChangedMs = 0;
int batteryPercent = -1;
bool batteryCharging = false;
bool touchActive = false;
bool controlRenderPending = false;
bool controlsDirty = false;
String bleDeviceName = codex_meter::kDeviceName;
uint8_t brightnessPercent = 70;
uint8_t volumePercent = 50;
uint8_t rotationCandidate = 0;
uint8_t rotationCandidateCount = 0;
bool otaActive = false;
bool otaShaStarted = false;
volatile bool otaUiDirty = false;
uint32_t otaExpectedSize = 0;
uint32_t otaBytesWritten = 0;
uint32_t otaVerifyAtMs = 0;
uint32_t otaRebootAtMs = 0;
uint32_t otaUiExpiresAtMs = 0;
uint8_t otaExpectedSha256[32]{};
uint8_t otaUiProgress = 0;
uint8_t otaLastNotifiedProgress = 255;
char otaUiStatus[32] = "PREPARING";
char otaTargetVersion[20]{};
mbedtls_sha256_context otaShaContext;
portMUX_TYPE otaUiMux = portMUX_INITIALIZER_UNLOCKED;
constexpr uint32_t kRenderIntervalMs = 10000u;
#ifndef CODEX_METER_SLEEP_TIMEOUT_MS
#define CODEX_METER_SLEEP_TIMEOUT_MS (60u * 60u * 1000u)
#endif
constexpr uint32_t kDisplaySleepTimeoutMs = CODEX_METER_SLEEP_TIMEOUT_MS;
constexpr uint32_t kInputDebounceMs = 45u;
constexpr uint32_t kNavigationCooldownMs = 300u;
constexpr uint32_t kControlFrameIntervalMs = 33u;
constexpr uint32_t kControlPersistDelayMs = 500u;
constexpr uint8_t kDisplayRotationOffset = 1u;  // Board is mounted 90 degrees from the panel's logical zero.
constexpr uint8_t kMinimumOtaBatteryPercent = 30u;

void logHeap(const char* stage) {
  Serial.printf("Heap after %s: internal=%u, psram=%u, valid=%s\n", stage,
                static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)),
                heap_caps_check_integrity_all(true) ? "yes" : "NO");
}

uint32_t semanticVersionKey(const char* version) {
  uint32_t result = 0;
  const char* cursor = version ? version : "";
  if (*cursor == 'v' || *cursor == 'V') ++cursor;
  for (uint8_t index = 0; index < 3; ++index) {
    result = result * 1000u + min<uint32_t>(strtoul(cursor, nullptr, 10), 999u);
    const char* dot = strchr(cursor, '.');
    if (!dot) {
      for (++index; index < 3; ++index) result *= 1000u;
      break;
    }
    cursor = dot + 1;
  }
  return result;
}

bool parseSha256(const char* text, uint8_t output[32]) {
  if (!text || strlen(text) != 64) return false;
  auto nibble = [](char value) -> int8_t {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
  };
  for (uint8_t index = 0; index < 32; ++index) {
    const int8_t high = nibble(text[index * 2]);
    const int8_t low = nibble(text[index * 2 + 1]);
    if (high < 0 || low < 0) return false;
    output[index] = static_cast<uint8_t>((high << 4) | low);
  }
  return true;
}

void setOtaUi(const char* status, uint8_t progress) {
  portENTER_CRITICAL(&otaUiMux);
  strlcpy(otaUiStatus, status ? status : "UPDATING", sizeof(otaUiStatus));
  otaUiProgress = min<uint8_t>(progress, 100);
  otaUiDirty = true;
  portEXIT_CRITICAL(&otaUiMux);
}

void notifyOtaStatus(const char* state, const char* error = "") {
  if (!bleConnected || !otaStatusCharacteristic) return;
  char payload[192] = {};
  const uint8_t progress = otaExpectedSize
      ? min<uint32_t>(100u, otaBytesWritten * 100u / otaExpectedSize)
      : 0;
  snprintf(payload, sizeof(payload),
           "{\"state\":\"%s\",\"offset\":%lu,\"size\":%lu,"
           "\"progress\":%u,\"error\":\"%s\"}",
           state ? state : "idle",
           static_cast<unsigned long>(otaBytesWritten),
           static_cast<unsigned long>(otaExpectedSize),
           progress, error ? error : "");
  otaStatusCharacteristic->setValue(
      reinterpret_cast<uint8_t*>(payload), strlen(payload));
  otaStatusCharacteristic->notify();
}

void releaseOtaSha() {
  if (!otaShaStarted) return;
  mbedtls_sha256_free(&otaShaContext);
  otaShaStarted = false;
}

void failOta(const char* error, bool notify = true) {
  if (otaActive) Update.abort();
  otaActive = false;
  releaseOtaSha();
  otaVerifyAtMs = 0;
  otaRebootAtMs = 0;
  otaUiExpiresAtMs = millis() + 8000u;
  setOtaUi("UPDATE FAILED", otaExpectedSize
                                 ? min<uint32_t>(
                                       100u,
                                       otaBytesWritten * 100u / otaExpectedSize)
                                 : 0);
  if (notify) notifyOtaStatus("error", error ? error : "update failed");
  Serial.printf("OTA failed: %s\n", error ? error : "update failed");
}

void refreshDeviceInfo() {
  if (!deviceInfoCharacteristic) return;
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
  const auto stats = ui::statusRenderStats();
  char payload[384] = {};
  const int written = snprintf(payload, sizeof(payload),
           "{\"version\":\"%s\",\"protocol\":%u,\"skin\":1,\"ota\":%s,"
           "\"board\":\"%s\",\"slot\":\"%s\","
           "\"ui\":{\"state\":%u,\"frames\":%u,\"avg_us\":%u,\"max_us\":%u,\"over33\":%u,\"fps10\":%u}}",
           codex_meter::kFirmwareVersion, codex_meter::kProtocolVersion,
           next ? "true" : "false", codex_meter::kBoardId,
           running ? running->label : "", stats.state, stats.frames,
           stats.averageUs, stats.maximumUs, stats.overBudget, stats.fpsTenths);
  if (written < 0 || static_cast<size_t>(written) >= sizeof(payload)) return;
  deviceInfoCharacteristic->setValue(
      reinterpret_cast<uint8_t*>(payload), strlen(payload));
}

void confirmRunningFirmware() {
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (!running) {
    Serial.println("Unable to inspect running OTA partition");
    return;
  }
  esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
  const esp_err_t stateResult =
      esp_ota_get_state_partition(running, &state);
  if (stateResult != ESP_OK) {
    Serial.printf("Unable to read OTA state for %s: 0x%x\n",
                  running->label, static_cast<unsigned>(stateResult));
    return;
  }
  if (state != ESP_OTA_IMG_PENDING_VERIFY) return;

  // Reaching this point means display, input, power and BLE initialization
  // all completed. Confirm the new slot so a later user-initiated reboot does
  // not make the ESP32 bootloader roll back an otherwise healthy OTA image.
  const esp_err_t result = esp_ota_mark_app_valid_cancel_rollback();
  if (result == ESP_OK) {
    Serial.printf("OTA partition %s marked valid\n", running->label);
  } else {
    Serial.printf("Failed to mark OTA partition %s valid: 0x%x\n",
                  running->label, static_cast<unsigned>(result));
  }
}

bool beginOta(uint32_t size, const char* sha256, const char* version,
              const char* board) {
  if (skin::busy()) {
    notifyOtaStatus("error", "skin transfer in progress");
    return false;
  }
  if (!board || strcmp(board, codex_meter::kBoardId) != 0) {
    notifyOtaStatus("error", "wrong board");
    return false;
  }
  if (!version ||
      semanticVersionKey(version) <=
          semanticVersionKey(codex_meter::kFirmwareVersion)) {
    notifyOtaStatus("error", "version is not newer");
    return false;
  }
  if (size == 0 || !parseSha256(sha256, otaExpectedSha256)) {
    notifyOtaStatus("error", "invalid manifest");
    return false;
  }
  if (pmuAvailable && batteryPercent >= 0 &&
      batteryPercent < kMinimumOtaBatteryPercent && !batteryCharging) {
    notifyOtaStatus("error", "battery below 30 percent");
    return false;
  }
  if (!esp_ota_get_next_update_partition(nullptr)) {
    notifyOtaStatus("error", "OTA partition unavailable");
    return false;
  }
  if (otaActive) Update.abort();
  releaseOtaSha();
  if (!Update.begin(size, U_FLASH)) {
    Update.printError(Serial);
    notifyOtaStatus("error", "flash partition rejected update");
    return false;
  }
  mbedtls_sha256_init(&otaShaContext);
  if (mbedtls_sha256_starts(&otaShaContext, 0) != 0) {
    Update.abort();
    notifyOtaStatus("error", "SHA-256 initialization failed");
    return false;
  }
  otaShaStarted = true;
  otaExpectedSize = size;
  otaBytesWritten = 0;
  otaLastNotifiedProgress = 255;
  otaUiExpiresAtMs = 0;
  strlcpy(otaTargetVersion, version, sizeof(otaTargetVersion));
  otaActive = true;
  lastDataActivityMs = millis();
  setOtaUi("INSTALLING", 0);
  notifyOtaStatus("ready");
  Serial.printf("OTA begin: version=%s bytes=%lu\n", otaTargetVersion,
                static_cast<unsigned long>(otaExpectedSize));
  return true;
}

void writeOtaData(const String& value) {
  if (!otaActive || value.length() < 5) return;
  const uint8_t* bytes =
      reinterpret_cast<const uint8_t*>(value.c_str());
  const uint32_t offset = static_cast<uint32_t>(bytes[0]) |
                          (static_cast<uint32_t>(bytes[1]) << 8) |
                          (static_cast<uint32_t>(bytes[2]) << 16) |
                          (static_cast<uint32_t>(bytes[3]) << 24);
  const size_t payloadSize = value.length() - 4;
  if (offset != otaBytesWritten ||
      otaBytesWritten + payloadSize > otaExpectedSize) {
    failOta("unexpected firmware offset");
    return;
  }
  uint8_t* payload = const_cast<uint8_t*>(bytes + 4);
  if (Update.write(payload, payloadSize) != payloadSize) {
    Update.printError(Serial);
    failOta("flash write failed");
    return;
  }
  if (mbedtls_sha256_update(&otaShaContext, payload, payloadSize) != 0) {
    failOta("SHA-256 update failed");
    return;
  }
  otaBytesWritten += payloadSize;
  const uint8_t progress = min<uint32_t>(
      100u, otaBytesWritten * 100u / otaExpectedSize);
  setOtaUi("INSTALLING", progress);
  if (otaLastNotifiedProgress == 255 ||
      progress >= static_cast<uint8_t>(otaLastNotifiedProgress + 2) ||
      progress == 100) {
    otaLastNotifiedProgress = progress;
    notifyOtaStatus("progress");
  }
}

void finishOta() {
  if (!otaActive) {
    notifyOtaStatus("error", "no update in progress");
    return;
  }
  if (otaBytesWritten != otaExpectedSize) {
    failOta("firmware is incomplete");
    return;
  }
  setOtaUi("VERIFYING", 100);
  notifyOtaStatus("verifying");
  otaVerifyAtMs = millis() + 850u;
}

void completeOtaVerification() {
  if (!otaActive || !otaVerifyAtMs) return;
  otaVerifyAtMs = 0;
  uint8_t actualSha256[32] = {};
  if (mbedtls_sha256_finish(&otaShaContext, actualSha256) != 0) {
    failOta("SHA-256 finalization failed");
    return;
  }
  releaseOtaSha();
  if (memcmp(actualSha256, otaExpectedSha256, sizeof(actualSha256)) != 0) {
    failOta("firmware SHA-256 mismatch");
    return;
  }
  if (!Update.end(false)) {
    Update.printError(Serial);
    failOta("firmware image validation failed");
    return;
  }
  otaActive = false;
  setOtaUi("RESTARTING", 100);
  notifyOtaStatus("restarting");
  otaRebootAtMs = millis() + 1500u;
  Serial.printf("OTA complete: %s\n", otaTargetVersion);
}

bool isExternalUpdateState(const char* state) {
  if (!state) return false;
  return strcmp(state, "IDLE") == 0 ||
         strcmp(state, "UPDATE AVAILABLE") == 0 ||
         strcmp(state, "DOWNLOADING") == 0 ||
         strcmp(state, "INSTALLING") == 0 ||
         strcmp(state, "VERIFYING") == 0 ||
         strcmp(state, "RESTARTING") == 0 ||
         strcmp(state, "UPDATE FAILED") == 0 ||
         strcmp(state, "ROLLED BACK") == 0;
}

void displayExternalUpdateState(const char* rawState, uint8_t progress) {
  String state = rawState ? rawState : "";
  state.trim();
  state.toUpperCase();
  if (!isExternalUpdateState(state.c_str())) {
    notifyOtaStatus("error", "unsupported display state");
    return;
  }
  if (otaActive && state != "IDLE") {
    notifyOtaStatus("error", "OTA is already in progress");
    return;
  }
  setOtaUi(state.c_str(), progress);
  const uint32_t now = millis();
  if (state == "UPDATE AVAILABLE") {
    otaUiExpiresAtMs = now + 5u * 60u * 1000u;
  } else if (state == "DOWNLOADING") {
    otaUiExpiresAtMs = now + 30000u;
  } else if (state == "UPDATE FAILED" || state == "ROLLED BACK") {
    otaUiExpiresAtMs = now + 8000u;
  } else {
    otaUiExpiresAtMs = 0;
  }
  notifyOtaStatus("displayed");
}

bool usageWindowChanged(const codex_meter::UsageWindow& left,
                        const codex_meter::UsageWindow& right) {
  return left.used_percent != right.used_percent ||
         left.window_minutes != right.window_minutes ||
         left.resets_at != right.resets_at ||
         left.tokens_used != right.tokens_used;
}

bool packetDataChanged(const CodexPacket& left, const CodexPacket& right) {
  // Sequence and generated_at intentionally do not count: the desktop sends a
  // 30-second heartbeat even when the displayed data is unchanged.
  return left.flags != right.flags ||
         usageWindowChanged(left.primary, right.primary) ||
         usageWindowChanged(left.secondary, right.secondary) ||
         left.input_tokens != right.input_tokens ||
         left.cached_input_tokens != right.cached_input_tokens ||
         left.output_tokens != right.output_tokens ||
         left.reasoning_output_tokens != right.reasoning_output_tokens ||
         left.total_tokens != right.total_tokens ||
         left.reset_count != right.reset_count ||
         left.cost_cny_cents != right.cost_cny_cents ||
         strcmp(left.plan, right.plan) != 0 ||
         strcmp(left.provider, right.provider) != 0 ||
         strcmp(left.model, right.model) != 0 ||
         strcmp(left.account_label, right.account_label) != 0 ||
         strcmp(left.status, right.status) != 0 ||
         left.needs_approval != right.needs_approval ||
         left.test_state != right.test_state;
}

bool readPacketFromNvs() {
  if (preferences.getBytesLength("snapshot") != sizeof(CodexPacket)) {
    return false;
  }
  CodexPacket saved{};
  if (preferences.getBytes("snapshot", &saved, sizeof(saved)) != sizeof(saved) ||
      !codex_meter::validateStoredData(saved)) {
    return false;
  }
  snapshot = saved;
  hasSnapshot = true;
  snapshotIsFresh = false;
  snapshotReceivedMs = millis();
  return true;
}

void savePacketToNvs(const CodexPacket& packet) {
  preferences.putBytes("snapshot", &packet, sizeof(packet));
}

void notifyAck(uint32_t sequence) {
  if (!bleConnected || !ackCharacteristic) return;
  uint8_t ack[5] = {
      codex_meter::kProtocolVersion,
      static_cast<uint8_t>(sequence),
      static_cast<uint8_t>(sequence >> 8),
      static_cast<uint8_t>(sequence >> 16),
      static_cast<uint8_t>(sequence >> 24),
  };
  ackCharacteristic->setValue(ack, sizeof(ack));
  ackCharacteristic->notify();
}

bool updateBattery() {
  if (!pmuAvailable || millis() - lastBatteryReadMs < 10000u) return false;
  lastBatteryReadMs = millis();
  const bool nextCharging = power.isCharging();
  const int nextPercent = power.isBatteryConnect() ? power.getBatteryPercent() : -1;
  const bool changed = nextCharging != batteryCharging ||
                       nextPercent != batteryPercent;
  batteryCharging = nextCharging;
  batteryPercent = nextPercent;
  return changed;
}

class ServerCallbacks final : public BLEServerCallbacks {
  void onConnect(BLEServer*) override {
    bleConnected = true;
  }

  void onDisconnect(BLEServer*) override {
    if (otaActive) failOta("Bluetooth connection lost", false);
    bleConnected = false;
  }
};

class DeviceInfoCallbacks final : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic*) override { refreshDeviceInfo(); }
};

class DataCallbacks final : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    if (otaActive) return;
    const String value = characteristic->getValue();
    if (value.isEmpty() || value.length() > codex_meter::kMaxJsonSize) {
      Serial.printf("Rejected BLE JSON length: %u\n",
                    static_cast<unsigned>(value.length()));
      return;
    }

    JsonDocument doc;
    const DeserializationError error = deserializeJson(doc, value);
    if (error || doc["v"].as<uint8_t>() != codex_meter::kProtocolVersion) {
      Serial.printf("Rejected BLE JSON: %s\n", error ? error.c_str() : "bad version");
      return;
    }

    CodexPacket packet{};
    packet.version = doc["v"] | codex_meter::kProtocolVersion;
    packet.sequence = doc["n"] | 0u;
    packet.generated_at = doc["ts"] | 0u;

    JsonArray q = doc["q"].as<JsonArray>();
    auto readWindow = [](JsonVariantConst source, codex_meter::UsageWindow& target) {
      JsonArrayConst a = source.as<JsonArrayConst>();
      target.used_percent = min<uint8_t>(a[0] | 0, 100);
      target.window_minutes = a[1] | 0;
      target.resets_at = a[2] | 0u;
      target.tokens_used = a[3] | 0u;
    };
    if (q.size() > 0 && !q[0].isNull()) {
      readWindow(q[0], packet.primary);
      packet.flags |= codex_meter::kPrimaryValid;
    }
    if (q.size() > 1 && !q[1].isNull()) {
      readWindow(q[1], packet.secondary);
      packet.flags |= codex_meter::kSecondaryValid;
    }

    JsonArray t = doc["t"].as<JsonArray>();
    if (t.size() >= 5) {
      packet.input_tokens = t[0] | 0ULL;
      packet.cached_input_tokens = t[1] | 0ULL;
      packet.output_tokens = t[2] | 0ULL;
      packet.reasoning_output_tokens = t[3] | 0ULL;
      packet.total_tokens = t[4] | 0ULL;
      packet.flags |= codex_meter::kTokenTotalsValid;
    }
    packet.reset_count = doc["rc"] | 0;
    const bool resetCountValid =
        doc["rcv"].is<bool>() ? doc["rcv"].as<bool>()
                              : !doc["rc"].isNull();
    if (resetCountValid) packet.flags |= codex_meter::kResetCountValid;
    packet.cost_cny_cents = doc["c"] | -1;
    strlcpy(packet.plan, doc["p"] | "", sizeof(packet.plan));
    strlcpy(packet.provider, doc["pr"] | "openai", sizeof(packet.provider));
    strlcpy(packet.model, doc["m"] | "", sizeof(packet.model));
    strlcpy(packet.account_label, doc["a"] | "", sizeof(packet.account_label));
    strlcpy(packet.status, doc["s"] | "", sizeof(packet.status));
    packet.needs_approval = doc["ap"] | false;
    packet.test_state = min<uint8_t>(
        doc["x"] | 0, codex_meter::kTestNeedApproval);
    if (packet.plan[0]) packet.flags |= codex_meter::kPlanValid;
    if (doc["r"] | false) packet.flags |= codex_meter::kRateLimitReached;
    if (doc["af"].is<bool>() && !doc["af"].as<bool>()) {
      packet.flags |= codex_meter::kAccountDataStale;
    }
    if (doc["lt"] | false) {
      packet.flags |= codex_meter::kLifetimeTokenTotal;
    }
    xQueueOverwrite(packetQueue, &packet);
  }
};

class OtaControlCallbacks final : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    const String value = characteristic->getValue();
    if (value.isEmpty() || value.length() > codex_meter::kMaxJsonSize) {
      notifyOtaStatus("error", "invalid OTA command");
      return;
    }
    JsonDocument doc;
    const DeserializationError error = deserializeJson(doc, value);
    if (error) {
      notifyOtaStatus("error", "invalid OTA JSON");
      return;
    }
    const char* operation = doc["op"] | "";
    if (strcmp(operation, "begin") == 0) {
      beginOta(
          doc["size"] | 0u,
          doc["sha256"] | "",
          doc["version"] | "",
          doc["board"] | "");
    } else if (strcmp(operation, "finish") == 0) {
      finishOta();
    } else if (strcmp(operation, "abort") == 0) {
      if (otaActive) {
        failOta("update cancelled");
      } else {
        notifyOtaStatus("idle");
      }
    } else if (strcmp(operation, "display") == 0) {
      const uint8_t progress = static_cast<uint8_t>(
          constrain(doc["progress"] | 0, 0, 100));
      displayExternalUpdateState(
          doc["state"] | "IDLE",
          progress);
    } else {
      notifyOtaStatus("error", "unsupported OTA command");
    }
  }
};

class OtaDataCallbacks final : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    writeOtaData(characteristic->getValue());
  }
};

void initBle() {
  packetQueue = xQueueCreate(1, sizeof(CodexPacket));
  uint8_t bluetoothMac[6] = {};
  esp_read_mac(bluetoothMac, ESP_MAC_BT);
  char uniqueName[24] = {};
  snprintf(uniqueName, sizeof(uniqueName), "%s-%02X%02X",
           codex_meter::kDeviceName, bluetoothMac[4], bluetoothMac[5]);
  bleDeviceName = uniqueName;
  ui::setDeviceName(bleDeviceName);
  BLEDevice::init(bleDeviceName.c_str());
  BLEDevice::setMTU(247);
  BLEDevice::setPower(ESP_PWR_LVL_P3);

  BLEServer* server = BLEDevice::createServer();
  static ServerCallbacks serverCallbacks;
  server->setCallbacks(&serverCallbacks);
  server->advertiseOnDisconnect(true);
  BLEService* service = server->createService(BLEUUID(codex_meter::kServiceUuid), 48);
  BLECharacteristic* data = service->createCharacteristic(
      codex_meter::kDataCharacteristicUuid,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  static DataCallbacks dataCallbacks;
  data->setCallbacks(&dataCallbacks);
  ackCharacteristic = service->createCharacteristic(
      codex_meter::kAckCharacteristicUuid,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  const uint8_t initialAck[5] = {codex_meter::kProtocolVersion, 0, 0, 0, 0};
  ackCharacteristic->setValue(initialAck, sizeof(initialAck));

  deviceInfoCharacteristic = service->createCharacteristic(
      codex_meter::kDeviceInfoCharacteristicUuid,
      BLECharacteristic::PROPERTY_READ);
  static DeviceInfoCallbacks deviceInfoCallbacks;
  deviceInfoCharacteristic->setCallbacks(&deviceInfoCallbacks);
  refreshDeviceInfo();

  BLECharacteristic* otaControl = service->createCharacteristic(
      codex_meter::kOtaControlCharacteristicUuid,
      BLECharacteristic::PROPERTY_WRITE);
  static OtaControlCallbacks otaControlCallbacks;
  otaControl->setCallbacks(&otaControlCallbacks);

  BLECharacteristic* otaData = service->createCharacteristic(
      codex_meter::kOtaDataCharacteristicUuid,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  static OtaDataCallbacks otaDataCallbacks;
  otaData->setCallbacks(&otaDataCallbacks);

  otaStatusCharacteristic = service->createCharacteristic(
      codex_meter::kOtaStatusCharacteristicUuid,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  otaStatusCharacteristic->setValue("{\"state\":\"idle\"}");
  skin::attachBle(service);
  service->start();

  BLEAdvertising* advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(codex_meter::kServiceUuid);
  advertising->setScanResponse(true);
  advertising->start();
  Serial.printf("BLE advertising as %s\n", bleDeviceName.c_str());
}

void initPower() {
  Wire.begin(I2C_SDA, I2C_SCL);
  pmuAvailable = power.begin(Wire, AXP2101_SLAVE_ADDRESS, I2C_SDA, I2C_SCL);
  if (!pmuAvailable) {
    Serial.println("AXP2101 not detected; battery indicator disabled");
    return;
  }
  power.enableBattDetection();
  power.enableBattVoltageMeasure();
  power.setChargeTargetVoltage(3);
  lastBatteryReadMs = millis() - 10000u;
  updateBattery();
}

void renderNow() {
  if (ui::displaySleeping()) {
    ui::wakeDisplay();
    Serial.println("Display awake");
  }
  ui::render(hasSnapshot ? &snapshot : nullptr, snapshotIsFresh, bleConnected,
             batteryPercent, batteryCharging, snapshotReceivedMs);
  lastRenderMs = millis();
}

bool writeCodecVolume(uint8_t percent) {
  percent = constrain(percent, 0, 100);
  const uint8_t registerValue = percent == 0
      ? 0
      : static_cast<uint8_t>((static_cast<uint16_t>(percent) * 256u / 100u) - 1u);
  Wire.beginTransmission(ES8311_I2C_ADDRESS);
  Wire.write(0x32);  // ES8311 DAC volume register.
  Wire.write(registerValue);
  return Wire.endTransmission() == 0;
}

void loadControls() {
  brightnessPercent = constrain(preferences.getUChar("brightness", 70), 10, 100);
  volumePercent = constrain(preferences.getUChar("volume", 50), 0, 100);
  ui::setControls(brightnessPercent, volumePercent);
  if (!writeCodecVolume(volumePercent)) {
    Serial.println("ES8311 volume register unavailable");
  }
}

void IRAM_ATTR onTouchInterrupt() {
  touchPending = true;
}

void initInputSensors() {
  pinMode(USER_BUTTON, INPUT_PULLUP);

  imuAvailable = imu.begin(
      Wire, QMI8658_L_SLAVE_ADDRESS, I2C_SDA, I2C_SCL);
  if (imuAvailable) {
    imu.configAccelerometer(SensorQMI8658::ACC_RANGE_4G,
                            SensorQMI8658::ACC_ODR_1000Hz,
                            SensorQMI8658::LPF_MODE_0);
    imu.enableAccelerometer();
    Serial.println("QMI8658 auto-rotation ready");
  } else {
    Serial.println("QMI8658 unavailable; auto-rotation disabled");
  }

  touch.setPins(TOUCH_RESET, TOUCH_INT);
  touchAvailable = touch.begin(
      Wire, TOUCH_ADDRESS, I2C_SDA, I2C_SCL);
  if (!touchAvailable) {
    touchAvailable = touch.begin(Wire, 0x1A, I2C_SDA, I2C_SCL);
  }
  if (touchAvailable) {
    touch.setMaxCoordinates(LCD_WIDTH, LCD_HEIGHT);
    // With the CO5300 kept in its native orientation, CST9220 coordinates are
    // already aligned to the composed frame. The Waveshare LVGL example swaps
    // XY only because that example also changes the panel MADCTL orientation.
    touch.setSwapXY(false);
    touch.setMirrorXY(false, false);
    pinMode(TOUCH_INT, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(TOUCH_INT), onTouchInterrupt, FALLING);
    Serial.printf("Touch ready: %s\n", touch.getModelName());
  } else {
    Serial.println("CST9220 unavailable; touch settings disabled");
  }
}

void updateButton() {
  static bool rawState = HIGH;
  static bool stableState = HIGH;
  static uint32_t rawChangedMs = 0;
  const bool reading = digitalRead(USER_BUTTON);
  if (reading != rawState) {
    rawState = reading;
    rawChangedMs = millis();
  }
  if (stableState != rawState && millis() - rawChangedMs >= kInputDebounceMs) {
    stableState = rawState;
    if (stableState == LOW) {
      lastDataActivityMs = millis();
      if (ui::displaySleeping()) {
        Serial.println("GPIO18: display wake");
        renderNow();
        return;
      }
    }
    const bool displayTestActive = hasSnapshot &&
        snapshot.test_state != codex_meter::kTestNone;
    if (stableState == LOW && ui::page() == ui::Page::Dashboard &&
        !displayTestActive) {
      ui::setPage(ui::Page::Settings);
      Serial.println("GPIO18: settings opened");
      renderNow();
    }
  }
}

void mapTouchToRotation(int16_t rawX, int16_t rawY,
                        int16_t& x, int16_t& y) {
  switch (ui::rotation()) {
    case 1:
      x = rawY;
      y = LCD_HEIGHT - 1 - rawX;
      break;
    case 2:
      x = LCD_WIDTH - 1 - rawX;
      y = LCD_HEIGHT - 1 - rawY;
      break;
    case 3:
      x = LCD_WIDTH - 1 - rawY;
      y = rawX;
      break;
    default:
      x = rawX;
      y = rawY;
      break;
  }
}

void handleTap(int16_t x, int16_t y) {
  const ui::Page page = ui::page();
  const uint32_t now = millis();
  lastDataActivityMs = now;
  if (ui::displaySleeping()) {
    Serial.println("Touch: display wake");
    renderNow();
    return;
  }
  if (hasSnapshot && snapshot.test_state != codex_meter::kTestNone) {
    if (snapshot.test_state == codex_meter::kTestThinking) {
      snapshot.test_state = codex_meter::kTestNeedApproval;
    } else if (snapshot.test_state == codex_meter::kTestNeedApproval) {
      snapshot.test_state = codex_meter::kTestLimitReached;
    } else if (snapshot.test_state == codex_meter::kTestSingleQuota) {
      snapshot.test_state = codex_meter::kTestDefault;
    } else {
      snapshot.test_state += 1;
    }
    Serial.printf("Test display state: %u/%u\n", snapshot.test_state,
                  codex_meter::kTestNeedApproval);
    renderNow();
    return;
  }
  if (page == ui::Page::Update) return;
  if (y >= 380) {
    if (now - lastNavigationMs < kNavigationCooldownMs) return;
    lastNavigationMs = now;
    ui::setPage(page == ui::Page::Settings
                    ? ui::Page::Dashboard
                    : ui::Page::Settings);
    Serial.printf("Touch BACK: page=%u\n", static_cast<unsigned>(ui::page()));
    renderNow();
    return;
  }

  if (page == ui::Page::Settings && y >= 120 && y <= 262) {
    if (now - lastNavigationMs < kNavigationCooldownMs) return;
    if (x >= 30 && x <= 230) {
      lastNavigationMs = now;
      ui::setPage(ui::Page::Brightness);
      Serial.println("Touch: brightness page");
      renderNow();
    } else if (x >= 250 && x <= 450) {
      lastNavigationMs = now;
      ui::setPage(ui::Page::Volume);
      Serial.println("Touch: volume page");
      renderNow();
    }
    return;
  }

  if ((page == ui::Page::Brightness || page == ui::Page::Volume) &&
      x >= 30 && x <= 450 && y >= 198 && y <= 318) {
    uint8_t value = static_cast<uint8_t>(
        constrain(map(x, 30, 450, 0, 100), 0, 100));
    if (page == ui::Page::Brightness) {
      brightnessPercent = constrain(value, 10, 100);
    } else {
      volumePercent = value;
      writeCodecVolume(volumePercent);
    }
    ui::setControls(brightnessPercent, volumePercent);
    controlsDirty = true;
    controlsChangedMs = now;
    controlRenderPending = true;
  }
}

void updateTouch() {
  if (!touchAvailable) return;
  if (!touchPending && digitalRead(TOUCH_INT) != LOW && !touchActive) return;
  const uint32_t now = millis();
  if (now - lastTouchMs < 16u) return;
  touchPending = false;
  lastTouchMs = now;
  const TouchPoints& points = touch.getTouchPoints();
  if (!points.hasPoints()) {
    touchActive = false;
    return;
  }
  const bool newTouch = !touchActive;
  touchActive = true;
  const TouchPoint point = points[0];
  int16_t x = 0;
  int16_t y = 0;
  mapTouchToRotation(point.x, point.y, x, y);
  if (now - lastTouchLogMs >= 250u) {
    lastTouchLogMs = now;
    Serial.printf("Touch coordinate: base=(%d,%d) mapped=(%d,%d) rotation=%u\n",
                  point.x, point.y, x, y, ui::rotation());
  }
  const bool displayTestActive = hasSnapshot &&
      snapshot.test_state != codex_meter::kTestNone;
  if (!displayTestActive || newTouch) handleTap(x, y);
}

void updateControlTasks() {
  const uint32_t now = millis();
  if (controlRenderPending &&
      now - lastControlRenderMs >= kControlFrameIntervalMs) {
    controlRenderPending = false;
    lastControlRenderMs = now;
    renderNow();
  }
  if (controlsDirty && now - controlsChangedMs >= kControlPersistDelayMs) {
    controlsDirty = false;
    preferences.putUChar("brightness", brightnessPercent);
    preferences.putUChar("volume", volumePercent);
    Serial.printf("Controls saved: brightness=%u%% volume=%u%%\n",
                  brightnessPercent, volumePercent);
  }
}

void updateRotation() {
  if (!imuAvailable || millis() - lastImuReadMs < 200u) return;
  lastImuReadMs = millis();
  if (!imu.getDataReady()) return;
  IMUdata acceleration{};
  if (!imu.getAccelerometer(
          acceleration.x, acceleration.y, acceleration.z)) return;

  const float absX = fabsf(acceleration.x);
  const float absY = fabsf(acceleration.y);
  if (max(absX, absY) < 0.65f) {
    rotationCandidateCount = 0;
    return;
  }

  uint8_t target = 0;
  if (absX > absY) {
    target = acceleration.x > 0 ? 3 : 1;
  } else {
    target = acceleration.y > 0 ? 0 : 2;
  }
  target = (target + kDisplayRotationOffset) & 0x03;
  // The QMI8658 axes run opposite to the clockwise convention used by the
  // software framebuffer rotator. Keep 0/180 and exchange 90/270.
  if (target == 1) {
    target = 3;
  } else if (target == 3) {
    target = 1;
  }
  if (target != rotationCandidate) {
    rotationCandidate = target;
    rotationCandidateCount = 1;
  } else if (rotationCandidateCount < 3) {
    ++rotationCandidateCount;
  }
  if (rotationCandidateCount >= 3 && target != ui::rotation()) {
    ui::setRotation(target);
    Serial.printf("Auto rotation: %u\n", target);
    renderNow();
  }
}

void updateOtaUiTasks() {
  if (otaVerifyAtMs &&
      static_cast<int32_t>(millis() - otaVerifyAtMs) >= 0) {
    completeOtaVerification();
  }
  if (otaUiDirty) {
    char status[sizeof(otaUiStatus)] = {};
    uint8_t progress = 0;
    portENTER_CRITICAL(&otaUiMux);
    strlcpy(status, otaUiStatus, sizeof(status));
    progress = otaUiProgress;
    otaUiDirty = false;
    portEXIT_CRITICAL(&otaUiMux);
    ui::setUpdateState(status, progress);
    renderNow();
  }
  if (otaRebootAtMs && static_cast<int32_t>(millis() - otaRebootAtMs) >= 0) {
    delay(50);
    ESP.restart();
  }
  if (otaUiExpiresAtMs &&
      static_cast<int32_t>(millis() - otaUiExpiresAtMs) >= 0) {
    otaUiExpiresAtMs = 0;
    ui::setUpdateState("IDLE", 0);
    renderNow();
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(250);
  Serial.printf("Codex Token Monitor %s starting\n",
                codex_meter::kFirmwareVersion);

  preferences.begin("codex-meter", false);
  readPacketFromNvs();
  skin::begin();
  logHeap("NVS");
  initPower();
  logHeap("power init");
  if (!ui::begin()) {
    Serial.println("Display initialization failed");
  }
  loadControls();
  logHeap("display init");
  initInputSensors();
  logHeap("input sensors");
  initBle();
  logHeap("BLE init");
  lastDataActivityMs = millis();
  renderNow();
  confirmRunningFirmware();
}

void loop() {
  updateOtaUiTasks();
  if (skin::poll(bleConnected, otaActive || otaRebootAtMs != 0)) {
    ui::skinChanged();
    renderNow();
  }
  const bool displayTestActive = hasSnapshot &&
      snapshot.test_state != codex_meter::kTestNone;
  if (otaActive ||
      (ui::page() == ui::Page::Update && !displayTestActive)) {
    updateBattery();
    const bool animateUpdate = ui::updateAnimationActive();
    const bool animateBattery = batteryCharging;
    if ((animateUpdate || animateBattery) &&
        millis() - lastRenderMs >=
            ui_theme::active().motion.animationFrameMs) {
      lastRenderMs = millis();
      if (animateUpdate) ui::renderUpdateAnimationFrame();
      if (animateBattery) {
        ui::renderBatteryAnimationFrame(
            hasSnapshot ? &snapshot : nullptr, snapshotIsFresh,
            batteryPercent, batteryCharging);
      }
    }
    delay(8);
    return;
  }
  updateButton();
  updateTouch();
  if (!ui::displaySleeping()) {
    updateControlTasks();
    updateRotation();
  }

  CodexPacket incoming{};
  if (xQueueReceive(packetQueue, &incoming, 0) == pdTRUE) {
    if (!(incoming.flags & codex_meter::kTokenTotalsValid) && hasSnapshot &&
        (snapshot.flags & codex_meter::kTokenTotalsValid)) {
      incoming.input_tokens = snapshot.input_tokens;
      incoming.cached_input_tokens = snapshot.cached_input_tokens;
      incoming.output_tokens = snapshot.output_tokens;
      incoming.reasoning_output_tokens = snapshot.reasoning_output_tokens;
      incoming.total_tokens = snapshot.total_tokens;
      incoming.flags |= codex_meter::kTokenTotalsValid;
    }
    const bool dataChanged = !hasSnapshot || packetDataChanged(snapshot, incoming);
    snapshot = incoming;
    hasSnapshot = true;
    snapshotIsFresh =
        !(snapshot.flags & codex_meter::kAccountDataStale);
    snapshotReceivedMs = millis();
    if (dataChanged) lastDataActivityMs = snapshotReceivedMs;
    if (snapshot.test_state != codex_meter::kTestNone) {
      ui::setPage(ui::Page::Dashboard);
    }
    if (snapshot.test_state == codex_meter::kTestNone && dataChanged) {
      savePacketToNvs(snapshot);
    }
    Serial.printf("Accepted JSON #%lu (%u%% / %u%%, test=%u)\n",
                  static_cast<unsigned long>(snapshot.sequence),
                  snapshot.primary.used_percent, snapshot.secondary.used_percent,
                  snapshot.test_state);
    if (dataChanged || !ui::displaySleeping()) renderNow();
    notifyAck(snapshot.sequence);
  }

  const bool batteryChanged = updateBattery();
  if (batteryChanged && !ui::displaySleeping()) renderNow();
  if (snapshotIsFresh && snapshot.test_state == codex_meter::kTestNone &&
      millis() - snapshotReceivedMs > 120000u) {
    snapshotIsFresh = false;
  }
  if (!ui::displaySleeping() &&
      millis() - lastDataActivityMs >= kDisplaySleepTimeoutMs) {
    Serial.println("Display sleeping after 1 hour without data changes");
    ui::sleepDisplay();
  }
  if (ui::displaySleeping()) {
    delay(20);
    return;
  }
  const bool animateStatus = ui::statusAnimationActive(
      hasSnapshot ? &snapshot : nullptr, snapshotIsFresh);
  const bool animateTokens = ui::tokenAnimationActive();
  int animatedBatteryPercent = batteryPercent;
  bool animatedBatteryCharging = batteryCharging;
  if (hasSnapshot && snapshot.test_state == codex_meter::kTestCharging) {
    animatedBatteryPercent = 85;
    animatedBatteryCharging = true;
  } else if (hasSnapshot &&
             snapshot.test_state == codex_meter::kTestLowBattery) {
    animatedBatteryPercent = 10;
    animatedBatteryCharging = false;
  }
  const bool animateBattery = animatedBatteryCharging;
  const bool animateUpdate = ui::updateAnimationActive();
  const bool animateUi =
      animateStatus || animateTokens || animateBattery || animateUpdate;
  const uint32_t renderInterval = animateUi
                                      ? ui_theme::active().motion.animationFrameMs
                                      : kRenderIntervalMs;
  if (millis() - lastRenderMs >= renderInterval) {
    lastRenderMs = millis();
    if (animateTokens) ui::renderTokenAnimationFrame();
    if (animateUpdate) ui::renderUpdateAnimationFrame();
    if (animateStatus) {
      ui::renderStatusAnimationFrame(
          hasSnapshot ? &snapshot : nullptr, snapshotIsFresh);
    }
    if (animateBattery) {
      ui::renderBatteryAnimationFrame(
          hasSnapshot ? &snapshot : nullptr, snapshotIsFresh,
          animatedBatteryPercent, animatedBatteryCharging);
    }
    if (!animateUi) renderNow();
  }
  // Finer scheduling while an animation is active avoids rounding the 33ms
  // target up to the old 10ms polling grid; still yield to BLE/FreeRTOS.
  delay(animateUi ? 1 : 10);
}
