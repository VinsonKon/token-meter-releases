// Exercise the actual production transaction/boot code with simulated BLE,
// flash, PSRAM and NVS. This does not claim real flash timing/power-loss testing.
#include "../src/skin_runtime.cpp"
#include <fstream>
#include <iostream>
#include <iterator>
using Bytes = std::vector<uint8_t>;
int tests = 0;
void check(bool good, const char* label) {
  ++tests;
  if (!good) { std::cerr << "FAIL: " << label << '\n'; std::exit(1); }
}
BLEService service;
JsonDocument status() {
  JsonDocument doc;
  deserializeJson(doc, service.characteristics["7f52000a-1b15-4f9f-8bd8-29dbe9e681a1"]->read());
  return doc;
}
bool command(const char* op, size_t size=0, const char* digest="", const char* tx="123abc") {
  JsonDocument doc;
  doc["op"] = op; doc["tx"] = tx; doc["size"] = static_cast<uint32_t>(size); doc["sha256"] = digest;
  std::string encoded; serializeJson(doc, encoded);
  service.characteristics["7f520008-1b15-4f9f-8bd8-29dbe9e681a1"]->write(encoded);
  return skin::poll(true, false);
}
void chunk(const Bytes& bytes, uint32_t offset, uint32_t length) {
  std::string encoded(4+length, '\0');
  memcpy(&encoded[0], &offset, 4); memcpy(&encoded[4], bytes.data()+offset, length);
  service.characteristics["7f520009-1b15-4f9f-8bd8-29dbe9e681a1"]->write(encoded);
  skin::poll(true, false);
}
void upload(const Bytes& bytes) {
  for (uint32_t at = 0; at < bytes.size(); at += 176) {
    const uint32_t count = uint32_t(bytes.size()-at) < 176 ? uint32_t(bytes.size()-at) : 176;
    chunk(bytes, at, count);
    check(status()["offset"].as<uint32_t>() == at+count, "ACK only after storage write");
  }
}
void reboot() {
  free(currentBytes); currentBytes = nullptr; currentSlot = incomingSlot = -1;
  receiving = false; currentHash[0] = transaction[0] = 0; generation = received = 0;
  delete events; events = nullptr; skin::begin();
}
int main(int argc, char** argv) {
  check(argc == 2 || argc == 3, "skin file required; optional private flash fixture");
  std::ifstream file(argv[1], std::ios::binary);
  Bytes bytes((std::istreambuf_iterator<char>(file)), {});
  check(!skin_format::validate(bytes.data(), bytes.size()), "valid fixture");
  uint8_t digest[32]; char sha[65]; mbedtls_sha256(bytes.data(), bytes.size(), digest, 0); hex(digest, sha);
  simulatedFlash[32] = 0x34; // Unrelated head data must never be erased.
  skin::begin(); skin::attachBle(&service);
  const uint32_t tail = slotOffset(2);
  check(tail == 0x710000 && slotOffset(3) + kSlotSize == simulatedPartition.size, "tail slots bounded to last 832 KiB");
  check(status()["storage"].as<int>() == 2 && status()["max_bytes"].as<uint32_t>() == 384*1024, "storage capacity advertised");
  check(!skin::imported(), "first boot defaults");
  command("begin", bytes.size(), sha); check(skin::busy(), "begin accepts blank slot");
  check(!skin::imported(), "begin does not change displayed skin");
  upload(bytes); check(command("commit"), "valid commit activates");
  check(persistedActive == 2 && skin::imported(), "commit persists tail slot two");
  reboot(); check(skin::imported() && currentSlot == 2, "reboot restores validated skin");
  failAllocation = true; reboot();
  check(!skin::imported() && currentSlot == 2, "boot allocation failure reserves active slot");
  auto allocationBefore = simulatedFlash;
  command("begin", bytes.size(), sha);
  check(incomingSlot == 3 && std::equal(allocationBefore.begin()+slotOffset(2),
      allocationBefore.begin()+slotOffset(2)+kSlotSize,simulatedFlash.begin()+slotOffset(2)),
      "staging after boot allocation failure preserves recoverable active bytes");
  command("abort"); failAllocation = false; reboot();
  check(skin::imported() && currentSlot == 2, "next boot recovers previously reserved skin");

  command("begin", bytes.size(), std::string(64,'0').c_str()); upload(bytes);
  check(!command("commit") && skin::imported() && persistedActive == 2, "hash failure preserves active slot");
  command("begin", bytes.size(), sha); chunk(bytes, 0, 176);
  check(!command("commit") && persistedActive == 2, "truncated transfer cannot commit");
  command("begin", bytes.size(), sha); chunk(bytes, 176, 176);
  check(!skin::busy() && persistedActive == 2, "out of order chunk rejected");
  command("begin", bytes.size(), sha); chunk(bytes, 0, 176); skin::poll(false,false);
  check(!skin::busy() && persistedActive == 2, "disconnect retains previous selection");

  command("begin", bytes.size(), sha); chunk(bytes, 0, 176); reboot();
  check(skin::imported() && currentSlot == 2, "power loss mid-transfer boots old skin");
  command("begin", bytes.size(), sha); upload(bytes); failPreferenceWrite = true;
  check(!command("commit") && currentSlot == 2, "NVS failure does not switch runtime");
  failPreferenceWrite = false; reboot();
  check(skin::imported() && currentSlot == 2, "power loss between record and selection commit keeps old skin");

  command("begin", bytes.size(), sha); upload(bytes); failAllocation = true;
  check(!command("commit") && currentSlot == 2, "allocation failure retains old skin"); failAllocation = false;
  command("begin", bytes.size(), sha); failFlashWriteAfter = 20; chunk(bytes, 0, 176);
  check(!skin::busy() && status()["offset"].as<uint32_t>() == 0, "partial flash failure never ACKs chunk");
  failFlashWriteAfter = -1; reboot(); check(skin::imported(), "partial inactive write survives reboot");

  command("begin", bytes.size(), sha); simulatedMillis += 30001; skin::poll(true,false);
  check(!skin::busy(), "idle transfer timeout");
  command("begin", bytes.size(), sha); skin::poll(true,true);
  check(!skin::busy() && persistedActive == 2, "OTA interrupts only staging skin");
  command("begin", bytes.size(), sha); upload(bytes); check(command("commit"), "second valid commit");
  check(persistedActive == 3 && currentSlot == 3, "double buffer alternates tail slots");
  reboot(); check(currentSlot == 3 && std::string(currentHash) == sha, "commit survives lost response and reboot");
  simulatedFlash[slotOffset(3) + kPayloadOffset + 1000] ^= 1;
  reboot(); check(!skin::imported(), "damaged active payload falls back to builtin");
  check(command("default") && persistedActive == -1, "default persists without upload");
  reboot(); check(!skin::imported(), "default survives reboot");
  memset(simulatedFlash.data()+tail, 255, kSlotSize*2);
  simulatedFlash[tail+10000] = simulatedFlash[tail+kSlotSize+10000] = 0x12;
  auto before = simulatedFlash;
  command("begin", bytes.size(), sha);
  check(!skin::busy() && simulatedFlash == before, "unidentified partition content is never erased");
  check(std::string(status()["error"].as<const char*>()) == "skin slot contains unrelated data", "occupied error is actionable");
  check(simulatedFlash[32] == 0x34, "unrelated partition bytes retained");
  // One occupied tail slot can be skipped, but must never be erased.
  memset(simulatedFlash.data()+tail+kSlotSize, 255, kSlotSize);
  command("begin", bytes.size(), sha); check(incomingSlot == 3, "first upload skips occupied slot");
  upload(bytes); check(command("commit"), "first upload may select second tail slot");
  before = simulatedFlash;
  command("begin", bytes.size(), sha);
  check(!skin::busy() && simulatedFlash == before, "active tail slot never sacrificed when other slot occupied");
  command("default");

  // A committed legacy >384 KiB skin stays readable, but new uploads are capped.
  skin_format::Header legacyHeader;
  memcpy(&legacyHeader, bytes.data(), sizeof(legacyHeader));
  Bytes legacy(bytes.begin(), bytes.begin()+sizeof(legacyHeader));
  const uint32_t oldEnd = legacyHeader.animations[1].offset;
  for (uint32_t i=1; i<=100; ++i) {
    const uint32_t end = i*10;
    const auto* p = reinterpret_cast<const uint8_t*>(&end);
    legacy.insert(legacy.end(), p, p+4);
  }
  legacy.insert(legacy.end(), 100*4096, 255);
  const uint32_t newEnd = static_cast<uint32_t>(legacy.size());
  legacy.insert(legacy.end(), bytes.begin()+oldEnd, bytes.end());
  legacyHeader.size = static_cast<uint32_t>(legacy.size());
  legacyHeader.animations[0].count = 100; legacyHeader.animations[0].cycle = 1000;
  for (int i=1; i<4; ++i) legacyHeader.animations[i].offset += newEnd-oldEnd;
  memcpy(legacy.data(), &legacyHeader, sizeof(legacyHeader));
  check(skin_format::validate(legacy.data(), legacy.size()) != nullptr &&
      !skin_format::validate(legacy.data(), legacy.size(), skin_format::kLegacyMaxBytes), "legacy-only extended read limit");
  Stored stored{}; memcpy(stored.magic, "TMSLOT1", 8);
  stored.size = static_cast<uint32_t>(legacy.size());
  mbedtls_sha256(legacy.data(), legacy.size(), stored.hash, 0);
  for (int oldSlot=0; oldSlot<2; ++oldSlot) {
    memcpy(simulatedFlash.data()+slotOffset(oldSlot), &stored, sizeof(stored));
    memcpy(simulatedFlash.data()+slotOffset(oldSlot)+kPayloadOffset, legacy.data(), legacy.size());
    persistedActive = static_cast<int8_t>(oldSlot); reboot();
    check(skin::imported() && currentSlot == oldSlot && skin::active()->size == legacy.size(), "old NVS index reads original large skin");
    before = simulatedFlash;
    command("begin", legacy.size(), sha);
    check(!skin::busy() && simulatedFlash == before && currentSlot == oldSlot, "oversize upload cannot erase old skin");
  }
  memset(simulatedFlash.data()+tail, 255, 2*kSlotSize);
  before = simulatedFlash;
  command("begin", bytes.size(), sha); upload(bytes);
  failPreferenceWrite = true; check(!command("commit"), "legacy transition NVS failure");
  failPreferenceWrite = false; reboot(); check(currentSlot == 1, "legacy selection survives interrupted transition");
  command("begin", bytes.size(), sha); upload(bytes); check(command("commit"), "legacy to tail commit");
  reboot(); check(currentSlot == 2, "versioned slot selection survives reboot");
  check(std::equal(before.begin(), before.begin()+tail, simulatedFlash.begin()), "entire old region unchanged by transition");
  command("default"); reboot(); check(!skin::imported(), "builtin selection suppresses legacy fallback");

  int erases = eraseCount;
  failFlashRead = true; command("begin", bytes.size(), sha);
  check(!skin::busy() && eraseCount == erases && std::string(status()["error"].as<const char*>()) == "skin storage read failed", "read error refuses erase");
  failFlashRead = false;
  missingPartition = true; reboot(); command("begin", bytes.size(), sha);
  check(eraseCount == erases && std::string(status()["error"].as<const char*>()) == "skin partition missing", "missing partition refuses erase");
  missingPartition = false; simulatedPartition.size = 0x100000; reboot(); command("begin", bytes.size(), sha);
  check(eraseCount == erases && std::string(status()["error"].as<const char*>()) == "skin partition size invalid", "undersize partition refuses erase");
  simulatedPartition.size = 0x7E0000; failPreferenceBegin = true; reboot(); command("begin", bytes.size(), sha);
  check(eraseCount == erases && std::string(status()["error"].as<const char*>()) == "skin preferences unavailable", "NVS init failure refuses erase");
  failPreferenceBegin = false; reboot();

  if (argc == 3) {
    std::ifstream backupFile(argv[2], std::ios::binary);
    Bytes backup((std::istreambuf_iterator<char>(backupFile)), {});
    if (backup.size() != 16*1024*1024) {
      check(!skin_format::validate(backup.data(),backup.size()) &&
          reinterpret_cast<const skin_format::Header*>(backup.data())->schema == 2, "optional classic fixture valid");
      mbedtls_sha256(backup.data(),backup.size(),digest,0); char classicSha[65]; hex(digest,classicSha);
      command("begin",backup.size(),classicSha); upload(backup); check(command("commit"),"classic commits to same safe tail storage");
      reboot(); check(skin::imported() && skin::active()->schema == 2 && std::string(currentHash)==classicSha,"classic survives reboot");
      const int classicSlot=currentSlot;
      command("begin",bytes.size(),sha); chunk(bytes,0,176); reboot();
      check(currentSlot==classicSlot && skin::active()->schema==2,"interrupted pixel transfer preserves classic");
      command("begin",bytes.size(),sha); upload(bytes); check(command("commit"),"pixel commits after classic");
      reboot(); check(skin::imported() && skin::active()->schema==1 && std::string(currentHash)==sha,"pixel schema one survives round trip");
      std::cout << "Cross-schema storage: classic and pixel alternate and survive simulated reboot/interruption.\n";
    } else {
    simulatedFlash.assign(backup.begin()+0x810000, backup.begin()+0xFF0000);
    persistedActive = -1; reboot();
    before = simulatedFlash;
    for (int slot=2; slot<=3; ++slot) {
      command("begin", bytes.size(), sha); upload(bytes); check(command("commit"), "actual device image tail accepts skin");
      reboot(); check(currentSlot == slot, "actual device image restores selected tail skin");
    }
    check(std::equal(before.begin(), before.begin()+tail, simulatedFlash.begin()), "all pre-existing device partition data preserved");
    std::cout << "Private flash fixture: both tail slots verified; pre-existing bytes unchanged.\n";
    }
  }
  std::cout << "Skin storage host: " << tests << " checks passed (simulated flash/NVS/BLE; production state machine).\n";
}
