#!/usr/bin/env python3
"""ztio-dns —— 只服务 ZeroTier 网络内部的静态 DNS。

设计约束（每一条都是有原因的，改之前先看这里）
================================================

1. 静态，不做递归
   只答自己 zone 里的记录，其余一律 NXDOMAIN。公网主机上跑递归 DNS 会变成
   DNS 放大攻击的帮凶 —— 攻击者伪造源地址发一个小查询，你回一个几十倍大的
   响应，全部打向受害者。不做递归，这条路就断了。

2. 绑 ZT 接口地址，绝不绑 0.0.0.0
   这是「DNS 只对网络内可见」的**全部支点**。绑 0.0.0.0 意味着公网能直接
   查到它，而安全组有没有开 53 是另一回事 —— 不能靠那个兜底。

3. 记录从 controller 的落盘数据派生，不存第二份
   成员的名字（name）和地址（ipAssignments）都在 controller 里，DNS 只是
   把它们翻译成 <name>.<zone> → <ip>。名字和地址因此不可能漂移。
   这也解释了为什么不需要数据库。
   派生是**持续的**：每 --refresh 秒重读一次，变了才换表。只在启动时读一次
   的话，加一台设备就得重启容器 —— 那很容易被当成「DNS 坏了」。

4. 报文解析交给 dnslib
   要写的是「静态表 + 隔离策略」，不是 DNS 协议栈。压缩指针、EDNS0、
   TCP fallback、大小写随机化（0x20 编码）这些边角，自己实现会烧掉大量
   时间而且**测不出来** —— 因为测试客户端是 dig，不是 macOS/iOS 的
   mDNSResponder。

5. 一个网络一个 socket
   不同网络的网段不同，因此服务器在每个网络里的地址不同。一个 socket 只能
   绑一个地址，所以是「一个进程、N 个 socket」，而不是 N 个服务。
   这样隔离由内核的路由保证：A 的成员**路由不到** B 的 socket —— 不是
   「代码里挡着」，是够不着。代码本身甚至不需要知道"网络"这个概念。
"""

import argparse
import ipaddress
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time

from dnslib import A, DNSRecord, QTYPE, RCODE, RR


def log(msg):
    print("[ztio-dns] %s" % msg, flush=True)


# ----------------------------------------------------------------------
# 从 controller 的落盘数据读网络定义和成员
# ----------------------------------------------------------------------
# 为什么直接读文件而不是走管理 API：
#   API 需要 controller 的 authtoken —— 那是整台机器最高权限的凭据。
#   为了读几个记录就把它交给另一个容器，是把最小权限原则反过来用。
#   而 controller.d 是 ZeroTier 自己的原生存储格式（FileDB），
#   本来就是这个仓库定义的唯一事实来源，backup.sh 备份的也是它。
# 代价：耦合了落盘格式。所以读取处全部做防御性解析，读不懂就跳过而不是崩。
# ----------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_networks(ctr_dir):
    """controller.d/network 下每个 <16位nwid>.json 是一个网络定义。"""
    out = []
    try:
        names = os.listdir(ctr_dir)
    except OSError:
        return out
    for n in sorted(names):
        if not n.endswith(".json"):
            continue
        d = load_json(os.path.join(ctr_dir, n))
        if isinstance(d, dict):
            d.setdefault("id", n[:-5])
            out.append(d)
    return out


def find_network(ctr_dir, nwid):
    """按 nwid 精确读一个网络定义（刷新时用，比 list_networks 少读别的网络）。"""
    d = load_json(os.path.join(ctr_dir, nwid + ".json"))
    if isinstance(d, dict):
        d.setdefault("id", nwid)
        return d
    return None


def load_members(ctr_dir, nwid):
    """controller.d/network/<nwid>/member/<10位地址>.json 是成员档案。"""
    out = {}
    mdir = os.path.join(ctr_dir, nwid, "member")
    try:
        names = os.listdir(mdir)
    except OSError:
        return out
    for n in sorted(names):
        if not n.endswith(".json"):
            continue
        d = load_json(os.path.join(mdir, n))
        if isinstance(d, dict):
            out[d.get("address") or n[:-5]] = d
    return out


def dns_address_for(net):
    """DNS 占用的地址 = 网段第一个 IP。

    和 ztnet.py 的 dns_ip_from_routes() 必须是同一个算法 —— 一边改另一边
    不改，DNS 就会绑在一个客户端够不着的地址上。
    """
    for r in (net.get("routes") or []):
        t = r.get("target") if isinstance(r, dict) else None
        if not t:
            continue
        try:
            n = ipaddress.ip_network(str(t), strict=False)
        except ValueError:
            continue
        if n.version != 4 or n.num_addresses < 4:
            continue
        return str(n.network_address + 1)
    return None


# ----------------------------------------------------------------------
# zone 表
# ----------------------------------------------------------------------

class Zone(object):
    """一个网络对应的一张静态表。

    它只知道自己的 zone 和记录 —— 不知道别的网络存在，也不需要知道。
    """

    def __init__(self, nwid, zone, address, records):
        self.nwid = nwid
        self.zone = zone
        self.address = address
        self.records = records          # {fqdn(lower): [ip, ...]}

    def lookup(self, qname):
        return self.records.get(qname.lower().rstrip("."))

    def describe(self):
        return "网络 %s  zone %s  地址 %s  记录 %d 条" % (
            self.nwid, self.zone, self.address, len(self.records))


class ZoneState(object):
    """**当前生效的** zone 的持有者。

    刷新线程与应答线程并发访问，所以切换必须是一次**原子赋值**：CPython 里
    给属性赋一个新引用是原子的，于是应答线程要么看到旧表、要么看到新表，
    永远看不到"改了一半"的表。这也意味着 Zone 本身必须当**不可变对象**用 ——
    只换引用，绝不原地改 records。
    """

    def __init__(self, zone):
        self.zone = zone


def build_zone(ctr_dir, net, nwid):
    zone = (net.get("dns") or {}).get("domain")
    if not zone:
        return None
    zone = zone.strip().rstrip(".").lower()
    if not zone:
        return None

    # 注意：这里是 **IP**，不是节点地址。
    # 两者都是"地址"但完全不同：4ceca00e42 是 ZeroTier 的节点标识，
    # 172.16.0.1 是它在这个网络里被分配的 IP。混淆过一次 ——
    # 症状是日志里"地址 4ceca00e42"然后一直绑不上。
    dns_ip = dns_address_for(net)
    if not dns_ip:
        return None

    records = {}

    # DNS 自己
    records["dns." + zone] = [dns_ip]

    # 每个成员：<name>.<zone> → 它被分配的地址
    # 另外始终额外发一条 <zt地址>.<zone>，这样成员还没起名字时也能用。
    for addr, m in load_members(ctr_dir, nwid).items():
        if not m.get("authorized"):
            continue
        ips = [str(x) for x in (m.get("ipAssignments") or []) if x]
        if not ips:
            continue
        name = (m.get("name") or "").strip().lower()
        # 防御性过滤：名字会原样变成 DNS 标签。正常路径上 member.py name 已经
        # 校验过字符集，但直接写 API 可以绕过它 —— 那种名字只会生成一条永远
        # 匹配不到的记录，不如在这里就丢掉。
        if name and re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
            records["%s.%s" % (name, zone)] = ips
        records["%s.%s" % (addr.lower(), zone)] = ips

    return Zone(nwid, zone, dns_ip, records)


# ----------------------------------------------------------------------
# DNS 应答
# ----------------------------------------------------------------------

class Responder(object):
    """持有某个 zone 的应答逻辑。

    刻意不叫 Handler —— socketserver 里 Handler 指的是"处理请求的那个类"，
    两者是不同层次的东西，同名会让下面的传参错误看起来是对的。

    它不直接持有 Zone，而是持有 ZoneState：记录会被刷新线程换掉。
    """

    def __init__(self, state):
        self.state = state

    def answer(self, data):
        try:
            req = DNSRecord.parse(data)
        except Exception:
            return None                     # 畸形包直接丢，不崩

        q = req.q
        qname = str(q.qname)
        qtype = q.qtype
        reply = req.reply()

        # AD（Authentic Data）位必须清掉，**所有**返回路径都要。dnslib 的
        # reply() 会原样复制问题段的 bitmap，而 dig / macOS 默认就带 AD 位 ——
        # 于是不校验 DNSSEC 的我们会回一个「这些数据已通过 DNSSEC 验证」的
        # 假信号（实测确实如此）。放在这里而不是各 return 之前，就是因为
        # 漏一条路径就会漏一个假信号。
        reply.header.ad = 0

        # 大小写：DNS 名字不区分大小写，但有些客户端会随机化大小写
        # （0x20 编码）并期望响应里原样回显。dnslib 的 reply() 用问题段的
        # 原始 qname，所以这里只做查找侧的小写化。
        ips = self.state.zone.lookup(qname)

        if ips is None:
            # 名字本身不在 zone 里 —— 这才是 NXDOMAIN 的正确用法。
            reply.header.rcode = RCODE.NXDOMAIN
            return reply.pack()

        if qtype == QTYPE.A:
            for ip in ips:
                try:
                    if ipaddress.ip_address(ip).version == 4:
                        reply.add_answer(RR(qname, QTYPE.A, rdata=A(ip), ttl=60))
                except ValueError:
                    continue

        # 名字存在、但这个类型没有数据（AAAA / NS / SOA / TXT…）→ NOERROR
        # 加空答案，即 NODATA。
        #
        # 这里**不能**回 NXDOMAIN：RFC 2308 里 NXDOMAIN 是「这个名字不存在」
        # 级别，负缓存是按**名字**而不是按类型生效的 —— 严格的客户端会把
        # macbook.ztio.internal 整个缓存成"不存在"，连累紧随其后的 A 查询。
        # 而 macOS 恰恰是 A 和 AAAA 一起发的。
        #
        # AAAA 目前一条都不发（ZeroTier 的 RFC4193 地址由节点地址派生，
        # v1 不生成），所以 AAAA 查询就落在这条 NODATA 路径上。
        return reply.pack()


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        out = self.server.responder.answer(data)
        if out:
            sock.sendto(out, self.client_address)


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            data = self.request.recv(65535)
        except OSError:
            return
        if len(data) < 2:
            return
        out = self.server.responder.answer(data[2:])
        if out:
            try:
                self.request.sendall(len(out).to_bytes(2, "big") + out)
            except OSError:
                pass


class UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True

    def __init__(self, addr, responder):
        # 只用 IPv4
        self.address_family = socket.AF_INET
        # 注意参数顺序：第二个是 **RequestHandlerClass（类对象）**，不是 zone。
        # 这里曾经把 responder 实例传进去，症状是每个查询都在服务端抛
        # "TypeError: 'Handler' object is not callable" —— 客户端全部超时，
        # 而服务端日志里才看得到真正原因。
        # zone 走 responder 属性传，不占用那个位置。
        self.responder = responder
        socketserver.ThreadingUDPServer.__init__(self, addr, UDPHandler)


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, addr, responder):
        self.address_family = socket.AF_INET
        self.responder = responder
        socketserver.ThreadingTCPServer.__init__(self, addr, TCPHandler)
        self.daemon_threads = True


def bind_with_retry(server_cls, addr, responder, deadline):
    """等 ZT 接口把地址配上去。

    地址由 controller 在授权成员之后下发，节点要等配置同步才建接口 ——
    所以启动时地址通常还没出现。这里重试而不是直接退出：
    退出的话容器会重启，重启又要重新同步，得不偿失。
    """
    last = None
    while time.time() < deadline:
        try:
            return server_cls(addr, responder)
        except OSError as e:
            last = e
            time.sleep(2)
    raise SystemExit("绑定 %s:53 失败（等待超时）：%s" % (addr, last))


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def refresh_loop(state, ctr_dir, nwid, interval, stop):
    """按 interval 秒重读 controller 数据，**变了才换表**。

    为什么不能只在启动时建一次表：成员名、新设备入网、取消授权，全都写在
    controller 的落盘数据里，而 DNS 是长驻进程。不重读的话，加一台设备就得
    重启容器 —— 而这很容易被当成「DNS 坏了」。

    三条纪律：
      - 没变化就不吭声。30 秒一条日志会把真正有用的信息淹掉。
      - 任何时候出错都用旧表继续服务。DNS 挂了比记录略旧严重得多。
      - 地址变了只报警不重绑：重新 bind 要动 socketserver 的生命周期，
        而这件事（改动网段/DNS 地址）几乎不会发生，报出来让人重启更划算。
    """
    while not stop.wait(interval):
        try:
            net = find_network(ctr_dir, nwid)
            if net is None:
                continue                     # 网络被删了：保留旧表，等它回来
            z = build_zone(ctr_dir, net, nwid)
            if z is None:
                continue

            old = state.zone
            if z.address != old.address:
                log(
                    "⚠️ DNS 地址 %s → %s：监听仍绑在旧地址上，请重启容器"
                    % (old.address, z.address)
                )
                continue
            if z.records == old.records:
                continue

            for k in sorted(set(z.records) - set(old.records)):
                log("  + %s → %s" % (k, ",".join(z.records[k])))
            for k in sorted(set(old.records) - set(z.records)):
                log("  - %s" % k)
            state.zone = z
            log("记录已更新：%d 条 → %d 条" % (len(old.records), len(z.records)))
        except Exception as e:               # 守护线程里绝不能把异常抛出去
            log("重读记录失败（继续用旧表）：%s" % e)


def main():
    ap = argparse.ArgumentParser(description="ztio static DNS for ZeroTier")
    ap.add_argument("--ctr", default="/ctr", help="controller.d/network 的挂载点")
    ap.add_argument("--nwid", default="", help="只服务这个网络（空=全部）")
    ap.add_argument("--refresh", type=int, default=30, help="重读记录间隔（秒）")
    ap.add_argument("--bind-timeout", type=int, default=600,
                    help="等待 ZT 地址出现的最长秒数")
    args = ap.parse_args()

    # ---- 建表 ----
    zones = []
    for net in list_networks(args.ctr):
        nwid = net.get("id") or ""
        if args.nwid and nwid != args.nwid:
            continue
        z = build_zone(args.ctr, net, nwid)
        if z is None:
            log("跳过网络 %s：没有 dns.domain 或推不出网段" % nwid)
            continue
        zones.append(z)
        log(z.describe())

    if not zones:
        raise SystemExit(
            "没有任何可服务的网络。\n"
            "  检查：网络是否设了 dns.domain（ztnet.py set <nwid> --dns-domain x.internal）\n"
            "        是否有一条局域网路由（--route 172.16.0.0/24）\n"
            "        --ctr 挂载点是否正确（%s）" % args.ctr
        )

    # ---- 逐个绑定 ----
    # 一个网络一个 socket。隔离由内核的路由保证：别的网络的成员够不着这个地址。
    deadline = time.time() + args.bind_timeout
    servers = []
    for z in zones:
        state = ZoneState(z)
        r = Responder(state)
        u = bind_with_retry(UDPServer, (z.address, 53), r, deadline)
        t = bind_with_retry(TCPServer, (z.address, 53), r, deadline)
        servers.append((state, u, t))
        log("已监听 %s:53 (UDP+TCP)  zone %s" % (z.address, z.zone))

    for _, u, t in servers:
        threading.Thread(target=u.serve_forever, daemon=True).start()
        threading.Thread(target=t.serve_forever, daemon=True).start()

    stop = threading.Event()

    # ---- 记录刷新 ----
    # 一个网络一个刷新线程；用 stop.wait(interval) 阻塞，所以收到信号能立刻退出。
    for state, _, _ in servers:
        threading.Thread(
            target=refresh_loop,
            args=(state, args.ctr, state.zone.nwid, args.refresh, stop),
            daemon=True,
        ).start()

    log(
        "就绪。只答自己 zone 里的记录，其余 NXDOMAIN，不做递归。"
        "每 %d 秒重读一次 controller 数据。" % args.refresh
    )

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        log("退出")
        for _, u, t in servers:
            u.shutdown()
            t.shutdown()


if __name__ == "__main__":
    main()
