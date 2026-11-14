# 架构与逆向记录

版本：Atrasis `com.atrasis.original` 18.647（build.tag `18_647_2`），`lib/arm64-v8a/libg.so`
SHA-256 `2b9d4c91…5d1c`。libg.so 没有加壳，section 表被剥离；Atrasis 额外追加了两个 PT_LOAD 段，
私服逻辑在 `libatrasis.so`。

## 1. 静态分析方法（tools/re）

不依赖 IDA/Ghidra：

- `elfre.py`：用 `.eh_frame_hdr` 搜索表恢复 62k 个函数入口；用 numpy 向量化扫描 `ADRP + ADD/LDR` 建立
  "数据地址 → 引用指令"索引，扫描 `BL/B` 建立调用图；解析 RELA 重定位以读取虚表等指针。
- `cmdtable.py`：具体执行 `LogicCommandManager::createCommand` 的比较树和跳转表，得到每个命令 ID 的构造函数和虚表。
- `find_offsets.py`：把下表每个结论写成"字符串锚点 + 代码特征"的推导，生成 `probe/generated/offsets.h`
  与 `tianze_coc/data/engine_offsets.json`。任一步失败直接报错；`--expect` 与本版本手工结果做回归比对。

## 2. 关键结论与证据

| 符号 | 地址 | 证据 |
| --- | --- | --- |
| `LogicCommandManager::createCommand` | 0xd6ff00 | 引用 "Unknown command type: %d"；比较树 + 跳转表 |
| `operator new` | 0x16b2320 | createCommand 中出现最多的 BL 目标 |
| `DebugUtil::deployAllTroops` | 0x7bf4f0 | 引用 "DebugUtil::deployAllTroops: total housing deployed %d"；游戏自带的调试部署函数，是注入路径的模板 |
| 放兵命令 构造/虚表 | 0xd7d46c / 0x1737650 | 字符串 "Replay LogicPlaceAttackerCommand load failed!" → load 函数 → 所在虚表 → 写虚表的构造函数；虚表槽 4 `getCommandType` 返回 **7885** |
| 法术命令 构造/虚表 | 0xd667f8 / 0x17353c8 | 同上，类型 **7739** |
| 英雄命令 构造/虚表 | 0xd7e240 / 0x17376b8 | 同上，类型 **7641** |
| 客户端提交命令 | 0xb7b904 | deployAllTroops 中构造之后调用；内部断言 "Not in the main thread"，最终调用 addCommand |
| `LogicCommandManager::addCommand` | 0xd71aa4 | 提交函数的最后一个调用；把命令追加到 `cm+0` 数组 |
| `LogicCommandManager::update` | 0xd71c8c | 引用 "Execute command failed! Command should have been executed already" |
| `LogicGameMode::updateSubTick` | 0xf92a5c | update ← updateOneSubTick(0xf92b4c) ← 本函数；**hook 点**（前 16 字节无 PC 相对指令） |
| `LogicGameMode::load*State` | 0xf94160 等 | 各自的 "called from invalid state" 字符串 |
| `LogicBattleLog::setDestruction` | 0xcf3ed0 | 引用 "m_destructionPercentage decreases"；摧毁率在 BattleLog+0x1d0 |
| 数据表 `getTable(表号)` | 0xdf4cdc | deployAllTroops 中 `mov w0,#0x1b; bl`；表号 = GlobalID/1e6 − 1 |

注意：本版本命令 ID 被重新编号，**不是**旧版公开资料里的 600 段。

### 命令对象布局（来自构造函数）

| 命令 | 大小 | 构造参数 → 字段 |
| --- | --- | --- |
| 放兵 | 0x28 | `(this, data, x, y, flag, extra)` → data@0x10, x@0x18, y@0x1c, flag@0x20, extra@0x24；+8 执行 tick（初始 −1） |
| 法术 | 0x30 | `(this, data, flag, extra, x, y, extra2)` → x@0x18, y@0x1c, flag@0x20, extra@0x24, extra2@0x28 |
| 英雄 | 0x20 | `(this, data, x, y)` → x@0x18, y@0x1c |

坐标单位：逻辑坐标 = 格子 × 512（deployAllTroops 中 `lsl #9`，tile 查询中 `asr #9`）。
`flag/extra` 在调试部署中都传 0；游戏 UI 部署时传入的值推测与部落援军/变体有关，**尚未支持援军部署**。

### 对象链

```
[libg+0x1838258]  客户端实例
  +0x160 → LogicGameMode     (+0 状态: 2=进攻 4=访问/特殊; +0xc 战斗结束; +0x10 Level; +0x18 CommandManager)
LogicLevel  +0x18/+0x1c 宽/高(逻辑单位)  +0x40 BattleLog  +0x50 进攻方 avatar  +0x58 状态指针
            +0x60 当前 tick  +0x220/+0x224 可摧毁建筑总数/剩余  +0x2fc 结束标志
            +0x68[村庄序号@+0x420] GameObjectManager   [+0x42c 序号] TileMap
GameObjectManager  +0x00..+0x50 按类型分组的 11 个数组(+0 数据 +0xc 数量)  +0x90 Level
GameObject  +0x18 CSV 数据  +0x30 全局 ID；虚表槽 20 getGameObjectType
Avatar      +0x40 兵种列表  +0x48 法术列表；每项 {data*, count@+8}
```

## 3. 探针（probe/src）

- **加载**：`libg.so` 的 `DT_NEEDED "libsentry-android.so"` 被原地改为 `"libtianze_probe.so"`，探针自身 `DT_NEEDED`
  原库。只改 17 个字节，不移动任何 ELF 结构。
- **安全措施**：只在 `com.atrasis.*` 主进程中启动；启动时比对 37 个函数入口指纹，任何不符就不安装 hook；
  推测性内存读取全部走 `process_vm_readv`（非法地址返回失败而不是崩溃）；只监听 127.0.0.1。
- **锁步**：hook `updateSubTick`。锁步开启且处于进攻时，没有预算就直接返回（冻结逻辑，画面照常），
  有预算则每帧最多推进 `turbo` 个子帧。
- **注入**：在游戏主线程、原函数调用前，按调试部署的方式 `new` + 构造命令，经客户端提交函数（`submit_mode=client`，
  默认，与游戏 UI 路径一致）或直接 `addCommand`（`direct`，不经网络层）。
- **执行结果**：为每种命令复制一份虚表，把槽 3（execute）换成包装函数，记录引擎真实返回码。
- **双重禁用**：探针只接受环境下发的 `allowed_ids`（部落战/排位战可携带的单位），未配置时拒绝一切部署。

### 协议（JSON 行，127.0.0.1:27650）

| op | 作用 |
| --- | --- |
| `hello` | 版本、libg 基址、就绪状态、指纹不符列表 |
| `status` | 游戏模式状态、是否在战斗、锁步预算 |
| `config` | `lockstep` `turbo` `submit_mode` `allowed_ids` `object_fields` `reset_counters` |
| `step` | 入队动作 → 推进 N 子帧 → 返回动作结果与观测（原子） |
| `observe` | 在下一个子帧边界生成快照 |
| `objects_raw` / `peek` | 校准用：对象指针/虚表、读取任意地址 |

## 3.5 全部战斗操作（已实现，均由 find_offsets.py 自动定位）

| 动作 kind | 命令 | 类型 ID | 说明 |
| --- | --- | --- | --- |
| `troop` / `siege` | PlaceAttacker | 7885 | 攻城机器从 avatar 的类型 3 列表扣除 |
| `spell` | CastSpell | 7739 | |
| `hero` | PlaceHero | 7641 | |
| `cc` | PlaceAlliancePortal | 7853 | 援军传送门数据由探针按 `getDataType()==15` 在数据表中查找；落点放出全部部落援军 |
| `cc_siege` | PlaceAttacker，flag=1，extra=城堡中第几台 | 7885 | 部落城堡里的攻城机器 |
| `ability` | TriggerHeroAbility | 7060 | 参数只有单位数据；对场上第一个存活的该单位触发技能。英雄技能、兵种技能、攻城机器释放共用 |
| `recalled` | PlaceRecalledUnits | 7654 | 回溯法术收回后的重新部署；flag/extra 原样透传，具体含义待上机确认 |

观测新增：`stars`（直接调用 `BattleLog::getStars`）、`th_destroyed`（BattleLog+0x1b9）、
`army.sieges`（avatar 类型 3 列表）、`army.cc`（avatar+0x1d0 部落援军）、`army.type2`（类型 2 列表，含义待确认）。

## 3.6 离线开局（已实现）

| 环节 | 实现 |
| --- | --- |
| 消息 ID | 本版本重新编号：EnemyHomeData 21940、NpcData 24346、OwnHomeData 25195、VisitedHomeData 25863（由 receiveMessage 的比较树 + 可达性分析推导） |
| 录制 | 替换这 4 种消息虚表的 decode 槽（槽 3），解码前复制字节流（消息+8）里 `+0x38` 缓冲区的前 `+0x18` 字节 |
| 构造 | 消息工厂 `createMessageByType(this, type)`（0xf3595c，不使用 this） |
| 填充 | ByteStream `+0x30` 所有权缓冲区、`+0x38` 缓冲区、`+0x18` 长度、`+0x14` 读位置、`+0x1c` 位索引 |
| 分发 | `deliver([g_messageManager], msg, 0)`（0xba41d8）；libg 自带的投递辅助函数和 libatrasis 都这样调用 |
| 时机 | hook `ClientInstance::update(client, dt)`（0xb82034）：每帧、主线程、在 GameMode 更新之前，重放不会打断正在运行的模拟 |
| 断网 | hook 网络线程的 `Messaging::sendOne`（0x11ec954），记录每条外发消息类型；离线模式下按 `block_all/allow_types/block_types` 丢弃（发送循环随后照常删除消息） |
| 换阵型 | `home_payload.py` 在负载里定位 zlib 压缩的村庄 JSON（校验长度前缀），替换后修正前缀 |

**关于 Atrasis**：libg 中约 1300 处调用被 Atrasis 改成经运行时函数表（.bss 中由 libatrasis 填写）跳转，
receiveMessage 等函数因此没有静态调用者；libatrasis 内部保存了 libg 基址 + 偏移表，会直接调用 receiveMessage。
我们的重放与它的做法一致。

用法：

```python
from pathlib import Path
from tianze_coc.battle_env import TianzeBattleEnv
from tianze_coc.native_client import ProbeClient
from tianze_coc.offline_session import OfflineStarter, save_records

# 1) 正常打一局后保存录制（主村 + 敌方村庄）
save_records(ProbeClient().connect(), "acct1")
# 2) 之后用重放开局，可选每局换阵型
starter = OfflineStarter(Path("tianze_coc/data/records/acct1.OwnHomeData.bin"),
                         Path("tianze_coc/data/records/acct1.EnemyHomeData.bin"), layouts=[...])
env = TianzeBattleEnv(battle_starter=starter)
```

## 3.65 第三轮（18 本满级账号，友谊战）

| 动作 kind | 命令 | 类型 | 真机结果 |
| --- | --- | --- | --- |
| `troop`/`hero`/`spell`/`siege` | 放兵/英雄/法术 | 7885/7641/7739 | ✅ 英雄出场时战宠一起上（战宠 GlobalID = 73000000 + 序号） |
| `ability`（英雄） | TriggerHeroAbility | 7060 | ✅ 野蛮人之王、飞龙公爵 |
| `cc` | PlaceAlliancePortal | 7853 | ✅ 援军兵种全部出场（传送门对象为 16000000） |
| `cc_siege` | 放兵 flag=1 extra=城堡中第几台 | 7885 | ✅ 与 UI 发出的命令字节一致 |
| `cc_spell` | 法术 flag=1 extra=援军法术等级 | 7739 | ✅ 援军每项 {data@0, 等级@8, 数量@0xc}，按 数据+等级 匹配；最后一个参数与游戏一致传 -1 |
| `hero_mode` | 英雄模式切换 (英雄数据@0x10, 模式@0x18) | 7398 | ✅ 大守护者切到空中模式后部署，画面中在空中；部署后不能再切（环境拒绝） |
| `recalled` | PlaceRecalledUnits (flag=0, 槽位, x, y) | 7654 | ✅ 与 UI 发出的命令一致 |

规则（环境层强制，与游戏一致）：
- 可携带多台攻城机器，但**一局只能部署一台**（自己的或援军的）；引擎层同样会返回 -7。
- 开战后可切换的只有：要部署哪台攻城机器（即部署时选哪个 GlobalID）、大守护者地面/空中模式（部署前）。其余配兵固定。
- 攻城机器没有手动提前释放：对攻城机器发技能命令返回 -5，UI 操作中也没有对应命令。

离线重放 EnemyHomeData（友谊战/匹配）需要客户端状态 ∈ {2,10,11}（`g_clientState`，掩码 0xc04）；
探针在重放前若状态不在集合内会设为 2。NpcData 没有这个检查。

**命令侦听**（`cmd_log`）：hook 客户端命令提交函数，记录每条命令（含游戏 UI 发出的）的类型与原始字节。
新机制只要在游戏里手动操作一次，就能拿到命令格式。英雄模式切换命令就是这样找到的。

## 3.66 换阵型（真机验证）

- EnemyHomeData 里只有一份村庄 JSON（偏移 37），即防守方的完整村庄数据（建筑/陷阱/装饰/障碍物 + 名字、大本营等级等）。
- 用 `home_payload.extract_home / shift_layout / replace_home_json` 把建筑整体左移 14 格并清空障碍物后离线重放：
  25 个建筑在游戏里的坐标与新 JSON **全部吻合**，障碍物为 0，进攻方军队/援军仍是原录制的满编。
- 因此训练时可以用一条录制（固定军队）配任意阵型库轮换（`OfflineStarter(layouts=[...])`）。
  阵型可以来自任何录制：自己的主村、友谊战、匹配到的对手。

## 3.7 真机联调结论（MuMu 12 / Android 12 / ARM64 转译，实例 1）

| 项目 | 结果 |
| --- | --- |
| 加载 | 54 个入口指纹全部匹配（Atrasis 没有在运行时改动我们 hook 的函数）；3 个 hook 安装成功；客户端稳定 |
| 消息 ID | OwnHomeData 25195、NpcData 24346 实际录到，与推导一致；村庄 JSON 压缩格式（大端总长 + 小端原长 + zlib）验证正确 |
| 帧 hook | 最初选的 0xb82034 只在部分状态下调用；改为 ClientInstance 虚表中的 update（0xb7fd60），实测约 60 次/秒 |
| 投递方式 | `deliver`（0xba41d8）与 Atrasis 改道桩都不会触发模式切换；**直接调用 `receiveMessage(mm, msg)` 成功**，已设为默认（deliver_mode=1） |
| 离线开局 | 重放 OwnHomeData 回主村、重放 NpcData 开战：约 1 秒/局；模拟确定性（同开局同操作同结束 tick） |
| 断网 | block_all + allow_types=[10108]（保活）可稳定连续对局；14393 每局一次（战斗结果上报），16203/19033 为周期同步，均被拦截 |
| 部署 | 引擎每个子帧只执行一个新命令，同子帧的其余命令被静默丢弃 → 探针改为每子帧注入一个 |
| 观测字段 | x/y = 对象+0x20/+0x24（逻辑单位，建筑与移动单位通用）；hp = [对象+0x48]+0x20、hp_max = +0x18、team = +0x14（1 防守 0 进攻）；数值为血量×100 |
| 速度 | turbo=16 时约 2.5~3 秒打完一局新手关卡（含开局） |
| 训练循环 | `python -m tianze_coc.training.loop` 在真实游戏上连续跑 20 局，每局记录奖励并更新策略 |

尚未在真机上验证：法术、英雄、部落援军、援军攻城机器、技能、回溯（新手号还没有这些单位）；
多人对战的 EnemyHomeData 录制与换阵型（需要先有军队并搜索一次对手）。

## 4. 后续工作（19 本版本）

本仓库对应 18 本最后一个版本（18.647），本版本不再训练模型。19 本版本发布后：

1. 用 `tools/re/find_offsets.py` 对新 libg.so 重新定位偏移；失败的条目按 docs 中的证据链人工补齐。
   英雄模式切换命令的类型 ID 需用命令侦听（`cmd_log`）重新抓取。
2. 用 `python -m tianze_coc.assets` 和 `defense_catalog` 重新生成单位规则表并人工审核。
3. 用 `tools/calibrate.py` 复核对象字段，用 `tools/live_action_test.py` 复测全部动作。
4. 录制新版本的主村/对手数据，建立阵型库，接入 GPU 上的模型训练（参照 FirstLight CR 的 IL → PPO 路线）。

已知未完成：回溯后可重新部署的单位池尚未进入观测；avatar 类型 2 兵力列表含义未确认；
重放后的消息对象未释放（少量内存泄漏）。
