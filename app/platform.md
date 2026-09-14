# 平台差异与权限

> **这是产品化最大的坑。** 协议正确但权限没处理，功能会「静默失效」——
> 而且从现象上**完全看不出是权限问题**。

**同一套协议，三端的分叉点几乎全在这里**：授权声明、出站链路绑定 API、后台策略。
设计见 [`../docs/exit-proxy.md`](../docs/exit-proxy.md)，工程结构见 [`README.md`](README.md)。

> **§4（出口接口绑定）是本项目的成败点。** 它曾经被当作「与 ztio 正交的 App 责任」，
> 但在出口代理的设计里，出口如果不把出站绑到正确的链路，整个代理等于白做 ——
> 而且从外部完全看不出问题。详见 §4。

---

## 1. 权限矩阵

| 平台 | 权限 | 触发时机 | 被拒后果 | 可否再次申请 |
|---|---|---|---|---|
| **iOS 14+** | 本地网络（Local Network） | 首次访问局域网地址 / 使用 mDNS | **局域网路径静默失效** | 只能引导去设置页 |
| **macOS 15+** | 本地网络 | 同上 | 同上 | 同上 |
| **Android 13+** | `NEARBY_WIFI_DEVICES` | 使用 NSD / 部分局域网 API | 发现能力受限 | 可再次申请 |
| **Android 12+** | `BLUETOOTH_*`（若需要） | —— | —— | —— |
| **所有平台** | 网络状态读取 | 查询链路类型 | 无法判断该把出站绑到哪条链路 | 通常免申请 |

### 必须的声明

**iOS / macOS `Info.plist`**：

```xml
<key>NSLocalNetworkUsageDescription</key>
<string>用于在局域网内直接连接你的其他设备，避免流量绕行公网。</string>
```

> **文案很重要**：这是用户唯一能看到的解释。
> 必须说清「为什么需要」，否则大多数用户会直接拒绝。

**Android `AndroidManifest.xml`**：

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
<uses-permission android:name="android.permission.NEARBY_WIFI_DEVICES"
                 android:usesPermissionFlags="neverForLocation"/>
<uses-permission android:name="android.permission.CHANGE_NETWORK_STATE"/>
```

---

## 2. 本地网络权限的处理原则

> **权限被拒不是错误，是一个正常的「LAN 不可用」状态。**

| 做法 | 评价 |
|---|---|
| 弹错误框说「连接失败」 | ❌ 用户不知道要做什么 |
| 静默降级到 ZT，什么都不说 | ⚠️ 可用，但用户不知道自己在花流量 |
| **降级 + 明确告知 + 提供一键跳转设置** | ✅ 正确 |

**UI 建议**：

```
当前走 ZeroTier 中继（较慢，消耗服务器流量）
原因：未授权访问本地网络
[ 去设置授权 ]   [ 暂不 ]   [ 不再提示 ]
```

**检测「被拒」与「隔离」的区别**：

| 现象 | 判据 |
|---|---|
| 权限被拒 | 系统 API 明确返回拒绝状态 |
| 客户端隔离 | 权限已授予，但 ZeroTier 始终选不出局域网直连路径 |

两者的表现都是「局域网直连不成立」，但原因完全不同：前者用户去设置里打开即可，
后者无解 —— 只能走公网或中继。区分方式是看 `zerotier-cli peers` 里对端的路径类型：
出现 `DIRECT` 且地址是局域网网段即说明权限没问题。

**这条判断不该由 ztio 自己做** —— ZeroTier VL1 已经在持续尝试并汇报路径状态，
重复探测只会得出同样的结论，却多一份要维护的代码。

---

## 3. 接口枚举

**用途已经变了。** 早期设计里枚举接口是为了自己构建局域网候选集 —— 那部分已删除
（ZeroTier VL1 自己做地址发现与打洞，见 [`../docs/exit-proxy.md`](../docs/exit-proxy.md) §7）。
现在枚举接口只剩一个用途：**认出哪条链路是蜂窝、哪条是 WiFi，好在出口侧把出站 socket 绑对**（见 §4）。

| 平台 | 方式 | 注意 |
|---|---|---|
| **iOS / macOS** | `NWPathMonitor` + `getifaddrs()` | 需过滤 `utun*`（含 libzt 自己的接口） |
| **Android** | `ConnectivityManager` + `NetworkInterface.getNetworkInterfaces()` | 需过滤 `tun0`、`zt*` |
| **桌面 Linux** | `getifaddrs()` 或 netlink | 过滤 `docker0`、`veth*`、`zt*` |
| **桌面 Windows** | `GetAdaptersAddresses()` | 过滤虚拟适配器 |

### 必须过滤的接口名模式

```
utun*   tun*   tap*   zt*   docker*   veth*   br-*   vmnet*   vboxnet*
```

> **这条是硬性的。** 不过滤会把 ZT 虚拟接口的地址当成 LAN 候选，
> 导致「局域网直连」静默退化为「绕 ZT」，且**从外部完全看不出来**。

### link-local IPv6 的 scope

iOS / Android / Linux 上，link-local 地址**必须带上接口标识**才能使用：

```c
// iOS / Linux
struct sockaddr_in6 { ... sin6_scope_id = if_nametoindex("en0"); }
```

```java
// Android
Inet6Address addr;  // 已带 scope，直接用
```

**丢失 scope 的 link-local 候选不可用**，枚举时必须丢弃而不是上报。

---

## 4. 出口接口绑定 ★ 本项目的成败点

这一节曾经被标为「应用层，与 ztio 正交」。**在出口代理的设计里不是这样了。**

**场景**：手机连着**那条受限 WiFi**，用户要它把流量从**蜂窝**送出去。

出口代理的第 ③ 步（见 [`../docs/exit-proxy.md`](../docs/exit-proxy.md) §3.4）是用宿主机栈
`Socket.connect` 连真实目标。如果就这么连，它会走**系统默认路由** ——
也就是那条受限 WiFi。结果是：**代理绕了一圈，出口仍然是那条出不了的网。**

而且症状极具迷惑性：连接成功、数据流通、延迟正常，**用户只是打不开网页**。
从日志上完全看不出是「出站走错了接口」。

所以出口在发起出站连接时**必须**显式绑定到预期的那条链路。
这不再是「App 的可选增强」，而是出口功能能否成立的**前提**。

| 平台 | 绑定能力 | 说明 |
|---|---|---|
| **Android** | ✅ 完整 | `ConnectivityManager.requestNetwork(TRANSPORT_CELLULAR)` → `Network` → `bindSocket()` / `bindProcessToNetwork()` |
| **iOS** | ⚠️ 受限 | `NWParameters.requiredInterfaceType = .cellular`（**仅 Network.framework**） |
| **iOS `URLSession`** | ❌ 不支持 | `allowsCellularAccess` 只是**权限开关**，不控制路由 |
| **双栈并行** | 需 MPTCP | `multipathServiceType` + Multipath Entitlement + 服务端 MPTCP 支持；**仅 TCP** |

### Android 的已知问题

系统默认在 **WiFi 连上时会拆掉蜂窝数据连接**以省电。症状：

> 蜂窝图标亮着，但 `NetworkCapabilities` 查询不到可用的蜂窝 `Network`。

- `requestNetwork(TRANSPORT_CELLULAR)` 会让系统知道你需要它
- 部分机型还依赖**开发者选项**里的「始终开启移动数据」
- **不能用开发者选项作为产品方案** —— 必须实测覆盖目标机型

### iOS 的 libzt 传输绑定

**iOS 没有进程级网络绑定 API。** libzt 的物理传输（UDP 9993）走系统默认路由（即 WiFi）。

若要让 ZT 传输也走蜂窝，唯一的办法是 ZeroTier 自己的 `local.conf`：

```json
{ "settings": { "bind": ["<蜂窝 IPv6 地址>"] } }
```

| 问题 | 说明 |
|---|---|
| 地址易变 | 蜂窝地址随基站切换变化，每次启动需重写 |
| 失去 LAN 直连 | 只绑蜂窝后，局域网路径不可用 |
| **可行性未实测** | 移动端 libzt 是否完整支持 `bind`，**需要实测验证** |

> **Android 上不存在这个问题**：`bindProcessToNetwork()` 是进程级的，
> 能把 libzt 的 socket 一起绑到蜂窝。

---

## 5. 后台与生命周期

| 平台 | 限制 | 应对 |
|---|---|---|
| **iOS** | 后台 socket 会被挂起 | 用 `NWPathMonitor` 感知网络变化；回前台时**强制重新枚举+重探测** |
| **Android** | Doze / 后台限制 | 前台服务；`registerDefaultNetworkCallback` |
| **macOS** | 无明显限制 | —— |

**关键**：**回前台时必须强制重新协商**，不能信任后台期间的候选集。
手机在后台期间切换过网络是常态。

---

## 6. 各平台能力速查

| 能力 | Android | iOS | macOS |
|---|---|---|---|
| 枚举本机接口 | ✅ | ✅ | ✅ |
| 局域网单播连接 | ✅ | ✅（需权限） | ✅（需权限，15+） |
| 网络变化回调 | ✅ | ✅ | ✅ |
| 绑定出口接口 | ✅ 进程级 | ⚠️ 仅 Network.framework | ✅ |
| libzt 支持 | ✅ | ✅ | ✅ |
| 后台保持连接 | ⚠️ 需前台服务 | ❌ 会被挂起 | ✅ |
