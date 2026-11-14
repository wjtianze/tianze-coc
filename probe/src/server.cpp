// JSON 行协议服务：每行一个请求对象，每行一个响应对象。
// 只监听 127.0.0.1，由 PC 端通过 `adb forward tcp:27650 tcp:27650` 访问。
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstring>
#include <string>
#include <thread>

#include "../generated/offsets.h"
#include "common.h"
#include "engine.h"
#include "netlink.h"
#include "json.h"

namespace tz {
namespace {

constexpr int kProtocolVersion = 1;

bool parse_actions(const json::Value &arr, std::vector<Action> &out, std::string &err) {
  if (!arr.is_arr()) return true;
  for (const auto &v : arr.a) {
    Action a;
    a.id = v["id"].i64();
    const std::string kind = v["kind"].str();
    if (kind == "troop" || kind == "siege") a.kind = ActionKind::Troop;
    else if (kind == "spell") a.kind = ActionKind::Spell;
    else if (kind == "hero") a.kind = ActionKind::Hero;
    else if (kind == "cc") a.kind = ActionKind::Portal;          // 部落援军（传送门）
    else if (kind == "cc_siege") a.kind = ActionKind::CcSiege;   // 部落城堡里的攻城机器
    else if (kind == "cc_spell") a.kind = ActionKind::CcSpell;   // 部落援军里的法术（extra=等级）
    else if (kind == "ability") a.kind = ActionKind::Ability;    // 英雄/兵种技能、攻城机器释放
    else if (kind == "recalled") a.kind = ActionKind::Recalled;  // 回溯后重新部署
    else if (kind == "hero_mode") a.kind = ActionKind::HeroMode;  // 英雄模式切换（flag: 0 地面 1 空中）
    else { err = "unknown action kind: " + kind; return false; }
    a.flag = static_cast<int>(v["flag"].i64());
    a.extra = static_cast<int>(v["extra"].i64());
    a.data_global_id = static_cast<int>(v["data"].i64());
    // 坐标以逻辑单位传入（Python 侧负责 格子 -> 逻辑单位 的换算）
    a.x = static_cast<int>(v["x"].i64());
    a.y = static_cast<int>(v["y"].i64());
    out.push_back(a);
  }
  return true;
}

void write_results(json::Writer &w, const std::vector<ActionResult> &rs) {
  w.key("results").begin_arr();
  for (const auto &r : rs) {
    w.begin_obj().key("id").num(r.id).key("status").str(r.status).key("code").num(static_cast<long long>(r.code));
    if (!r.reason.empty()) w.key("reason").str(r.reason);
    w.key("tick").num(r.tick).end_obj();
  }
  w.end_arr();
}

std::string handle(const std::string &line) {
  json::Value req;
  json::Writer w;
  w.begin_obj();
  if (!json::Parser(line).parse(req) || !req.is_obj()) {
    w.key("ok").boolean(false).key("error").str("bad json").end_obj();
    return w.out;
  }
  if (req.has("seq")) w.key("seq").num(req["seq"].i64());
  const std::string op = req["op"].str();
  Engine &e = Engine::get();
  const int timeout = static_cast<int>(req["timeout_ms"].i64(5000));

  if (op == "hello") {
    w.key("ok").boolean(true).key("protocol").num(static_cast<long long>(kProtocolVersion));
    w.key("build_tag").str(off::kBuildTag).key("libg_sha256").str(off::kLibgSha256);
    w.key("ready").boolean(e.ready()).key("base").num(static_cast<long long>(e.base()));
    if (!e.ready()) w.key("init_error").str(e.init_error());
    w.key("fingerprint_mismatches").begin_arr();
    for (const auto &m : e.fingerprint_mismatches()) w.str(m);
    w.end_arr();
  } else if (!e.ready()) {
    w.key("ok").boolean(false).key("error").str("engine not ready: " + e.init_error());
  } else if (op == "status") {
    w.key("ok").boolean(true);
    e.status(w);
    w.key("frames").num(net::frame_count());
  } else if (op == "config") {
    w.key("ok").boolean(true);
    e.configure(req, w);
  } else if (op == "act") {
    std::vector<Action> acts;
    std::string err;
    if (!parse_actions(req["actions"], acts, err)) {
      w.key("ok").boolean(false).key("error").str(err);
    } else {
      e.enqueue(acts);
      w.key("ok").boolean(true).key("queued").num(static_cast<long long>(acts.size()));
    }
  } else if (op == "step") {
    // 原子操作：入队动作 -> 推进 n 个子帧 -> 返回动作结果与观测
    std::vector<Action> acts;
    std::string err;
    if (!parse_actions(req["actions"], acts, err)) {
      w.key("ok").boolean(false).key("error").str(err);
    } else {
      e.enqueue(acts);
      const bool done = e.step(static_cast<int>(req["subticks"].i64(1)), timeout);
      w.key("ok").boolean(true).key("completed").boolean(done);
      write_results(w, e.take_results());
      if (req["observe"].boolean(true)) {
        w.key("obs");
        if (!e.snapshot(w, timeout, req["objects"].boolean(true))) w.null();
      }
    }
  } else if (op == "observe") {
    w.key("ok").boolean(true).key("obs");
    if (!e.snapshot(w, timeout, req["objects"].boolean(true))) w.null();
    write_results(w, e.take_results());
  } else if (op == "objects_raw") {
    w.key("ok").boolean(true).key("raw");
    e.objects_raw(w, timeout);
  } else if (op == "cmd_log") {
    w.key("ok").boolean(true);
    e.cmd_log(w, req["clear"].boolean(false));
  } else if (op == "records") {
    w.key("ok").boolean(true).key("net_ready").boolean(net::ready());
    net::list_records(w);
  } else if (op == "record_get") {
    const bool found = net::get_record(static_cast<int>(req["type"].i64()), w);
    w.key("ok").boolean(found);
    if (!found) w.key("error").str("no record");
  } else if (op == "record_put") {
    std::string err;
    const bool ok = net::put_record(static_cast<int>(req["type"].i64()), req["hex"].str(), err);
    w.key("ok").boolean(ok);
    if (!ok) w.key("error").str(err);
  } else if (op == "replay") {
    // 在下一帧的主线程把录制的消息重新投递给客户端（离线开局 / 回主村）
    std::string err;
    const long long t = net::queue_replay(static_cast<int>(req["type"].i64()), err);
    if (t < 0) {
      w.key("ok").boolean(false).key("error").str(err);
    } else {
      std::string st;
      const bool ok = net::wait_replay(t, timeout, st);
      w.key("ok").boolean(ok).key("status").str(st);
      if (!ok) w.key("error").str("replay " + st);
    }
  } else if (op == "net_config") {
    w.key("ok").boolean(true);
    net::configure(req, w);
  } else if (op == "net_log") {
    w.key("ok").boolean(true);
    net::write_net_log(w);
  } else if (op == "peek") {
    // 校准用：读取任意地址（安全读取，非法地址返回 null）
    const auto addr = static_cast<std::uintptr_t>(req["addr"].i64());
    const auto len = static_cast<std::size_t>(std::min<long long>(req["len"].i64(64), 4096));
    std::string buf(len, '\0');
    w.key("ok").boolean(true).key("hex");
    if (safe_read(addr, buf.data(), len)) {
      static const char *hx = "0123456789abcdef";
      std::string h;
      h.reserve(len * 2);
      for (unsigned char c : buf) { h += hx[c >> 4]; h += hx[c & 15]; }
      w.str(h);
    } else {
      w.null();
    }
  } else {
    w.key("ok").boolean(false).key("error").str("unknown op: " + op);
  }
  w.end_obj();
  return w.out;
}

void serve_client(int fd) {
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  std::string buf;
  char chunk[4096];
  while (true) {
    const ssize_t n = recv(fd, chunk, sizeof chunk, 0);
    if (n <= 0) break;
    buf.append(chunk, static_cast<std::size_t>(n));
    std::size_t pos;
    while ((pos = buf.find('\n')) != std::string::npos) {
      std::string line = buf.substr(0, pos);
      buf.erase(0, pos + 1);
      if (line.empty()) continue;
      std::string resp = handle(line);
      resp += '\n';
      const char *p = resp.data();
      std::size_t left = resp.size();
      while (left > 0) {
        const ssize_t s = send(fd, p, left, MSG_NOSIGNAL);
        if (s <= 0) { close(fd); return; }
        p += s;
        left -= static_cast<std::size_t>(s);
      }
    }
  }
  close(fd);
}

}  // namespace

void start_server(int port) {
  std::thread([port] {
    const int srv = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(srv, reinterpret_cast<sockaddr *>(&addr), sizeof addr) != 0 || listen(srv, 4) != 0) {
      LOGE("监听 127.0.0.1:%d 失败: %s", port, std::strerror(errno));
      close(srv);
      return;
    }
    LOGI("探针服务监听 127.0.0.1:%d", port);
    while (true) {
      const int c = accept(srv, nullptr, nullptr);
      if (c < 0) continue;
      std::thread(serve_client, c).detach();  // 每个连接一个线程；实际只有一个 Python 客户端
    }
  }).detach();
}

}  // namespace tz
