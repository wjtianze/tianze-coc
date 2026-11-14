// 最小 JSON：够解析协议请求、生成响应即可。
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace tz::json {

struct Value {
  enum class T { Null, Bool, Num, Str, Arr, Obj } t = T::Null;
  bool b = false;
  double n = 0;
  std::string s;
  std::vector<Value> a;
  std::map<std::string, Value> o;

  bool is_obj() const { return t == T::Obj; }
  bool is_arr() const { return t == T::Arr; }
  bool has(const std::string &k) const { return t == T::Obj && o.count(k); }
  const Value &operator[](const std::string &k) const {
    static const Value null_v;
    auto it = o.find(k);
    return (t == T::Obj && it != o.end()) ? it->second : null_v;
  }
  double num(double d = 0) const { return t == T::Num ? n : (t == T::Bool ? (b ? 1 : 0) : d); }
  long long i64(long long d = 0) const { return t == T::Num ? static_cast<long long>(std::llround(n)) : d; }
  bool boolean(bool d = false) const { return t == T::Bool ? b : (t == T::Num ? n != 0 : d); }
  const std::string &str() const { return s; }
};

class Parser {
 public:
  explicit Parser(const std::string &src) : p_(src.c_str()), end_(src.c_str() + src.size()) {}
  bool parse(Value &out) {
    ws();
    if (!value(out)) return false;
    ws();
    return p_ == end_;
  }

 private:
  const char *p_;
  const char *end_;
  void ws() {
    while (p_ < end_ && (*p_ == ' ' || *p_ == '\n' || *p_ == '\r' || *p_ == '\t')) ++p_;
  }
  bool lit(const char *w) {
    const char *q = p_;
    for (; *w; ++w, ++q)
      if (q >= end_ || *q != *w) return false;
    p_ = q;
    return true;
  }
  bool value(Value &v) {
    if (p_ >= end_) return false;
    switch (*p_) {
      case '{': return object(v);
      case '[': return array(v);
      case '"': v.t = Value::T::Str; return string(v.s);
      case 't': v.t = Value::T::Bool; v.b = true; return lit("true");
      case 'f': v.t = Value::T::Bool; v.b = false; return lit("false");
      case 'n': v.t = Value::T::Null; return lit("null");
      default: return number(v);
    }
  }
  bool number(Value &v) {
    char *e = nullptr;
    v.n = std::strtod(p_, &e);
    if (e == p_) return false;
    p_ = e;
    v.t = Value::T::Num;
    return true;
  }
  bool string(std::string &out) {
    ++p_;
    while (p_ < end_ && *p_ != '"') {
      if (*p_ == '\\' && p_ + 1 < end_) {
        ++p_;
        switch (*p_) {
          case 'n': out += '\n'; break;
          case 't': out += '\t'; break;
          case 'r': out += '\r'; break;
          case 'u':
            if (end_ - p_ < 5) return false;
            out += '?';  // 协议里只用 ASCII 键值
            p_ += 4;
            break;
          default: out += *p_;
        }
        ++p_;
      } else {
        out += *p_++;
      }
    }
    if (p_ >= end_) return false;
    ++p_;
    return true;
  }
  bool array(Value &v) {
    v.t = Value::T::Arr;
    ++p_;
    ws();
    if (p_ < end_ && *p_ == ']') return ++p_, true;
    while (true) {
      v.a.emplace_back();
      ws();
      if (!value(v.a.back())) return false;
      ws();
      if (p_ < end_ && *p_ == ',') { ++p_; continue; }
      if (p_ < end_ && *p_ == ']') return ++p_, true;
      return false;
    }
  }
  bool object(Value &v) {
    v.t = Value::T::Obj;
    ++p_;
    ws();
    if (p_ < end_ && *p_ == '}') return ++p_, true;
    while (true) {
      ws();
      std::string k;
      if (p_ >= end_ || *p_ != '"' || !string(k)) return false;
      ws();
      if (p_ >= end_ || *p_ != ':') return false;
      ++p_;
      ws();
      if (!value(v.o[k])) return false;
      ws();
      if (p_ < end_ && *p_ == ',') { ++p_; continue; }
      if (p_ < end_ && *p_ == '}') return ++p_, true;
      return false;
    }
  }
};

// 流式输出：调用方保证结构正确。
class Writer {
 public:
  std::string out;
  Writer &raw(const std::string &s) { comma(); out += s; return *this; }
  Writer &key(const char *k) { comma(); out += '"'; out += k; out += "\":"; fresh_ = true; return *this; }
  Writer &str(const std::string &s) {
    comma();
    out += '"';
    for (char c : s) {
      if (c == '"' || c == '\\') { out += '\\'; out += c; }
      else if (c == '\n') out += "\\n";
      else if (static_cast<unsigned char>(c) < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); out += b; }
      else out += c;
    }
    out += '"';
    return *this;
  }
  Writer &num(long long v) { comma(); out += std::to_string(v); return *this; }
  Writer &num(double v) { comma(); char b[32]; std::snprintf(b, sizeof b, "%.6g", v); out += b; return *this; }
  Writer &boolean(bool v) { comma(); out += v ? "true" : "false"; return *this; }
  Writer &null() { comma(); out += "null"; return *this; }
  Writer &begin_obj() { comma(); out += '{'; fresh_ = true; return *this; }
  Writer &end_obj() { out += '}'; fresh_ = false; return *this; }
  Writer &begin_arr() { comma(); out += '['; fresh_ = true; return *this; }
  Writer &end_arr() { out += ']'; fresh_ = false; return *this; }

 private:
  bool fresh_ = true;
  void comma() {
    if (!fresh_ && !out.empty()) {
      char c = out.back();
      if (c != '{' && c != '[' && c != ':') out += ',';
    }
    fresh_ = false;
  }
};

}  // namespace tz::json
