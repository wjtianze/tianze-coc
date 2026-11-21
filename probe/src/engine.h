// 引擎访问层：所有对游戏内存/函数的操作都集中在这里。
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "json.h"

namespace tz {

// 动作种类。Troop/Spell/Hero/Portal/Ability/Recalled 各对应一种命令类；CcSiege 复用放兵命令（flag=1）。
enum class ActionKind { Troop, Spell, Hero, Portal, Ability, Recalled, CcSiege, CcSpell, HeroMode };
constexpr int kCmdClasses = 7;  // 7 种命令类各有一份命令虚表副本

struct Action {
  long long id = 0;
  ActionKind kind = ActionKind::Troop;
  int data_global_id = 0;  // Portal 不需要（自动取援军传送门数据）
  int x = 0;               // 逻辑坐标（格子 << 9）
  int y = 0;
  int flag = 0;   // Recalled：回溯单位标志字节；HeroMode：模式（0 地面 1 空中）
  int extra = 0;  // CcSiege：城堡中第几台攻城机器；CcSpell：援军法术等级；Recalled：回溯槽位参数
};

struct ActionResult {
  long long id = 0;
  std::string status;  // queued / submitted / executed / rejected
  int code = 0;        // 引擎 execute() 返回值（0 = 成功）
  std::string reason;
  long long tick = -1;
};

// 观测字段布局：由 Python 侧下发（校准后写入 data/layout.json），
// path 为指针链：base=obj; 对除最后一个外的偏移解引用；最后一个偏移读 i32。
struct FieldSpec {
  std::string name;
  std::vector<int> path;
};

class Engine {
 public:
  static Engine &get();

  bool init();  // 定位 libg、校验指纹、安装 hook
  bool ready() const { return ready_.load(); }
  std::uintptr_t base() const { return base_; }
  const std::string &init_error() const { return init_error_; }
  std::vector<std::string> fingerprint_mismatches() const { return mismatches_; }

  // ---- 由服务线程调用（线程安全）
  void configure(const json::Value &cfg, json::Writer &w);
  void enqueue(const std::vector<Action> &actions);
  // 锁步：增加 n 个子帧的预算并等待其消耗完（或战斗结束/超时）
  bool step(int subticks, int timeout_ms);
  // 在下一次子帧边界（游戏线程）生成快照
  bool snapshot(json::Writer &w, int timeout_ms, bool include_objects);
  std::vector<ActionResult> take_results();
  void status(json::Writer &w);
  void objects_raw(json::Writer &w, int timeout_ms);

  // ---- 由 hook 在游戏线程调用
  bool on_update_sub_tick(void *game_mode);
  void service_snapshot();  // 每帧（ClientInstance::update）调用

  // 命令虚表包装：记录 execute 返回值
  static int exec_wrapper(void *cmd, void *level);
  // 命令侦听：记录经过客户端提交函数的每条命令（含游戏 UI 发出的），用于摸清新机制的命令格式
  void on_submit(void *cmd);
  void cmd_log(json::Writer &w, bool clear);

  // ---- 通用命令通道（主村/战斗均可）：按 命令类型 + encode 字节 构造任意命令并走客户端提交路径。
  // 字节格式即命令 encode() 的输出（首个 int 为执行 tick，提交时由命令管理器重设），
  // 与 cmd_log 抓到的 "enc" 字段完全一致，因此可以"抓一次 UI 操作 -> 改字段 -> 重放"。
  // vtable/size：命令类虚表（相对 libg）与对象大小，来自 tools/re/cmd_vtables.py 的静态扫描；
  // 客户端 UI 命令不在 createCommand 工厂里，需要按虚表直接构造。vtable=0 时只用工厂。
  long long enqueue_raw(int type, std::vector<std::uint8_t> bytes, std::uintptr_t vtable = 0, int size = 0);
  void service_raw();  // 每帧（ClientInstance::update，主线程）调用：每帧最多提交一条
  void raw_results(json::Writer &w, bool clear);
  static int generic_exec_wrapper(void *cmd, void *level);
  // 大本营升级强化（军队满级 / 英雄满级）：读取或设置战斗中两个 avatar 槽上的标志
  void attacker_boost(const json::Value &req, json::Writer &w);

 private:
  Engine() = default;
  bool resolve_base();
  bool verify_fingerprints();

  // 游戏线程内部
  std::uintptr_t current_game_mode() const;
  bool in_battle(std::uintptr_t gm) const;
  void inject_pending(std::uintptr_t gm);
  bool submit_one(std::uintptr_t gm, const Action &a, ActionResult &res);
  std::uintptr_t data_by_global_id(int gid) const;
  void write_snapshot(std::uintptr_t gm, json::Writer &w, bool include_objects);
  void write_army(std::uintptr_t level, json::Writer &w);
  std::uintptr_t portal_data();
  void write_objects(std::uintptr_t level, json::Writer &w, bool raw);
  void *make_cmd_vtable(ActionKind kind);
  void serve_snapshot_locked(std::uintptr_t gm);
  std::uintptr_t scratch_stream();                             // 主线程：取一个可复用的 ByteStream
  bool encode_cmd(void *cmd, std::vector<std::uint8_t> &out);  // 主线程
  void *generic_vtable(void *cmd, int type);                   // 为某类型命令复制虚表并包装 execute

  std::atomic<bool> ready_{false};
  std::uintptr_t base_ = 0;
  std::string init_error_;
  std::vector<std::string> mismatches_;

  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<Action> pending_;
  std::vector<ActionResult> results_;
  std::unordered_map<std::uintptr_t, long long> cmd_to_action_;  // 命令对象 -> 动作 id

  // 锁步状态
  bool lockstep_ = false;
  bool submit_mode_client_ = true;  // true: 走客户端提交路径（同 DebugUtil::deployAllTroops）；false: 直接 addCommand
  int turbo_ = 1;
  long long budget_ = 0;
  long long subticks_done_ = 0;
  bool battle_over_seen_ = false;

  // 快照请求（服务线程 -> 游戏线程）
  bool snap_requested_ = false;
  bool snap_objects_ = false;
  bool snap_raw_ = false;
  bool snap_ready_ = false;
  std::string snap_out_;

  // 允许部署的 GlobalID（部落战/排位战可携带的角色）；为空表示尚未配置，此时拒绝一切部署
  std::set<int> allowed_ids_;
  std::vector<FieldSpec> object_fields_;

  void *vt_copy_[kCmdClasses] = {};
  std::uintptr_t orig_exec_[kCmdClasses] = {};
  std::uintptr_t portal_data_ = 0;
  struct SpyEntry {
    long long tick;
    int type;
    bool ours;
    int data_gid;
    std::string hex;
    std::string enc;  // 命令 encode() 的完整字节
  };
  std::deque<SpyEntry> spy_;
  std::mutex spy_mu_;

  struct RawCmd {
    long long id = 0;
    int type = 0;
    std::vector<std::uint8_t> bytes;
    std::uintptr_t vtable = 0;
    int size = 0;
  };
  struct RawResult {
    long long id;
    int type;
    std::string status;  // submitted / executed / rejected:原因
    int code;            // executed/rejected：引擎 execute 返回值；submitted：decode 消耗的字节数
    long long tick;
  };
  std::deque<RawCmd> raw_pending_;      // 受 mu_ 保护
  std::vector<RawResult> raw_results_;  // 受 mu_ 保护
  std::unordered_map<std::uintptr_t, long long> raw_cmd_ids_;  // 命令对象 -> 请求 id（受 mu_ 保护）
  long long raw_seq_ = 0;
  struct GenericVt {
    int type;
    std::uintptr_t *copy;  // 指向虚表第 0 槽
    std::uintptr_t orig_exec;
  };
  std::vector<GenericVt> generic_vts_;  // 只在主线程读写
  std::uintptr_t scratch_msg_ = 0;  // 独立的锁：on_submit 会在注入命令时（已持有 mu_）被调用，不能再申请 mu_
};

}  // namespace tz
