# app — Flutter 多端应用

ztio 协议的**参考实现**。一个 Flutter 工程，包含 **platform plugin** 与**多端应用**。

| 目标平台 | 形态 |
|---|---|
| **macOS** | **状态栏（menubar）应用** —— 常驻，无主窗口 |
| iOS | 常规 App |
| Android | 常规 App |

**为什么是「状态栏应用」**：ztio 的锚点需要一台**常开**的设备。
Mac 通常是最合适的那个 —— 它是常开的、网络稳定的、且用户就在旁边。
状态栏形态让它无需窗口即可常驻并显示连接状态。

---

## 1. 目录结构

```
app/
├── pubspec.yaml                 应用工程
├── lib/                         Dart：UI + 协议引擎
│   ├── engine/                  协议状态机（三端共享一份）
│   ├── ui/
│   │   ├── macos/               状态栏菜单 + 设置窗口
│   │   └── mobile/              iOS / Android 页面
│   └── main.dart
├── packages/
│   └── ztio/                    ★ Flutter platform plugin
│       ├── pubspec.yaml
│       ├── lib/ztio.dart        Dart API 与平台原语抽象
│       ├── android/             Kotlin
│       ├── ios/                 Swift
│       └── macos/               Swift
└── platform.md                  各平台授权与实现差异
```

---

## 2. 分层：什么共享，什么分叉

> **用户提出的事实**：SDK 是协议，各平台的**授权和实现必然不同**。
>
> 所以问题不是「要不要分叉」，而是**把分叉限制在哪一层**。
> 分叉得越靠上，维护成本越低。

**建议的分工**：

| 层 | 位置 | 份数 | 理由 |
|---|---|---|---|
| UI | Dart | 1（分支） | Flutter 的本职 |
| **协议状态机** | **Dart** | **1** | **这是项目的核心价值，复制三份必然分叉** |
| 候选枚举 / 选路 / 降级 | Dart | 1 | 纯逻辑，无平台 API |
| libzt 绑定 | Dart FFI | 1 | libzt 是 C API |
| **接口枚举** | **原生** | **3** | API 完全不同：`NWPathMonitor` / `ConnectivityManager` |
| **link-local 探测** | **原生** | **3** | 需要 `scope_id`，Dart 无法表达 |
| **权限申请** | **原生** | **3** | 授权声明本身就是平台机制 |

**即：协议逻辑共享一份，平台差异收在原生插件的少数原语后面。**

原生只需要暴露两个原语给 Dart：

```
enumerateInterfaces() → [ {name, addrs[], kind} ]
probe(addr, port, scope) → ok(rttMs) | timeout | refused | unreachable
```

**数据面不需要原语** —— 已知对端的直连地址后，普通 socket 会按路由表自动走对的接口。
只有 **link-local** 需要显式 scope，这也是它被单独列出的原因。

---

## 3. ⚠️ 需要验证的前提

**以下都还**没有**实测过，标注出来是为了不把它们当成既成事实：**

| # | 待验证 | 风险 | 验证方式 |
|---|---|---|---|
| V1 | **Dart FFI → libzt** 在 iOS / Android 上可行 | 高 —— 若不成立，方案 A 退化为方案 B | 最小工程：`zts_node_start` 能被 Dart 调用并返回 |
| V2 | iOS `NWParameters.requiredInterfaceType` 是否真能强制走蜂窝 | 中 | 最小工程 + 抓包确认 |
| V3 | Android `requestNetwork(TRANSPORT_CELLULAR)` + `bindSocket` | 中 | 同上；注意系统默认在 WiFi 连接时关闭蜂窝 |
| V4 | Android 13+ `NEARBY_WIFI_DEVICES` 对局域网**单播**的实际限制 | 中 | 实测被拒后单播是否仍可用 |
| V5 | libzt 与 zerotier-one **1.14.1** 的 `vProto` 兼容性 | **高** —— 不兼容则整体不成立 | 检查成员对象的 `vProto` 字段 |

> **V1 和 V5 是「一票否决」级的。** 建议在写任何 UI 之前先验证这两项 ——
> 它们决定整个工程结构，越晚发现代价越大。

**V1 不成立时的方案 B**：协议引擎下沉到原生，三端各一份实现。
代价是三份状态机需要同步演进，**必须配一套协议一致性测试**（用一个共享的测试向量集跑三端）。

---

## 4. 上游依赖

| 依赖 | 用途 | 许可 |
|---|---|---|
| **libzt**（ZeroTier SDK） | ZT 传输层 | BSL → Change Date 已过 → **Apache 2.0** |
| Flutter SDK | UI | BSD-3 |
| 平台网络框架 | `Network.framework`（iOS/macOS）、`ConnectivityManager`（Android） | 系统 |

**构建方式**（libzt 官方）：

```bash
./build.sh iphoneos-framework     # iOS / macOS
./build.sh android-aar            # Android
./build.sh macos-framework
```

> **libzt 的 README 关于许可的描述是过时的**（仍写着需要商业授权）。
> 实际 BSL Change Date 已过，转为 Apache 2.0。见
> [`../server/README.md`](../server/README.md#5-许可)。

---

## 5. 路线

| 阶段 | 内容 | 前置 |
|---|---|---|
| **0** | 验证 V1 / V5 | —— |
| **1** | 两个桌面端跑通：枚举候选 → 经 ZT 交换 → 局域网直连 | V1 |
| **2** | 接入移动端，处理权限与生命周期 | 阶段 1 |
| **3** | macOS 状态栏应用 + 锚点常驻 | 阶段 2 |

> **阶段 1 失败即结论。** 如果两台同 WiFi 的设备在协议正确的前提下仍连不上，
> 那就说明受限环境比预期更严 —— 此时应当重新评估方案，而不是继续加功能。

各平台的具体差异见 [`platform.md`](platform.md)。
