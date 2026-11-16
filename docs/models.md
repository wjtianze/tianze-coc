# 三个模型的前置接口

计划训练三个模型：**摆阵**、**配兵**、**进攻**。前两个不需要模拟器（输入账号等级，输出阵型/配兵），
进攻模型在模拟器里打。最终三者组合：摆阵模型控制私服账号的阵型和防守援军，配兵模型控制当前配兵，
然后进攻模型自对弈训练。本文列出为此准备好的全部接口（18.647，均已在真机验证，除非另有说明）。

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
| `HomeController.set_layout(account)` | **控制私服**：普通模式逐座移动建筑（命令 6535），自动排顺序，被挡住时先挪到临时空地；避开障碍物 |
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
| `HomeController.set_army(army)` | **控制私服**：清空后逐个加入兵种/法术/攻城机器（6092/6584），换出战英雄（6611）、装备（5504）、战宠（6022）、大守护者模式（5462） |

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

1. 修改要等下一条 EndClientTurn（外发消息 19033）发出才到达服务器；`run()` 默认等待。过早关游戏会丢失修改。
2. **同一批里只要有一条服务器认为非法的命令，整批都会被丢弃**（客户端本地仍显示已改）。发送前务必先 validate。
3. 加入配兵每条只能加 1 个；移除可以一次多个。整套配兵约 150~350 条命令，约 3~6 秒。
4. 登录时录下的 OwnHomeData 之后不会自动更新；要看服务器最新状态就重登（`tools/payload_probe.restart_game()`）。
