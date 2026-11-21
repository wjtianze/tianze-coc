# 三个模型的前置接口

计划训练三个模型：**摆阵**、**配兵**、**进攻**。前两个不需要模拟器（输入账号等级，输出阵型/配兵），
进攻模型在模拟器里打。最终三者组合：摆阵模型控制私服账号的阵型和防守援军，配兵模型控制当前配兵，
然后进攻模型自对弈训练。本文列出为此准备好的全部接口（最初在 18.647 上完成，2026-11-21 已迁移到 19.32.7 并重新真机验证，除非另有说明）。

所有等级都是 **1 起的游戏等级**；GlobalID 见 `data/unit_catalog.json` / `data/progression.json`。

## 0. 规则表与修改器（三个模型共用）

| 模块 | 作用 |
| --- | --- |
| `assets/progression.py` → `data/progression.json` | 成长规则：每种建筑/陷阱每个大本营的数量上限、每级所需大本营、占地；兵种/攻城每级所需实验室；法术每级所需实验室；英雄每级所需大本营+英雄大厅；战宠每级所需战宠小屋；装备每级所需铁匠铺、可用英雄；合并建筑原料（含"须已改装"）；兵营/法术/攻城/城堡容量；障碍物占地 |
| `account.py` `Account` | 账号 = 村庄 JSON + avatar 单位等级。`from_payload(负载, "own"/"defender"/"attacker")`、`validate()`、`apply_to_payload()` |
| 修改器 | `set_th`、`set_structure_level`、`set_structure_count`、`add_structure`、`set_unit_level`、`clamp_units`、`max_out(th)`、`randomize(th, rng, progress)` |

`validate()` 检查（违规以 `Violation(code, message, gid)` 列出）：

- 数量 ≤ 当前大本营上限；建筑等级 ≤ 当前大本营允许的等级；
- 合并建筑：原料数 + Σ(份数×成品数) ≤ 原料上限（箭塔同时被多人箭塔、复合机械塔占用时合计）；
- **升本须先建齐**（默认开，`require_all_built=False` 关闭）：T 本时每种普通建筑 ≥ T-1 本上限。
  注意 18.647 的 globals.csv 中 `REQUIRE_ALL_BUILDINGS_PLACED_FOR_TOWN_HALL_UPGRADE = FALSE`，这条比游戏本身更严；
  城墙、陷阱、合并建筑及其原料不参与；
- 兵种/攻城/法术等级 ≤ 实验室上限且已解锁（生产建筑等级）；英雄 ≤ 大本营+英雄大厅；战宠 ≤ 战宠小屋；装备 ≤ 铁匠铺；
- 坐标在 44×44 可建造区内、互不重叠。

修改器只改本地/离线数据（训练用）。私服账号的建筑等级和单位等级**不能**通过客户端命令随意修改
（那需要升级、花资源、等时间），要改私服真实账号需要私服本身支持。

## 1. 摆阵模型

输入：`layout.inventory(account)`（每一件的种类、等级、占地、是否隐藏）；输出：`layout.Layout`。

| 接口 | 作用 |
| --- | --- |
| `Layout(placements=[Placement(index, x, y, mode)], cc=[(GlobalID, 数量, 等级)])` | 阵型 + 防守部落城堡援军 |
| `layout.validate(layout, account)` | 每件恰好摆一次、不越界、不重叠；援军只能是兵种、人口 ≤ 城堡容量、等级 ≤ 本账号等级 |
| `layout.apply(layout, account)` | 写入账号（之后 `apply_to_payload` 用于离线对战） |
| `layout.encode_grid(account, attacker_view=False)` | (C, 50, 50) 张量：占用、隐藏、每种建筑一个通道（值 = 等级/最高等级） |
| `layout.random_layout` / `random_defense_cc` | 随机合法阵型/援军（基线、数据增广） |
| `HomeController.set_layout(account)` | **控制私服**：普通模式逐座移动建筑（命令 5488；18.647 为 6535），自动排顺序，被挡住时先挪到临时空地；避开障碍物 |
| `HomeController.set_defense_cc(entries, CC_RANKED / CC_FRIENDLY)` | **控制私服**：防守援军（命令 5160） |

防守援军只有 **排位战** 和 **友谊战** 可以自定义；常规战的援军与进攻共用，部落战的防守援军由其他成员捐赠，
都不能单独设置。

## 2. 配兵模型

输入：`Account`（单位等级 + 建筑等级决定的容量）；输出：`army.Army`。

| 接口 | 作用 |
| --- | --- |
| `Army(troops, spells, sieges, heroes=[HeroPick(gid, equipment, pet, mode)], cc)` | 一套配兵 |
| `army.capacity(account)` | 兵营人口、法术、攻城机器、英雄槽、城堡容量（18 本满级：352 / 11 / 3 / 4，城堡 55/4/2） |
| `army.validate(army, account)` | 只能用部落战/排位战可带的单位、已解锁、不超容量、超级兵 ≤ 2 种、英雄 ≤ 4 且不重复、装备属于该英雄且已拥有（每人 ≤ 2 件）、战宠已解锁且不重复、只有大守护者能切模式 |
| `army.random_army` | 随机合法配兵 |
| `HomeController.set_army(army)` | **控制私服**：清空后逐个加入兵种/小队/法术/攻城机器（6565/6623），换出战英雄（6944）、装备（5603）、战宠（5417）、大守护者模式（5621）；19.32 编号（18.647 为 6092/6584/6611/5504/6022/5462） |

攻城机器可带多台，但一局只能部署一台（战斗环境强制）。

## 3. 进攻模型

| 接口 | 作用 |
| --- | --- |
| `TianzeBattleEnv(EnvConfig(fair_view=True))` | **公平观测**：陷阱一律不可见，特斯拉电磁塔受伤（现身）前不可见；原始观测里也删掉，禁止开挂 |
| `layout.attacker_view(account)` / `encode_grid(..., attacker_view=True)` | 开战前已知的对手阵型（不含陷阱、特斯拉） |
| `env.prepare(siege=GlobalID, warden_mode=0/1)` | **开战前决策**：根据对手阵型选定本局攻城机器（自己的或援军的），切换大守护者模式；之后只能部署选定的那台 |
| `EnvConfig(realtime=True)` | 实时模式：不锁步，按真实时间推进（联机打真人友谊战用） |
| `selfplay.Match` + `build_enemy_payload(模板, match)` | **自对弈**：防守方账号+阵型+援军、进攻方单位等级+配兵 → 一条 EnemyHomeData |
| `OfflineStarter(..., payload_fn=lambda i: payload)` | 每局用新负载离线开局 |
| `selfplay.Match(..., mode="esports")` / `battle_modifier.set_modifier` | **难度模式**（战斗属性调整）：普通 / 传奇杯 3 / 2 / 1 / 电竞，写入负载末尾 4 字节；真机验证进攻方英雄生命上限按表变化 |

真机验证（`tools/selfplay_check.py`）：16 本随机防守方 269 座建筑、23 个陷阱、随机阵型与援军，
18 本进攻方随机配兵——开局后引擎中的兵种、法术、攻城机器、防守建筑与注入完全一致；公平视角下陷阱数为 0。

## 4. 实时控制的底层：通用命令通道

探针新增 `cmd` / `cmd_results`：按 **命令类型 + encode 字节** 在主线程构造任意客户端命令，走与 UI 相同的提交路径。

- 客户端 UI 命令不在 `createCommand` 工厂里；`tools/re/cmd_vtables.py` 静态扫描出 338 种命令的虚表和对象大小
  （`data/command_types.json`），探针按虚表构造（清零 → 写虚表 → tick=-1 → decode），并校验 `getCommandType`。
- 命令侦听 `cmd_log` 现在带 `enc`（命令 encode 的完整字节）；`tools/cmd_recorder.py` 持续记录 UI 操作，
  新机制"让用户手动操作一次 → 对照字节 → 写编码器"即可。
- 命令格式表、每条的验证状态见 `tianze_coc/home_control.py` 文件头；编码器必须逐字节复现抓到的 UI 命令（有单元测试）。

注意事项（联调得出）：

1. 修改要等下一条 EndClientTurn（外发消息 10307；18.647 为 19033）发出才到达服务器；`run()` 默认等待。过早关游戏会丢失修改。
2. **同一批里只要有一条服务器认为非法的命令，整批都会被丢弃**（客户端本地仍显示已改）。发送前务必先 validate。
3. 加入配兵每条只能加 1 个；移除可以一次多个。整套配兵约 150~350 条命令，约 3~6 秒。
4. 登录时录下的 OwnHomeData 之后不会自动更新；要看服务器最新状态就重登（`tools/payload_probe.restart_game()`）。

## 附：部落城堡援军等级上限（攻防相同）

援军等级只取决于**接收方部落城堡等级 C**，与部落等级、捐兵者等级无关（超出的自动降到上限）。
实际对局中援军一般就是上限等级，所以随机援军直接取上限（`progression.cc_level_cap`）。
规则取自 libg 代码（18.647：`0xded150` 算最高等级，`0xded060` 判断能否请求；19.32.7：`0xeeb300` / `0xeeb210`，逻辑与常数完全相同，只有数据字段偏移后移），不是查表：

参数都是 C − 1；实验室数组存 LaboratoryLevel − 1，所以"−4 ≤ C−1"即"所需实验室 ≤ C + 4"。

| 类别 | 能否请求（`0xded060`） | 最高等级（`0xded150`） | 例（C = 6） |
| --- | --- | --- | --- |
| 兵种、法术 | 1 级所需实验室 ≤ C+4（只有冰冻 1 级要 7 级实验室，1、2 级城堡没有） | 从 2 级起逐级检查所需实验室 ≤ C+4，遇到不满足即停 | 龙 7、气球 8 |
| 攻城机器 | T ≤ C+6，T = 解锁它的攻城工坊等级所需大本营（`ProductionBuilding` → `dd3770`） | C − T + 8，不超满级 | 飞艇 2，训练营不能 |
| 超级兵 | 原兵种第 MinOriginalLevel+1 级所需实验室 ≤ C+4（MinOriginalLevel 是 0 起的） | 用原兵种实验室数组，偏移 VisualLevel−1；显示等级 = 原兵种上限 | 超级野蛮人 9；超级矿工/野猪/大雪怪不能 |

字段（18.647 → 19.32.7）：`+0xde → +0xe2` EnabledBySuperLicence、`+0xf0 → +0xf8` ProductionBuilding、
`+0xf8 → +0x100` 超级兵许可，虚函数槽 `+0xb0/+0xb8/+0xc8 → +0xb8/+0xc0/+0xd0`；18.647 的其余字段：`+0xc8` 等级数、`+0x98` 实验室数组、`+0xcc` VisualLevel−1；超级兵许可（`super_licences.csv`：`+0x70` Original、`+0x78` Replacement、`+0x80` MinOriginalLevel）。
请求界面（`0xa242f8`）另外要求：人口 ≤ 城堡容量、不是 DisableDonate、本村单位、超级兵许可在日历中启用；18.647 的单位全部满足后三条。

超级兵最低城堡等级：隐秘哥布林 4；超级野蛮人/弓箭手/炸弹人 5；超级巨人、法师、飞龙、地狱飞龙、亡灵、瓦基丽、女巫、
寒冰猎犬、巨石投手、火箭气球 6；超级矿工、野猪骑士、大雪怪 7（前三档与官方公告一致）。
人口也要放得下：6 级城堡容量 35，所以 40 人口的超级飞龙、超级女巫、寒冰猎犬虽然等级允许，界面里仍不可请求。
6 级城堡的超级兵列表已与游戏界面逐个核对一致（2026-11-21）。随机援军（进攻、防守）都会包含超级兵。

已用 6、7 级城堡在游戏"请求增援 → 编辑"界面的显示逐项核对（`tools/cc_level_boot.py` 改城堡等级离线启动），
单元测试 `ClanCastleLevelCapTest` 固定这些值。19 本版本需要重新确认这两个函数的常数（+4、+6、+8）是否变化。

## 附：19.32.7 迁移要点（2026-11-21）

- **小队**（`squads.csv`，单位种类 `squad`）：由小队训练营（8 本解锁）生产，是 characters.csv 里的一行；配兵放在兵种槽（槽 0），
  部署走普通放兵命令（UI 侦听类型 7962，GlobalID 就是小队本身），出场后变成成员单位（如野蛮人+弓箭手）；不能捐赠（DisableDonate）。
  人口可按等级变化（`housing_levels`，`army.unit_housing` 按账号等级取）。军队里小队**种类数不限**（用户确认；
  `townhall_levels.csv` 的 StrengthMaxSquadTypes 只是"实力"统计列）。`army.validate` 接受小队并计入兵营人口，随机配兵会带小队。
- **EnemyHomeData**：防守方 avatar 也带一条完整的列表链（数量为空），`avatar_codec.find_chain` 取最后一条（进攻方）。
  进攻方的数量列表是"当前配兵"，**引擎按 army_all 生成本局军队**（友谊战用友谊战军队时两者不同），`attacker_payload` 已按此处理。
- 装备等级列表的条目数是 **VInt**（18.647 里也是，只是前面的史诗装备列表为空，int32 解析碰巧成立）。
- 放兵/法术命令构造函数末尾多一个布尔字段（+0x28 / +0x2c，UI 常规路径传 0），探针已补传。
- 难度模式数值上调（见 `progression.battle_modifiers`），模式 GlobalID 不变；真机测得进攻方英雄生命比例 0.90/0.85/0.80 与表一致，
  电竞 0.71（表中 75% 再叠加装备降级）。

## 附：随机账号的数量与等级分布（`Account.randomize`，2026-11-21 用户要求）

- **数量**：游戏规定先建齐当前本的建筑才能升本，所以每种建筑（含城墙、陷阱）的数量都在"上一本上限 ~ 本本上限"之间。
  合并建筑先定成品数量，原料按"自身 + 已合并进成品的份数"计（例如 19 本箭塔的等效数量 9 = 自身 + 多人箭塔×2 + 复合机械塔 + 烟花炮手塔）。
  `Account.validate` 的 `not_all_built` 用同一口径检查。
- **全满**（约 10%，`FULL_MAX_PROB`）：在该大本营等级内全满——建筑建满升满、单位满级、可超级充能的充满（任何大本营等级都可能）。
- **稳本**（约 70%）：每项建筑、单位的等级主要落在"上一本满级 ~ 本本满级"之间，由账号的整体进度 `progress` 决定偏向哪一端。
- **速本**（约 20%，`RUSH_PROB`）：只有关键建筑（大本营与军事类：兵营、实验室、训练营、法术工厂、攻城工坊、部落城堡、
  英雄殿堂、战宠小屋、铁匠铺、守卫神殿、小队训练营）按稳本升级；防御、资源、城墙、陷阱停在 3 本以前（`RUSH_LAG`）到上一本满级之间。
  单位等级由实验室等建筑决定，所以速本的兵种等级也接近本本。
- **英雄殿堂**：游戏规定殿堂没升满不能升大本营（`rules.maxed_before_th_up`），所以 t 本的殿堂不低于 t-1 本满级，
  且 75%（`HERO_HALL_MAX_PROB`）直接是本本满级；英雄等级因此落在"上一本满级 ~ 本本满级"之间。`validate` 的 `not_maxed` 检查此规则。
- **逐项低等级**：不论稳本速本，每个建筑、每个单位都独立地以 10%（`LOW_TAIL`）的概率取低于上一本满级的等级：
  一半贴近上一本满级，一半在 1 级 ~ 上一本满级之间均匀，所以会出现"满级熔岩猎犬 + 1 级气球"这类组合。
  不设单独的"奇怪基地/奇怪配兵"模式。

## 附：超级充能（`progression.structures[*].supercharge`）

- 数据：`mini_levels.csv`（每种充能 2~3 个小等级、所需大本营）+ `buildings.csv` 的 **MiniLevels** 列（按等级，只有填了的那一级能充）。
  19.32 出了新等级的建筑（金矿、圣水收集器、暗黑钻井、防空火箭、特斯拉、连弩、多人箭塔、跳弹加农炮、复合机械塔）新的最高级没填，
  不能充，与游戏弹窗"它们的所有超级充能效果已结束"一致。19.32 可充能：迫击炮、炸弹塔、地狱之塔、投石炮、擎天巨柱、火焰喷射器、反击之塔、超级法师塔。
- 村庄 JSON 字段 `minilvl`（从 1 起），导出格式叫 `supercharge`。真机核对：地狱之塔 minilvl 2 → 生命 +200（×100）；金矿、防空火箭写入无效。
- 随机账号：满足条件的建筑各自独立地以一定概率（随进度增加）充 1~满 级；`max_out()` 默认充满；`validate()` 检查充能合法性。

## 附：配兵与军事建筑

配兵的解锁、容量与等级上限都由进攻方的军事建筑决定（`Account.army_buildings()` 列出 BuildingClass=Army 的建筑等级，也是配兵模型的输入）：
解锁看训练营/暗黑训练营/法术工厂/暗黑法术工厂/攻城工坊/小队训练营/战宠小屋/英雄殿堂；容量看兵营、法术工厂、攻城工坊、部落城堡；
等级上限看实验室（兵种、法术、攻城机器）、战宠小屋（战宠）、铁匠铺（装备）、大本营与英雄殿堂（英雄）、部落城堡（援军，`cc_level_cap`）。
`army.validate` 在有村庄时直接按建筑判断解锁，不依赖单位等级表。

## 附：村庄导出 JSON（`tianze_coc/village_export.py`）

摆阵模型、配兵模型的输入是游戏"导出村庄数据"的 JSON（加上个性化设置），输出是阵型与配兵，不需要进游戏；战斗模型在游戏里实时运行。
`village_export.to_account(json)` / `from_account(account)` 互转：等级 1 起；`cnt` 计数；`lvl: 0` 的建造中建筑作为占地、已消耗合并原料的
1 级建筑保留（标记 construction，导出写回 0）；`supercharge` ↔ minilvl，`gear_up` ↔ gearup；守卫 ↔ guardian_manager，
`building_variants` ↔ 大本营条目的 building_variant；其余字段（夜世界、装饰、皮肤、助手等）原样往返。导出格式没有坐标，导入时自动摆到空地。
