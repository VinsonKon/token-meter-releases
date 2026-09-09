#pragma once
#include "Arduino.h"
#include <map>
#include <memory>
class BLECharacteristic;
class BLECharacteristicCallbacks {
 public:
  virtual void onRead(BLECharacteristic*) {}
  virtual void onWrite(BLECharacteristic*) {}
  virtual ~BLECharacteristicCallbacks() = default;
};
class BLECharacteristic {
 public:
  static constexpr int PROPERTY_READ = 1, PROPERTY_WRITE = 2;
  BLECharacteristicCallbacks* callbacks = nullptr;
  String value;
  void setCallbacks(BLECharacteristicCallbacks* c) { callbacks = c; }
  String getValue() { return value; }
  void setValue(uint8_t* data, size_t length) { value.assign(reinterpret_cast<char*>(data), length); }
  void write(const std::string& data) { value = data; callbacks->onWrite(this); }
  std::string read() { callbacks->onRead(this); return value; }
};
class BLEService {
 public:
  std::map<std::string,std::unique_ptr<BLECharacteristic>> characteristics;
  BLECharacteristic* createCharacteristic(const char* uuid, int) {
    auto value = std::make_unique<BLECharacteristic>();
    auto* result = value.get(); characteristics[uuid] = std::move(value); return result;
  }
};
