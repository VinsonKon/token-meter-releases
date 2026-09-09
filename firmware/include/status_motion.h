#pragma once

#include <stdint.h>

// State selection and time-based transitions shared by rendering and its
// scheduler. No frame counters: BLE traffic cannot speed up/reset a loop.
namespace status_motion {

enum class State { Idle, Waiting, Approval, Stale, Thinking, Other };

constexpr State resolve(bool hasSnapshot, bool waitingForSync,
                        bool approval, bool fresh, bool thinking,
                        bool otherStatus) {
  return !hasSnapshot || waitingForSync ? State::Waiting
       : approval ? State::Approval
       : !fresh ? State::Stale
       : thinking ? State::Thinking
       : otherStatus ? State::Other : State::Idle;
}

constexpr bool loops(State state) {
  return state == State::Thinking || state == State::Approval ||
      state == State::Stale || state == State::Waiting;
}

constexpr uint32_t kTransitionMs = 240;

class Transition {
 public:
  bool initialized() const { return initialized_; }
  State state() const { return state_; }
  State previous() const { return previous_; }
  uint32_t elapsed(uint32_t now) const { return now - started_; }
  float progress(uint32_t now) const {
    const uint32_t age = elapsed(now);
    return age >= kTransitionMs ? 1.0f
                               : static_cast<float>(age) / kTransitionMs;
  }
  bool active(uint32_t now) const {
    return initialized_ && elapsed(now) < kTransitionMs;
  }
  float width(uint32_t now) const {
    const float inverse = 1.0f - progress(now);
    const float eased = 1.0f - inverse * inverse * inverse;
    return fromWidth_ + (targetWidth_ - fromWidth_) * eased;
  }
  bool update(State state, float targetWidth, uint32_t now,
              bool labelChanged = false) {
    if (initialized_ && state == state_ && !labelChanged &&
        targetWidth == targetWidth_) return false;
    const bool first = !initialized_;
    const float visibleWidth = first ? targetWidth : width(now);
    previous_ = first ? state : state_;
    state_ = state;
    fromWidth_ = visibleWidth;
    targetWidth_ = targetWidth;
    started_ = now;
    initialized_ = true;
    // Warnings/approval must be readable in the very first frame. Leaving
    // approval also removes its wording immediately, so a resolved request
    // never lingers while the next state fades in.
    if (first || state == State::Approval || state == State::Stale ||
        state == State::Waiting || previous_ == State::Approval ||
        state == State::Idle) {
      fromWidth_ = targetWidth;
      started_ = now - kTransitionMs;
    }
    return true;
  }

 private:
  bool initialized_ = false;
  State state_ = State::Idle;
  State previous_ = State::Idle;
  uint32_t started_ = 0;
  float fromWidth_ = 0;
  float targetWidth_ = 0;
};

}  // namespace status_motion
