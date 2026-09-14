# server — 控制面

自建的 ZeroTier **Planet + Controller**，为 ztio 提供 L0（身份 / 加密 / 地址 / 兜底可达性）。

```
zerotier-one 1.14.1  ─┬─ Planet    根服务：信令、打洞协助、中继
                      ├─ Controller 网络定义与成员授权
                      └─ 控制面 API 127.0.0.1:9993
        mkworld      ──── 生成 planet / moon 文件
        ztiotool     ──── 容器内 CLI，供 agent 操作控制面
```

**对外只暴露一个端口：`UDP 9993`**（ZT 线协议）。
管理 API（TCP 9993）只绑回环，**不对公网开放**。

---

## 1. 为什么锁定 1.14.1

| 原因 | 说明 |
|---|---|
| **许可** | 1.14.1 的 BSL 1.1 Change Date 为 `2026-01-01`，**已到期转为 Apache 2.0**。更早版本未必 |
| **协议稳定** | 与主流客户端版本一致，`vProto` 兼容 |
| **可复现** | 镜像与二进制都可固定 digest / sha256 |

**风险**：1.14.1 不再收到安全更新。这是**有意接受的取舍** ——
控制面不暴露到公网（见 §4），攻击面主要是 ZT 线协议本身。

**升级时的检查清单**：

1. 新版本的 BSL Change Date 是否已过？（未过则不是 Apache 2.0）
2. Controller 部分是否仍受 **ZeroTier Source-Available License** 约束？
3. `vProto` 是否仍与现网客户端兼容？
4. `identity.secret` / `planet` / `controller.d/` 迁移是否完整？

---

## 2. mkworld

从 ZT 身份生成自定义根服务文件。

```bash
/var/lib/zerotier-one/mkworld > /var/lib/zerotier-one/planet
```

生成的 `planet` 内含你的根节点地址，**必须分发给所有客户端**（替换内置 planet）。

> **`mkworld` 会改写 `previous.c25519` / `current.c25519`。**
> 容器里这两个文件在可写层，重跑会在镜像层留下垃圾 —— 生成后应清理。

**分发**：客户端用自建 planet 替换 `/var/lib/zerotier-one/planet`。
这是「气隙根」成立的唯一条件 —— 已验证：网络外的节点只会看到
`<controller地址> PLANET -1 RELAY`，不会回落到官方根。

---

## 3. ztiotool

**容器内的控制面 CLI。** 它不是给终端用户用的，而是**给 agent 用的** ——
agent 通过 `docker exec` 调用它来配置网络，无需手工拼 JSON 或碰控制面 API。

### 为什么需要它

ZeroTier 的 Controller API 有个危险的特性：**只校验 JSON 语法，不做语义校验**。
写错的值会被**静默接受并改写**，从返回结果里看不出来。

| 你写入 | 实际存下 | 后果 |
|---|---|---|
| `"mtu": "abc"` | `1280` | 静默取默认值 |
| `"private": "yes"` | `false` | **网络变公开** |
| `"mtu": 999999` | `10000` | 超出上限被截断 |
| `"multicastLimit": -5` | `18446744073709551611` | 无符号回绕 |
| `"rules": [非法]` | `[]` | **规则被清空** |
| `"10.0.0.0/abc"` | `10.0.0.0/0` | **变成一个覆盖全网的巨型路由** |

**`ztiotool` 的职责就是在写入前做语义校验、写入后回读比对**，
把「静默改写」变成「明确报错」。

### 命令

```bash
ztiotool create  <name>                       创建网络，返回 nwid
ztiotool show    <nwid>                       显示网络定义（可读格式）
ztiotool member  add|list|rm <nwid> [...]     成员管理与授权
ztiotool gateway set|unset <nwid> <ip>        设置网段网关
ztiotool dns     sync <nwid>                  同步 DNS 配置
ztiotool doctor                               自检
```

`doctor` 检查项：

| 检查 | 说明 |
|---|---|
| 控制面可达 | `GET /status` 返回 200 |
| 身份完整 | `identity.public` / `identity.secret` 都存在且匹配 |
| **灾难守卫** | `.deployed` 存在但 `identity.secret` 缺失 → **立即报错** |
| planet 已生成 | `/var/lib/zerotier-one/planet` 存在且非空 |
| 网络定义可解析 | `controller.d/network/*.json` 全部合法 |
| **回读比对** | 本地定义与 API 返回一致 |
| 监听状态 | 线协议端口在监听 |

### 关键约束

| 约束 | 后果 |
|---|---|
| **只连 `127.0.0.1:9993`** | 不暴露任何端口；不产生新的攻击面 |
| **写入前校验** | 拒绝非法值，而不是依赖服务端 |
| **写入后回读比对** | 检出静默改写 |
| **幂等** | 重复执行同样的命令结果相同 |
| **不删网络** | 删除走显式确认；`DELETE` 的键是 `id` 而非 `nwid` |

### 语义陷阱（已实测确认）

| 陷阱 | 事实 |
|---|---|
| **成员对象没有路由 / 下一跳字段** | POST `routes` / `nextHop` 会被**静默丢弃**。完整字段表只有 `activeBridge, address, authenticationExpiryTime, authorized, capabilities, creationTime, id, ipAssignments, lastAuthorizedCredential, lastAuthorizedCredentialType, lastAuthorizedTime, lastDeauthorizedTime, noAutoAssignIps, nwid, objtype, remoteTraceLevel, remoteTraceTarget, revision, ssoExempt, tags, vMajor, vMinor, vProto, vRev` |
| **路由是网络级的** | `routes[].via` 全网络生效，**不能按成员设** |
| **`via: null` 表示本地 LAN 路由** | 不是「无下一跳」 |
| **托管地址必须落在已分配的路由内** | 官方约束，否则客户端不会应用 |
| **`allowManaged` / `allowGlobal` / `allowDefault` / `allowDNS` 是客户端设置** | 控制面**无法**下发，只能由客户端 `zerotier-cli set` 自己设 |

> **最后一条最容易被误解成「控制面配置」。** 它不是。
> 想让某台设备成为出口网关，必须在**那台设备上**设置 `allowDefault=1` ——
> 控制面只能提供路由信息。

### 网络 ID 的构成

```
Network ID = <controller 的 10 位十六进制 ZT 地址> + <6 位十六进制序号>
```

**网络无法在 controller 之间迁移** —— 这是硬约束，多租户设计时必须考虑。

---

## 4. 部署形态

```
公网
 │  只开 UDP 9993
 ▼
┌─ 云主机 ────────────────────────────────┐
│  ┌─ 容器（network_mode: host）────────┐  │
│  │  zerotier-one 1.14.1               │  │
│  │    ├ 线协议      UDP 9993  ← 唯一对外│  │
│  │    ├ 管理 API    TCP 9993  127.0.0.1│  │
│  │    └ 数据卷      identity/planet/…  │  │
│  │  ztiotool  ← docker exec 调用        │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
```

| 项 | 要求 |
|---|---|
| `network_mode` | `host` —— 否则 UDP 9993 的打洞协助需要额外端口映射 |
| 能力 | `NET_ADMIN`, `SYS_ADMIN`；挂载 `/dev/net/tun` |
| 管理 API | **必须只绑回环**（`local.conf` 的 `allowManagementFrom`） |
| Web 面板 | 需要时**走 SSH 隧道**，不要对公网开端口 |

### ⚠️ 成本约束

Planet 会**中继**流量。云主机的出网流量按 GB 计费，长期中继会持续烧钱。

| 规则 | 说明 |
|---|---|
| **绝不长期停留在 RELAY** | 定期 `zerotier-cli peers` 检查 |
| **出口网关不能是本机** | 它只能是信令与中继的兜底，不能承载数据出口 |
| 中继是「兜底」不是「常态」 | 客户端应尽快建立直连 |

---

## 5. 许可

**本目录的部署方式涉及三层不同的许可**，必须分清：

| 组件 | 许可 | 商用 |
|---|---|---|
| `zerotier-one` 1.14.1 | BSL 1.1，Change Date `2026-01-01` **已过** → **Apache 2.0** | ✅ 可以 |
| **Controller**（`service/`） | **ZeroTier Source-Available License 1.0** | ⚠️ **受限，见下** |
| `libzt`（SDK） | 同上，Change Date 已过 → Apache 2.0 | ✅ 可以 |
| `ztncui`（第三方面板） | GPLv3 | 视分发方式 |

### Controller 许可的实际约束

**Source-Available License 1.0 的要点**：

| 项 | 内容 |
|---|---|
| 非商业用途 | 个人 / 教育 / 评估（评估期 ≤ 30 天） |
| **商业用途的定义** | **包括非营利组织、慈善机构，以及付费或*免费*的对第三方服务** |
| 专利授权 | ❌ **不授予** |
| 管辖 | 加州法律 |

> **⚠️ 关键：即使你的服务完全免费，只要是对第三方的服务，也算商业用途。**
>
> 「我自己和家人的设备用」= 非商业 ✅
> 「给朋友的设备用，不收费」 = **商业** ⚠️
> 「公司内部用」 = **商业** ⚠️
>
> 这是 ZeroTier 官方的定义，不是解释。**对外提供服务前必须确认许可**，
> 或向 ZeroTier 取得商业授权。

**本项目的定位**：ztio 自身是协议与实现（Apache 2.0 兼容），
但**自建 Controller 的部署方式**受上述许可约束 —— 二者要分开看。
