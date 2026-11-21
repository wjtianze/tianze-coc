#include "offline_boot.h"

#include <arpa/inet.h>
#include <dlfcn.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/system_properties.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <mutex>
#include <thread>

#include "common.h"
#include "inline_hook.h"
#include "netlink.h"

namespace tz::offline {
namespace {

std::atomic<bool> g_enabled{false};
std::atomic<int> g_game_port{9339};
std::atomic<int> g_fake_port{27651};
std::atomic<int> g_keepalive_req{10108};
std::atomic<int> g_keepalive_resp{20108};
std::atomic<long long> g_redirected{0};
std::mutex g_mu;
bool g_hooked = false;
bool g_server_started = false;

int raw_connect(int fd, const sockaddr *addr, socklen_t len) {
  return static_cast<int>(syscall(__NR_connect, fd, addr, len));
}

int hk_connect(int fd, const sockaddr *addr, socklen_t len) {
  if (g_enabled.load() && addr) {
    const int port = g_game_port.load();
    if (addr->sa_family == AF_INET && len >= static_cast<socklen_t>(sizeof(sockaddr_in))) {
      const auto *a = reinterpret_cast<const sockaddr_in *>(addr);
      if (ntohs(a->sin_port) == port) {
        sockaddr_in b = *a;
        b.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        b.sin_port = htons(static_cast<uint16_t>(g_fake_port.load()));
        g_redirected.fetch_add(1);
        LOGI("离线启动：游戏服务器连接已改到本地假服务器");
        return raw_connect(fd, reinterpret_cast<const sockaddr *>(&b), sizeof b);
      }
    } else if (addr->sa_family == AF_INET6 && len >= static_cast<socklen_t>(sizeof(sockaddr_in6))) {
      const auto *a = reinterpret_cast<const sockaddr_in6 *>(addr);
      if (ntohs(a->sin6_port) == port) {
        sockaddr_in6 b = *a;
        std::memset(&b.sin6_addr, 0, sizeof b.sin6_addr);
        // ::ffff:127.0.0.1（双栈套接字）
        b.sin6_addr.s6_addr[10] = 0xff;
        b.sin6_addr.s6_addr[11] = 0xff;
        b.sin6_addr.s6_addr[12] = 127;
        b.sin6_addr.s6_addr[15] = 1;
        b.sin6_port = htons(static_cast<uint16_t>(g_fake_port.load()));
        g_redirected.fetch_add(1);
        LOGI("离线启动：游戏服务器连接（IPv6）已改到本地假服务器");
        return raw_connect(fd, reinterpret_cast<const sockaddr *>(&b), sizeof b);
      }
    }
  }
  return raw_connect(fd, addr, len);
}

// 假服务器：接受连接，读掉客户端发来的一切，从不回复
void start_fake_server() {
  if (g_server_started) return;
  g_server_started = true;
  const int port = g_fake_port.load();
  std::thread([port] {
    const int srv = socket(AF_INET6, SOCK_STREAM, 0);
    int one = 1, zero = 0;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(srv, IPPROTO_IPV6, IPV6_V6ONLY, &zero, sizeof zero);  // 同时接受 IPv4（::ffff:127.0.0.1）
    sockaddr_in6 a{};
    a.sin6_family = AF_INET6;
    a.sin6_port = htons(static_cast<uint16_t>(port));
    a.sin6_addr = in6addr_any;
    if (bind(srv, reinterpret_cast<sockaddr *>(&a), sizeof a) != 0 || listen(srv, 4) != 0) {
      LOGE("离线启动：假服务器监听 %d 失败", port);
      close(srv);
      return;
    }
    LOGI("离线启动：假服务器监听 %d", port);
    while (true) {
      const int c = accept(srv, nullptr, nullptr);
      if (c < 0) continue;
      std::thread([c] {
        char buf[4096];
        while (recv(c, buf, sizeof buf, 0) > 0) {
        }
        close(c);
      }).detach();
    }
  }).detach();
}

bool install_hook() {
  if (g_hooked) return true;
  void *h = dlopen("libc.so", RTLD_NOW | RTLD_NOLOAD);
  auto target = reinterpret_cast<std::uintptr_t>(h ? dlsym(h, "connect") : nullptr);
  if (!target) {
    LOGE("离线启动：找不到 libc connect");
    return false;
  }
  std::uint8_t cur[16];
  std::memcpy(cur, reinterpret_cast<const void *>(target), 16);
  void *orig = nullptr;  // 不使用：替换函数自己发系统调用
  g_hooked = install_inline_hook(target, cur, reinterpret_cast<void *>(&hk_connect), &orig, "libc connect");
  return g_hooked;
}

void enable() {
  std::lock_guard<std::mutex> lk(g_mu);
  start_fake_server();
  if (install_hook()) g_enabled = true;
}

}  // namespace

void init_from_property() {
  char v[PROP_VALUE_MAX] = {};
  if (__system_property_get("debug.tianze.offline_boot", v) > 0 && std::strcmp(v, "1") == 0) {
    LOGI("离线启动：由系统属性启用");
    enable();
  }
}

void configure(const json::Value &cfg, json::Writer &w) {
  if (cfg.has("game_port")) g_game_port = static_cast<int>(cfg["game_port"].i64());
  if (cfg.has("fake_port")) g_fake_port = static_cast<int>(cfg["fake_port"].i64());
  if (cfg.has("keepalive_req")) g_keepalive_req = static_cast<int>(cfg["keepalive_req"].i64());
  if (cfg.has("keepalive_resp")) g_keepalive_resp = static_cast<int>(cfg["keepalive_resp"].i64());
  if (cfg.has("enable")) {
    if (cfg["enable"].boolean()) enable();
    else g_enabled = false;
  }
  w.key("enabled").boolean(g_enabled.load()).key("hooked").boolean(g_hooked);
  w.key("game_port").num(static_cast<long long>(g_game_port.load()));
  w.key("redirected").num(g_redirected.load());
}

bool enabled() { return g_enabled.load(); }
int keepalive_resp_type() { return g_keepalive_resp.load(); }

void on_outgoing(int type) {
  if (g_enabled.load() && type == g_keepalive_req.load()) net::queue_synthetic(g_keepalive_resp.load());
}

}  // namespace tz::offline
