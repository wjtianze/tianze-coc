#include "netlink.h"

#include <sys/mman.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>

#include "../generated/offsets.h"
#include "common.h"
#include "engine.h"
#include "inline_hook.h"
#include "offline_boot.h"

namespace tz::net {
namespace {

using DecodeFn = void (*)(void *msg);
using GetTypeFn = int (*)(void *msg);
using FactoryFn = void *(*)(void *factory, int type);
using DeliverFn = void (*)(void *message_manager, void *msg, int flag);
using ReceiveFn = void (*)(void *message_manager, void *msg);
using NewFn = void *(*)(std::size_t);
using ClientUpdateFn = void (*)(void *client, float dt);
using SendOneFn = std::uint64_t (*)(void *messaging, void *msg);

constexpr std::size_t kMaxRecord = 16u << 20;

struct Patched {
  int type;
  std::uintptr_t vtable;
  DecodeFn orig_decode;
};

std::uintptr_t g_base = 0;
std::atomic<bool> g_ready{false};
Patched g_patched[4];
int g_npatched = 0;

std::mutex g_rec_mu;
struct Record {
  std::vector<std::uint8_t> bytes;
  long long seq = 0;
  long long captured_ms = 0;
  bool from_python = false;
};
std::map<int, Record> g_records;
long long g_rec_seq = 0;
thread_local bool t_replaying = false;
thread_local bool g_in_replay_call = false;

// 重放队列（服务线程 -> 主线程）
std::mutex g_rep_mu;
std::condition_variable g_rep_cv;
struct Job {
  long long ticket;
  int type;
  std::vector<std::uint8_t> bytes;
};
std::deque<Job> g_jobs;
std::map<long long, std::string> g_job_status;  // ticket -> "ok" / 错误
long long g_ticket = 0;

ClientUpdateFn g_orig_client_update = nullptr;
ReceiveFn g_orig_receive = nullptr;

// 收包日志：每条进入 MessageManager::receiveMessage 的消息（类型、长度、时间、是否我们重放的）
struct InboxEntry {
  long long ms;
  int type;
  int len;
  bool replayed;
};
std::mutex g_inbox_mu;
std::deque<InboxEntry> g_inbox;
std::atomic<bool> g_record_all{true};
std::atomic<int> g_login_prep{0};  // 1 = 下一帧把登录状态设为"已发登录、握手已验证"（离线启动）
std::atomic<int> g_login_state_seen{-1};  // 录下所有类型的消息字节（供离线启动重放）
std::atomic<long long> g_frames{0};
SendOneFn g_orig_send = nullptr;

// 外发过滤
std::mutex g_net_mu;
bool g_offline = false;
std::atomic<int> g_deliver_mode{1};  // 1（默认，联调验证可用）: receiveMessage(mm,msg)  0: deliver(mm,msg,0)  2: 改道桩(*mm,msg)
std::atomic<std::uintptr_t> g_stub{0};   // 改道桩地址（相对 libg 基址），由 Python 下发
// EnemyHomeData 只在客户端状态 ∈ clientState_enemyHomeMask 时被处理（例如"搜索对手中"）。
// 重放前若状态不在集合内，把它设为 g_gate_value（默认 2）。
std::atomic<int> g_gate_value{2};
bool g_block_all = false;
std::set<int> g_block, g_allow;
std::map<int, long long> g_sent_count, g_dropped_count;
std::deque<int> g_recent;

long long now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

int type_of(void *msg) {
  const std::uintptr_t vt = rdp(reinterpret_cast<std::uintptr_t>(msg));
  const std::uintptr_t fn = vt ? rdp(vt + 8 * off::PiranhaMessage_vt_getType) : 0;
  if (!fn || fn < g_base || fn > g_base + 0x1900000) return -1;
  return reinterpret_cast<GetTypeFn>(fn)(msg);
}

void decode_wrapper(void *msg) {
  const std::uintptr_t vt = rdp(reinterpret_cast<std::uintptr_t>(msg));
  const Patched *p = nullptr;
  for (int i = 0; i < g_npatched; ++i)
    if (g_patched[i].vtable == vt) p = &g_patched[i];
  if (p == nullptr) return;  // 不应发生
  if (!t_replaying) {
    const std::uintptr_t bs = reinterpret_cast<std::uintptr_t>(msg) + off::PiranhaMessage_byteStream;
    const int len = rd_or<int>(bs + off::ByteStream_length, 0);
    const std::uintptr_t buf = rdp(bs + off::ByteStream_buffer);
    if (len > 0 && static_cast<std::size_t>(len) <= kMaxRecord && buf) {
      Record r;
      r.bytes.resize(static_cast<std::size_t>(len));
      if (safe_read(buf, r.bytes.data(), r.bytes.size())) {
        std::lock_guard<std::mutex> lk(g_rec_mu);
        r.seq = ++g_rec_seq;
        r.captured_ms = now_ms();
        g_records[p->type] = std::move(r);
        LOGI("录制消息 type=%d len=%d", p->type, len);
      }
    }
  }
  p->orig_decode(msg);
}

bool patch_slot(std::uintptr_t slot_addr, std::uintptr_t value) {
  const std::uintptr_t page = static_cast<std::uintptr_t>(sysconf(_SC_PAGESIZE));
  const std::uintptr_t start = slot_addr & ~(page - 1);
  if (mprotect(reinterpret_cast<void *>(start), page, PROT_READ | PROT_WRITE) != 0) return false;
  __atomic_store_n(reinterpret_cast<std::uintptr_t *>(slot_addr), value, __ATOMIC_SEQ_CST);
  mprotect(reinterpret_cast<void *>(start), page, PROT_READ);
  return true;
}

// 主线程：执行一次重放
std::string run_job(const Job &j) {
  void *msg = reinterpret_cast<FactoryFn>(g_base + off::MessageFactory_create)(nullptr, j.type);
  if (msg == nullptr) return "factory_returned_null";
  if (type_of(msg) != j.type) return "factory_type_mismatch";
  const std::uintptr_t bs = reinterpret_cast<std::uintptr_t>(msg) + off::PiranhaMessage_byteStream;
  void *buf = reinterpret_cast<NewFn>(g_base + off::operator_new)(j.bytes.size());
  std::memcpy(buf, j.bytes.data(), j.bytes.size());
  *reinterpret_cast<void **>(bs + off::ByteStream_ownedBuffer) = buf;  // 由 ByteStream 析构释放
  *reinterpret_cast<void **>(bs + off::ByteStream_buffer) = buf;
  *reinterpret_cast<int *>(bs + off::ByteStream_length) = static_cast<int>(j.bytes.size());
  *reinterpret_cast<int *>(bs + off::ByteStream_offset) = 0;
  *reinterpret_cast<int *>(bs + off::ByteStream_bitIndex) = 0;
  t_replaying = true;
  const std::uintptr_t vt = rdp(reinterpret_cast<std::uintptr_t>(msg));
  reinterpret_cast<DecodeFn>(rdp(vt + 8 * off::PiranhaMessage_vt_decode))(msg);
  t_replaying = false;
  void *mm = reinterpret_cast<void *>(rdp(g_base + off::g_messageManager));
  if (mm == nullptr) return "message_manager_null";
  if (j.type == off::msg_EnemyHomeData) {
    auto *state = reinterpret_cast<int *>(g_base + off::g_clientState);
    const int cur = *state;
    if (cur < 0 || cur > 31 || !((off::clientState_enemyHomeMask >> cur) & 1)) {
      LOGI("客户端状态 %d -> %d（使 EnemyHomeData 被接受）", cur, g_gate_value.load());
      *state = g_gate_value.load();
    }
  }
  // 消息对象不在此释放（处理函数可能"租借"它）。
  switch (g_deliver_mode.load()) {
    case 1:
      g_in_replay_call = true;
      reinterpret_cast<ReceiveFn>(g_base + off::MessageManager_receiveMessage)(mm, msg);
      g_in_replay_call = false;
      return "ok:receive";
    case 2: {
      const std::uintptr_t stub = g_stub.load();
      if (!stub) return "stub_not_configured";
      reinterpret_cast<ReceiveFn>(g_base + stub)(reinterpret_cast<void *>(rdp(reinterpret_cast<std::uintptr_t>(mm))), msg);
      return "ok:stub";
    }
    default:
      reinterpret_cast<DeliverFn>(g_base + off::MessageManager_deliver)(mm, msg, 0);
      return "ok";
  }
}

void hk_receive(void *mm, void *msg) {
  const int t = msg ? type_of(msg) : -1;
  const bool replay = t_replaying || g_in_replay_call;
  if (msg && t > 0) {
    const std::uintptr_t bs = reinterpret_cast<std::uintptr_t>(msg) + off::PiranhaMessage_byteStream;
    const int len = rd_or<int>(bs + off::ByteStream_length, 0);
    {
      std::lock_guard<std::mutex> lk(g_inbox_mu);
      g_inbox.push_back({now_ms(), t, len, replay});
      if (g_inbox.size() > 2048) g_inbox.pop_front();
    }
    const std::uintptr_t buf = rdp(bs + off::ByteStream_buffer);
    if (!replay && g_record_all.load() && len > 0 && static_cast<std::size_t>(len) <= kMaxRecord && buf) {
      Record r;
      r.bytes.resize(static_cast<std::size_t>(len));
      if (safe_read(buf, r.bytes.data(), r.bytes.size())) {
        std::lock_guard<std::mutex> lk(g_rec_mu);
        auto it = g_records.find(t);
        // 已由 decode 钩子录下的同一条消息不重复覆盖（两者内容相同）
        if (it == g_records.end() || it->second.bytes != r.bytes) {
          r.seq = ++g_rec_seq;
          r.captured_ms = now_ms();
          g_records[t] = std::move(r);
        }
      }
    }
  }
  g_orig_receive(mm, msg);
}

void hk_client_update(void *client, float dt) {
  g_frames.fetch_add(1, std::memory_order_relaxed);
  {
    const std::uintptr_t ls = rdp(g_base + off::g_loginState);
    if (ls) g_login_state_seen = rd_or<int>(ls, -1);
    if (ls && g_login_prep.exchange(0) == 1) {
      // 离线启动：LoginOk 处理函数要求 状态 == 等待登录回复 且 握手已验证，否则丢弃
      *reinterpret_cast<int *>(ls) = off::LoginState_awaitingValue;
      *reinterpret_cast<std::uint8_t *>(ls + off::LoginState_validatedFlag) = 1;
      LOGI("离线启动：登录状态已设置");
    }
  }
  std::deque<Job> jobs;
  {
    std::lock_guard<std::mutex> lk(g_rep_mu);
    jobs.swap(g_jobs);
  }
  for (const auto &j : jobs) {
    const std::string st = run_job(j);
    LOGI("重放消息 type=%d len=%zu -> %s", j.type, j.bytes.size(), st.c_str());
    std::lock_guard<std::mutex> lk(g_rep_mu);
    if (j.ticket) g_job_status[j.ticket] = st;
    g_rep_cv.notify_all();
  }
  // 战斗被教程/弹窗暂停时 updateSubTick 不会被调用，因此快照请求也在每帧这里处理
  Engine::get().service_snapshot();
  Engine::get().service_raw();
  g_orig_client_update(client, dt);
}

std::uint64_t hk_send_one(void *messaging, void *msg) {
  const int t = type_of(msg);
  offline::on_outgoing(t);
  bool drop = false;
  {
    std::lock_guard<std::mutex> lk(g_net_mu);
    if (g_offline) drop = g_block_all ? !g_allow.count(t) : g_block.count(t) > 0;
    (drop ? g_dropped_count : g_sent_count)[t]++;
    g_recent.push_back(drop ? -t : t);
    if (g_recent.size() > 256) g_recent.pop_front();
  }
  if (drop) return 0;  // 发送循环随后照常析构并删除消息
  return g_orig_send(messaging, msg);
}

}  // namespace

bool init(std::uintptr_t base) {
  g_base = base;
  const std::pair<int, std::uintptr_t> msgs[4] = {
      {off::msg_EnemyHomeData, off::EnemyHomeDataMessage_vtable},
      {off::msg_NpcData, off::NpcDataMessage_vtable},
      {off::msg_OwnHomeData, off::OwnHomeDataMessage_vtable},
      {off::msg_VisitedHomeData, off::VisitedHomeDataMessage_vtable}};
  for (const auto &m : msgs) {
    const std::uintptr_t vt = base + m.second;
    const std::uintptr_t slot = vt + 8 * off::PiranhaMessage_vt_decode;
    Patched p{m.first, vt, reinterpret_cast<DecodeFn>(rdp(slot))};
    if (!p.orig_decode) continue;
    g_patched[g_npatched++] = p;
    if (!patch_slot(slot, reinterpret_cast<std::uintptr_t>(&decode_wrapper))) {
      LOGE("无法改写消息 %d 的 decode 槽", m.first);
      --g_npatched;
    }
  }
  void *o1 = nullptr, *o2 = nullptr;
  const bool h1 = install_inline_hook(base + off::Client_update, off::Client_update_prologue,
                                      reinterpret_cast<void *>(&hk_client_update), &o1, "ClientInstance::update");
  const bool h2 = install_inline_hook(base + off::Messaging_sendOne, off::Messaging_sendOne_prologue,
                                      reinterpret_cast<void *>(&hk_send_one), &o2, "Messaging::sendOne");
  g_orig_client_update = reinterpret_cast<ClientUpdateFn>(o1);
  g_orig_send = reinterpret_cast<SendOneFn>(o2);
  void *o3 = nullptr;
  if (install_inline_hook(base + off::MessageManager_receiveMessage, off::MessageManager_receiveMessage_prologue,
                          reinterpret_cast<void *>(&hk_receive), &o3, "MessageManager::receiveMessage")) {
    g_orig_receive = reinterpret_cast<ReceiveFn>(o3);
  } else {
    LOGW("收包录制不可用");
  }
  g_ready = h1 && h2 && g_npatched == 4;
  return g_ready;
}

bool ready() { return g_ready.load(); }
long long frame_count() { return g_frames.load(); }

void list_records(json::Writer &w) {
  std::lock_guard<std::mutex> lk(g_rec_mu);
  w.key("records").begin_arr();
  for (const auto &kv : g_records) {
    w.begin_obj().key("type").num(static_cast<long long>(kv.first));
    w.key("len").num(static_cast<long long>(kv.second.bytes.size())).key("record_seq").num(kv.second.seq);
    w.key("from_python").boolean(kv.second.from_python).end_obj();
  }
  w.end_arr();
}

bool get_record(int type, json::Writer &w) {
  std::lock_guard<std::mutex> lk(g_rec_mu);
  auto it = g_records.find(type);
  if (it == g_records.end()) return false;
  static const char *hx = "0123456789abcdef";
  std::string h;
  h.reserve(it->second.bytes.size() * 2);
  for (unsigned char c : it->second.bytes) {
    h += hx[c >> 4];
    h += hx[c & 15];
  }
  w.key("hex").str(h).key("record_seq").num(it->second.seq);
  return true;
}

bool put_record(int type, const std::string &hex, std::string &err) {
  if (hex.size() % 2 || hex.size() / 2 > kMaxRecord) {
    err = "bad hex length";
    return false;
  }
  Record r;
  r.bytes.resize(hex.size() / 2);
  auto nib = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  for (std::size_t i = 0; i < r.bytes.size(); ++i) {
    const int a = nib(hex[2 * i]), b = nib(hex[2 * i + 1]);
    if (a < 0 || b < 0) {
      err = "bad hex";
      return false;
    }
    r.bytes[i] = static_cast<std::uint8_t>(a << 4 | b);
  }
  std::lock_guard<std::mutex> lk(g_rec_mu);
  r.seq = ++g_rec_seq;
  r.captured_ms = now_ms();
  r.from_python = true;
  g_records[type] = std::move(r);
  return true;
}

long long queue_replay(int type, std::string &err) {
  if (!g_ready) {
    err = "netlink not ready";
    return -1;
  }
  Job j;
  j.type = type;
  {
    std::lock_guard<std::mutex> lk(g_rec_mu);
    auto it = g_records.find(type);
    if (it == g_records.end()) {
      err = "no record for this message type";
      return -1;
    }
    j.bytes = it->second.bytes;
  }
  std::lock_guard<std::mutex> lk(g_rep_mu);
  j.ticket = ++g_ticket;
  g_jobs.push_back(std::move(j));
  return g_ticket;
}

bool wait_replay(long long ticket, int timeout_ms, std::string &status) {
  std::unique_lock<std::mutex> lk(g_rep_mu);
  const bool ok = g_rep_cv.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                                    [&] { return g_job_status.count(ticket) > 0; });
  status = ok ? g_job_status[ticket] : "timeout";
  if (ok) g_job_status.erase(ticket);
  return ok && status.rfind("ok", 0) == 0;
}

void configure(const json::Value &cfg, json::Writer &w) {
  std::lock_guard<std::mutex> lk(g_net_mu);
  if (cfg.has("offline")) g_offline = cfg["offline"].boolean();
  if (cfg.has("block_all")) g_block_all = cfg["block_all"].boolean();
  auto read_set = [&](const char *k, std::set<int> &out) {
    if (!cfg[k].is_arr()) return;
    out.clear();
    for (const auto &v : cfg[k].a) out.insert(static_cast<int>(v.i64()));
  };
  if (cfg.has("deliver_mode")) g_deliver_mode = static_cast<int>(cfg["deliver_mode"].i64());
  if (cfg.has("deliver_stub")) g_stub = static_cast<std::uintptr_t>(cfg["deliver_stub"].i64());
  if (cfg.has("gate_value")) g_gate_value = static_cast<int>(cfg["gate_value"].i64());
  if (cfg.has("record_all")) g_record_all = cfg["record_all"].boolean();
  read_set("block_types", g_block);
  read_set("allow_types", g_allow);
  w.key("offline").boolean(g_offline).key("block_all").boolean(g_block_all);
  w.key("deliver_mode").num(static_cast<long long>(g_deliver_mode.load()));
  w.key("block_types").num(static_cast<long long>(g_block.size()));
  w.key("allow_types").num(static_cast<long long>(g_allow.size()));
}

void login_prep() { g_login_prep = 1; }

bool replay_now(int type, std::string &status) {
  Job j;
  j.type = type;
  {
    std::lock_guard<std::mutex> lk(g_rec_mu);
    auto it = g_records.find(type);
    if (it == g_records.end()) { status = "no record"; return false; }
    j.bytes = it->second.bytes;
  }
  // 登录状态门控（与 hk_client_update 中一致）：引导阶段帧循环还没跑到那段代码
  const std::uintptr_t ls = rdp(g_base + off::g_loginState);
  if (ls && g_login_prep.exchange(0) == 1) {
    *reinterpret_cast<int *>(ls) = off::LoginState_awaitingValue;
    *reinterpret_cast<std::uint8_t *>(ls + off::LoginState_validatedFlag) = 1;
  }
  status = run_job(j);
  return status.rfind("ok", 0) == 0;
}

void queue_synthetic(int type) {
  Job j;
  j.type = type;
  j.ticket = 0;  // 无人等待结果
  std::lock_guard<std::mutex> lk(g_rep_mu);
  g_jobs.push_back(std::move(j));
}
int login_state() { return g_login_state_seen.load(); }

void write_inbox(json::Writer &w, bool clear) {
  std::lock_guard<std::mutex> lk(g_inbox_mu);
  w.key("inbox").begin_arr();
  for (const auto &e : g_inbox) {
    w.begin_arr().num(e.ms).num(static_cast<long long>(e.type)).num(static_cast<long long>(e.len));
    w.boolean(e.replayed).end_arr();
  }
  w.end_arr();
  if (clear) g_inbox.clear();
}

void write_net_log(json::Writer &w) {
  std::lock_guard<std::mutex> lk(g_net_mu);
  auto dump = [&](const char *k, const std::map<int, long long> &m) {
    w.key(k).begin_arr();
    for (const auto &kv : m) w.begin_arr().num(static_cast<long long>(kv.first)).num(kv.second).end_arr();
    w.end_arr();
  };
  dump("sent", g_sent_count);
  dump("dropped", g_dropped_count);
  w.key("recent").begin_arr();
  for (int t : g_recent) w.num(static_cast<long long>(t));
  w.end_arr();
}

}  // namespace tz::net
