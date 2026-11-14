// ARM64 inline hook。
//
// 只支持"前 16 字节不含 PC 相对指令"的函数（find_offsets.py 生成头文件时已检查）。
// 入口被改写为：
//     ldr x16, #8 ; br x16 ; .quad replacement
// 跳板（trampoline）为：
//     <原 16 字节> ; ldr x16, #8 ; br x16 ; .quad target+16
// x16/x17 是 AAPCS64 规定的过程间临时寄存器，函数入口处可以自由使用。
#pragma once

#include <sys/mman.h>
#include <unistd.h>

#include <cstdint>
#include <cstring>

#include "common.h"

namespace tz {

inline bool make_rwx(std::uintptr_t addr, std::size_t len) {
  const std::uintptr_t page = static_cast<std::uintptr_t>(sysconf(_SC_PAGESIZE));
  const std::uintptr_t start = addr & ~(page - 1);
  const std::uintptr_t end = (addr + len + page - 1) & ~(page - 1);
  return mprotect(reinterpret_cast<void *>(start), end - start, PROT_READ | PROT_WRITE | PROT_EXEC) == 0;
}

inline void flush_icache(std::uintptr_t addr, std::size_t len) {
  __builtin___clear_cache(reinterpret_cast<char *>(addr), reinterpret_cast<char *>(addr + len));
}

// 成功时 *original 指向可调用的跳板。
inline bool install_inline_hook(std::uintptr_t target, const std::uint8_t (&expected)[16], void *replacement,
                                void **original, const char *label) {
  if (std::memcmp(reinterpret_cast<const void *>(target), expected, 16) != 0) {
    LOGE("hook %s: 入口指纹不符，拒绝安装", label);
    return false;
  }
  const std::size_t page = static_cast<std::size_t>(sysconf(_SC_PAGESIZE));
  void *tramp = mmap(nullptr, page, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (tramp == MAP_FAILED) {
    LOGE("hook %s: 无法分配跳板", label);
    return false;
  }
  auto *t = static_cast<std::uint32_t *>(tramp);
  std::memcpy(t, reinterpret_cast<const void *>(target), 16);
  t[4] = 0x58000050;  // ldr x16, #8
  t[5] = 0xd61f0200;  // br  x16
  const std::uint64_t back = target + 16;
  std::memcpy(&t[6], &back, 8);
  flush_icache(reinterpret_cast<std::uintptr_t>(tramp), 32);

  if (!make_rwx(target, 16)) {
    LOGE("hook %s: mprotect 失败", label);
    munmap(tramp, page);
    return false;
  }
  std::uint32_t patch[4];
  patch[0] = 0x58000050;  // ldr x16, #8
  patch[1] = 0xd61f0200;  // br  x16
  const std::uint64_t dst = reinterpret_cast<std::uint64_t>(replacement);
  std::memcpy(&patch[2], &dst, 8);
  *original = tramp;
  // 先写后 8 字节（地址），再写前 8 字节（指令），减少其他线程看到半成品的窗口。
  __atomic_store_n(reinterpret_cast<std::uint64_t *>(target + 8), *reinterpret_cast<std::uint64_t *>(&patch[2]),
                   __ATOMIC_SEQ_CST);
  __atomic_store_n(reinterpret_cast<std::uint64_t *>(target), *reinterpret_cast<std::uint64_t *>(&patch[0]),
                   __ATOMIC_SEQ_CST);
  flush_icache(target, 16);
  LOGI("hook %s 已安装 @%p", label, reinterpret_cast<void *>(target));
  return true;
}

}  // namespace tz
