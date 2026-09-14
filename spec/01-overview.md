# 01 · 总览

## 1.1 问题

两台设备（同一用户的 Mac 与手机）各跑一个自有 App，需要它们之间建立可靠连接。现实约束：

1. **两者经常在同一个局域网内**（家庭 / 办公 / 酒店 WiFi）—— 此时走公网绕一圈是浪费
2. **受限网络普遍屏蔽组播/广播** —— mDNS、SSDP、UDP 广播发现全部失效
3. **可达性取决于双方的地址族与网络类型** —— 这是最核心的约束，见 §1.2
4. **不能依赖需要公网暴露的发现服务** —— 增加运维负担、单点、且会把内网拓扑送到公网
5. **不能要求用户手动填 IP** —— 局域网 IP 由 DHCP 分配、随时变化

---

## 1.2 连通性的现实：IPv4 与 IPv6 是两个不同的问题

> **关键认识：IPv4 和 IPv6 面临的不是同一个问题，而真正麻烦的那个只存在于 IPv4。**

| | IPv4 | IPv6 |
|---|---|---|
| 地址变换 | ✅ 有 NAT，且常有多层 | ❌ **没有 NAT**（NAT66 罕见） |
| 地址稀缺 | ✅ 稀缺 → CGNAT / 多层 NAT | ❌ 每个主机一个 `/128` |
| 入站阻断机制 | NAT **加** 防火墙 | **仅防火墙** |
| 打洞涉及什么 | 地址变换 **加** 开洞 | **只涉及开洞** |
| 打洞是否确定 | ❌ 取决于 NAT 类型，不可预测 | ✅ **确定、可预测** |

### IPv4：真正的问题是「对称型 NAT」

| NAT 类型 | 打洞 | 特征 |
|---|---|---|
| Full-cone（全锥形） | ✅ 能 | **Endpoint-Independent Mapping** |
| Address-restricted cone | ✅ 能 | 同上 |
| Port-restricted cone | ✅ 能 | 同上 |
| **Symmetric（对称型）** | ❌ **不能** | 对每个目标给出**不同**的映射，打洞失效 |

**锥形 NAT 之所以能打洞，是因为它们映射一致**；对称型每个目标一个映射，所以打洞在原理上就不成立。

**而对称型恰恰是主流默认配置**：

> Symmetric NAT-like issues, **which is the majority of default NAT configuration out there,
> even on CGNAT products** —— [ipSpace.net](https://blog.ipspace.net/2025/04/response-nat-traversal)

**另一类失败场景是「同一个 NAT 后的两台设备」**：此时打洞要求路由器支持 NAT hairpin（loopback）。
它**不是**「必然失败」，而是**依赖路由器能力**，因此不可预测。
不过这一类场景会被 §1.2 的 IPv6 路径自然绕过。

### IPv6：没有 NAT，但仍然要打洞

**「有 IPv6」不等于「天然可达」。** 两端的**状态防火墙**仍然阻断未请求的入站连接：

- **家用路由器**：默认阻断所有未请求的入站 IPv6 ——
  "Routers aimed at the consumer space **block inbound IPv6 by default**,
  you have to explicitly open it up"
  （[IPv6 防火墙配置](https://oneuptime.com/blog/post/2026-03-20-ipv6-firewall-home-routers/view)）
- **移动运营商**：普遍对入站连接做过滤

**但 IPv6 的打洞要简单得多**，因为不涉及地址变换：

> There are **two separate problems** with IPv4 and **only one applies to IPv6**.
> Allowing incoming connections through a restrictive firewall is applicable to both.
> **Address mangling via NAT applies only to one.**
> —— [Hacker News](https://news.ycombinator.com/item?id=47384032)

> Decent stateful firewalls match on the full 5-tuple, which is functionally equivalent to symmetric NAT,
> but **don't change the UDP port numbers** when packets traverse them, so it's easier to discover
> what hole your peer punched in their firewall. **Firewall hole punching only involves STUN, and that's it.**
> —— [ipSpace.net](https://blog.ipspace.net/2025/04/response-nat-traversal)

**结论**：IPv6 的入站阻断是**有状态防火墙**，靠**双方同时发起**即可打通。
不涉及地址变换，因而是**确定的、可预测的** —— 这与 IPv4 对称型 NAT 的「原理上不可行」有本质区别。

### 现实中的典型组合

| 设备 | 典型网络 | IPv4 | IPv6 |
|---|---|---|---|
| 手机 | WiFi 或蜂窝 | 对称型 NAT / CGNAT | ✅ 通常有 |
| 家庭服务器 | 家宽 | 锥形 NAT | ✅ 通常有 |
| Mac | 任意 | 视网络 | ✅ 通常有 |

**两端都有 IPv6 是常态。** 这意味着通常存在一条 **IPv6 直连**路径，
只要双方同时发起就能打通 —— 而它比任何 NAT 打洞都更确定。

---

## 1.3 问题的本质

被混淆的两件事：

| 层 | 问题 | 典型机制 | 受限网络下 |
|---|---|---|---|
| **发现** | 对方的**地址**（局域网 IP / IPv6）是多少？ | mDNS / 广播 / 服务器下发 | ❌ **最常被屏蔽** |
| **可达** | 往那个地址发包能到吗？ | 单播 TCP/UDP（可能需同时发起） | ✅ **多数情况下是好的** |

**ztio 的大部分工作，是用一条能通的通道（ZT 链路）去送「发现」这一层的信息，
从而让本来就没坏的「可达」这一层发挥作用。**

> **注意 §1.2 带来的一个修正**：可达这一层**并非无条件好**。
> 在 IPv6 路径上，两端的防火墙都要求**出站包先开洞** ——
> 所以 ztio 的探测必须**双向同时发起**，而不是单向 `connect`。
> 见 `06-path-selection.md` §6.2。

---

## 1.4 目标

| # | 目标 |
|---|---|
| G1 | **IPv6 直连优先**，其次是局域网直连，都不经过任何公网绕行 |
| G2 | 直连不可用时**自动降级**到 ZeroTier，功能不中断 |
| G3 | **零手动配置** —— 不填 IP、不填端口、不配对 |
| G4 | **零公网服务依赖** —— 除自建 planet 外不需要任何额外服务 |
| G5 | 对上层暴露稳定 API：`connect(peerName)` |
| G6 | 网络变化（换 WiFi / 切换网络）**自愈** |
| G7 | 可被第三方复用 —— 换一套部署即可运行 |

> **G1 的顺序依据**：IPv6 没有 NAT，路径确定；局域网还要看 AP 是否隔离。
> 两者都比 ZT 便宜，具体取舍由 §1.5 的路径优先级和实测 RTT 决定。

## 1.5 非目标

| # | 非目标 | 说明 |
|---|---|---|
| N1 | 不实现自己的 P2P 传输栈 | 直接复用 ZeroTier，不自研 |
| N2 | **不做 NAT 穿透算法** | ZT 负责；ztio 只做**地址交换与路径选择** |
| N3 | 不做虚拟网卡 / 系统级路由 | 全部在应用层，运行于用户态 |
| N4 | 不依赖系统 VPN API | 移动端 VPN API 限制多（不支持组播/广播） |
| N5 | 不做跨用户的公共网络 | 面向单用户 / 单组织的私有部署 |

> **N2 值得强调**：ztio **不实现打洞**。
> 它把「双方同时发起」当作探测方式的一部分（见 06），但穿透与否交给底层。

## 1.6 前提假设

| # | 假设 | 不成立时 |
|---|---|---|
| A1 | ZeroTier 链路**至少能经 relay 建立** | 整个方案不成立（无信令通道） |
| A2 | 各设备可访问自建 planet | 同上 |
| A3 | 设备能枚举自己的网络接口 | 无候选，退化为纯 ZT 路径 |
| A4 | 用户已授予局域网访问权限 | 退化为纯 ZT 路径（正常降级，非错误） |

> **A1 是整个设计的地基。** 正因为 ZT 提供了「最差也能通」的通道，
> 才使得「不需要任何公网发现服务」成为可能。

---

## 1.7 设计约束

| 约束 | 来源 | 影响 |
|---|---|---|
| 直连探测超时 ≤ 2s | 用户感知 | 参数表（见 07） |
| 必须并行发起，不能串行 | 同上 | 选路策略（见 06） |
| **探测必须双向同时发起** | IPv6 状态防火墙（见 §1.2） | 探测方式（见 06 §6.2） |
| 候选集必须含 IPv6 | **IPv6 没有 NAT，是最确定的路径** | 枚举规则（见 04） |
| 地址不得离开设备间加密链路 | 隐私 | 信令设计（见 05 / 09） |
| 权限被拒不得报错 | 移动端生态 | 状态机（见 05 / 08） |

---

## 1.8 术语表

| 术语 | 英文 | 定义 |
|---|---|---|
| **对端** | Peer | 你想要连接的另一台设备 |
| **名字** | peerName | 稳定的逻辑标识（如 `nas`、`phone`），不是 IP |
| **锚点** | Anchor | ZT 网络内地址固定、提供名字解析的节点 |
| **花名册** | Roster | 锚点维护的「名字 → ZT 地址」映射 |
| **候选** | Candidate | 一台设备对外可被连接的一个具体地址（IP + 端口 + 类型） |
| **候选集** | Candidate Set | 一台设备的全部候选 |
| **信令** | Signaling | 两端经 ZT 链路交换候选集与状态的过程 |
| **连通性检查** | Connectivity Check | 对一个候选发起的**双向**连接尝试 |
| **同时发起** | Simultaneous Open | 两端各自向对方发起，用于穿过状态防火墙 |
| **路径** | Path | 一条已确认可用、正在承载数据的连接 |
| **路径优先级** | Path Priority | IPv6 直连 / 局域网 > ZT DIRECT > ZT RELAY |
| **降级** | Fallback | 主路径失败后切换到次优先路径 |

---

## 1.9 文档索引

| 文档 | 内容 |
|---|---|
| `02-architecture.md` | 分层架构、四个抽象、组件职责 |
| `03-bootstrap.md` | 锚点模型、花名册协议 |
| `04-candidates.md` | 候选枚举规则与过滤条件 |
| `05-signaling.md` | 消息格式、连接状态机 |
| `06-path-selection.md` | 探测（含同时发起）、选路、降级、自愈 |
| `07-parameters.md` | 超时 / 重试 / 阈值 |
| `08-platform-notes.md` | iOS / Android / macOS 差异与权限 |
| `09-security.md` | 信任模型、隐私、威胁分析 |
