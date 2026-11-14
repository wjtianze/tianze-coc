#include "engine.h"

#include <dlfcn.h>
#include <link.h>
#include <pthread.h>

#include <chrono>
#include <cstring>

#include "../generated/offsets.h"
#include "common.h"
#include "inline_hook.h"
#include "netlink.h"

namespace tz {
namespace {

using UpdateFn = bool (*)(void *game_mode);
using NewFn = void *(*)(std::size_t);
using TroopCtor = void (*)(void *self, void *data, int x, int y, int flag, int extra);
using SpellCtor = void (*)(void *self, void *data, int flag, int extra, int x, int y, int extra2);
using HeroCtor = void (*)(void *self, void *data, int x, int y);
using AddCommandFn = void (*)(void *command_manager, void **cmd);
using SubmitFn = void (*)(void **cmd);
using GetTableFn = void *(*)(int table_index);
using ExecFn = int (*)(void *cmd, void *level);
using PortalCtor = void (*)(void *self, void *portal_data, int x, int y);
using AbilityCtor = void (*)(void *self, void *unit_data);
using RecalledCtor = void (*)(void *self, int flag, int slot, int x, int y);
using HeroModeCtor = void (*)(void *self, void *hero_data, int mode);
using GetStarsFn = int (*)(void *battle_log);
using InstanceFn = void *(*)();
using DataTypeFn = int (*)(void *data);

int cmd_class(ActionKind k) {
  switch (k) {
    case ActionKind::Troop:
    case ActionKind::CcSiege: return 0;
    case ActionKind::Spell:
    case ActionKind::CcSpell: return 1;
    case ActionKind::Hero: return 2;
    case ActionKind::Portal: return 3;
    case ActionKind::Ability: return 4;
    case ActionKind::Recalled: return 5;
    case ActionKind::HeroMode: return 6;
  }
  return 0;
}

UpdateFn g_orig_update = nullptr;
SubmitFn g_orig_submit = nullptr;
void hk_submit(void **holder) {
  if (holder && *holder) Engine::get().on_submit(*holder);
  g_orig_submit(holder);
}
pthread_t g_game_thread{};
std::atomic<bool> g_game_thread_known{false};

constexpr int kVtSlots = 40;  // 复制的虚表长度（含 offset-to-top / typeinfo 两个前置槽）

bool hk_update_sub_tick(void *gm) { return Engine::get().on_update_sub_tick(gm); }

int dl_cb(dl_phdr_info *info, size_t, void *data) {
  const char *name = info->dlpi_name;
  if (!name) return 0;
  const std::size_t n = std::strlen(name);
  if (n >= 7 && std::strcmp(name + n - 7, "libg.so") == 0 && (n == 7 || name[n - 8] == '/')) {
    *static_cast<std::uintptr_t *>(data) = info->dlpi_addr;
    return 1;
  }
  return 0;
}

}  // namespace

Engine &Engine::get() {
  static Engine e;
  return e;
}

bool Engine::resolve_base() {
  std::uintptr_t b = 0;
  dl_iterate_phdr(dl_cb, &b);
  base_ = b;
  return b != 0;
}

bool Engine::verify_fingerprints() {
  mismatches_.clear();
  for (const auto &f : off::kFingerprints) {
    std::uint8_t cur[8];
    if (!safe_read(base_ + f.offset, cur, 8) || std::memcmp(cur, f.bytes, 8) != 0) mismatches_.emplace_back(f.name);
  }
  return mismatches_.empty();
}

bool Engine::init() {
  if (ready_) return true;
  if (!resolve_base()) {
    init_error_ = "libg.so 尚未加载";
    return false;
  }
  if (!verify_fingerprints()) {
    init_error_ = "函数指纹不符（libg 版本不匹配或被改动），共 " + std::to_string(mismatches_.size()) + " 处";
    LOGE("%s", init_error_.c_str());
    return false;
  }
  for (int k = 0; k < kCmdClasses; ++k) make_cmd_vtable(static_cast<ActionKind>(k));
  void *orig = nullptr;
  if (!install_inline_hook(base_ + off::LogicGameMode_updateSubTick, off::LogicGameMode_updateSubTick_prologue,
                           reinterpret_cast<void *>(&hk_update_sub_tick), &orig, "LogicGameMode::updateSubTick")) {
    init_error_ = "安装 updateSubTick hook 失败";
    return false;
  }
  g_orig_update = reinterpret_cast<UpdateFn>(orig);
  void *orig_submit = nullptr;
  if (install_inline_hook(base_ + off::ClientCommand_submit, off::ClientCommand_submit_prologue,
                          reinterpret_cast<void *>(&hk_submit), &orig_submit, "ClientCommand::submit")) {
    g_orig_submit = reinterpret_cast<SubmitFn>(orig_submit);
  } else {
    LOGW("命令侦听不可用");
  }
  if (!net::init(base_)) {
    // 消息录制/重放不可用时仍允许在线对战，但离线开局会报错
    LOGW("netlink 初始化失败：离线开局不可用");
  }
  ready_ = true;
  LOGI("engine ready, libg base=%p build=%s", reinterpret_cast<void *>(base_), off::kBuildTag);
  return true;
}

// 为每种命令复制一份虚表，把槽 3（execute）替换为包装函数，用来拿到引擎的真实执行结果。
// 复制而不是改原虚表：原虚表在 RELRO 只读段，且被所有同类命令共享。
void *Engine::make_cmd_vtable(ActionKind kind) {
  const int k = cmd_class(kind);
  if (vt_copy_[k]) return vt_copy_[k];
  static const std::uintptr_t kVtables[kCmdClasses] = {
      off::LogicPlaceAttackerCommand_vtable, off::LogicCastSpellCommand_vtable, off::LogicPlaceHeroCommand_vtable,
      off::LogicPlaceAlliancePortalCommand_vtable, off::LogicTriggerHeroAbilityCommand_vtable,
      off::LogicPlaceRecalledUnitsCommand_vtable, off::LogicHeroModeCommand_vtable};
  const std::uintptr_t vt = base_ + kVtables[k];
  auto *copy = new std::uintptr_t[kVtSlots + 2];
  for (int i = 0; i < kVtSlots + 2; ++i) copy[i] = rdp(vt - 16 + 8 * i);
  orig_exec_[k] = copy[2 + 3];
  copy[2 + 3] = reinterpret_cast<std::uintptr_t>(&Engine::exec_wrapper);
  vt_copy_[k] = copy + 2;
  return vt_copy_[k];
}

// 通用包装：根据对象的虚表副本找到种类，调用原 execute 并记录返回值。
int Engine::exec_wrapper(void *cmd, void *level) {
  Engine &e = get();
  const auto vt = *reinterpret_cast<void **>(cmd);
  int k = 0;
  for (; k < kCmdClasses; ++k)
    if (e.vt_copy_[k] == vt) break;
  if (k == kCmdClasses) return -100;  // 不应发生
  const int rc = reinterpret_cast<ExecFn>(e.orig_exec_[k])(cmd, level);
  std::lock_guard<std::mutex> lk(e.mu_);
  auto it = e.cmd_to_action_.find(reinterpret_cast<std::uintptr_t>(cmd));
  if (it != e.cmd_to_action_.end()) {
    ActionResult r;
    r.id = it->second;
    r.status = rc == 0 ? "executed" : "rejected";
    r.code = rc;
    r.reason = rc == 0 ? "" : "engine_execute";
    r.tick = rd_or<int>(reinterpret_cast<std::uintptr_t>(level) + off::LogicLevel_currentTick, -1);
    e.results_.push_back(r);
    e.cmd_to_action_.erase(it);
  }
  return rc;
}

std::uintptr_t Engine::current_game_mode() const {
  const std::uintptr_t client = rdp(base_ + off::g_clientInstance);
  return client ? rdp(client + off::ClientInstance_gameMode) : 0;
}

bool Engine::in_battle(std::uintptr_t gm) const {
  const int st = rd_or<int>(gm + off::LogicGameMode_state, -1);
  return st == 2 || st == 4;  // 2=进攻, 4=访问/特殊进攻（Client_getAvatar 对两者同样返回进攻方）
}

std::uintptr_t Engine::data_by_global_id(int gid) const {
  const int table = gid / 1000000 - off::global_id_table_bias;
  const int index = gid % 1000000;
  if (table < 0 || table > 200) return 0;
  const std::uintptr_t t =
      reinterpret_cast<std::uintptr_t>(reinterpret_cast<GetTableFn>(base_ + off::LogicDataTables_getTable)(table));
  if (!t) return 0;
  const int count = rd_or<int>(t + off::LogicDataTable_count, 0);
  if (index < 0 || index >= count) return 0;
  const std::uintptr_t items = rdp(t + off::LogicDataTable_items);
  const std::uintptr_t data = rdp(items + 8 * static_cast<std::uintptr_t>(index));
  // 交叉校验：数据对象自身记录的 GlobalID 必须一致
  if (!data || rd_or<int>(data + off::LogicData_globalId, -1) != gid) return 0;
  return data;
}

// 援军传送门数据：遍历数据表，取 getDataType() == data_type_alliance_portal 的表的第一项。
std::uintptr_t Engine::portal_data() {
  if (portal_data_) return portal_data_;
  const std::uintptr_t inst =
      reinterpret_cast<std::uintptr_t>(reinterpret_cast<InstanceFn>(base_ + off::LogicDataTables_instance)());
  const std::uintptr_t holder = rdp(inst + off::LogicDataTables_tableListHolder);
  const std::uintptr_t list = rdp(holder + 8);
  const std::uintptr_t arr = rdp(list + off::LogicArrayList_data);
  const int n = rd_or<int>(list + off::LogicArrayList_count, 0);
  for (int t = 0; t < n && t < 256; ++t) {
    const std::uintptr_t table = rdp(arr + 8 * static_cast<std::uintptr_t>(t));
    if (!table || rd_or<int>(table + off::LogicDataTable_count, 0) <= 0) continue;
    const std::uintptr_t item = rdp(rdp(table + off::LogicDataTable_items));
    const std::uintptr_t vt = item ? rdp(item) : 0;
    const std::uintptr_t fn = vt ? rdp(vt + 8 * off::LogicData_vt_getDataType) : 0;
    if (!fn || fn < base_ || fn > base_ + 0x1900000) continue;  // 虚函数必须落在 libg 内
    if (reinterpret_cast<DataTypeFn>(fn)(reinterpret_cast<void *>(item)) == off::data_type_alliance_portal) {
      portal_data_ = item;
      break;
    }
  }
  return portal_data_;
}

namespace {
void reject(ActionResult &res, const char *why) {
  res.status = "rejected";
  res.reason = why;
}
}  // namespace

bool Engine::submit_one(std::uintptr_t gm, const Action &a, ActionResult &res) {
  res.id = a.id;
  const bool needs_unit = a.kind != ActionKind::Portal && a.kind != ActionKind::Recalled;
  const bool needs_pos = a.kind != ActionKind::Ability && a.kind != ActionKind::HeroMode;
  if (needs_unit && (allowed_ids_.empty() || !allowed_ids_.count(a.data_global_id))) {
    reject(res, "unit_not_allowed");
    return false;
  }
  std::uintptr_t data = 0;
  if (a.kind == ActionKind::Portal) {
    data = portal_data();
    if (!data) {
      reject(res, "alliance_portal_data_not_found");
      return false;
    }
  } else if (needs_unit) {
    data = data_by_global_id(a.data_global_id);
    if (!data) {
      reject(res, "unknown_data");
      return false;
    }
  }
  const std::uintptr_t level = rdp(gm + off::LogicGameMode_level);
  const int w = rd_or<int>(level + off::LogicLevel_width, 0), h = rd_or<int>(level + off::LogicLevel_height, 0);
  if (needs_pos && (a.x < 0 || a.y < 0 || a.x >= w || a.y >= h)) {
    reject(res, "out_of_map");
    return false;
  }
  auto new_fn = reinterpret_cast<NewFn>(base_ + off::operator_new);
  void *d = reinterpret_cast<void *>(data);
  void *cmd = nullptr;
  switch (a.kind) {
    case ActionKind::Troop:
    case ActionKind::CcSiege:
      cmd = new_fn(off::LogicPlaceAttackerCommand_size);
      // flag=1 时从部落城堡的攻城机器中取第 extra 台（见放兵 execute 与 avatar 援军攻城机器查找）
      reinterpret_cast<TroopCtor>(base_ + off::LogicPlaceAttackerCommand_ctor)(
          cmd, d, a.x, a.y, a.kind == ActionKind::CcSiege ? 1 : 0, a.kind == ActionKind::CcSiege ? a.extra : 0);
      break;
    case ActionKind::Spell:
    case ActionKind::CcSpell:
      cmd = new_fn(off::LogicCastSpellCommand_size);
      // flag=1：从部落援军中扣除，extra=援军法术等级（execute 按 数据+等级 匹配援军项）；最后一个参数与游戏一致传 -1
      reinterpret_cast<SpellCtor>(base_ + off::LogicCastSpellCommand_ctor)(
          cmd, d, a.kind == ActionKind::CcSpell ? 1 : 0, a.kind == ActionKind::CcSpell ? a.extra : 0, a.x, a.y, -1);
      break;
    case ActionKind::Hero:
      cmd = new_fn(off::LogicPlaceHeroCommand_size);
      reinterpret_cast<HeroCtor>(base_ + off::LogicPlaceHeroCommand_ctor)(cmd, d, a.x, a.y);
      break;
    case ActionKind::Portal:
      cmd = new_fn(off::LogicPlaceAlliancePortalCommand_size);
      reinterpret_cast<PortalCtor>(base_ + off::LogicPlaceAlliancePortalCommand_ctor)(cmd, d, a.x, a.y);
      break;
    case ActionKind::Ability:
      cmd = new_fn(off::LogicTriggerHeroAbilityCommand_size);
      reinterpret_cast<AbilityCtor>(base_ + off::LogicTriggerHeroAbilityCommand_ctor)(cmd, d);
      break;
    case ActionKind::HeroMode:
      // 与 UI 相同：模式切换命令 (英雄数据, 模式)，需在英雄部署前发出
      cmd = new_fn(off::LogicHeroModeCommand_size);
      reinterpret_cast<HeroModeCtor>(base_ + off::LogicHeroModeCommand_ctor)(cmd, d, a.flag);
      break;
    case ActionKind::Recalled:
      cmd = new_fn(off::LogicPlaceRecalledUnitsCommand_size);
      reinterpret_cast<RecalledCtor>(base_ + off::LogicPlaceRecalledUnitsCommand_ctor)(cmd, a.flag, a.extra, a.x, a.y);
      break;
  }
  *reinterpret_cast<void **>(cmd) = make_cmd_vtable(a.kind);
  cmd_to_action_[reinterpret_cast<std::uintptr_t>(cmd)] = a.id;
  void *holder = cmd;
  if (submit_mode_client_) {
    // 与游戏 UI / DebugUtil::deployAllTroops 相同的提交路径
    // 经过 hook 后的提交函数（命令侦听也会记录我们自己的命令）；hook 未装上时直接调用原函数
    reinterpret_cast<SubmitFn>(base_ + off::ClientCommand_submit)(&holder);
  } else {
    const std::uintptr_t cm = rdp(gm + off::LogicGameMode_commandManager);
    reinterpret_cast<AddCommandFn>(base_ + off::LogicCommandManager_addCommand)(reinterpret_cast<void *>(cm), &holder);
  }
  if (holder == nullptr) {  // 提交函数在拒绝时会删除命令并把指针清零
    cmd_to_action_.erase(reinterpret_cast<std::uintptr_t>(cmd));
    reject(res, "command_manager_refused");
    return false;
  }
  res.status = "submitted";
  res.tick = rd_or<int>(level + off::LogicLevel_currentTick, -1);
  return true;
}

// 每个子帧最多注入一个命令：联调发现同一子帧提交的多个命令只有第一个会被执行，其余被引擎静默丢弃。
// 被本地拒绝的动作不占用名额，继续尝试下一个。
void Engine::inject_pending(std::uintptr_t gm) {
  while (!pending_.empty()) {
    Action a = pending_.front();
    pending_.pop_front();
    ActionResult r;
    const bool submitted = submit_one(gm, a, r);
    results_.push_back(r);
    if (submitted) break;
  }
}

bool Engine::on_update_sub_tick(void *game_mode) {
  if (!g_game_thread_known.exchange(true)) g_game_thread = pthread_self();
  const auto gm = reinterpret_cast<std::uintptr_t>(game_mode);
  if (gm != current_game_mode()) return g_orig_update(game_mode);  // 其他模拟（回放预览等）不干预

  std::unique_lock<std::mutex> lk(mu_);
  const bool battle = in_battle(gm);
  serve_snapshot_locked(gm);
  if (lockstep_ && battle && budget_ <= 0) {
    // 冻结：不推进逻辑，但画面线程照常运行
    return rd_or<std::uint8_t>(gm + off::LogicGameMode_battleOver, 0) != 0;
  }
  int runs = 1;
  if (lockstep_ && battle) runs = static_cast<int>(std::min<long long>(turbo_, budget_));
  bool r = false;
  for (int i = 0; i < runs; ++i) {
    if (battle) inject_pending(gm);
    lk.unlock();
    r = g_orig_update(game_mode);
    lk.lock();
    if (lockstep_ && battle) {
      --budget_;
      ++subticks_done_;
    }
    if (rd_or<std::uint8_t>(gm + off::LogicGameMode_battleOver, 0) != 0) {
      battle_over_seen_ = true;
      budget_ = 0;
      break;
    }
  }
  cv_.notify_all();
  return r;
}

void Engine::on_submit(void *cmd) {
  const auto c = reinterpret_cast<std::uintptr_t>(cmd);
  const std::uintptr_t vt = rdp(c);
  SpyEntry e;
  e.ours = false;
  for (int k = 0; k < kCmdClasses; ++k)
    if (reinterpret_cast<std::uintptr_t>(vt_copy_[k]) == vt) e.ours = true;
  const std::uintptr_t fn = vt ? rdp(vt + 4 * 8) : 0;  // 槽 4：getCommandType
  e.type = (fn > base_ && fn < base_ + 0x1900000) ? reinterpret_cast<int (*)(void *)>(fn)(cmd) : -1;
  const std::uintptr_t data = rdp(c + 0x10);
  e.data_gid = data ? rd_or<int>(data + off::LogicData_globalId, -1) : -1;
  std::uint8_t raw[0x40] = {};
  safe_read(c, raw, sizeof raw);
  static const char *hx = "0123456789abcdef";
  for (unsigned char b : raw) {
    e.hex += hx[b >> 4];
    e.hex += hx[b & 15];
  }
  const std::uintptr_t gm = current_game_mode();
  const std::uintptr_t level = gm ? rdp(gm + off::LogicGameMode_level) : 0;
  e.tick = level ? rd_or<int>(level + off::LogicLevel_currentTick, -1) : -1;
  std::lock_guard<std::mutex> lk(spy_mu_);
  spy_.push_back(std::move(e));
  if (spy_.size() > 512) spy_.pop_front();
}

void Engine::cmd_log(json::Writer &w, bool clear) {
  std::lock_guard<std::mutex> lk(spy_mu_);
  w.key("commands").begin_arr();
  for (const auto &e : spy_) {
    w.begin_obj().key("tick").num(e.tick).key("type").num(static_cast<long long>(e.type));
    w.key("ours").boolean(e.ours).key("data").num(static_cast<long long>(e.data_gid)).key("hex").str(e.hex).end_obj();
  }
  w.end_arr();
  if (clear) spy_.clear();
}

void Engine::serve_snapshot_locked(std::uintptr_t gm) {
  if (!snap_requested_ || !gm) return;
  json::Writer w;
  if (snap_raw_) {
    w.begin_obj();
    write_objects(rdp(gm + off::LogicGameMode_level), w, true);
    w.end_obj();
  } else {
    write_snapshot(gm, w, snap_objects_);
  }
  snap_out_ = std::move(w.out);
  snap_requested_ = false;
  snap_ready_ = true;
  cv_.notify_all();
}

void Engine::service_snapshot() {
  if (!ready_) return;
  const std::uintptr_t gm = current_game_mode();
  std::lock_guard<std::mutex> lk(mu_);
  serve_snapshot_locked(gm);
}

void Engine::configure(const json::Value &cfg, json::Writer &w) {
  std::lock_guard<std::mutex> lk(mu_);
  if (cfg.has("lockstep")) lockstep_ = cfg["lockstep"].boolean();
  if (cfg.has("turbo")) turbo_ = std::max(1, static_cast<int>(cfg["turbo"].i64(1)));
  if (cfg.has("submit_mode")) submit_mode_client_ = cfg["submit_mode"].str() != "direct";
  if (cfg["allowed_ids"].is_arr()) {
    allowed_ids_.clear();
    for (const auto &v : cfg["allowed_ids"].a) allowed_ids_.insert(static_cast<int>(v.i64()));
  }
  if (cfg["object_fields"].is_arr()) {
    object_fields_.clear();
    for (const auto &f : cfg["object_fields"].a) {
      FieldSpec s;
      s.name = f["name"].str();
      for (const auto &p : f["path"].a) s.path.push_back(static_cast<int>(p.i64()));
      if (!s.name.empty() && !s.path.empty()) object_fields_.push_back(s);
    }
  }
  if (cfg.has("reset_counters") && cfg["reset_counters"].boolean()) {
    subticks_done_ = 0;
    battle_over_seen_ = false;
    budget_ = 0;
    results_.clear();
    pending_.clear();
  }
  w.key("lockstep").boolean(lockstep_).key("turbo").num(static_cast<long long>(turbo_));
  w.key("submit_mode").str(submit_mode_client_ ? "client" : "direct");
  w.key("allowed_count").num(static_cast<long long>(allowed_ids_.size()));
  w.key("object_fields").num(static_cast<long long>(object_fields_.size()));
}

void Engine::enqueue(const std::vector<Action> &actions) {
  std::lock_guard<std::mutex> lk(mu_);
  for (const auto &a : actions) pending_.push_back(a);
}

bool Engine::step(int subticks, int timeout_ms) {
  std::unique_lock<std::mutex> lk(mu_);
  const long long target = subticks_done_ + subticks;
  budget_ += subticks;
  const bool ok = cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                               [&] { return subticks_done_ >= target || battle_over_seen_; });
  if (!ok) budget_ = 0;  // 超时：收回预算，避免后台继续跑
  return ok;
}

bool Engine::snapshot(json::Writer &w, int timeout_ms, bool include_objects) {
  std::unique_lock<std::mutex> lk(mu_);
  snap_requested_ = true;
  snap_objects_ = include_objects;
  snap_raw_ = false;
  snap_ready_ = false;
  if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms), [&] { return snap_ready_; })) {
    snap_requested_ = false;
    return false;
  }
  w.raw(snap_out_);
  return true;
}

void Engine::objects_raw(json::Writer &w, int timeout_ms) {
  std::unique_lock<std::mutex> lk(mu_);
  snap_requested_ = true;
  snap_raw_ = true;
  snap_ready_ = false;
  if (cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms), [&] { return snap_ready_; })) {
    w.raw(snap_out_);
  } else {
    snap_requested_ = false;
    w.null();
  }
}

std::vector<ActionResult> Engine::take_results() {
  std::lock_guard<std::mutex> lk(mu_);
  std::vector<ActionResult> out;
  out.swap(results_);
  // 已提交但尚未执行的动作保留在 cmd_to_action_，执行后会产生第二条结果
  return out;
}

void Engine::status(json::Writer &w) {
  const std::uintptr_t gm = current_game_mode();
  std::lock_guard<std::mutex> lk(mu_);
  w.key("ready").boolean(ready_.load());
  w.key("game_mode").num(static_cast<long long>(gm ? 1 : 0));
  w.key("gm_state").num(static_cast<long long>(gm ? rd_or<int>(gm + off::LogicGameMode_state, -1) : -1));
  w.key("in_battle").boolean(gm && in_battle(gm));
  w.key("lockstep").boolean(lockstep_).key("budget").num(budget_).key("subticks_done").num(subticks_done_);
  w.key("battle_over_seen").boolean(battle_over_seen_);
  w.key("pending").num(static_cast<long long>(pending_.size()));
  w.key("game_thread_seen").boolean(g_game_thread_known.load());
}

void Engine::write_army(std::uintptr_t level, json::Writer &w) {
  const std::uintptr_t avatar = rdp(level + off::LogicLevel_visitorAvatar);
  auto write_list = [&](const char *key, std::uintptr_t list, int count_off, int level_off = -1) {
    w.key(key).begin_arr();
    const std::uintptr_t arr = list ? rdp(list + off::LogicArrayList_data) : 0;
    const int n = list ? rd_or<int>(list + off::LogicArrayList_count, 0) : 0;
    for (int i = 0; i < n && i < 256; ++i) {
      const std::uintptr_t slot = rdp(arr + 8 * static_cast<std::uintptr_t>(i));
      const std::uintptr_t data = slot ? rdp(slot) : 0;
      if (!data) continue;
      w.begin_arr()
          .num(static_cast<long long>(rd_or<int>(data + off::LogicData_globalId, -1)))
          .num(static_cast<long long>(rd_or<int>(slot + count_off, 0)));
      if (level_off >= 0) w.num(static_cast<long long>(rd_or<int>(slot + level_off, 0)));
      w.end_arr();
    }
    w.end_arr();
  };
  w.key("army").begin_obj();
  // 进攻方 avatar 的兵力列表按单位类型分组：avatar + unitList + 8*类型（0 兵种 1 法术 2 待确认 3 攻城机器）
  static const char *kNames[4] = {"units", "spells", "type2", "sieges"};
  for (int t = 0; t < 4; ++t)
    write_list(kNames[t], avatar ? rdp(avatar + off::LogicAvatar_unitList + 8 * t) : 0, off::LogicUnitSlot_count);
  // 部落援军每项 {data@0, 等级@8, 数量@0xc}
  write_list("cc", avatar ? rdp(avatar + off::LogicAvatar_allianceUnits) : 0, off::LogicAllianceUnitSlot_count, 8);
  w.end_obj();
}

void Engine::write_objects(std::uintptr_t level, json::Writer &w, bool raw) {
  const int vidx = rd_or<int>(level + off::LogicLevel_villageIndex, 0);
  const std::uintptr_t gom = rdp(level + off::LogicLevel_gameObjectManagers + 8 * static_cast<std::uintptr_t>(vidx));
  w.key("objects").begin_arr();
  for (int t = 0; gom && t < off::LogicGameObjectManager_typeCount; ++t) {
    const std::uintptr_t list = rdp(gom + off::LogicGameObjectManager_typeLists + 8 * t);
    const std::uintptr_t arr = list ? rdp(list + off::LogicArrayList_data) : 0;
    const int n = list ? rd_or<int>(list + off::LogicArrayList_count, 0) : 0;
    for (int i = 0; i < n && i < 4096; ++i) {
      const std::uintptr_t obj = rdp(arr + 8 * static_cast<std::uintptr_t>(i));
      if (!obj) continue;
      const std::uintptr_t data = rdp(obj + off::LogicGameObject_data);
      w.begin_obj();
      w.key("t").num(static_cast<long long>(t));
      w.key("gid").num(static_cast<long long>(rd_or<int>(obj + off::LogicGameObject_globalId, -1)));
      w.key("data").num(static_cast<long long>(data ? rd_or<int>(data + off::LogicData_globalId, -1) : -1));
      if (raw) {
        w.key("ptr").num(static_cast<long long>(obj));
        w.key("vt").num(static_cast<long long>(rdp(obj) - base_));
      }
      for (const auto &f : object_fields_) {
        std::uintptr_t p = obj;
        bool ok = true;
        for (std::size_t k = 0; k + 1 < f.path.size() && ok; ++k) {
          p = rdp(p + f.path[k]);
          ok = p != 0;
        }
        int v = 0;
        if (ok && rd(p + f.path.back(), v)) w.key(f.name.c_str()).num(static_cast<long long>(v));
        else w.key(f.name.c_str()).null();
      }
      w.end_obj();
    }
  }
  w.end_arr();
}

void Engine::write_snapshot(std::uintptr_t gm, json::Writer &w, bool include_objects) {
  const std::uintptr_t level = rdp(gm + off::LogicGameMode_level);
  w.begin_obj();
  w.key("gm_state").num(static_cast<long long>(rd_or<int>(gm + off::LogicGameMode_state, -1)));
  w.key("in_battle").boolean(in_battle(gm));
  w.key("battle_over").boolean(rd_or<std::uint8_t>(gm + off::LogicGameMode_battleOver, 0) != 0);
  if (level) {
    w.key("tick").num(static_cast<long long>(rd_or<int>(level + off::LogicLevel_currentTick, -1)));
    w.key("level_state").num(static_cast<long long>(rd_or<int>(rdp(level + off::LogicLevel_statePtr), -1)));
    w.key("width").num(static_cast<long long>(rd_or<int>(level + off::LogicLevel_width, 0)));
    w.key("height").num(static_cast<long long>(rd_or<int>(level + off::LogicLevel_height, 0)));
    w.key("battle_end_flag").boolean(rd_or<std::uint8_t>(level + off::LogicLevel_battleEndFlag, 0) != 0);
    const int total = rd_or<int>(level + off::LogicLevel_destructibleTotal, 0);
    const int remaining = rd_or<int>(level + off::LogicLevel_destructibleRemaining, 0);
    w.key("destructible_total").num(static_cast<long long>(total));
    w.key("destructible_remaining").num(static_cast<long long>(remaining));
    const std::uintptr_t blog = rdp(level + off::LogicLevel_battleLog);
    w.key("destruction").num(static_cast<long long>(blog ? rd_or<int>(blog + off::LogicBattleLog_destructionPercent, 0) : 0));
    if (blog) {
      // BattleLog::getStars：(摧毁率==100 ? 2 : 摧毁率>49) + 大本营已摧毁
      w.key("stars").num(static_cast<long long>(
          reinterpret_cast<GetStarsFn>(base_ + off::LogicBattleLog_getStars)(reinterpret_cast<void *>(blog))));
      w.key("th_destroyed").boolean(rd_or<std::uint8_t>(blog + off::LogicBattleLog_townHallDestroyed, 0) != 0);
    }
    write_army(level, w);
    if (include_objects) write_objects(level, w, false);
  }
  w.end_obj();
}

}  // namespace tz
