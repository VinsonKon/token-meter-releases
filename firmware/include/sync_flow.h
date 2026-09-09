#pragma once

#include <stdint.h>

namespace sync_flow {

// A visible update page is not an active flash operation. Keep accepting
// ordinary packets on every page, except while a real OTA owns the device.
constexpr bool canReceive(bool otaActive, bool rebootPending) {
  return !otaActive && !rebootPending;
}

constexpr bool leavingTest(bool hasSnapshot, uint8_t previous, uint8_t incoming) {
  return hasSnapshot && previous != 0 && incoming == 0;
}

class UpdateOwnership {
 public:
  void assign(bool simulated, bool active = true) {
    simulated_ = simulated;
    active_ = active;
  }
  bool realActive() const { return active_ && !simulated_; }
  bool releaseTest() {
    const bool owned = active_ && simulated_;
    if (owned) {
      active_ = false;
      simulated_ = false;
    }
    return owned;
  }

 private:
  bool simulated_ = false;
  bool active_ = false;
};

}  // namespace sync_flow
