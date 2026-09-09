#pragma once
#include <Arduino.h>
#include <BLEDevice.h>
#include "skin_format.h"
#include "status_pixel.h"
#include "status_label.h"

namespace skin {
void begin();
void attachBle(BLEService* service);
bool poll(bool connected, bool otaBusy); // true when a skin was activated
bool busy();
bool imported();
const char* id();
const skin_format::Header* active();
int16_t layout(skin_format::Layout field);
uint32_t revision();
status_pixel::Frame sample(status_pixel::Mode mode, uint32_t elapsed);
status_label::Look light(status_pixel::Mode mode, uint32_t elapsed);
const skin_format::Animation* animation(status_pixel::Mode mode);
} // namespace skin
