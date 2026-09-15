# ztio

> **自建 ZeroTier 网络，外加一个特殊能力：出口代理。**
> 让一台设备把流量交给另一台，由后者替它出网。

---

## 解决什么问题

```
受限 WiFi（封锁目标站点，或干脆不给出口）
   Mac ─────── 同一 WiFi，二层可达 ───────→ 手机 ─────── 蜂窝 ───────→ 互联网
   （Mac 只需要能到手机，不需要这条 WiFi 出网）
```

**Mac 把流量交给手机，手机用自己的蜂窝链路替它走出去。**

成立条件只有两个：Mac 到手机二层可达；手机有另一条上行。

> 完整的场景分析、方案取舍、以及为什么**不能**用 IP 层出口，见
> [`docs/exit-proxy.md`](docs/exit-proxy.md)。那份文档是项目的核心设计。

---

## 三个部分

```
ztio/
├── docs/
│   └── exit-proxy.md          ★ 出口代理设计 —— 唯一的核心文档
├── server/                    自建 planet + controller + 网段内的 DNS
│   ├── Dockerfile             固定 ZeroTier 1.14.2，用预编译包（不编译源码）
│   ├── dns/                   ztio-dns：独立容器、独立身份的静态 DNS
│   ├── scripts/               deploy / ztnet（网络增删改查）/ member / backup
│   └── README.md
├── app/                       Flutter 客户端
│   ├── README.md              代理引擎的分层与两个角色
│   └── platform.md            各平台权限与实现差异 ← 分叉几乎全在这里
├── resources/                 ★ 输入资源（随 app 打包，不是运行时产物）
│   ├── planet                 257 字节 World 文件（只有公钥，无任何私钥）
│   └── README.md              格式逐字节解析 / 换 planet 的规则 / app 接入方式
└── architecture/              架构图（可交互 HTML）
```

| 部分 | 是什么 | 技术 |
|---|---|---|
| **server** | 自建 Planet + Controller，取代 ZeroTier Central；外加网段内的 DNS | Docker / Shell / Python |
| **app** | Flutter 工程：`ztio` plugin + 三端应用（macOS 为状态栏应用） | Dart + Swift / Kotlin |

---

## 核心决策

| # | 决策 | 理由 |
|---|---|---|
| E1 | **出口放在应用层，不做 IP 层转发** | IP 层出口要求两端 root，**手机做不到**；且 ZeroTier 的路由是全网统一的，无法按节点指定下一跳。详见 [`exit-proxy.md` §2](docs/exit-proxy.md) |
| E2 | **出口用宿主机栈出网，不用 libzt** | 内嵌 libzt 时系统里没有 ZT 虚拟网卡，宿主机栈的连接自然走本机正常网络 —— 不需要 root、TUN、IP 转发 |
| E3 | **出口默认关闭，客户端默认不使用出口** | 代价由出口承担（流量、电池、发热），不能默认开启 |
| E4 | **不做对端地址发现与选路** | ZeroTier VL1 已经完整实现，重复做只是多一份要维护的代码。见下 |
| E5 | **候选出口由配置指定，不做动态公告** | ZT 地址与成员身份绑定、永不改变，静态配置不会失效 |
| E6 | **网络配置声明式，写后回读比对** | controller 的 API 只校验 JSON 语法，会把 `10.0.0.0/abc` 静默改成 `0.0.0.0/0` |

### 关于 E4：删掉了什么

早期设计里 ztio 打算自己做**对端候选地址枚举、交换、探测、选路、降级、自愈** ——
曾经有 `docs/protocol.md`（582 行）和 `docs/path-selection.md`（287 行）两份文档。

**这部分已被证伪并整个删除。** 核实后的结论是：ZeroTier VL1 已经完整实现了它，
而且做得更多。

| ztio 打算做的 | ZeroTier 1.14.2 里的实现 |
|---|---|
| 枚举本机接口地址并告知对端 | `node/Peer.cpp:212` —— `VERB_PUSH_DIRECT_PATHS`，每 15 秒推一次 |
| 收到后尝试直连 | `node/IncomingPacket.cpp:1400` —— `attemptToContactAt` |
| **双向同时发起**打洞 | `node/IncomingPacket.cpp:736` —— `_doRENDEZVOUS` 先发低 TTL 垃圾包打开 NAT/防火墙 |
| uPnP / NAT-PMP 端口映射 | `osdep/PortMapper.cpp` + `ext/miniupnpc/` + `ext/libnatpmp/` |

**libzt 也一样**（`libzt/src/NodeService.cpp:474-488` 枚举接口地址喂给核心）。
所以同 WiFi 时 ZeroTier 会自动建立局域网直连，**不需要 app 做任何事**。

> 顺带纠正一个曾经写进文档的错误说法：**1.14.x 没有**「针对对称型 NAT 的端口预测」
> 机制（源码里搜不到 `predict` / `birthday` / `portScan` / `guessPort`）。
> 那个说法来自当前官方文档，对 1.14.x 不成立。

---

## 现状

**已经核实过的**（读源码或实测，不是推测）：

- ZeroTier 客户端的 managed 路由只能整体开关或按白名单**过滤**，**不能改 `via`**
- `member` 对象**没有**任何路由 / 下一跳字段，POST 会被静默丢弃
- `v4AssignMode` 只认 `zt`，写 `dhcp` 会被静默丢弃
- libzt 的路由 API **全部只读**，且**没有** NAT / 转发 / 出口能力
- libzt **有**完整的服务端 socket 能力（`zts_bsd_bind/listen/accept/poll`）

**还没验证的**（详见 [`app/README.md` §6](app/README.md)）：

| # | 待验证 | 风险 |
|---|---|---|
| V1 | Dart FFI → libzt 在 iOS / Android 上可行 | **一票否决** |
| V2 | 出口能否把出站绑到指定链路（蜂窝） | **高 —— 见下** |
| V4 | libzt 与 zerotier-one 1.14.2 的 `vProto` 兼容性 | **高** |

> **V2 是最容易静默失败的一项。** 如果出口只是 `Socket.connect`，它会走系统默认路由 ——
> 也就是那条受限 WiFi。症状是「连接成功、数据流通、网页打不开」，从日志上看不出问题。

---

## 从哪里开始读

| 你是 | 读 |
|---|---|
| **想理解为什么这么设计** | [`docs/exit-proxy.md`](docs/exit-proxy.md) |
| **要搭控制面** | [`server/README.md`](server/README.md) |
| **要写客户端** | [`app/README.md`](app/README.md) → [`app/platform.md`](app/platform.md) |

---

## 仓库约定

> **本仓库是公开的。** 往这里加内容时请遵守：

| 规则 | 说明 |
|---|---|
| **真实公网 IP 不入库** | 一律用文档保留地址 `203.0.113.10`（RFC 5737）代替 |
| **密钥 / token / 身份文件不入库** | `identity.secret`、`current.c25519`、`authtoken.secret`、私钥一律不进 git |
| **部署实况不入库** | 真实地址与运维手册在部署主机上 |
| **每份文档只有一个位置** | 尤其是架构图：**全部在 [`architecture/`](architecture/)，不在别处留副本** |

---

## 许可

**为什么停在 1.14.x，而不追最新版** —— 查证过的事实：

| 版本 | controller 位置 | 许可 |
|---|---|---|
| ≤ **1.14.2** | `controller/` | BSL 1.1，Change Date **`2026-01-01` 已过 → Apache 2.0** |
| ≥ 1.16.0 | **`nonfree/controller/`** | ⚠️ **Source-Available，仅限非商业** |

BSL 正文写明「**逐版本适用**，Change Date 可能不同」，同时有「**以 Change Date 与
该版本首次发布的四周年中较早者为准**」的兜底条款。1.14.x 的 Change Date 都是
`2026-01-01`，早于各自四周年，所以它们**同一天一起**转为 Apache 2.0 ——
不存在「哪个版本是最低 Apache 版本」的说法。

而 1.16.0 起 ZeroTier 把 Controller 移进 `nonfree/`，换成一份把「Commercial Use」
定义得**涵盖非营利组织**的许可。对「自建 controller」这个用途，那是**严格更差**的选择。

> 附带一个文本瑕疵：1.14.x 的 `LICENSE.txt` 里 `Licensed Work` 字段写的还是
> `ZeroTier Network Virtualization Engine 1.4.4`（2019 年遗留未更新），
> 靠 BSL 的「逐版本适用」条款兜住。

| 部分 | 许可 |
|---|---|
| 本仓库的文档与自有代码 | Apache 2.0（**尚未添加 LICENSE 文件**） |
| **zerotier-one 1.14.2**（`server/`） | BSL 1.1 → Change Date 已过 → **Apache 2.0** |
| **libzt**（`app/`） | BSL 1.1 → Change Date 已过 → **Apache 2.0** |
| `attic/world/mkworld.cpp`（`server/` 编译它） | ⚠️ 文件头是 **GPLv3**，与仓库其余部分不同 |

> libzt 的 README 仍写着需要商业授权，**那已经过时**。
>
> `mkworld.cpp` 是 GPLv3 这点，是本仓库**只收补丁脚本、不收改过的源码文件**的原因 ——
> 见 [`server/patches/mkworld-env.py`](server/patches/mkworld-env.py) 顶部的说明。

本仓库自用不涉及「对第三方提供服务」。若要开放给他人使用，需按上表自行评估。
