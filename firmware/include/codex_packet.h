#pragma once

#include <Arduino.h>

namespace codex_meter {

constexpr uint8_t kProtocolVersion = 1;
constexpr size_t kMaxJsonSize = 512;

constexpr char kDeviceName[] = "Codex Meter";
constexpr char kServiceUuid[] = "7f520001-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kDataCharacteristicUuid[] = "7f520002-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kAckCharacteristicUuid[] = "7f520003-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kDeviceInfoCharacteristicUuid[] = "7f520004-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kOtaControlCharacteristicUuid[] = "7f520005-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kOtaDataCharacteristicUuid[] = "7f520006-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kOtaStatusCharacteristicUuid[] = "7f520007-1b15-4f9f-8bd8-29dbe9e681a1";
constexpr char kBoardId[] = "waveshare-amoled-2.16";

enum PacketFlags : uint8_t {
  kPrimaryValid = 1 << 0,
  kSecondaryValid = 1 << 1,
  kTokenTotalsValid = 1 << 2,
  kRateLimitReached = 1 << 3,
  kPlanValid = 1 << 4,
  kAccountDataStale = 1 << 5,
  kResetCountValid = 1 << 6,
  kLifetimeTokenTotal = 1 << 7,
};

// Optional desktop-driven display test scenarios. Real synchronization uses
// kTestNone; test packets are intentionally not persisted to NVS.
enum TestState : uint8_t {
  kTestNone = 0,
  kTestDefault = 1,
  kTestThinking = 2,
  kTestLimitReached = 3,
  kTestDataStale = 4,
  kTestWaitingForSync = 5,
  kTestCharging = 6,
  kTestLowBattery = 7,
  kTestUpdateAvailable = 8,
  kTestDownloading = 9,
  kTestInstalling = 10,
  kTestVerifying = 11,
  kTestRestarting = 12,
  kTestUpdateFailed = 13,
  kTestRolledBack = 14,
  kTestSingleQuota = 15,
  // Appended to preserve the established wire ids for test states 1-15.
  kTestNeedApproval = 16,
};

struct UsageWindow {
  uint8_t used_percent = 0;
  uint16_t window_minutes = 0;
  uint32_t resets_at = 0;
  uint32_t tokens_used = 0;
};

// Internal render model. This is deliberately independent of the variable-size
// JSON wire message so UI code stays strongly typed.
struct CodexPacket {
  uint8_t version = kProtocolVersion;
  uint8_t flags = 0;
  uint32_t sequence = 0;
  uint32_t generated_at = 0;
  // Wire q[0]/primary is the short-window slot; q[1]/secondary is the weekly
  // slot. The desktop classifies official App Server windows by duration.
  UsageWindow primary;
  UsageWindow secondary;
  uint64_t input_tokens = 0;
  uint64_t cached_input_tokens = 0;
  uint64_t output_tokens = 0;
  uint64_t reasoning_output_tokens = 0;
  uint64_t total_tokens = 0;
  uint16_t reset_count = 0;
  int32_t cost_cny_cents = -1;
  char plan[16]{};
  char provider[12]{};
  char model[24]{};
  char account_label[20]{};
  char status[32]{};
  uint8_t test_state = kTestNone;
};

inline bool validateStoredData(const CodexPacket& data) {
  return data.version == kProtocolVersion && data.primary.used_percent <= 100 &&
         data.secondary.used_percent <= 100;
}

}  // namespace codex_meter
