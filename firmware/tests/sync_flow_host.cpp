#include <cstdio>
#include <cstdlib>
#include "sync_flow.h"

int checks = 0;
void check(bool condition) {
  ++checks;
  if (!condition) { std::fprintf(stderr, "FAIL at check %d\n", checks); std::exit(1); }
}

int main() {
  for (int previous = 0; previous <= 16; ++previous) {
    for (int incoming = 0; incoming <= 16; ++incoming) {
      for (int has = 0; has <= 1; ++has) {
        check(sync_flow::leavingTest(has != 0, static_cast<uint8_t>(previous),
                                    static_cast<uint8_t>(incoming)) ==
              (has && previous != 0 && incoming == 0));
      }
    }
  }
  for (int ota = 0; ota <= 1; ++ota) {
    for (int reboot = 0; reboot <= 1; ++reboot) {
      check(sync_flow::canReceive(ota != 0, reboot != 0) == (!ota && !reboot));
    }
  }
  sync_flow::UpdateOwnership owner;
  check(!owner.realActive());
  check(!owner.releaseTest());
  owner.assign(true);
  check(!owner.realActive());
  check(owner.releaseTest());
  check(!owner.releaseTest());
  // A genuine update that supersedes a test is never cancelled by test exit.
  owner.assign(true);
  owner.assign(false);
  check(owner.realActive());
  check(!owner.releaseTest());
  check(owner.realActive());
  owner.assign(false, false);  // IDLE releases actual update ownership.
  check(!owner.realActive());
  // Simulated RESTARTING -> real packet -> next real packet: both accepted.
  bool updatePage = true;
  unsigned char testState = 12;
  owner.assign(true);
  int acknowledgements = 0;
  for (int packet = 0; packet < 3; ++packet) {
    if (sync_flow::canReceive(false, false)) {
      if (sync_flow::leavingTest(true, testState, 0) && owner.releaseTest()) {
        updatePage = false;
      }
      testState = 0;
      ++acknowledgements;
    }
    check(!updatePage);
  }
  check(acknowledgements == 3);
  // A real display-only update page also cannot starve sync (no flash active).
  owner.assign(false);
  check(sync_flow::canReceive(false, false));
  check(!owner.releaseTest());
  check(owner.realActive());
  std::printf("PASS: %d sync flow and update-ownership checks\n", checks);
}
