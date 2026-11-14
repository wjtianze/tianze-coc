# 安装与使用

## 需要的环境

| 项目 | 用途 |
| --- | --- |
| Windows + Python 3.12 及以上 | 中间层、工具脚本 |
| `pip install -r requirements.txt` | numpy、pyelftools、capstone（只有逆向工具需要） |
| MuMu 模拟器 12（Android 12，root，ARM64 支持） | 运行私服客户端 |
| Android NDK r26 及以上 | 编译探针 |
| SDK build-tools + JDK 17 | zipalign / apksigner 重新签名 |

## 步骤

1. 复制 `.env.example` 为 `.env`，填写 adb、设备序列号、NDK、build-tools、JDK 路径。
2. 把原版私服 APK 放到 `user-provided/atrasis.apk`（或在 `.env` 的 `TZ_ORIGINAL_APK` 指定）。
3. （换版本时）重新生成偏移和单位目录：

```bash
python -I tools/re/find_offsets.py <解出的 libg.so> --build-tag <版本>
```

```bash
python -m tianze_coc.assets user-provided/atrasis.apk --markdown docs/units.md
```

4. 编译探针、注入、安装：

```bash
python tools/build_probe.py
```

```bash
python -m tianze_coc.offline_build
```

```bash
python -m tianze_coc.offline_install
```

   重新签名后与原版签名不同。若模拟器上已装原版私服客户端，需要先手动卸载（会清空它的本地数据）。
   脚本只操作 `TZ_PACKAGE` 指定的包，不会碰官方部落冲突。

5. 打开客户端登录私服，发起一场进攻，然后在 Python 中：

```python
from tianze_coc.battle_env import TianzeBattleEnv
from tianze_coc.contracts import ActionV1

env = TianzeBattleEnv()
obs = env.reset()                 # 检测到进攻后进入锁步
print(env.legal_units(obs))
obs, info, done = env.step([ActionV1("troop", 4000000, 10.0, 2.0)])
print(info.results, obs.destruction)
```

## 不连模拟器的测试

```bash
python -m unittest discover -s tests -t .
```

模拟探针也可以单独启动，供中间层或上层代码联调：

```bash
python -m tianze_coc.fake_probe --port 27650
```

## 排错

- `hello` 返回 `fingerprint_mismatches`：libg 版本与偏移表不符，或被其他补丁改过。重跑 `find_offsets.py`。
- 查看探针日志：

```bash
adb logcat -s tianze
```
