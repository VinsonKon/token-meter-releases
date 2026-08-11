#pragma once

#include <Arduino.h>

#include "codex_packet.h"

// Temporary on-device theme. Replace renderer.cpp after the Figma handoff;
// BLE, persistence, and the JSON protocol do not need to change.
namespace ui {

enum class Page : uint8_t {
  Dashboard,
  Settings,
  Brightness,
  Volume,
  Update,
};

bool begin();

void setPage(Page page);
Page page();
void setControls(uint8_t brightnessPercent, uint8_t volumePercent);
void setDeviceName(const String& deviceName);
void setUpdateState(const String& status, uint8_t progressPercent);
void setBrightness(uint8_t brightnessPercent);
void setRotation(uint8_t rotation);
uint8_t rotation();
void sleepDisplay();
void wakeDisplay();
bool displaySleeping();

void render(const codex_meter::CodexPacket* snapshot, bool fresh,
            bool bleConnected, int batteryPercent, bool batteryCharging,
            uint32_t receivedAtMs);
bool tokenAnimationActive();
bool updateAnimationActive();
void renderTokenAnimationFrame();
void renderUpdateAnimationFrame();
void renderStatusAnimationFrame(const codex_meter::CodexPacket* snapshot,
                                bool fresh);
void renderBatteryAnimationFrame(const codex_meter::CodexPacket* snapshot,
                                 bool fresh, int batteryPercent,
                                 bool batteryCharging);

}  // namespace ui
