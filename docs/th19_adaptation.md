# 19 本（新版本）适配指南

私服已推出 19 本。本文是把 tianze-coc 从 18.647 迁移到新版本的**执行清单**，写给接手的会话：先通读，再按顺序做。
原则：**迁移优先，不在 18 本上继续加功能**；每一步做完就验证，失败就停下来修，不要带着错误往下走。

> 本机环境（工具链、模拟器、adb 路径、`.env`）和禁区的完整说明在工作区的《版本更新说明.md》（仓库上一级目录，不进仓库），
> 动手前一并读完。本文侧重"迁移要改什么、怎么验证"，并补充了 18.647 后期新增、那份说明里没写全的部分。

## 0. 硬性约束（每一步都适用）

- 模拟器**只用实例 1**；不要启动或操作实例 0，不要碰官方客户端（只在 `com.atrasis.*` 里工作）。
- 不向 Supercell 官方服务器发任何请求；正式服 APK 不做接口。
- 录制数据（`tianze_coc/data/records/`，含登录包 `*.login/`）含账号信息，**绝不能提交**；`.env`、`build/`、APK 也不进仓库。
- 接受服务条款、在私服创建账号由用户本人操作或明确同意；下载/安装新依赖先问。
- git 身份只在仓库内设置（wjtianze / 298313773+wjtianze@users.noreply.github.com）；**推送前征得用户同意**。
- 新建分支 `th19` 做迁移，测试通过后再合并回 main。
- 找数据靠读代码和数据表，不要靠在游戏界面里点来点去"试"出来；界面只用于让用户最后核对结果。

## 1. 准备

1. 用户提供新 APK 后，`.env` 的 `TZ_ORIGINAL_APK` 指向它；把 `lib/arm64-v8a/`、`assets/logic/`、`assets/localization/`、
   `assets/build.tag` 解到临时目录（命令见《版本更新说明》1.1）。记下 build.tag。
2. 粗看新版本变化，作为后面各步的检查清单：
   - `townhall_levels.csv` 多出的一行（19 本）及各建筑的 `TownHallLevel` 列；
   - `buildings.csv` / `traps.csv` 新建筑、`MergeRequirement`（新合并建筑）、大本营武器/守卫（18 本是守卫三选一）；
   - `characters.csv` / `spells.csv` / `heroes.csv` / `pets.csv` / `items.csv` 新单位与等级上限；
   - `super_licences.csv`、`battle_modifiers.csv`（难度模式）、`globals.csv` 有无新规则。

## 2. 重新定位偏移（最关键，先做）

```bash
python -I tools/re/find_offsets.py <新libg.so> --build-tag <新build.tag>
```

自动重写 `probe/generated/offsets.h`、`tianze_coc/data/engine_offsets.json`。不要加 `--expect`。
推导失败就按 `docs/architecture.md` 每个符号的"证据"栏用 `tools/re/elfre.py` 手工找，再修推导逻辑。

18.647 后期新增、需要一并推出的符号（都在 `find_offsets.py` 里，有对照值可参考）：

| 符号 | 用途 | 18.647 值 / 说明 | 19.32.7 值 |
| --- | --- | --- | --- |
| `MessageManager_receiveMessage` | 收包侦听、录制全部消息、离线登录投递 | 重放必须直接调它（`deliver_mode=1`） | `0xc6682c`（同一日志字符串还出现在 3 个小函数里，取同时引用四类消息日志的那个） |
| `LogicDataTables_getTable` | `trace` 操作：记录某个界面访问了哪些数据表及调用位置 | 找"规则写在代码里"的机制用 | 推导通过 |
| `g_loginState`、`LoginState_awaitingValue`、`validatedFlag` | 离线启动时伪造"已发登录、握手已验证" | `0x18322c0`；字段 0 = 2，`+0x4d` = 1 | 检查不再在函数开头，改为在整个函数里找；值 2 / `+0x4d` 不变 |
| `LogicCommand_vt_encode/decode`、`operator_delete` | 通用命令通道 | | 槽位 5/6 不变 |
| 英雄模式切换命令类型 | 常量 `SPY_TYPES_HERO_MODE` | 7398，**必须用命令侦听重新抓** | 7216（指令序列比对得出，命令侦听已确认） |

命令 ID、消息 ID 每个版本都重新编号，不要沿用。

## 3. 重新生成规则表并让用户审核

```bash
python -m tianze_coc.assets <新APK> --markdown docs/units.md
python -m tianze_coc.assets.defense_catalog <新APK> --markdown docs/defenses.md
python -m tianze_coc.assets.progression <新APK>
python -I tools/re/cmd_vtables.py <新libg.so>
```

- 单位/防御清单**生成后发给用户逐项审核**（用户要求的流程；上一版审核规则见《版本更新说明》1.3）。
- `progression.py` 要检查的 19 本相关点：
  - 新大本营等级的数量上限、等级上限；`require_all_built`（升本须建齐）对新建筑是否适用；
  - 19 本大本营武器/守卫怎么处理（18 本：大本营改为守卫三选一，`Townhall18` 标为 excluded），新守卫/新合并建筑的原料与占用；
  - 1~2 本城堡是废墟、1 级兵种不需要实验室等低本规则是否仍成立；
  - `rules["max_super_troop_kinds"]`（配兵最多 2 种超级兵）是否变化。
- `command_types.json` 依赖第 2 步的 `engine_offsets.json`，顺序不能反。

## 4. 编译、打包、安装、联网录制

```bash
python tools/build_probe.py
python -m tianze_coc.offline_build
python -m tianze_coc.offline_install
```

1. 看 `logcat -s tianze`：hook 全部安装、`engine ready`，`status.frames` 每秒约 60。
2. 联网登录一次，录下新版本的 OwnHomeData；请用户打一场友谊战录 EnemyHomeData（普通一场、选"电竞"一场）。
   录制文件按新版本命名（例如 `th19.OwnHomeData.bin`、`th19_friendly.EnemyHomeData.bin`），
   `tests/test_model_interfaces.py` 顶部的 `OWN` / `ENEMY` 改成新文件。
3. **离线登录包**重新抓：

```bash
python -m tianze_coc.offline_boot capture th19
python -m tianze_coc.offline_boot boot th19
```

   - 登录消息顺序、保活请求/回复 ID 都可能变（18.647：客户端发 10100、10101；服务器依次 23654、25195、20007、22000、20553、23822、
     25418、29808、22287、27845、22767、29023、23825、27349；保活 10108 → 20108）。`capture` 会把新顺序写进 `meta.json`。
   - 验收：断开游戏服务器的情况下约 10 秒内进主村，可以连续反复启动。

## 5. 逐项重新验证接口（按顺序，每项都有现成脚本）

| 项 | 怎么做 | 18.647 的关键事实（可能变） |
| --- | --- | --- |
| 对象字段 | `tools/calibrate.py` 前后两次快照推断 | x/y = +0x20/+0x24，hp = [+0x48]+0x20 |
| 战斗动作 | `tools/live_action_test.py --plan cc_siege --launch`、`--plan modes_recall` | 方案里的录制名和 GlobalID 换新 |
| 离线开局/换阵型 | `OfflineStarter` 连开几局；`home_payload.shift_layout` | 约 1 秒开局、结果确定 |
| 主村控制命令 | 后台跑 `tools/cmd_recorder.py`，请用户按《版本更新说明》1.5 第 8 条顺序手动操作，对照字节更新 `home_control.py` 的 `CMD_*`、编码器、`CAPTURED` | EndClientTurn 19033；同批有一条非法则整批丢弃 |
| 防守方援军注入 | `tools/cc_check.py` | 引擎读防守方 avatar 二进制里的援军列表（`int32 n + n×{int32 gid, vint 数量, vint 等级-1}`，紧跟英雄状态列表之后，`account.defender_cc_span` 定位），必须在替换村庄 JSON 之前改写 |
| 难度模式 | `tools/modifier_check.py` | EnemyHomeData 末尾 4 字节 = 属性调整 GlobalID：普通 0、电竞 92000000、传奇杯 1/2/3 = 92000001/2/3 |
| 自对弈注入 | `tools/selfplay_check.py --home ... --template ...` | 引擎中兵种/法术/攻城/防守建筑与注入一致，公平视角陷阱数 0 |
| 各大本营开局 | `tools/th_sweep.py` | 1~19 本随机账号都能离线开局不崩溃 |
| 天鹰火炮 | `tools/ammo_check.py --eagle-only` | 不需要弹药字段；苏醒门槛 WakeUpSpace = 200 人口 |

## 6. 部落城堡援军等级上限（代码里算的，必须重新核对）

规则不在数据表里，在 libg 代码中（详细字段见 `docs/models.md` 附录）：

- 18.647：`0xded060` 能否请求、`0xded150` 最高等级，参数都是"城堡等级 − 1"；
  兵种/法术"所需实验室 ≤ 城堡 + 4"（含 1 级），攻城机器"解锁它的工坊等级所需大本营 T ≤ 城堡 + 6，等级 = 城堡 − T + 8"，
  超级兵用原兵种实验室数组（偏移 VisualLevel−1），门槛"原兵种上限 ≥ MinOriginalLevel + 1"；请求界面另需人口放得下。
- 新版本定位：在 libg 里找 `sub w8, w8, #4; cmp w8, w19` 形状的函数（18.647 全库只有十几个），
  或调用超级兵许可查找函数（加载 `MinOriginalLevel` 字符串的那个 createReferences 旁边、按 Replacement 查表的函数）的函数；
  也可以打开请求增援界面时用 `trace` 看访问了哪些表。核对 +4 / +6 / +8 三个常数和超级兵分支。
- 改了就同步 `progression.cc_level_cap` 和 `ClanCastleLevelCapTest`，再用 `tools/cc_level_boot.py th19 <城堡等级>`
  以指定城堡等级离线启动，请用户在"部落聊天 → 请求增援 → 编辑"界面核对一两个等级（重点：攻城机器和超级兵列表）。

## 7. 让测试适配 19 本

`python -m unittest discover -s tests -t .` 全部通过才算完成。写死 18.647 数值的地方要改：

- `tests/test_catalogs.py`：APK 路径、单位数量、守卫/大本营武器规则；
- `tests/test_model_interfaces.py`：`blank(18)`、18 本容量（352 / 11 / 3 / 4）、火焰喷射器 2 座、城墙 325、英雄上限等，
  能改成按 `th_max` 计算的就改成通用写法，并补 19 本的对应值；援军上限测试里的 6、7 级城堡实测值要在新版本重新核对。

## 8. 收尾

1. 更新 `README.md` 的游戏版本与状态表、`docs/models.md` 中标注 18.647 的数值、本文的"18.647 值"列补上新版本值。
2. 工作区《版本更新说明》补充本次踩到的新坑。
3. 确认 `git status` 里没有录制数据、`.env`、`build/`、APK，向用户汇报后再提交、合并、推送。

## 9. 迁移完成后（再做，不在迁移中做）

规则脚本/基准集/行为指纹、配兵与摆阵训练、联赛评估、Linux 移植、私服真人数据 5 项自动化
（注册升级、常规战配兵进攻、报名排位赛、排位防守阵型与援军、排位战配兵进攻；批量注册前须用户确认代点服务条款）。
`features.py`、`tensorize.py`、`training/` 写了但没测（本机没有 torch，安装需用户同意）。新单位/新建筑要能被模型的
单位词表与建筑通道吸收（见 `docs/model_plan.md` 的版本适配一节）。

## 10. 19.32.7 迁移记录（2026-11-21）

已按本清单完成第 1~8 步，分支 `th19`。新版本踩到的坑与结论（下次版本更新优先检查这些）：

1. `build.tag` 没有更新（仍是 `18_647_2`），版本号以 APK 文件名为准；`find_offsets.py --build-tag 19_32_7`。
2. 偏移推导失效点：submit 变成薄包装（`follow_wrapper`）；GameMode 内 level 从 +0x10 移到 +0x18；
   "Replay ... load failed" 等日志被内联到多处（只取出现在虚表里的函数）；`deliver` 不再紧挨 `receiveMessage`；
   LoginOk 的状态检查不在函数开头；Level 的村庄序号 0x420→0x490（改为按访问器推导）；人口改为每级数组；
   第二个可执行段超过 0x1900000（探针改用生成的 `kLibgCodeEnd`）。`--expect` 现在按 libg 哈希自动选版本。
3. CSV 资源前加了 `Sig:` + 64 字节签名。
4. 消息 ID 未变；EndClientTurn 19033 → 10307（同一消息类，用虚函数比对确定）；登录消息顺序有变化（见登录包 meta.json）。
5. 主村命令全部重新编号且字段顺序打乱，详见 `home_control.py` 文件头；"军队"字段（1 当前、3 友谊战）写错时
   客户端会"执行成功"但没有效果——必须重登核对服务器状态，不能只看执行结果。
6. EnemyHomeData：防守方 avatar 也带完整列表链；本局军队以 army_all 为准；装备等级列表的计数是 VInt。
7. 用户审核：禁用新装备"护盾胸针""暴雪战斧"（`unit_catalog.USER_EXCLUDED`）；军队里小队种类不限。
8. **战斗结束标志**：19.32 打完时先置 `GameMode+0xd`（`LogicGameMode_battleEnded`，从 updateSubTick 推导），游戏随即停止
   调用 updateSubTick，而 `+0xc` 要到结算才置位。探针原来只认 `+0xc`，锁步推进会一直等到超时（每局白等约 30 秒）。
   现在任一置位即视为结束；扫描每局从约 75 秒降到约 25 秒。
9. 英雄战旗（`heroFlags`）：录制里都是空列表，格式未知，随机基地暂未放置（待用户提供样本）。
10. **进攻方永远满级**：刚升大本营的账号有数天的"军队满级 / 英雄满级"强化。放兵/英雄等级 = max(账号等级, 大本营保底)，
    18/19 本即满级，注入的等级全部无效（18.647 也一样，只是之前没核对等级）。强化存成进攻方 avatar 变量数组里的两个计时器
    （军队 20/21/22、英雄 25/26/27，find_offsets 从 avatar 构造函数推导），载入对局时计时器在运行就会开启。
    `attacker_payload.clear_th_boost` 在载入前把它们清零，OfflineStarter 默认执行（`EnvConfig.attacker_th_boost=True` 可保留），
    实际等级与出兵栏角标都正确；th_sweep 会逐兵种核对血量 = 注入等级的血量。
11. 超级充能、村庄导出 JSON、随机账号分布等见 `docs/models.md` 附录。
