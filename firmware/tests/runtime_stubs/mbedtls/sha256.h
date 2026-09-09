#pragma once
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#pragma comment(lib, "bcrypt.lib")
inline int mbedtls_sha256(const uint8_t* source, size_t length, uint8_t* digest, int) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0)) return -1;
  const auto result = BCryptHash(algorithm, nullptr, 0, const_cast<PUCHAR>(source), static_cast<ULONG>(length), digest, 32);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  return result ? -1 : 0;
}
