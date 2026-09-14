# 08 · 平台差异与权限

**这一节是产品化最大的坑。** 协议正确但权限没处理，功能会「静默失效」。

## 8.1 权限矩阵

| 平台 | 权限 | 触发时机 | 被拒后果 | 可否再次申请 |
|---|---|---|---|---|
| **iOS 14+** | 本地网络（Local Network） | 首次访问局域网地址 / 使用 mDNS | **局域网路径静默失效** | 只能引导去设置页 |
| **macOS 15+** | 本地网络 | 同上 | 同上 | 同上 |
| **Android 13+** | `NEARBY_WIFI_DEVICES` | 使用 NSD / 部分局域网 API | 发现能力受限 | 可再次申请 |
| **Android 12+** | `BLUETOOTH_*`（若需要） | —— | —— | —— |
| **所有平台** | 网络状态读取 | 枚举接口 | 无法构建候选集 | 通常免申请 |

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

## 8.2 本地网络权限的处理原则

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
| 权限被拒 | 系统 API 明确返回拒绝状态；**或**局域网探测全部立即失败 |
| 客户端隔离 | 局域网探测**全部超时**（无 RST），且权限已授予 |

见 `07-parameters.md` §7.8 的指标区分。

---

## 8.3 接口枚举

| 平台 | 方式 | 注意 |
|---|---|---|
| **iOS / macOS** | `getifaddrs()` | 需过滤 `utun*`（含 libzt 自己的接口） |
| **Android** | `NetworkInterface.getNetworkInterfaces()` | 需过滤 `tun0`、`zt*` |
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

## 8.4 出口接口绑定（应用层，与 ztio 正交）

用户可能会要求「手机连着 WiFi，但用蜂窝 IPv6 出去」。**这是 App 的责任，ztio 不介入。**

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

## 8.5 后台与生命周期

| 平台 | 限制 | 应对 |
|---|---|---|
| **iOS** | 后台 socket 会被挂起 | 用 `NWPathMonitor` 感知网络变化；回前台时**强制重新枚举+重探测** |
| **Android** | Doze / 后台限制 | 前台服务；`registerDefaultNetworkCallback` |
| **macOS** | 无明显限制 | —— |

**关键**：**回前台时必须强制重新协商**，不能信任后台期间的候选集。
手机在后台期间切换过网络是常态。

---

## 8.6 各平台能力速查

| 能力 | Android | iOS | macOS |
|---|---|---|---|
| 枚举本机接口 | ✅ | ✅ | ✅ |
| 局域网单播连接 | ✅ | ✅（需权限） | ✅（需权限，15+） |
| 网络变化回调 | ✅ | ✅ | ✅ |
| 绑定出口接口 | ✅ 进程级 | ⚠️ 仅 Network.framework | ✅ |
| libzt 支持 | ✅ | ✅ | ✅ |
| 后台保持连接 | ⚠️ 需前台服务 | ❌ 会被挂起 | ✅ |
