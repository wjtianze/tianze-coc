// 探针入口。
//
// libtianze_probe.so 通过改写 libg.so 的一个 DT_NEEDED 条目被加载（见 tianze_coc/offline_build.py），
// 它自己再 DT_NEEDED 被顶替掉的原库，保证原依赖仍被加载。
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>

#include "common.h"
#include "engine.h"

namespace tz {
void start_server(int port);
}

namespace {

// 只在 Atrasis 私服客户端里工作；在其他进程（例如官方部落冲突）里什么都不做。
constexpr const char *kAllowedPackagePrefix = "com.atrasis.";
constexpr int kDefaultPort = 27650;

bool process_allowed() {
  char name[256] = {};
  FILE *f = std::fopen("/proc/self/cmdline", "rb");
  if (!f) return false;
  std::fread(name, 1, sizeof name - 1, f);
  std::fclose(f);
  return std::strncmp(name, kAllowedPackagePrefix, std::strlen(kAllowedPackagePrefix)) == 0 &&
         std::strchr(name, ':') == nullptr;  // 只在主进程，不在 :remote 等子进程
}

__attribute__((constructor)) void tianze_probe_init() {
  if (!process_allowed()) return;
  LOGI("tianze probe loaded");
  tz::start_server(kDefaultPort);
  // libg 映射完成后才能校验与 hook；作为 libg 的依赖库，我们的构造函数可能早于 libg 进入 soinfo 列表，
  // 因此在后台线程轮询，最多等 30 秒。
  std::thread([] {
    for (int i = 0; i < 300; ++i) {
      if (tz::Engine::get().init()) return;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
      if (!tz::Engine::get().fingerprint_mismatches().empty()) return;  // 指纹不符：不再重试
    }
    LOGE("probe init 超时: %s", tz::Engine::get().init_error().c_str());
  }).detach();
}

}  // namespace
