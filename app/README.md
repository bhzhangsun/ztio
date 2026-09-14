# app —— ztio 客户端

一个 Flutter 工程，包含 **platform plugin** 与**多端应用**。

核心能力只有一个：**出口代理（exit proxy）** —— 把流量交给网络里的另一台设备，由它替你出网。
设计见 [`../docs/exit-proxy.md`](../docs/exit-proxy.md)。

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
| **V4** | libzt 与 zerotier-one **1.14.1** 的 `vProto` 兼容性 | **高** | 检查成员对象的 `vProto` 字段 |
| V5 | 移动端 libzt 是否支持 `local.conf` 的 `bind` | 中 | 实测 |

> **V1、V2、V4 是一票否决级的。** 建议在写任何 UI 之前先验证这三项 ——
> 它们决定整个工程结构，越晚发现代价越大。

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

| 阶段 | 内容 | 前置 |
|---|---|---|
| **0** | 验证 V1 / V2 / V4 | —— |
| **1** | 两台桌面端（macOS ↔ Linux）跑通出口代理：客户端经 ZT 连到出口，出口用宿主机栈出网 | V1、V4 |
| **2** | 出口侧加上配额、条件、允许名单、流量统计 | 阶段 1 |
| **3** | 接入移动端：出站绑定到蜂窝、权限、后台生命周期 | V2 |
| **4** | macOS 状态栏应用 | 阶段 3 |

> **阶段 1 失败即结论。** 如果两台设备在协议正确的前提下仍连不通，
> 那说明环境比预期更严 —— 此时应当重新评估方案，而不是继续加功能。

各平台的具体差异见 [`platform.md`](platform.md)。
