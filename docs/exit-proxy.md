# 出口代理（Exit Proxy）

ztio 的唯一核心能力：**让一个节点把流量交给另一个节点，由后者替它出网。**

---

## 1. 场景

```
受限 WiFi（封锁目标站点，或干脆不给出口）
   Mac ─────── 同一 WiFi，二层可达 ───────→ 手机 ─────── 蜂窝 ───────→ 互联网
   （Mac 只需要能到手机，不需要这条 WiFi 出网）
```

Mac 把流量交给手机，手机用**自己的蜂窝链路**替它走出去。

这条路径成立的两个条件，缺一不可：

1. **Mac 到手机可达** —— 同处一个 WiFi，二层可达即可，不要求这条 WiFi 能出网
2. **手机有另一条链路** —— 蜂窝、另一个 WiFi、有线，任意

> 反方向（手机 → Mac）用同一套机制，但**前提是 Mac 的上行与手机的不同**。
> 如果两台在同一个受限 WiFi 上，Mac 的出网也是那条受限 WiFi，反向没有收益。

---

## 2. 为什么不是 IP 层出口

最直觉的做法是「把某个节点设成默认网关」，让 IP 层转发。**这条路在 ZeroTier 上走不通**，四条独立的原因，都已核实：

### 2.1 controller 的路由是全网统一的

网络配置里的 `routes[].via` 对**所有成员**生效，而 **member 对象没有任何路由字段** ——
向成员 POST `routes` / `nextHop` 会被**静默丢弃**。

成员对象的完整字段（实测）：

```
activeBridge, address, authenticationExpiryTime, authorized, capabilities,
creationTime, id, ipAssignments, lastAuthorizedCredential,
lastAuthorizedCredentialType, lastAuthorizedTime, lastDeauthorizedTime,
noAutoAssignIps, nwid, objtype, remoteTraceLevel, remoteTraceTarget,
revision, ssoExempt, tags, vMajor, vMinor, vProto, vRev
```

**没有 nextHop。** 所以「每个节点选自己的下一跳」在 controller 层无法表达。

### 2.2 客户端侧只能「过滤」，不能「改向」

ZeroTier 客户端确实有一套按节点的 managed 设置覆盖：

| 设置 | 位置 | 语义 |
|---|---|---|
| `allowManaged` | `networks.d/<nwid>.local.conf` | 总开关，全有/全无 |
| `allowManagedWhitelist` | 同上（**未文档化**） | 路由**不在白名单内就不接受** |
| `allowGlobal` / `allowDefault` / `allowDNS` | 同上 | 各自的总开关 |

`allowManagedWhitelist` 的判定逻辑（`service/OneService.cpp:2704`）：

```cpp
if (!n.allowManaged()) return false;
if (!n.allowManagedWhitelist().empty()) {
    for (InetAddress addr : n.allowManagedWhitelist())
        if (addr.containsAddress(target) && addr.netmaskBits() <= target.netmaskBits())
            return true;
    return false;   // 不在白名单 → 这条 managed route 不装
}
```

**它能说「这条路由我不要」，不能说「这条路由改走别处」。**

### 2.3 `local.conf` 里没有任何路由相关的键

官方文档列出的全部 `settings` 键：

```
primaryPort, secondaryPort, tertiaryPort, portMappingEnabled,
forceTcpRelay, interfacePrefixBlacklist, allowManagementFrom,
allowTcpFallbackRelay, bind
```

外加 `physical`（物理网段黑名单）和 `virtual`（按对端的路径提示）。
**没有任何一条能设置路由或下一跳。**

### 2.4 出口是手机时，IP 层转发根本做不到

IP 层出口要求出口节点开启 **IP 转发 + NAT**，这需要 root / 管理员权限。
iOS 上应用无法做 IP 转发，Android 受限，**手机当出口在 IP 层是不成立的。**

### 2.5 libzt 也没有这条路

libzt 的路由 API **全部是只读的**：

```
zts_core_query_route        // 查
zts_core_query_route_count  // 查
zts_route_is_assigned       // 查
```

没有任何写接口，也没有 NAT / 转发能力。**内嵌 libzt 的 app 无法在 IP 层做事。**

---

## 3. 设计

### 3.1 结论：把出口放在应用层

|  | IP 层出口 | 应用层代理出口 |
|---|---|---|
| 需要 root / 管理员 | 两端都要 ❌ | **都不需要** ✅ |
| 出口是手机 | 做不到 ❌ | **可以** ✅ |
| 选择粒度 | 全网统一 ❌ | **按节点** ✅ |
| 按目标分流 | 不能 ❌ | **能**（不同域名走不同出口）✅ |
| 对 UDP / 任意 IP 协议 | 透明支持 ✅ | **不支持**（见 §7） |
| 需要 libzt 配合 | 没有接口 ❌ | **libzt 完全够用** ✅ |

**「下一跳」在 ztio 里退化为一个纯配置项：选哪个成员当出口。**

### 3.2 两个角色

| 角色 | 职责 |
|---|---|
| **出口（exit）** | 在 ZT 地址上监听代理端口；收到连接后，**用宿主机网络栈**向真实目标发起连接 |
| **客户端（client）** | 把本地连接经 ZT 链路转交给出口 |

**同一个节点可以同时是两者的任意组合**，包括同时服务多个客户端。

### 3.3 数据流

```
用户 app
   │  ① 连接 ztio 的本地入口
   ▼
ztio(客户端) ── 代理客户端
   │  ② 经 ZT 链路连接出口的 ZT 地址:端口
   │     （同 WiFi 时 ZeroTier 自动建立局域网直连；否则走公网或中继）
   ▼
ztio(出口) ── 代理服务端（libzt 的 listen/accept）
   │  ③ ★ 用宿主机栈（dart:io Socket）连接真实目标
   │     这一步走的是手机自己的网络 —— 蜂窝
   ▼
真实目标
```

### 3.4 关键点：第 ③ 步用宿主机栈，不用 libzt

这是整个设计的枢纽：

- **接受连接**用 libzt（`zts_bsd_listen` / `zts_bsd_accept`）—— 因为对方在 ZT 网内
- **发起连接**用宿主机栈（`dart:io` 的 `Socket.connect`）—— 因为目标是公网

因为 app 内嵌 libzt 时**系统里没有 ZT 虚拟网卡**，宿主机栈的连接会自然走手机的正常网络，
也就是蜂窝。**不需要 root、不需要 TUN、不需要 IP 转发、不需要任何系统权限。**

### 3.5 两条链路各自的作用

| 链路 | 承载 | 谁建立 |
|---|---|---|
| Mac → 手机（ZT） | 代理控制 + 被代理的流量 | 内嵌的 libzt |
| 手机 → 目标（宿主机） | 真实出网流量 | `dart:io` |

**ZeroTier 在这里的作用**：让 Mac 能用一个**稳定的地址**找到手机，并在同一局域网时
自动走局域网直连。Mac 不需要知道手机的局域网 IP、不需要知道蜂窝地址、不需要打洞。

---

## 4. 协议

代理用 **SOCKS5**（RFC 1928 + RFC 1929）跑在 ZT 链路上。

选它的理由：

- 只需 `CONNECT` 一种命令，实现量极小
- **域名由出口解析** —— 客户端送 `example.com:443`，出口侧解析并连接。
  这避免了「客户端解析出 IP 再送过去」带来的 DNS 泄漏，也避免了客户端所在网络
  的 DNS 污染
- 标准协议，出问题可以用现成工具（`curl --socks5`）单独验证出口是否正常

**认证**：ZT 网络本身是加密且私有的（`private=true` + 成员白名单），链路层已经认证过身份。
在此之上，ztio 在出口侧额外维护一份**允许名单**（按 ZT 地址），实现「不是所有成员都能用我这个出口」。

---

## 5. 配置与发现

### 5.1 出口不动态公告

候选出口**由配置指定**，不做网络公告。

理由：ZT 地址由 controller 确定性分配、**与成员身份绑定，永不改变**。
所以「出口是谁」这个信息不会失效，静态配置够用，不需要一套公告协议。

### 5.2 每个节点的配置

```
exit         = <成员名 | ZT 地址>      # 默认空 = 不使用出口，直接出网
exit_enabled = false                   # 客户端侧开关，默认关
```

### 5.3 出口侧的配置

```
serve_exit         = false       # 默认关。不开就不会成为任何人的出口
allowed_clients    = [ ... ]     # ZT 地址允许名单，空 = 不限制（仍要求是网络成员）
conditions         = { ... }     # 见 §6.2
```

---

## 6. 安全与配额

### 6.1 默认姿态

**客户端默认不使用出口，出口默认不服务。** 两个开关都要显式打开。
不存在「装了 ztio 就自动变成别人的出口」这种情况。

### 6.2 代价由出口承担，所以限制加在出口侧

手机当出口会消耗**蜂窝流量、电池、产生发热**。因此出口侧应支持条件与配额：

| 条件 | 作用 |
|---|---|
| 仅在充电时 | 避免电池消耗 |
| 仅在 WiFi 下 | 避免蜂窝计费 |
| 每月流量上限 | 硬上限，超出自动停止服务 |
| 仅允许名单内客户端 | 精细授权 |
| 计时/手动撤销 | **出口方随时可一键断开** |

**流量统计必须对出口方可见** —— 手机主人要能知道被用了多少。

### 6.3 反向的前提检查

交互上应当在用户选择出口时做一次提示：

> 出口的上行链路与当前节点的上行链路**相同**时，使用该出口没有收益。

例如两台设备在同一个受限 WiFi 上时选对方当出口，等于绕了一圈回到同一条链路。

---

## 7. 非目标

| 不做 | 原因 |
|---|---|
| IP 层透明出口 | 见 §2，需要两端 root，手机做不到 |
| UDP / 任意 IP 协议转发 | SOCKS5 只做 TCP `CONNECT`。需要时才扩展 |
| 全局系统代理 | iOS/Android 无法设置系统级代理；macOS 上可作为可选项 |
| 出口候选的动态公告 | ZT 地址恒定，静态配置足够（§5.1） |
| 地址发现 / NAT 穿透 | **ZeroTier VL1 已经做了**，见下 |

### 关于「地址发现」

早期设计中 ztio 曾计划自己做对端候选地址交换与路径选择。**这部分已被证伪并删除** ——
ZeroTier VL1 已经完整实现：

- `node/Peer.cpp:212` —— `VERB_PUSH_DIRECT_PATHS` 每 15 秒把本机接口地址 + 公网外部地址推给对端
- `node/IncomingPacket.cpp:1400` —— 收到后 `attemptToContactAt` 直接尝试直连
- `node/IncomingPacket.cpp:736` —— `_doRENDEZVOUS` 先发低 TTL 垃圾包打开本地 NAT / 状态防火墙
- `osdep/PortMapper.cpp` —— uPnP / NAT-PMP 端口映射

**libzt 也一样**：`libzt/src/NodeService.cpp:474-488` 枚举本机接口地址并喂给 ZeroTier 核心。
内嵌 libzt 的 app 天然享有完整的 VL1 直连能力。

ztio **不重复实现这一层**。
