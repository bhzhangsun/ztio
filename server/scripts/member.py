#!/usr/bin/env python3
"""
成员管理 —— 网络配置是声明式的（apply-network.py），只有成员授权是运行时动作，
所以单独留这一个命令。

用法
----
    ./member.py list                    列出全部成员
    ./member.py pending                 只看在敲门但还没授权的设备
    ./member.py authorize <地址...>      授权（支持一次多个）
    ./member.py deauthorize <地址...>    取消授权（保留成员记录）
    ./member.py rm <地址...>             彻底移除成员
    ./member.py ip <地址> <IPv4>         给成员指定固定地址（覆盖自动分配）
    ./member.py ip <地址> --clear        清掉固定地址，回到自动分配

地址是 10 位十六进制，即设备的 ZeroTier 地址。设备第一次 join 时会出现在
`pending` 里 —— 因为网络是 private=true，未授权就什么也做不了。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, os.pardir, "network.json")


class API:
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
            raise SystemExit(
                "FATAL: HTTP %s（%s %s）\n  %s"
                % (e.code, method, path, e.read().decode(errors="replace").strip()[:400])
            )
        except urllib.error.URLError as e:
            raise SystemExit("FATAL: 连不上 controller（%s）：%s" % (self.base, e))

    def status(self):
        return self._req("GET", "/status")

    def get_network(self, nwid):
        return self._req("GET", "/controller/network/" + nwid)

    def members(self, nwid):
        return self._req("GET", "/controller/network/%s/member" % nwid)

    def set_member(self, nwid, addr, patch):
        self._req("POST", "/controller/network/%s/member/%s" % (nwid, addr), patch)
        # 回读验证：controller 对不认识的字段是静默丢弃
        got = self._req("GET", "/controller/network/%s/member/%s" % (nwid, addr))
        for k, v in patch.items():
            if got.get(k) != v:
                raise SystemExit(
                    "❌ 回读比对失败：%s 写入 %r，回读 %r" % (k, v, got.get(k))
                )
        return got

    def del_member(self, nwid, addr):
        self._req("DELETE", "/controller/network/%s/member/%s" % (nwid, addr))


def resolve_nwid(api, cfg_path):
    cfg = json.load(open(os.path.abspath(cfg_path)))
    nwid = (cfg.get("networkId") or "").strip()

    if not nwid:
        raise SystemExit(
            "FATAL: %s 里的 networkId 是空的。\n"
            "  先跑一次 scripts/apply-network.py 创建网络并取得 ID。" % cfg_path
        )

    st = api.status()
    if not nwid.startswith(st["address"]):
        raise SystemExit(
            "FATAL: networkId %s 的前 10 位不是本 controller 的地址 %s"
            % (nwid, st["address"])
        )
    return nwid


def fmt_ts(ms):
    if not ms:
        return "-"
    import datetime

    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def cmd_list(api, nwid, args):
    ms = api.members(nwid)
    if not ms:
        print("（没有成员。设备 join 后会出现在这里。）")
        return 0

    print("%-12s %-6s %-18s %-16s %s" % ("地址", "已授权", "分配地址", "首次出现", "最后授权"))
    print("-" * 78)
    for addr in sorted(ms):
        m = ms[addr]
        ips = ",".join(m.get("ipAssignments") or []) or "-"
        print(
            "%-12s %-6s %-18s %-16s %s"
            % (
                addr,
                "是" if m.get("authorized") else "否",
                ips,
                fmt_ts(m.get("creationTime")),
                fmt_ts(m.get("lastAuthorizedTime")),
            )
        )
    return 0


def cmd_pending(api, nwid, args):
    ms = api.members(nwid)
    pend = {a: m for a, m in ms.items() if not m.get("authorized")}
    if not pend:
        print("没有待授权的设备。")
        return 0
    print("以下设备已 join 但**未授权**（private=true 下它们还什么都做不了）：")
    for addr in sorted(pend):
        m = pend[addr]
        print(
            "  %s  vMajor=%s vMinor=%s 首次出现=%s"
            % (addr, m.get("vMajor"), m.get("vMinor"), fmt_ts(m.get("creationTime")))
        )
    print("\n授权：%s authorize %s" % (sys.argv[0], " ".join(sorted(pend))))
    return 0


def _each(api, nwid, addrs, fn):
    rc = 0
    for a in addrs:
        try:
            fn(api, nwid, a)
        except SystemExit as e:
            print("  %s: %s" % (a, e))
            rc = 1
    return rc


def cmd_authorize(api, nwid, args):
    def f(api, nwid, a):
        api.set_member(nwid, a, {"authorized": True})
        print("  %s 已授权" % a)

    return _each(api, nwid, args.addr, f)


def cmd_deauthorize(api, nwid, args):
    def f(api, nwid, a):
        api.set_member(nwid, a, {"authorized": False})
        print("  %s 已取消授权（成员记录保留）" % a)

    return _each(api, nwid, args.addr, f)


def cmd_rm(api, nwid, args):
    def f(api, nwid, a):
        api.del_member(nwid, a)
        print("  %s 已移除" % a)

    return _each(api, nwid, args.addr, f)


def cmd_ip(api, nwid, args):
    import ipaddress

    if args.clear:
        ips = []
    else:
        if not args.ip:
            raise SystemExit("需要给出 IPv4，或用 --clear")
        try:
            ipaddress.IPv4Address(args.ip)
        except ValueError:
            raise SystemExit("不是合法 IPv4：%r" % args.ip)
        ips = [args.ip]

    # ipAssignments 是整体替换的数组。同时把 noAutoAssignIps 置位，
    # 否则 controller 仍可能再自动分配一个地址进来。
    m = api.set_member(
        nwid, args.addr[0], {"ipAssignments": ips, "noAutoAssignIps": bool(ips)}
    )
    print("  %s 的固定地址现在是：%s" % (args.addr[0], m.get("ipAssignments") or "（无，回到自动分配）"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="ztio 成员管理")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="network.json 路径")
    ap.add_argument("--api", default="http://127.0.0.1:9993")
    ap.add_argument(
        "--token-file",
        default=os.path.join(HERE, os.pardir, "data", "one", "authtoken.secret"),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全部成员")
    sub.add_parser("pending", help="只看未授权的设备")

    for name, helptext in (
        ("authorize", "授权"),
        ("deauthorize", "取消授权"),
        ("rm", "移除成员"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("addr", nargs="+", help="10 位十六进制的 ZeroTier 地址")

    p = sub.add_parser("ip", help="给成员指定固定地址")
    p.add_argument("addr", nargs=1)
    p.add_argument("ip", nargs="?", help="IPv4 地址")
    p.add_argument("--clear", action="store_true", help="清掉固定地址")

    args = ap.parse_args()

    token_path = os.path.abspath(args.token_file)
    if not os.path.isfile(token_path):
        raise SystemExit("FATAL: 找不到 authtoken：%s" % token_path)
    api = API(args.api, open(token_path).read().strip())
    nwid = resolve_nwid(api, args.config)

    return {
        "list": cmd_list,
        "pending": cmd_pending,
        "authorize": cmd_authorize,
        "deauthorize": cmd_deauthorize,
        "rm": cmd_rm,
        "ip": cmd_ip,
    }[args.cmd](api, nwid, args)


if __name__ == "__main__":
    sys.exit(main())
