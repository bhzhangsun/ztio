#!/usr/bin/env python3
"""
成员管理 —— 成员授权是纯粹的运行时动作，所以它本来就该是命令式的。
网络定义本身用 ztnet.py 管；两者的状态都在 controller 自己的存储里
（controller.d/network/<nwid>.json 与 .../member/*.json），没有配置文件。
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
    ./member.py name <地址> <名字>       给成员起名字（ztio-dns 的 A 记录用它）
    ./member.py name <地址> --clear      清掉名字，DNS 里退回用地址前缀

地址是 10 位十六进制，即设备的 ZeroTier 地址。设备第一次 join 时会出现在
`pending` 里 —— 因为网络是 private=true，未授权就什么也做不了。

`name` 不是装饰：ztio-dns 的 A 记录就是从成员名派生的（`<名字>.<zone>`），
没有名字时才退化成 `<地址前 10 位>.<zone>`。所以给设备起名字，等于决定
DNS 里能不能用人类可读的名字。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

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

    def list_networks(self):
        r = self._req("GET", "/controller/network")
        return r if isinstance(r, list) else []

    def members(self, nwid):
        """返回 {地址: 成员记录}。

        注意：GET /controller/network/<nwid>/member 返回的**不是**成员对象数组，
        而是 {地址: revision} —— 例如 {"4911bad593": 1}。之前这里直接透传，
        调用方拿到的是整数，一 .get() 就崩。

        真正的成员记录（authorized / ipAssignments / vProto / creationTime…）
        必须按地址逐个取。
        """
        listing = self._req("GET", "/controller/network/%s/member" % nwid)

        addrs = []
        if isinstance(listing, dict):
            addrs = [a for a in listing.keys() if a]
        elif isinstance(listing, list):
            for m in listing:
                a = m.get("address") if isinstance(m, dict) else m
                if a:
                    addrs.append(a)

        return {
            a: self._req("GET", "/controller/network/%s/member/%s" % (nwid, a))
            for a in addrs
        }

    def set_member(self, nwid, addr, patch):
        self._req("POST", "/controller/network/%s/member/%s" % (nwid, addr), patch)
        # 回读验证：controller 对不认识的字段是静默丢弃
        got = self._req("GET", "/controller/network/%s/member/%s" % (nwid, addr))
        for k, v in patch.items():
            actual = got.get(k)
            # 空字符串是「清空」语义：controller 可能把字段整个丢掉，而不是
            # 存成 ""。两者对调用方等价，所以只在非空时要求严格相等。
            if v == "" and not actual:
                continue
            if actual != v:
                raise SystemExit(
                    "❌ 回读比对失败：%s 写入 %r，回读 %r" % (k, v, got.get(k))
                )
        return got

    def del_member(self, nwid, addr):
        self._req("DELETE", "/controller/network/%s/member/%s" % (nwid, addr))


def resolve_nwid(api, nwid):
    """nwid 来自命令行或自动探测 —— 不再从配置文件读。

    controller 上没有配置文件这一说，状态就是它自己的存储
    （controller.d/network/<nwid>.json）。所以这里也照着来：
    显式给了就用，没给且只存在一个网络就选它，多个就要求显式指定。
    """
    nwid = (nwid or "").strip()

    if not nwid:
        nets = api.list_networks()
        if len(nets) == 1:
            nwid = nets[0]
        elif not nets:
            raise SystemExit(
                "FATAL: controller 上还没有网络。\n"
                "  先用 scripts/ztnet.py create 建一个。"
            )
        else:
            raise SystemExit(
                "FATAL: controller 上有 %d 个网络，必须用 --nwid 指定：\n    %s"
                % (len(nets), "\n    ".join(nets))
            )

    st = api.status()
    if not nwid.startswith(st["address"]):
        raise SystemExit(
            "FATAL: nwid %s 的前 10 位不是本 controller 的地址 %s\n"
            "  网络无法在 controller 之间迁移 —— 只能重建。" % (nwid, st["address"])
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

    print(
        "%-12s %-14s %-6s %-18s %-16s %s"
        % ("地址", "名字", "已授权", "分配地址", "首次出现", "最后授权")
    )
    print("-" * 94)
    for addr in sorted(ms):
        m = ms[addr]
        ips = ",".join(m.get("ipAssignments") or []) or "-"
        print(
            "%-12s %-14s %-6s %-18s %-16s %s"
            % (
                addr,
                m.get("name") or "-",
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


def cmd_name(api, nwid, args):
    """给成员起名字。

    名字会**原样**变成 DNS 标签（`<名字>.<zone>`），所以这里必须挡住非法字符：
    一个带空格或中文的名字会生成一条永远匹配不到、也永远解析不了的记录，
    症状是「名字设了但 dig 查不到」，排查起来很费劲。
    """
    import re

    if args.clear:
        name = ""
    else:
        if not args.name:
            raise SystemExit("需要给出名字，或用 --clear")
        name = args.name.strip()
        if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", name):
            raise SystemExit(
                "名字不合法：%r\n"
                "  它会原样变成 DNS 标签（<名字>.<zone>），所以只允许 [A-Za-z0-9-]，\n"
                "  不能以连字符开头或结尾，长度不超过 63。" % name
            )

    addr = args.addr[0]
    m = api.set_member(nwid, addr, {"name": name})
    print(
        "  %s 的名字现在是：%s"
        % (addr, m.get("name") or "（无 —— DNS 里退回用 <地址>.<zone>）")
    )
    return 0


def main():
    ap = argparse.ArgumentParser(description="ztio 成员管理")
    ap.add_argument("--nwid", help="网络 ID。省略时若 controller 上只有一个网络则自动选它")
    ap.add_argument("--api", default="http://127.0.0.1:9993")
    ap.add_argument(
        "--token-file",
        default=None,
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

    p = sub.add_parser("name", help="给成员起名字（ztio-dns 的 A 记录用它）")
    p.add_argument("addr", nargs=1)
    p.add_argument("name", nargs="?", help="名字，只允许 [A-Za-z0-9-]")
    p.add_argument("--clear", action="store_true", help="清掉名字，退回用地址前缀")

    args = ap.parse_args()

    token_path = find_token(args.token_file)
    api = API(args.api, open(token_path).read().strip())
    nwid = resolve_nwid(api, args.nwid)

    return {
        "list": cmd_list,
        "pending": cmd_pending,
        "authorize": cmd_authorize,
        "deauthorize": cmd_deauthorize,
        "rm": cmd_rm,
        "ip": cmd_ip,
        "name": cmd_name,
    }[args.cmd](api, nwid, args)


if __name__ == "__main__":
    sys.exit(main())
