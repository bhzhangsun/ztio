#!/usr/bin/env python3
"""
直接在 controller 上管网络 —— 不维护任何配置文件。

为什么没有配置文件
------------------
早先这里有个 network.json，当"声明式配置"用。那是错的设计，实测已经出问题：
controller 上那个网络的 ipAssignmentPools / v4AssignMode / v6AssignMode / dns
**全部和文件不一致**，而且没有任何东西会告诉你。

两个数据源必然漂移。真正的事实来源只有一个 —— controller 自己的存储：

    <数据目录>/controller.d/network/<16位nwid>.json          网络定义
    <数据目录>/controller.d/network/<16位nwid>/member/*.json  成员

那是 ZeroTier 的原生布局（EmbeddedNetworkController + FileDB），backup.sh
备份的就是它。所以本脚本不存状态，只做两件事：**读**和**带校验地写**。

校验为什么必要
--------------
controller 的管理 API **只校验 JSON 语法，不校验语义**，不合法的值会被静默改写：

    {"mtu": "abc"}             -> 1280
    {"private": "yes"}         -> false
    {"mtu": 999999}            -> 10000
    {"multicastLimit": -5}     -> 18446744073709551611
    {"target": "10.0.0.0/abc"} -> 10.0.0.0/0，即整个 IPv4 空间
    只有 {"invalid 这种语法错误才返回 500 parse_error.101

把 10.0.0.0/abc 悄悄变成 0.0.0.0/0 对一个"把全网设备连起来"的东西是灾难性的。

校验怎么做（不另写一份 schema）
------------------------------
自己维护一份 schema 必然漏字段 —— 实测 API 返回里有 capabilities / tags /
rules / ssoEnabled / authorizationEndpoint / rulesSource / remoteTraceTarget，
手写的定义一个都没覆盖。所以：

  1. 可写字段集**从 API 自己推导** —— 先 GET 一个网络，响应里的键就是字段全集，
     减掉只读字段（id/nwid/objtype/creationTime/revision）
  2. 值的类型必须与现状一致
  3. 只对"说错话会变危险"的字段做前置检查：CIDR 掩码、mtu/multicastLimit 范围
  4. **回读比对作总兜底** —— 写进去的和读回来不一致就报错。这条覆盖所有
     意料之外的字段，且不需要预先知道 schema

用法
----
    ./ztnet.py ls                       列出 controller 上的所有网络
    ./ztnet.py show <nwid>              打印网络现状（API 原样返回）
    ./ztnet.py fields [nwid]            列出可写字段（含当前值，用于照抄格式）
    ./ztnet.py create [选项]            创建网络，打印出 nwid
    ./ztnet.py set <nwid> [选项]        修改字段
    ./ztnet.py rm <nwid>                删除网络

    --dry-run   只校验并打印将要发送的内容，不发请求
"""

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOKEN = os.path.join(HERE, os.pardir, "data", "one", "authtoken.secret")

def find_token(explicit=None):
    """authtoken.secret 的位置随运行环境不同 —— 三个都要能认出来。

        容器内   /var/lib/zerotier-one/authtoken.secret   （ZT_HOME 默认值）
        主机上   $ZTIO_DATA_ROOT/one/authtoken.secret      （默认 /var/lib/ztio）

    所以脚本在容器里（docker compose exec ztplanet ztnet.py ...）和主机上
    （./scripts/ztnet.py ...）都用同一份，不需要额外参数。
    """
    if explicit:
        return explicit

    cands = []
    env = os.environ.get("ZTIO_DATA_DIR")
    if env:
        cands.append(os.path.join(env, "authtoken.secret"))

    # .env 的 ZTIO_DATA_ROOT —— 与 docker-compose.yml 读的是同一个值
    root = os.environ.get("ZTIO_DATA_ROOT")
    if not root:
        for envfile in (os.path.join(HERE, os.pardir, ".env"),
                        os.path.join(HERE, os.pardir, os.pardir, ".env")):
            try:
                with open(envfile, encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("ZTIO_DATA_ROOT="):
                            root = line.split("=", 1)[1].strip()
            except OSError:
                continue
            if root:
                break
    if root:
        cands.append(os.path.join(root, "one", "authtoken.secret"))

    cands += [
        "/var/lib/zerotier-one/authtoken.secret",   # 容器内（ZT_HOME）
        "/var/lib/ztio/one/authtoken.secret",       # 主机上的系统默认路径
    ]

    for c in cands:
        if os.path.isfile(c):
            return c
    raise SystemExit(
        "FATAL: 找不到 authtoken.secret。找过这些位置：\n    %s\n"
        "  用 --token-file 显式指定，或设 ZTIO_DATA_ROOT。" % "\n    ".join(cands)
    )

# controller 自己产生、不接受写入的字段
READONLY = {"id", "nwid", "objtype", "creationTime", "revision"}

# 实测的边界。写在这里只是为了让错误在**请求之前**发生 —— 真正的一致性由回读比对保证。
MTU_MIN, MTU_MAX = 1280, 10000
MCAST_MIN, MCAST_MAX = 0, 1000000


# ----------------------------------------------------------------------
# 与 controller 的通信
# ----------------------------------------------------------------------
class Controller:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
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
                "  确认容器在跑：docker compose ps\n"
                '  curl -s -H "X-ZT1-Auth: $(cat /var/lib/ztio/one/authtoken.secret)" '
                "http://127.0.0.1:9993/status\n  %s" % (self.base, e)
            )

    def status(self):
        return self._req("GET", "/status")

    def list_networks(self):
        r = self._req("GET", "/controller/network")
        return r if isinstance(r, list) else []

    def get(self, nwid):
        return self._req("GET", "/controller/network/" + nwid)

    def create(self, controller_addr):
        # 后 6 位留空，让 controller 自己分配
        return self._req("POST", "/controller/network/" + controller_addr + "______", {})

    def patch(self, nwid, payload):
        return self._req("POST", "/controller/network/" + nwid, payload)

    def delete(self, nwid):
        return self._req("DELETE", "/controller/network/" + nwid)


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
class Invalid(Exception):
    pass


def writable_fields(baseline):
    """可写字段集来自 API 自己 —— 不手写，就不会漏。"""
    return sorted(k for k in baseline.keys() if k not in READONLY)


def check_type(key, value, baseline):
    """值类型必须与现状一致。这条挡的是 mtu:"abc" / private:"yes" 这类。

    例外：**空容器不携带类型信息**。controller 用 [] 表示"这个字段没设置"，
    设置之后可能是对象 —— 例如新建网络的 dns 是 []，设置后是
    {"domain":...,"servers":[...]}；rules 同理。拿 [] 去要求对方也是数组，
    会把正确的写法判成错的。空容器只要求"是容器"，具体形状交给回读比对。
    """
    if key not in baseline:
        raise Invalid("%s 不在 controller 的字段集里" % key)
    want = baseline[key]

    if want is None:
        return                      # 可空字段，推断不出类型
    if isinstance(want, (list, dict)) and len(want) == 0:
        if not isinstance(value, (list, dict)):
            raise Invalid("%s 必须是对象或数组，实际是 %r" % (key, value))
        return
    if isinstance(want, bool):
        if not isinstance(value, bool):
            raise Invalid("%s 必须是 true/false，实际是 %r" % (key, value))
    elif isinstance(want, int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise Invalid("%s 必须是整数，实际是 %r" % (key, value))
    elif isinstance(want, str):
        if not isinstance(value, str):
            raise Invalid("%s 必须是字符串，实际是 %r" % (key, value))
    elif isinstance(want, list):
        if not isinstance(value, list):
            raise Invalid("%s 必须是数组，实际是 %r" % (key, value))
    elif isinstance(want, dict):
        if not isinstance(value, dict):
            raise Invalid("%s 必须是对象，实际是 %r" % (key, value))


def want_cidr(s, where, strict=True):
    """CIDR 必须严格校验 —— controller 会把非法掩码静默改成 /0。"""
    try:
        net = ipaddress.ip_network(s, strict=strict)
    except ValueError as e:
        raise Invalid(
            "%s 不是合法 CIDR：%r（%s）\n"
            "  注意：controller 会把非法掩码静默改成 /0，等于整个 IPv4 空间，"
            "  所以这类错误必须在发请求之前拦住。" % (where, s, e)
        )
    if net.prefixlen == 0:
        raise Invalid(
            "%s 是 %s —— /0 会覆盖整个地址空间。如果确实要全网默认路由，"
            "请在 routes 里用具体 target 表达，或确认这确实是你想要的。"
            % (where, s)
        )
    return net


def want_ip(s, where):
    try:
        return ipaddress.ip_address(s)
    except ValueError as e:
        raise Invalid("%s 不是合法 IP：%r（%s）" % (where, s, e))


def want_int(s, where, lo, hi):
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise Invalid("%s 必须是整数，实际是 %r" % (where, s))
    if not (lo <= v <= hi):
        raise Invalid("%s 必须在 %d..%d 之间，实际是 %d" % (where, lo, hi, v))
    return v


# ----------------------------------------------------------------------
# 从命令行参数构造 payload
# ----------------------------------------------------------------------
def parse_pool(s):
    if "-" not in s:
        raise Invalid("--pool 的格式是 起始IP-结束IP，实际是 %r" % s)
    a, _, b = s.partition("-")
    start = want_ip(a.strip(), "--pool 起始")
    end = want_ip(b.strip(), "--pool 结束")
    if start.version != end.version:
        raise Invalid("--pool 的起止地址版本不一致：%s / %s" % (start, end))
    if int(end) < int(start):
        raise Invalid("--pool 的结束地址 %s 小于起始地址 %s" % (end, start))
    return {"ipRangeStart": str(start), "ipRangeEnd": str(end)}


def parse_route(s):
    # 支持 "10.0.0.0/8" 或 "10.0.0.0/8%172.16.0.1"
    target, _, via = s.partition("%")
    want_cidr(target.strip(), "--route target", strict=False)
    r = {"target": target.strip()}
    if via:
        want_ip(via.strip(), "--route via")
        r["via"] = via.strip()
    return r


def build_payload(args):
    """只把用户显式给出的字段放进 payload —— 其余字段保持 controller 上的原值。"""
    p = {}

    if args.name is not None:
        p["name"] = args.name
    if args.mtu is not None:
        p["mtu"] = want_int(args.mtu, "--mtu", MTU_MIN, MTU_MAX)
    if args.multicast_limit is not None:
        p["multicastLimit"] = want_int(
            args.multicast_limit, "--multicast-limit", MCAST_MIN, MCAST_MAX
        )

    if args.private is not None:
        p["private"] = args.private
    if args.broadcast is not None:
        p["enableBroadcast"] = args.broadcast

    if args.pool:
        p["ipAssignmentPools"] = [parse_pool(x) for x in args.pool]

    if args.dns_domain is not None or args.dns_server:
        dns = {"servers": []}
        if args.dns_domain is not None:
            dns["domain"] = args.dns_domain
        for s in args.dns_server or []:
            want_ip(s, "--dns-server")
            dns["servers"].append(s)
        p["dns"] = dns

    if args.route:
        p["routes"] = [parse_route(x) for x in args.route]

    v4 = {}
    if args.v4_zt is not None:
        v4["zt"] = args.v4_zt
    if v4:
        p["v4AssignMode"] = v4

    v6 = {}
    if args.v6_rfc4193 is not None:
        v6["rfc4193"] = args.v6_rfc4193
    if args.v6_6plane is not None:
        v6["6plane"] = args.v6_6plane
    if args.v6_zt is not None:
        v6["zt"] = args.v6_zt
    if v6:
        p["v6AssignMode"] = v6

    if args.capability:
        p["capabilities"] = [want_int(c, "--capability", 0, 2 ** 32 - 1) for c in args.capability]
    if args.tag:
        p["tags"] = [want_int(t, "--tag", 0, 2 ** 32 - 1) for t in args.tag]

    return p


def _route_covers(routes, ip_str):
    """ip_str 是否落在 routes 的某条 target 里。"""
    try:
        ip = ipaddress.ip_address(str(ip_str))
    except ValueError:
        return False
    for r in (routes or []):
        t = r.get("target") if isinstance(r, dict) else None
        if not t:
            continue
        try:
            if ip in ipaddress.ip_network(str(t), strict=False):
                return True
        except ValueError:
            continue
    return False


def _pool_route_hint(pool):
    """给出能覆盖这个 pool 的建议路由；推不出单个 /24 时返回 None。"""
    try:
        s = ipaddress.ip_address(str(pool.get("ipRangeStart")))
        e = ipaddress.ip_address(str(pool.get("ipRangeEnd")))
    except (ValueError, TypeError):
        return None
    if s.version != 4 or e.version != 4:
        return None
    net = ipaddress.ip_network("%s/24" % s, strict=False)
    return str(net) if e in net else None


def validate(payload, baseline):
    """请求之前的检查：字段在不在、类型对不对。"""
    allowed = set(writable_fields(baseline))
    for k, v in payload.items():
        if k not in allowed:
            raise Invalid(
                "%s 不是可写字段。controller 认的字段是：\n    %s"
                % (k, "\n    ".join(allowed))
            )
        if k in baseline:
            check_type(k, v, baseline)

    # ---- 分配池必须落在某条 route 里 ----
    # controller 源码（controller/EmbeddedNetworkController.cpp）里：
    #
    #   int routedNetmaskBits = -1;
    #   for (rk...) if (routes[rk].target.containsAddress(ip)) routedNetmaskBits = ...;
    #   if (routedNetmaskBits >= 0) { nc->staticIps[...] = ip; }   // 把地址放进配置
    #
    # 自动分配那段是 routedNetmaskBits > 0 才分配。
    # 也就是说：**没有覆盖 pool 的路由，controller 一个地址都不会下发**，
    # 但网段/DNS/MTU 照常下发 —— 客户端 status=OK、netconfRevision 正常，
    # 只有 assignedAddresses 永远是空的。这个失败模式完全不报错，只能靠这条路拦。
    routes = payload.get("routes", baseline.get("routes"))
    pools = payload.get("ipAssignmentPools", baseline.get("ipAssignmentPools")) or []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        s0, e0 = pool.get("ipRangeStart"), pool.get("ipRangeEnd")
        if not s0 or not e0:
            continue
        if not _route_covers(routes, s0):
            hint = _pool_route_hint(pool)
            raise Invalid(
                "分配池 %s-%s 没有落在任何 route 里。\n"
                "    controller 只会下发给「落在某条 route 覆盖范围内」的地址 ——\n"
                "    这种情况下网段/DNS/MTU 照常下发，客户端 status=OK，\n"
                "    但 assignedAddresses 永远是空的，且服务端不报任何错。\n"
                "    %s"
                % (s0, e0,
                   ("建议加上：--route %s" % hint) if hint
                   else "请显式指定一条覆盖它的 --route")
            )
    return payload


# ----------------------------------------------------------------------
# 回读比对 —— 总兜底，覆盖所有意料之外的改写
# ----------------------------------------------------------------------
def normalize(v):
    """归一化后再比对。

    关键：**丢掉值为 None 的键**。controller 会把没写的字段补成 null 填回来 ——
    例如写入 {"target": "172.16.0.0/24"} 会回读成 {"target": "...", "via": null}。
    语义完全一致，但严格比对会报「被改写或丢弃」，把正常写入误判成失败，
    还附带一句「控制器上的状态已经是错的」—— 那句话会把排查方向带偏。
    """
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in sorted(v.items()) if x is not None}
    if isinstance(v, list):
        return [normalize(x) for x in v if x is not None]
    return v


def diff(target, actual, path=""):
    out = []
    if isinstance(target, dict):
        for k, tv in target.items():
            p = "%s.%s" % (path, k) if path else k
            if not isinstance(actual, dict) or k not in actual:
                out.append("%s: 写入 %r，回读时该键不存在" % (p, tv))
                continue
            av = actual[k]
            if isinstance(tv, dict) and isinstance(av, dict):
                out.extend(diff(tv, av, p))
            elif isinstance(tv, list) and isinstance(av, list):
                if normalize(tv) != normalize(av):
                    out.append("%s: 写入 %r，回读 %r  ← 被改写或丢弃" % (p, tv, av))
            elif normalize(tv) != normalize(av):
                out.append("%s: 写入 %r，回读 %r  ← 被改写或丢弃" % (p, tv, av))
    return out


def write_verified(zt, nwid, payload, dry_run=False):
    before = zt.get(nwid)
    validate(payload, before)          # ← 请求之前

    if dry_run:
        print("将要 POST 到 /controller/network/%s：" % nwid)
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    zt.patch(nwid, payload)
    after = zt.get(nwid)
    d = diff(payload, after)           # ← 写之后回读
    if d:
        print("\n❌ 回读比对失败 —— 以下字段被 controller 改写或丢弃：")
        for line in d:
            print("  - " + line)
        print(
            "\n这说明有 controller 不接受的语义。控制器上的状态**已经是错的**，"
            "\n请立刻用 `show` 确认并改回来。"
        )
        return 1

    print("✅ 写入成功，回读比对通过（revision %s -> %s）"
          % (before.get("revision"), after.get("revision")))
    return 0


# ----------------------------------------------------------------------
def need_nwid(zt, nwid):
    """不传 nwid 时，controller 上只有一个网络就自动选它。"""
    if nwid:
        return nwid
    nets = zt.list_networks()
    if len(nets) == 1:
        return nets[0]
    if not nets:
        raise SystemExit("FATAL: controller 上还没有网络。先 create 一个。")
    raise SystemExit(
        "FATAL: controller 上有 %d 个网络，必须显式指定 nwid：\n    %s"
        % (len(nets), "\n    ".join(nets))
    )


def cmd_ls(zt, args):
    addr = zt.status()["address"]
    nets = zt.list_networks()
    print("controller 地址 : %s" % addr)
    print("网络数          : %d" % len(nets))
    for n in nets:
        try:
            d = zt.get(n)
        except SystemExit:
            print("  %s  （读取失败）" % n)
            continue
        pool = ""
        if d.get("ipAssignmentPools"):
            p0 = d["ipAssignmentPools"][0]
            pool = "  %s-%s" % (p0.get("ipRangeStart"), p0.get("ipRangeEnd"))
        print("  %s  %-16s private=%-5s mtu=%-5s%s"
              % (n, d.get("name") or "(无名)", d.get("private"), d.get("mtu"), pool))
    return 0


def cmd_show(zt, args):
    nwid = need_nwid(zt, args.nwid)
    print(json.dumps(zt.get(nwid), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_fields(zt, args):
    baseline = zt.get(need_nwid(zt, args.nwid))
    print("可写字段（从 API 的现状推导；括号内是当前值，照抄格式即可）：\n")
    for k in writable_fields(baseline):
        v = baseline[k]
        print("  %-24s %s" % (k, json.dumps(v, ensure_ascii=False)[:96]))
    print("\n只读字段（拒绝写入）：%s" % ", ".join(sorted(READONLY)))
    return 0


def cmd_create(zt, args):
    addr = zt.status()["address"]
    payload = build_payload(args)

    if args.dry_run:
        print("controller 地址 : %s" % addr)
        print("将要创建网络并写入：")
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    created = zt.create(addr)
    nwid = created["nwid"]
    print("controller 地址 : %s" % addr)
    print("★ 新建网络 ID   : %s" % nwid)
    print("  （前 10 位 %s 是 controller 地址，后 6 位由 controller 分配）" % addr)
    print("  注意：此时只有默认值，下面的初始配置若校验失败会自动回滚。")

    if not payload:
        print("\n未给出任何字段，网络保持 controller 的默认值。")
        return 0

    print("\n写入初始配置…")
    try:
        rc = write_verified(zt, nwid, payload)
    except BaseException as e:
        # 崩了也要回滚 —— 只处理「校验失败」是不够的，脚本自身的异常同样会留下孤儿。
        print("\n写入过程出错：%s" % e)
        rc = 1
    if rc != 0:
        # 校验发生在创建之后，所以失败时必须把刚建出来的网络删掉，
        # 否则 controller 上会留下一个用着默认值、没人知道的孤儿网络。
        print("\n回滚：删除刚创建的网络 %s（避免留下孤儿）" % nwid)
        try:
            zt.delete(nwid)
            print("✅ 已回滚，controller 上不留痕迹。修正参数后重试。")
        except SystemExit:
            print("⚠️ 回滚失败 —— 请手动执行：ztnet.py rm %s --yes" % nwid)
    else:
        print("\n记住这个 ID —— 它不会再被打印第二次。查看：ztnet.py ls")
    return rc


def cmd_set(zt, args):
    nwid = need_nwid(zt, args.nwid)
    payload = build_payload(args)
    if not payload:
        raise SystemExit("FATAL: 没有给出任何要修改的字段。用 --help 看选项。")
    before = zt.get(nwid)
    print("网络            : %s（%s，revision %s）"
          % (nwid, before.get("name"), before.get("revision")))
    print("将要修改        : %s" % ", ".join(sorted(payload)))
    print()
    return write_verified(zt, nwid, payload, dry_run=args.dry_run)


def cmd_rm(zt, args):
    nwid = need_nwid(zt, args.nwid)
    d = zt.get(nwid)
    if not args.yes:
        print("即将删除网络 %s（%s），成员数 %d。"
              % (nwid, d.get("name"), len(d.get("members") or {})))
        print("这会断开该网络上所有设备。加 --yes 确认。")
        return 1
    zt.delete(nwid)
    print("✅ 已删除 %s" % nwid)
    return 0


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="在 controller 上管网络（不维护配置文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----\n")[-1],
    )
    ap.add_argument("--api", default="http://127.0.0.1:9993", help="controller 管理 API")
    ap.add_argument("--token-file", default=None,
                    help="authtoken.secret 路径（默认自动找：容器内 /var/lib/zerotier-one/，"
                         "主机上 /var/lib/ztio/one/）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ls", help="列出所有网络").set_defaults(fn=cmd_ls)

    p = sub.add_parser("show", help="打印网络现状")
    p.add_argument("nwid", nargs="?")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("fields", help="列出可写字段")
    p.add_argument("nwid", nargs="?")
    p.set_defaults(fn=cmd_fields)

    def add_write_opts(p):
        p.add_argument("--name")
        p.add_argument("--mtu", help="%d..%d" % (MTU_MIN, MTU_MAX))
        p.add_argument("--multicast-limit", dest="multicast_limit")
        p.add_argument("--private", dest="private", action="store_true", default=None,
                       help="私有网络，需授权才能加入（默认）")
        p.add_argument("--public", dest="private", action="store_false",
                       help="公开网络，任何人可加入")
        p.add_argument("--broadcast", dest="broadcast", action="store_true", default=None)
        p.add_argument("--no-broadcast", dest="broadcast", action="store_false")
        p.add_argument("--pool", action="append", metavar="起始IP-结束IP")
        p.add_argument("--dns-domain", dest="dns_domain")
        p.add_argument("--dns-server", dest="dns_server", action="append", metavar="IP")
        p.add_argument("--route", action="append", metavar="CIDR[%VIA]")
        p.add_argument("--v4-zt", dest="v4_zt", action="store_true", default=None,
                       help="由 controller 分配 IPv4")
        p.add_argument("--no-v4-zt", dest="v4_zt", action="store_false")
        p.add_argument("--v6-rfc4193", dest="v6_rfc4193", action="store_true", default=None)
        p.add_argument("--no-v6-rfc4193", dest="v6_rfc4193", action="store_false")
        p.add_argument("--v6-6plane", dest="v6_6plane", action="store_true", default=None)
        p.add_argument("--no-v6-6plane", dest="v6_6plane", action="store_false")
        p.add_argument("--v6-zt", dest="v6_zt", action="store_true", default=None)
        p.add_argument("--no-v6-zt", dest="v6_zt", action="store_false")
        p.add_argument("--capability", action="append", metavar="N")
        p.add_argument("--tag", action="append", metavar="N")
        p.add_argument("--dry-run", action="store_true", help="只校验并打印，不发请求")

    p = sub.add_parser("create", help="创建网络")
    add_write_opts(p)
    p.set_defaults(fn=cmd_create)

    p = sub.add_parser("set", help="修改字段")
    p.add_argument("nwid", nargs="?")
    add_write_opts(p)
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("rm", help="删除网络")
    p.add_argument("nwid", nargs="?")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_rm)

    args = ap.parse_args()

    token_path = find_token(args.token_file)
    zt = Controller(args.api, open(token_path).read().strip())

    try:
        return args.fn(zt, args)
    except Invalid as e:
        print("❌ 校验未通过，**没有发出任何请求**：\n  %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
