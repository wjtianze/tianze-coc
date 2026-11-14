// tianze-coc 探针公共工具：日志、安全内存读取。
#pragma once

#include <android/log.h>
#include <sys/uio.h>
#include <unistd.h>

#include <cstddef>
#include <cstdint>
#include <cstring>

#define TZ_TAG "tianze"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TZ_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, TZ_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TZ_TAG, __VA_ARGS__)

namespace tz {

// 用 process_vm_readv 读自身内存：地址非法时返回 false 而不是崩溃。
// 所有"推测性"读取（对象遍历、校准用 peek）都必须走这里。
inline bool safe_read(std::uintptr_t addr, void *out, std::size_t len) {
  if (addr < 0x10000) return false;
  iovec local{out, len};
  iovec remote{reinterpret_cast<void *>(addr), len};
  return process_vm_readv(getpid(), &local, 1, &remote, 1, 0) == static_cast<ssize_t>(len);
}

template <typename T>
inline bool rd(std::uintptr_t addr, T &out) {
  return safe_read(addr, &out, sizeof(T));
}

template <typename T>
inline T rd_or(std::uintptr_t addr, T fallback) {
  T v;
  return rd(addr, v) ? v : fallback;
}

inline std::uintptr_t rdp(std::uintptr_t addr) { return rd_or<std::uintptr_t>(addr, 0); }

}  // namespace tz
