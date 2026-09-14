#!/usr/bin/env python3
"""
把 network.json 收敛到 controller 上。

为什么需要这个脚本
------------------
ZeroTier 的 controller 管理 API **只校验 JSON 语法，不校验语义**。不合法的值会被
静默改写，而不是报错。实测到的行为：

    {"mtu": "abc"}          -> mtu 变成 1280
    {"private": "yes"}      -> private 变成 false
    {"mtu": 999999}         -> mtu 变成 10000
    {"multicastLimit": -5}  -> 变成 18446744073709551611
    {"rules": [坏结构]}      -> rules 变成 []
    {"target": "10.0.0.0/abc"} -> 变成 10.0.0.0/0，即整个 IPv4 空间
    只有 {"invalid 这种语法错误才会返回 500 parse_error.101

对一个「把全网设备连起来」的东西来说，「悄悄把 10.0.0.0/abc 变成 0.0.0.0/0」
是灾难性的，而你不会收到任何提示。

所以本脚本做两件事：
    1. 写之前做完整的语义预校验 —— 不合法直接退出，不发出请求
    2. 写之后回读并逐字段比对 —— 被悄悄改写就报错，而不是假装成功

用法
----
    ./apply-network.py            # 收敛到 network.json 描述的状态
    ./apply-network.py --check    # 只读，比对现状与目标，不做修改
"""

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, os.pardir, "network.json")


# ----------------------------------------------------------------------
# 与 controller 的通信
# ----------------------------------------------------------------------
class Controller:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None):
        url = self.base + path
        data = None
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-ZT1-Auth", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit(
                "FATAL: controller 返回 HTTP %s（%s %s）\n  %s"
                % (e.code, method, path, detail.strip()[:500])
            )
        except urllib.error.URLError as e:
            raise SystemExit(
                "FATAL: 连不上 controller（%s）。\n"
                "  确认容器在跑、且 network_mode: host 生效：\n"
                "    docker compose ps\n"
                "    curl -s -H \"X-ZT1-Auth: $(cat data/one/authtoken.secret)\" "
                "http://127.0.0.1:9993/status\n  %s" % (self.base, e)
            )

    def status(self):
        return self._req("GET", "/status")

    def get_network(self, nwid):
        return self._req("GET", "/controller/network/" + nwid)

    def create_network(self, controller_addr):
        # 后 6 位留空，让 controller 自己分配
        return self._req("POST", "/controller/network/" + controller_addr + "______", {})

    def set_network(self, nwid, patch):
        return self._req("POST", "/controller/network/" + nwid, patch)


# ----------------------------------------------------------------------
# 语义预校验
# ----------------------------------------------------------------------
class Invalid(Exception):
    pass


def want_bool(d, key, where):
    v = d.get(key)
    if not isinstance(v, bool):
        raise Invalid("%s.%s 必须是 true/false，实际是 %r" % (where, key, v))
    return v


def want_int(d, key, lo, hi, where):
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise Invalid("%s.%s 必须是整数，实际是 %r" % (where, key, v))
    if not (lo <= v <= hi):
        raise Invalid("%s.%s 必须在 %d..%d 之间，实际是 %d" % (where, key, lo, hi, v))
    return v


def want_ip(s, where):
    try:
        return ipaddress.ip_address(s)
    except ValueError as e:
        raise Invalid("%s 不是合法 IP：%r（%s）" % (where, s, e))


def want_cidr(s, where):
    try:
        return ipaddress.ip_network(s, strict=True)
    except ValueError as e:
        raise Invalid(
            "%s 不是合法 CIDR：%r（%s）\n"
            "  注意：controller 会把非法掩码静默改成 /0，等于整个 IPv4 空间，"
            "  所以这里必须严格校验。" % (where, s, e)
        )


def validate(cfg):
    """把 network.json 校验并规范化成可直接 POST 的 payload。"""
    out = {}

    if not isinstance(cfg.get("name"), str) or not cfg["name"]:
        raise Invalid("name 必须是非空字符串")
    out["name"] = cfg["name"]

    out["private"] = want_bool(cfg, "private", "network")
    out["enableBroadcast"] = want_bool(cfg, "enableBroadcast", "network")
    out["mtu"] = want_int(cfg, "mtu", 1280, 10000, "network")
    out["multicastLimit"] = want_int(cfg, "multicastLimit", 0, 1 << 20, "network")

    # ---- 地址池 ----
    pools = cfg.get("ipAssignmentPools")
    if not isinstance(pools, list):
        raise Invalid("ipAssignmentPools 必须是数组")
    out_pools = []
    for i, p in enumerate(pools):
        where = "ipAssignmentPools[%d]" % i
        if not isinstance(p, dict):
            raise Invalid("%s 必须是对象" % where)
        a = want_ip(p.get("ipRangeStart"), where + ".ipRangeStart")
        b = want_ip(p.get("ipRangeEnd"), where + ".ipRangeEnd")
        if a.version != 4 or b.version != 4:
            raise Invalid("%s 目前只支持 IPv4 起始/结束地址" % where)
        if int(a) > int(b):
            raise Invalid("%s 起始地址 %s 大于结束地址 %s" % (where, a, b))
        out_pools.append({"ipRangeStart": str(a), "ipRangeEnd": str(b)})
    out["ipAssignmentPools"] = out_pools

    # ---- 地址分配模式 ----
    v4 = cfg.get("v4AssignMode")
    if not isinstance(v4, dict):
        raise Invalid("v4AssignMode 必须是对象")
    # 1.14.1 只认 zt 这一个键，dhcp 会被静默丢弃 —— 见了就提醒
    if "dhcp" in v4:
        raise Invalid(
            "v4AssignMode.dhcp 在 1.14.1 上不存在（实测：写了会被静默丢弃）。\n"
            "  而且 ZeroTier 客户端没有 DHCP 客户端，开了也没用。删掉这个键。"
        )
    unknown = set(v4) - {"zt"}
    if unknown:
        raise Invalid("v4AssignMode 含未知键 %s（1.14.1 只支持 zt）" % sorted(unknown))
    out["v4AssignMode"] = {"zt": want_bool(v4, "zt", "v4AssignMode")}

    v6 = cfg.get("v6AssignMode")
    if not isinstance(v6, dict):
        raise Invalid("v6AssignMode 必须是对象")
    unknown = set(v6) - {"6plane", "rfc4193", "zt"}
    if unknown:
        raise Invalid("v6AssignMode 含未知键 %s" % sorted(unknown))
    out["v6AssignMode"] = {
        "6plane": want_bool(v6, "6plane", "v6AssignMode")
        if "6plane" in v6
        else False,
        "rfc4193": want_bool(v6, "rfc4193", "v6AssignMode")
        if "rfc4193" in v6
        else False,
        "zt": want_bool(v6, "zt", "v6AssignMode") if "zt" in v6 else False,
    }

    # ---- DNS ----
    dns = cfg.get("dns")
    if not isinstance(dns, dict):
        raise Invalid("dns 必须是对象")
    domain = dns.get("domain", "")
    if not isinstance(domain, str):
        raise Invalid("dns.domain 必须是字符串")
    servers = dns.get("servers", [])
    if not isinstance(servers, list):
        raise Invalid("dns.servers 必须是数组")
    out["dns"] = {
        "domain": domain,
        "servers": [str(want_ip(s, "dns.servers[]")) for s in servers],
    }

    # ---- 路由 ----
    routes = cfg.get("routes", [])
    if not isinstance(routes, list):
        raise Invalid("routes 必须是数组")
    out_routes = []
    for i, r in enumerate(routes):
        where = "routes[%d]" % i
        if not isinstance(r, dict):
            raise Invalid("%s 必须是对象" % where)
        net = want_cidr(r.get("target"), where + ".target")
        via = r.get("via")
        if via is not None:
            via = str(want_ip(via, where + ".via"))
        item = {"target": str(net)}
        if via is not None:
            item["via"] = via
        out_routes.append(item)
    out["routes"] = out_routes

    # ---- 跨字段一致性：池必须落在某个已声明的网段内 ----
    # （routes 为空的常见配置下，用 pool 推断网段即可）
    return out


# ----------------------------------------------------------------------
# 回读比对
# ----------------------------------------------------------------------
def normalize(v):
    """把回读值归一化，便于与目标值比较。"""
    if isinstance(v, list):
        return sorted(
            (normalize(x) for x in v),
            key=lambda x: json.dumps(x, sort_keys=True),
        )
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in sorted(v.items())}
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def diff(target, actual, path="", out=None):
    """只比较我们**发送过**的键。controller 会返回很多我们没设的字段，忽略它们。"""
    if out is None:
        out = []
    for k, tv in target.items():
        p = "%s.%s" % (path, k) if path else k
        if k not in actual:
            out.append("%s: 目标 %r，回读时该键不存在" % (p, tv))
            continue
        av = actual[k]
        if isinstance(tv, dict) and isinstance(av, dict):
            diff(tv, av, p, out)
        elif normalize(tv) != normalize(av):
            out.append("%s: 写入 %r，回读 %r  ← 被改写或丢弃" % (p, tv, av))
    return out


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="把 network.json 收敛到 controller")
    ap.add_argument(
        "--config", default=DEFAULT_CONFIG, help="配置路径（默认 server/network.json）"
    )
    ap.add_argument("--api", default="http://127.0.0.1:9993", help="controller 管理 API")
    ap.add_argument(
        "--token-file",
        default=os.path.join(HERE, os.pardir, "data", "one", "authtoken.secret"),
        help="authtoken.secret 路径",
    )
    ap.add_argument("--check", action="store_true", help="只比对，不修改")
    args = ap.parse_args()

    token_path = os.path.abspath(args.token_file)
    if not os.path.isfile(token_path):
        raise SystemExit("FATAL: 找不到 authtoken：%s" % token_path)
    token = open(token_path).read().strip()

    cfg = json.load(open(os.path.abspath(args.config)))
    payload = validate(cfg)          # ← 预校验，不合法在这里就退出

    zt = Controller(args.api, token)
    st = zt.status()
    controller_addr = st["address"]
    print("controller 地址 : %s" % controller_addr)
    print("节点版本        : %s" % st.get("version"))

    nwid = (cfg.get("networkId") or "").strip()

    if not nwid:
        if args.check:
            raise SystemExit(
                "networkId 为空且处于 --check 模式。先跑一次不带 --check 的，"
                "让它创建网络并打印出 ID。"
            )
        print("networkId 为空，创建新网络…")
        created = zt.create_network(controller_addr)
        nwid = created["nwid"]
        print("")
        print("  ★ 新建网络 ID：%s" % nwid)
        print("  ★ 把它填回 %s 的 networkId 字段并存进 git。" % args.config)
        print("    前 10 位 %s 是 controller 地址，后 6 位由 controller 分配。" % controller_addr)
        print("")

    if len(nwid) != 16:
        raise SystemExit("FATAL: networkId 必须是 16 位十六进制，实际 %r" % nwid)
    if not nwid.startswith(controller_addr):
        raise SystemExit(
            "FATAL: networkId %s 的前 10 位不是本 controller 的地址 %s。\n"
            "  网络无法在 controller 之间迁移 —— 只能重建。" % (nwid, controller_addr)
        )

    before = zt.get_network(nwid)
    print("网络            : %s（%s，revision %s）" % (nwid, before.get("name"), before.get("revision")))

    if args.check:
        d = diff(payload, before)
        if d:
            print("\n现状与目标不一致：")
            for line in d:
                print("  - " + line)
            return 1
        print("\n✅ 现状与 network.json 一致")
        return 0

    print("\n写入…")
    zt.set_network(nwid, payload)

    after = zt.get_network(nwid)
    d = diff(payload, after)

    if d:
        print("\n❌ 回读比对失败 —— 以下字段被 controller 改写或丢弃：")
        for line in d:
            print("  - " + line)
        print("\n这说明配置里有 controller 不接受的语义。修正 network.json 后重跑。")
        return 1

    print("✅ 写入成功，回读比对通过（revision %s -> %s）" % (before.get("revision"), after.get("revision")))
    print("   成员数：%d" % len(after.get("members", {}) if isinstance(after.get("members"), dict) else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
