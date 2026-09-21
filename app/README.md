# app —— ztio 客户端

一个 Flutter 工程，包含 **platform plugin** 与**多端应用**。

## 客户端有三件事，出口代理只是其中一件

> ⚠️ **这份文档曾经写「核心能力只有一个：出口代理」。那是错的。**
> 按 [`../server/Spec.md`](../server/Spec.md) 的排序，出口代理是 **F4 / P2 —— 最低优先级**，
> 而排在最前面的是「**入网 + DNS**」（F1a）。

| # | 职责 | 对应特性 | 优先级 |
|---|---|---|---|
| 1 | **入网与身份** | 零账号加入自建网络 · 授权状态可见 | **P0** |
| 2 | **DNS 客户端** | F1a：同一个名字在家和在外都能用 | **P0** |
| — | 可观测性 | 路径 / 授权 / DNS / 流量 | P1 |
| 3 | 出口代理 | F4：把流量交给网络里另一台设备出网 | P2 |

设计见 [`../docs/exit-proxy.md`](../docs/exit-proxy.md)（那只覆盖第 3 项）。

**客户端明确不做的**：

| 不做 | 为什么 |
|---|---|
| 四级路径选路（F2） | ZeroTier VL1 已经做了，只需**可观测**（见 §6） |
| 公网入口（F3） | 服务端的事 |
| 家庭侧 DNS 服务器（F5） | 用户自己在家里跑（见 [`../server/README.md`](../server/README.md#dns)） |
| controller 管理面 | 客户端只消费，不管理 |

| 目标平台 | 形态 |
|---|---|
| **macOS** | **状态栏（menubar）应用** —— 常驻，无主窗口 |
| iOS | 常规 App |
| Android | 常规 App |

**为什么 macOS 是状态栏应用**：出口代理要求至少有一台设备**常开且网络稳定**，
Mac 通常就是那台。状态栏形态让它无需窗口即可常驻，并一眼看到当前是不是出口、被用了多少流量。

---

## 1. 目录

```
app/
├── pubspec.yaml
├── lib/
│   ├── proxy/                    ★ 代理引擎（客户端 + 出口两侧，三端共享一份）
│   │   ├── socks5.dart           SOCKS5 服务端/客户端
│   │   ├── exit_server.dart      出口：libzt 监听 → 宿主机栈出站
│   │   ├── exit_client.dart      客户端：本地入口 → libzt 连接出口
│   │   ├── policy.dart           配额、条件、允许名单
│   │   └── stats.dart            流量统计
│   ├── ui/
│   │   ├── macos/                状态栏菜单 + 设置窗口
│   │   └── mobile/               iOS / Android 页面
│   └── main.dart
├── packages/
│   └── ztio/                    ★ Flutter platform plugin
│       ├── lib/ztio.dart         设备配置、出口选择
│       ├── android/              Kotlin
│       ├── ios/                  Swift
│       └── macos/                Swift
└── platform.md                   各平台权限与实现差异
```

---

## 2. 两个角色

同一台设备可以同时是两者的任意组合，也可以都不是（那就只是普通 ZT 节点）。

### 出口（exit）—— 别人借我的网

```
libzt: bind/listen/accept  ──→  宿主机栈: Socket.connect  ──→  真实目标
      ↑ 对方在 ZT 网内                     ↑ 走我自己的网络（蜂窝）
```

**关键在第 2 步**：接受连接用 libzt（因为对方在 ZT 网内），
发起连接用**宿主机栈**（`dart:io` 的 `Socket.connect`），因为目标是公网。

因为内嵌 libzt 时系统里**没有 ZT 虚拟网卡**，宿主机栈的连接会自然走本机的正常网络。

### 客户端 —— 我借别人的网

```
用户 app ──→ ztio 本地入口 ──→ libzt 连到出口的 ZT 地址 ──→ 出口
              ↑ SOCKS5
```

两种入口形态，都做：

| 形态 | 适用 | 说明 |
|---|---|---|
| **Dart API** | 集成 ztio 的 app | `ztio.connect(host, port)` 直接返回 socket |
| **本地 SOCKS5** | 未改造的 app | 桌面端监听 `127.0.0.1:<port>`，任何支持 SOCKS5 的程序都能用 |

移动端只做第一种（iOS/Android 无法设置系统级代理）。

### 用哪个 planet

**官方客户端没有「自定义 planet」功能** —— 移动端官方 app 的数据目录在沙箱里，无处可放。
所以 app **必须自带** planet，否则用户连不上我们自己的网络：

```
resources/planet                  ← 仓库根目录，随 app 打包
```

完整说明、文件格式、以及「什么情况下换 planet 会被静默拒绝」见
[`../resources/README.md`](../resources/README.md)。两条要点：

1. **`zts_init_set_roots()` 必须在 `zts_node_start()` 之前调用** —— 顺序错了会静默退回官方根
2. **要同时关掉根缓存**（`zts_init_allow_roots_cache(0)`），否则已装用户缓存着旧 planet，
   你推的新 planet 会因为验签不过被**默默拒掉**，而用户以为更新成功了

---

## 2.5 入网与身份（P0）—— 零账号体验的落点

**这是「用户可以完全不知道 ZeroTier 存在」在客户端侧的实现。** 加入一个自建 controller
的网络**不需要任何 ZeroTier 账号**，所以 app 只需三步：

```
① 内置 planet         zts_init_set_roots(planet_bytes, len)   ← 必须在 node_start 之前
② 加入网络            zts_net_join(nwid)                       ← 零账号
③ 显示自己的设备地址   zts_node_get_id()                        ← 用户拿它去换授权
```

> **API 已从 libzt 头文件核实**：
> ```c
> ZTS_API int zts_init_set_roots(const void* roots_data, unsigned int len);
> ```
> 注释写明 **"(binary)"** —— 也就是 **`resources/planet` 的原始字节**，不需要转成别的结构体。
> `zts_init_allow_roots_cache(unsigned int allowed)` 也已核实存在。

### 授权状态机：别自己发明，libzt 直接给了

`zts_net_get_status(nwid)` 的返回值就是完整状态机：

| 值 | 含义 | UI 该说什么 |
|---|---|---|
| `..._REQUESTING_CONFIGURATION` = 0 | 已加入，正在请求配置（**= 等待授权**） | 「等待管理员授权 —— 把这个地址发给他」 |
| `..._OK` = 1 | 已授权，拿到地址 | 「已连接」 |
| `..._ACCESS_DENIED` = 2 | **被拒绝**（未授权，或被踢出） | 「被拒绝了，联系管理员」 |
| `..._NOT_FOUND` = 3 | 网络不存在 | 「网络 ID 不对」 |
| `..._PORT_ERROR` = 4 | 端口问题 | 罕见，转诊断 |
| `..._CLIENT_TOO_OLD` = 5 | **客户端版本太旧** | 「请升级 app」 |

★ **状态 0 和状态 2 必须区分清楚。** 两者都表现为「连不上」，但一个要**等**、一个要**找人**。
混在一起的话用户永远不知道该做什么 —— 而这正是零账号流程里最需要讲清楚的一件事。

> 状态 5 是个容易忘的坑：服务端升级后老版本 app 会被明确拒掉。
> 这反而是好事 —— 它给了你一个能说清楚的错误，而不是静默失败。

配套要接的事件：`ZTS_EVENT_STORE_NETWORK`、`ZTS_EVENT_ADDR_ADDED_IP4` / `_REMOVED_IP4`、
`ZTS_EVENT_STORE_PLANET`、`ZTS_EVENT_PEER_PATH_DISCOVERED` / `_DEAD`（最后两个给可观测性用）。

---

## 2.6 DNS 客户端（P0）—— F1a 的另一半，且必须自己写

### 先说一件容易搞错的事：`allowDNS` 在 libzt 里不存在

| | 官方客户端 | **libzt（= 你的 app）** |
|---|---|---|
| 开关 | `networks.d/<nwid>.local.conf` 的 `allowDNS=0/1`，**逐网络、默认关** | **头文件里没有任何对应开关** |
| 谁去改系统 DNS | ZeroTier 服务自己（Win / macOS / Android / iOS 实现了） | **你** —— libzt 是库，不会碰宿主机 resolver |
| search domain | 支持 | **头文件里没有 domain / search 相关 API** |

**所以这不是「能不能默认开」的问题，是你必须自己实现这段逻辑 —— 不写就永远解析不了。**

> 顺带澄清：`local.conf` 的**系统级** `settings` 块里**也没有** `allowDNS`
> （只有 `primaryPort` / `secondaryPort` / `tertiaryPort` / `portMappingEnabled` /
> `forceTcpRelay` / `interfacePrefixBlacklist` / `allowManagementFrom` /
> `allowTcpFallbackRelay` / `bind`）。**连官方客户端都没法全局默认开。**

### ⚠️ libzt 的 `zts_dns_set_server()` 不是你要的那个

```c
ZTS_API int zts_dns_set_server(uint8_t index, const zts_ip_addr* addr);
ZTS_API const zts_ip_addr* zts_dns_get_server(uint8_t index);
```

它配的是 **libzt 内部自己的 resolver**（lwIP 的 DNS 客户端）—— 只对
`zts_bsd_gethostbyname()` 和「带域名的 `zts_net_connect()`」这类**库内部**解析生效。

**它不影响宿主机的浏览器、curl、任何普通 app。**

### 所以 app 要做两件独立的事

| 目标 | 做法 | 用户能感觉到吗 |
|---|---|---|
| **A. libzt 内部能按名字连** | `zts_dns_set_server(0, <DNS 地址>)` | 间接（出口代理连对端时用） |
| **B. 宿主机 app 能按名字访问** | 改系统 DNS：macOS/iOS `NEDNSSettings` + `matchDomains`；Android `VpnService` DNS | ✅ **这才是用户看到的那件事** |

### ⚠️ 必须 split DNS，绝不能全局设

DNS 服务器（`172.16.0.1`）**只在 ZT 网内可达**。设成全局 DNS 的后果：

> 用户**离开家或断开 ZT 的那一刻，整台设备的域名解析全挂** —— 包括他正在打开的网页。

所以必须限定到 `dns.domain` 这一个域：
- macOS / iOS：`NEDNSSettings.matchDomains = ["ztio.internal"]`
- Android：`VpnService` 的 per-domain DNS

### ⚠️ 「网络下发的 DNS 是空的」会怎样 —— 什么都不会发生

**这是安全的**，而且**是你现在的常态**：`ztio-dns` 已删，DNS 服务器由用户自己在家里起，
在他起起来之前，网络里就是没有 `dns.servers`。

| 网络配置 | app 应该做什么 |
|---|---|
| domain 空 + servers 空 | **什么都不做**，正常 |
| domain 空 + servers 有值 | 可设 resolver，不设 search domain |
| **domain 有值 + servers 空** | ⚠️ **什么都别做** —— 只设 search domain 会让「半个域名」去问公网 DNS，产生极难排查的怪异解析 |
| domain 有值 + servers 有值 | ✅ 两个都设 |

★ **判据：`servers` 非空才动作。** 只看 `domain` 会踩上面第三行。

### ❓ 一个还没解决的设计问题：app 怎么知道 DNS 在哪

libzt **没有** `dns.servers` 的 getter —— `zts_net_get_*` 只有
name / status / type / mac / mtu / broadcast。

**但掩码是有的**（这条之前写错过，已更正）。`zts_net_info_t.assigned_addrs[]` 的注释：

```c
/**
 * ZeroTier-assigned addresses (in sockaddr_storage structures)
 *
 * For IP, the port number of the sockaddr_XX structure contains the number
 * of bits in the address netmask. Only the IP address and port are used.
 */
struct zts_sockaddr_storage assigned_addrs[ZTS_MAX_ASSIGNED_ADDRESSES];
```

**掩码藏在 `sockaddr` 的 `port` 字段里。** 所以「自己 IP + 掩码 → 网段第一个地址」
这条路是通的。`zts_net_info_t` 通过事件的 `msg->network` 拿到
（`ZTS_EVENT_STORE_NETWORK` / `ZTS_EVENT_ADDR_ADDED_IP4`）。

| 方案 | 评价 |
|---|---|
| **A. 解析 `STORE_NETWORK` 的 blob** 拿 `dns.domain` + `dns.servers` | 拿得到**真正的下发值**（换域名也对），但 blob 编码格式未验证 → **V7** |
| **B. 从 `assigned_addrs` 推出网段第一个地址** | ✅ **可行**（掩码在 `port` 里）。**纯派生、零配置**，符合 §2.4-b。但隐含「DNS 在网段第一个地址」这个约定 |
| C. 硬编码 `172.16.0.1` | 单网络可用，**换个子网就废** |
| D. 邀请链接 / 二维码里带上 DNS 地址 | 那是「配置的」不是「派生的」，违反 §2.4-b |

**建议：B 为主（它和服务端 `dns_ip_from_routes()` 是同一条约定，两边不会漂移），A 作为补充用来拿 `dns.domain`。**
`dns.domain` 拿不到的话，`matchDomains` 也没法设 —— 那 split DNS 就无从谈起，所以 A 至少得解决 domain 那一半。

---

## 2.7 ⚠️ 一个会改变客户端形态的事实：libzt 是用户态 socket 库，不建系统网卡

> **这一节的结论如果成立，「客户端要做成什么」会变。** 先看证据，再看后果。

### 证据

| 证据 | 内容 |
|---|---|
| libzt README 自述 | *"P2P cross-platform encrypted **sockets library** using ZeroTier"* |
| 头文件里是**整套自己的 socket API** | `zts_bsd_socket` / `connect` / `bind` / `listen` / `accept` / `read` / `write` … —— **app 调的是这些，不是系统 `socket()`** |
| 头文件里**没有**任何建网卡的 API | `zts_init_*` 一共 19 个，**没有一个**跟接口创建有关 |
| `ZTS_EVENT_NETIF_*` | 注释写着 *(for debug purposes)* —— 那是 lwIP 内部的 netif，不是系统接口 |
| **没有原始包注入接口** | 这条决定了能否与 `VpnService` / `NEPacketTunnelProvider` 对接 |

### 后果

**libzt 里的 ZT 地址，只有 libzt 自己的 socket 能访问。**

| 功能 | libzt 够不够 |
|---|---|
| **F4 出口代理** | ✅ **够** —— 全程是 app 自己的进程在用 socket |
| **F1a 内网名字** | ❌ **不够** —— 你的**浏览器打不开 `nas.ztio.internal`** |

原因很直接：**宿主机协议栈没有到 `172.16.0.0/24` 的路由。**
DNS 就算解析出来了，`172.16.0.10` 这个地址也没有人送得到。

**而 F1a 恰恰是 Spec 的 P0，也是你最初的痛点。**

### 所以 F1a 在客户端侧只有三条路

| 路线 | 桌面 | 移动端 | 评价 |
|---|---|---|---|
| **甲：libzt 用户态** | ❌ | ❌ | F1a 不成立 |
| **乙：自己写系统 VPN**（macOS/iOS `NEPacketTunnelProvider`、Android `VpnService`） | ✅ | ✅ | **唯一能全平台兑现 F1a 的路**。代价：工作量大得多；iOS 需要 Network Extension entitlement（**要 Apple 批准**） |
| **丙：让用户装官方客户端** | ✅ 可行 —— 桌面端能手动放 `planet` | ❌ **不行** —— 官方移动端 app 数据目录在沙箱里，**放不进自定义 planet**（见 §2「用哪个 planet」） | 最省事，但移动端直接堵死 |

★ **合起来看：移动端要兑现 F1a，只能走乙 —— 也就是自己写一个 ZeroTier 客户端。** 这不是小工程，得在排期里正视。

> **这条必须先验证（§6 的 V8），它比 V1 更靠前。**
> V1 决定「libzt 能不能用」，V8 决定「**客户端到底要做成什么形态**」。
> 如果 V8 的结论如证据所示，那么 `platform.md` §4 那一整套「出站链路绑定」的讨论
> 在系统 VPN 形态下会**完全不同** —— 因为那时流量已经走系统路由，绑定是另一套做法。

---

## 3. 分层：什么共享，什么分叉

> **SDK 是协议，各平台的授权和实现必然不同。**
> 所以问题不是「要不要分叉」，而是**把分叉限制在哪一层** —— 分叉得越靠上，维护成本越低。

| 层 | 位置 | 份数 | 理由 |
|---|---|---|---|
| UI | Dart | 3（分支） | Flutter 的本职 |
| **代理引擎**（SOCKS5、配额、统计） | **Dart** | **1** | 纯逻辑，无平台 API。这是项目的核心价值 |
| libzt 绑定 | Dart FFI | 1 | libzt 是 C API |
| **出站链路绑定** | **原生** | **3** | API 完全不同，且**这是本项目的成败点**（见 §4） |
| **权限申请** | **原生** | **3** | 授权声明本身就是平台机制 |
| 生命周期 / 网络变化回调 | 原生 | 3 | 各平台机制不同 |

原生对 Dart 暴露的原语：

```
listUplinks()              → [ {name, addrs[], kind: wifi|cellular|ethernet} ]
bindOutbound(socket, uplink) → ok | unsupported
onNetworkChanged()         → stream
```

---

## 4. ⚠️ 出口侧必须把出站绑到正确的链路

**这一节是整套设计里最容易静默失败的地方。**

场景：手机连着**那条受限 WiFi**，用户希望流量从**蜂窝**出去。

如果出口只是简单地 `Socket.connect`，它会走**系统默认路由**，
也就是那条受限 WiFi —— **代理绕了一圈，出口还是那条出不了的网，而且从外部完全看不出来。**

所以出口在发起出站连接时，必须显式绑定到预期的那条链路：

| 平台 | 绑定能力 | 说明 |
|---|---|---|
| **Android** | ✅ 完整 | `ConnectivityManager.requestNetwork(TRANSPORT_CELLULAR)` → `bindProcessToNetwork()`；进程级，libzt 的 socket 也一起绑 |
| **iOS** | ⚠️ 受限 | `NWParameters.requiredInterfaceType = .cellular`，**仅 Network.framework**；`dart:io` 的 `Socket` 用不了 |
| **macOS** | ✅ | 默认路由通常就是对的 |

**iOS 的限制是本项目最大的实现风险**：出口的出站必须走 `Network.framework`，
而 `dart:io` 的 `Socket` 没有这个能力。可选路径：

1. 出口的出站连接下沉到 Swift（`Network.framework`），Dart 只负责 libzt 一侧
2. 用 `NEPacketTunnelProvider`（需额外 entitlement，且要 Apple 审核）
3. 让用户在系统里配置

详见 [`platform.md`](platform.md) §4。

### Android 的已知问题

系统默认在 **WiFi 连上时会拆掉蜂窝数据连接**以省电。症状：
蜂窝图标亮着，但 `NetworkCapabilities` 查不到可用的蜂窝 `Network`。

`requestNetwork(TRANSPORT_CELLULAR)` 会让系统知道你需要它，但部分机型还依赖
开发者选项里的「始终开启移动数据」—— **不能用开发者选项作为产品方案**，必须实测覆盖目标机型。

---

## 5. 权限

完整矩阵见 [`platform.md`](platform.md) §1。最容易踩的一条：

**iOS 14+ / macOS 15+ 的本地网络权限被拒后，局域网路径会静默失效。**
用户看到的是「连不上」，看不出是权限问题。

```xml
<key>NSLocalNetworkUsageDescription</key>
<string>用于在局域网内直接连接你的其他设备，避免流量绕行公网。</string>
```

**权限被拒不是错误，是一个正常的「局域网不可用」状态** —— 应当降级到 ZT 中继、
明确告知用户、并提供一键跳转设置，而不是弹一个「连接失败」。

---

## 6. 待验证的前提

**以下都还没有实测过**，列出来是为了不把它们当成既成事实：

| # | 待验证 | 风险 | 验证方式 |
|---|---|---|---|
| **V1** | Dart FFI → libzt 在 iOS / Android 上可行 | **高（一票否决）** | 最小工程：`zts_node_start` 能被 Dart 调用并返回 |
| **V2** | 出口能否把出站绑到指定链路 | **高** | Android `bindProcessToNetwork` 后抓包确认走蜂窝；iOS `NWParameters` 同理 |
| V3 | Android 13+ `NEARBY_WIFI_DEVICES` 对局域网单播的实际限制 | 中 | 实测被拒后单播是否仍可用 |
| ~~V4~~ | ~~协议兼容性~~ | ✅ **已验证（2026-09-14）** | 见下方「V4 结论」 |
| V5 | 移动端 libzt 是否支持 `local.conf` 的 `bind` | 中 | 实测 |
| ~~V6~~ | ~~`zts_init_set_roots` 的 `roots_data` 是 planet 二进制还是 `zts_root_set_t`~~ | ✅ **已解答（读头文件）** | `zts_init_set_roots(const void* roots_data, unsigned int len)`，注释写明 **"(binary)"** → **就是 planet 原始字节**。只剩实测确认「World 文件原样喂进去」 |
| **V7** | `ZTS_EVENT_STORE_NETWORK` 的 `cache` blob 是什么编码，能不能从里面取出 `dns.domain` | 中 | 抓事件 payload 看格式 —— 头文件只写 *"network configs"*，**没写编码** |
| **V8** | **libzt 是否真的不建系统网卡、也无法与 `VpnService` / `NEPacketTunnelProvider` 对接** | **极高 —— 决定客户端形态** | 最小工程：`zts_node_start()` 后 `ifconfig` / `ip addr` 看有没有多出接口；再确认有没有原始包 fd 可拿 |

> **V8 现在排在最前面，顺序建议是 V8 → V1 → V2。**
> V1 决定「libzt 能不能用」，而 V8 决定「**客户端做成什么形态**」——
> 后者会推翻前者的结论。如果 libzt 确实不建网卡，那么**无论 V1 成不成立，F1a 都得靠系统 VPN**，
> 而 V2（出站链路绑定）的讨论也要重写。
>
> **V1、V2 是一票否决级的**（V4、V6 已排除）。建议在写任何 UI 之前先验证 ——
> 它们决定整个工程结构，越晚发现代价越大。

### V4 结论：协议兼容，实测通过

**2026-09-14 实测，不需要再验证。** 官方客户端 **1.16.2** 连自建 **1.14.2** controller：

```
controller 记录到的成员对象
  vMajor   1
  vMinor   16
  vProto   13        ← 客户端自己报上来的
  ...

服务器侧 listpeers
  4911bad593  123.138.183.207/57053  42  1.16.2  LEAF
```

`vProto` 能填上，说明节点**真的完成了控制层握手**（不是只发了个包）；
`LEAF` 对端说明数据层也通了，直连 42ms。所以 1.14.2 的 controller 能服务
1.16 的客户端，**跨两个次要版本没问题**。

> **但这个结论只覆盖官方 zerotier-one 客户端，不覆盖 libzt。**
> libzt 是另一份代码（`libzt/` 目录），它的 `vProto` 要单独实测 ——
> V4 原来的表述是 "libzt 与 1.14.2 的兼容性"，这里验证的是官方客户端那条路径。
> 真正写 app 时仍应先用 libzt 跑一次最小工程。

另一个实测副产品：**macOS 上 ZeroTier 用 `feth` 接口，不是 `zt`**，
而且是成对的（`feth1927` / `feth6927`，`peer:` 互指）。
排查时 `ifconfig | grep zt` 是找不到东西的。

### ⚠️ DNS 是**逐客户端**开关，默认关闭

controller 下发了 `dns.domain = ztio.internal`、`dns.servers = [172.16.0.1]`，
但官方客户端本地的实际状态是：

```
// networks.d/<nwid>.local.conf
allowManaged=1
allowGlobal=0
allowDefault=0
allowDNS=0        ← 关键：默认不接管 DNS
```

**`allowDNS=0` 意味着客户端不会用网络下发的 DNS** —— 也就是需求 #3
（"在这个网段内用域名即可访问"）在默认设置下**不生效**。

这必须由客户端一侧打开：

```bash
sudo zerotier-cli set <nwid> allowDNS=1
```

对照 libzt：**这个开关根本不存在。** 头文件里既没有 `allowDNS`，
也没有任何 domain / search 相关 API —— libzt 是库，它不会去碰宿主机的 resolver。
所以对 app 来说，这不是「打开一个开关」，而是**必须自己实现整段逻辑**。详见 §2.6。

> 这条对 app 是硬需求：**app 必须自己把网络下发的 DNS 应用到系统上**，
> 否则用户配了 DNS 却发现域名解析不了，而且看不出原因 —— controller 那边一切正常。
> 官方客户端的 `allowDNS=0` 不是 bug，是 ZeroTier 刻意的安全默认值；
> 而在 libzt 里连这个默认值都没有，**什么都没人替你做**。


**V1 不成立时的退路**：代理引擎下沉到原生，三端各一份实现。
代价是三份逻辑需要同步演进，必须配一套共享测试向量集。

### 已经不再是问题的

早期设计里 ztio 曾计划自己做**对端地址发现与选路**（枚举候选、探测、交换、降级）。
**这部分已被删除** —— ZeroTier VL1 已经完整实现了：

- `node/Peer.cpp:212` —— `VERB_PUSH_DIRECT_PATHS` 每 15 秒把本机接口地址 + 公网外部地址推给对端
- `node/IncomingPacket.cpp:1400` —— 收到后 `attemptToContactAt` 直接尝试直连
- `node/IncomingPacket.cpp:736` —— `_doRENDEZVOUS` 先发低 TTL 垃圾包打开 NAT / 状态防火墙
- `osdep/PortMapper.cpp` —— uPnP / NAT-PMP 端口映射

**libzt 也一样**（`libzt/src/NodeService.cpp:474-488` 枚举本机接口地址并喂给核心）。
所以同 WiFi 时 ZeroTier 会自动建立局域网直连 —— **不需要 app 做任何事**。

---

## 7. 上游依赖

| 依赖 | 用途 | 许可 |
|---|---|---|
| **libzt**（ZeroTier SDK） | ZT 传输层 | BSL → Change Date 已过 → **Apache 2.0** |
| Flutter SDK | UI | BSD-3 |
| `Network.framework`（iOS/macOS）、`ConnectivityManager`（Android） | 出站链路绑定 | 系统 |

```bash
./build.sh iphoneos-framework     # iOS / macOS
./build.sh android-aar            # Android
./build.sh macos-framework
```

> libzt 的 README 关于许可的描述**是过时的**（仍写着需要商业授权），
> 实际 BSL Change Date 已过，转为 Apache 2.0。

---

## 8. 路线

> **这一节改过。** 原来把出口代理放在阶段 1，但它是 Spec 的 **P2**，
> 而 Spec 的 P0（F1a 内网名字）在这条路线里**根本没有出现过**。

| 阶段 | 内容 | 前置 |
|---|---|---|
| **0** | **验证 V8 → V1 → V2**（V8 先说清客户端要做成什么形态） | —— |
| **0.5** | **形态决策**：走乙（自己写系统 VPN）还是 v1 先只做桌面 | V8 |
| **1** | **入网 + DNS 客户端**：一台 macOS 跑通「加入网络 → 应用 DNS → `nas.ztio.internal` 解析并连上」 | V1、V7 |
| **2** | 授权流程 + 状态可见（零账号体验闭环） | 阶段 1 |
| **3** | 出口代理：两台桌面端（macOS ↔ Linux）跑通，客户端经 ZT 连到出口，出口用宿主机栈出网 | V2 |
| **4** | 出口侧加配额、条件、允许名单、流量统计 | 阶段 3 |
| **5** | 接入移动端 | 阶段 0.5 的结论 + V2 |
| **6** | macOS 状态栏应用 | 阶段 5 |

**为什么把入网 + DNS 提到前面**：它**不依赖任何未验证的平台能力**
（libzt 在桌面端的 socket 能力最成熟），却能直接回答最初的痛点 ——
「在家 WiFi 和在外面 ZT 是两个网段，访问同一个服务要切 IP」。而且它是 Spec 的 P0。

**最小闭环**（建议先用它验证，一行 UI 都不写）：

```
macOS 命令行：set_roots → join → 应用 DNS → dig nas.ztio.internal
```

> **阶段 3 失败即结论。** 如果两台设备在协议正确的前提下仍连不通，
> 那说明环境比预期更严 —— 此时应当重新评估方案，而不是继续加功能。

各平台的具体差异见 [`platform.md`](platform.md)。
