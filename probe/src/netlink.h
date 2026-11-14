// 消息层：录制服务器下发的村庄/战斗数据消息，并在本地重放以离线开局；离线时过滤外发消息。
//
// 录制：替换 EnemyHomeData / NpcData / OwnHomeData / VisitedHomeData 消息虚表的 decode 槽，
//       在解码前复制消息字节流里的原始负载。
// 重放：在 ClientInstance::update（每帧、主线程、GameMode 更新之外）中用消息工厂创建同类型消息，
//       填入字节、调用 decode，再交给 MessageManager 的分发入口——与 libg/libatrasis 自己投递消息的方式相同。
// 过滤：hook 网络线程的单条发送函数，记录每条外发消息的类型；离线模式下按配置丢弃。
#pragma once

#include <cstdint>
#include <set>
#include <string>
#include <vector>

#include "json.h"

namespace tz::net {

bool init(std::uintptr_t libg_base);
bool ready();
long long frame_count();  // ClientInstance::update hook 被调用的次数

// 录制
void list_records(json::Writer &w);
bool get_record(int type, json::Writer &w);  // 写入 "hex" 字段
bool put_record(int type, const std::string &hex, std::string &err);

// 重放：排队，由下一帧的主线程执行。返回排队序号；wait_done 等待其完成。
long long queue_replay(int type, std::string &err);
bool wait_replay(long long ticket, int timeout_ms, std::string &status);

// 外发过滤
void configure(const json::Value &cfg, json::Writer &w);
void write_net_log(json::Writer &w);

}  // namespace tz::net
