// 完全离线启动：不登录私服的游戏服务器也能进入主村。
//
// 做法（18.647 真机验证）：
//   1. hook libc 的 connect：目标端口是游戏端口（9339）时，改连探针自己开的本地假服务器；
//      其他连接（启动器的 HTTPS 等）照常放行。假服务器只接收、从不回复，客户端以为已连上。
//   2. 不经服务器握手，直接把登录状态设为"已发登录、握手已验证"（netlink 的 login_prep），
//      再按顺序重放录制的 LoginOk、OwnHomeData 等登录消息（由 Python 驱动）。
//   3. 客户端每隔几秒发保活请求，探针自动回放一条保活回复，避免"连接中断"。
// 这样多个模拟器可以共用同一份录制，不会在私服上登录、互相挤下线，也不给私服增加负担。
//
// libc 的 connect 在 ARM 转译环境里是桥接桩（首条指令为进入宿主的 svc），不能搬走执行；
// 因此替换函数不调用原函数，而是直接用 connect 系统调用完成连接。
#pragma once

#include "json.h"

namespace tz::offline {

// 探针加载时调用：若系统属性 debug.tianze.offline_boot=1 则立即启用（可在启动游戏前用 adb setprop 设置）。
void init_from_property();
// 运行时启用/配置：{"enable":true,"game_port":9339,"keepalive_req":10108,"keepalive_resp":20108}
void configure(const json::Value &cfg, json::Writer &w);
bool enabled();
// 外发消息钩子调用：离线启动时收到保活请求则安排一条保活回复
void on_outgoing(int type);
int keepalive_resp_type();

}  // namespace tz::offline
