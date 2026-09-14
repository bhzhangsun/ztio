# server —— 自建 ZeroTier planet + controller

这台机器做两件事：

| 角色 | 作用 |
|---|---|
| **planet（root）** | ZeroTier 的根服务器。所有节点靠它互相找到对方、协助打洞、必要时中继 |
| **controller** | 网络定义、成员授权、地址分配。你的网络不再依赖 ZeroTier Central |

它**自己不加入任何网络** —— 所以不需要虚拟网卡、不需要 `NET_ADMIN`、不需要 `/dev/net/tun`。

---

## 快速开始

```bash
cp .env.example .env
$EDITOR .env                 # 至少填 ZTIO_PUBLIC_IP4

./scripts/deploy.sh          # 构建 + 启动 + 验证 planet
./scripts/apply-network.py   # 按 network.json 创建网络
./scripts/member.py pending  # 看谁在敲门
```

客户端接入时，把 `data/dist/planet` 覆盖到设备的 ZeroTier 数据目录即可。

---

## 目录

```
server/
├── Dockerfile                  固定 1.14.1，装预编译包 + 只编译 mkworld
├── docker-compose.yml
├── .env.example
├── entrypoint.sh               生成 identity / planet / local.conf
├── network.json                ★ 网络的声明式定义，进 git
├── patches/
│   └── mkworld-env.py          把 mkworld 的硬编码 root 改成读环境变量
└── scripts/
    ├── deploy.sh               构建 + 启动 + 验证 planet
    ├── apply-network.py        收敛 network.json，写后回读比对
    ├── member.py               成员授权 / 固定地址
    └── backup.sh
```

---

## 镜像从哪里取 ZeroTier

**不从源码构建。** 一开始是源码构建的，但那个选择是错的：在 2 vCPU / 1.6 GB 的机器上，
`make -j2` 编译 ZeroTier 的 `node/*.cpp` 内存峰值很高，**实测把整台机器压进 swap 抖动到
SSH 都无法完成握手**，只能靠控制台重启。而它换来的只是「不依赖第三方包」。

实测过的各条来源：

| 来源 | 从目标服务器可达 | 结论 |
|---|---|---|
| `download.zerotier.com`（官方 apt 源 / 安装脚本） | ❌ 超时 | 不可用 |
| GitHub Releases 的 `.deb` | ❌ 1.14.1 那个 release **没有任何附件** | 不存在 |
| **`mirrors.sustech.edu.cn/zerotier/`** | ✅ HTTP 200 / 1.05s | **zerotier-one 的 .deb** |
| `codeload.github.com` 源码 tarball | ✅ HTTP 200 / 0.98s | **只用来编译 mkworld** |

```dockerfile
# zerotier-one：3.2 MB 的预编译包，实测 1.2 秒下载完，sha256 已与官方核对
ARG ZT_DEB_URL=https://mirrors.sustech.edu.cn/zerotier/RELEASES/${ZT_VERSION}/dist/debian/bookworm/zerotier-one_${ZT_VERSION}_amd64.deb
ARG ZT_DEB_SHA256=6f6f8c0ed785b5f05b8d831b4f208f23c6ea8e91220f9f7d4417123873bb0317

# mkworld：发行包里没有它（attic 工具），只能编译 —— 但它只依赖 8 个小文件，几秒钟
ARG ZT_SRC_URL=https://codeload.github.com/zerotier/ZeroTierOne/tar.gz/refs/tags/${ZT_VERSION}
```

两个来源都做成 build-arg，**换环境只需改这两行**（比如把镜像换成官方源或自建缓存）。

### 两个必须知道的坑

**1. 换 `ZT_VERSION` 时必须同时换 `ZT_DEB_SHA256`。** 这是刻意的：宁可构建失败，
也不要在不知情的情况下装上一个未校验的二进制。

**2. 不要用 `dpkg-deb -x <deb> /`。** 实测会把镜像的 `/usr/bin` 清空 ——
解包后 `ls`、`grep`、`dpkg-deb` 全部 "not found"，而那个 deb 里根本没有 `usr/bin/`。
Dockerfile 里改成解到暂存目录再 `cp -a` 挑需要的 `usr/sbin`。

---

## 网络配置是声明式的

`network.json` 描述目标状态，`apply-network.py` 负责收敛。首次运行会创建网络并打印 ID，
把它填回 `network.json` 的 `networkId` 并存进 git。

### 为什么必须「回读比对」

controller 的管理 API **只校验 JSON 语法，不校验语义**。不合法的值会被**静默改写**而不是报错。
实测（1.14.1）：

| 写入 | 回读 |
|---|---|
| `{"mtu": "abc"}` | `1280` |
| `{"mtu": 999999}` | `10000` |
| `{"private": "yes"}` | `false` |
| `{"multicastLimit": -5}` | `18446744073709551611` |
| `{"target": "10.0.0.0/abc"}` | `10.0.0.0/0` ← **整个 IPv4 空间** |
| `"rules": [坏结构]` | `[]` |
| `{"invalid`（语法错） | 500 `parse_error.101` ← 只有这种才报错 |

对一个「把全网设备连起来」的东西来说，把 `10.0.0.0/abc` 悄悄变成 `0.0.0.0/0` 是灾难性的，
而你不会收到任何提示。

所以 `apply-network.py` 做两件事：写之前做完整语义预校验（不合法就不发请求），
写之后回读并逐字段比对（被改写就报错，而不是假装成功）。

```bash
./scripts/apply-network.py --check    # 只比对现状与目标，不改动
```

---

## 成员管理

网络是 `private=true`，设备 join 之后处于**未授权**状态，什么都做不了。

```bash
./scripts/member.py pending              # 看哪些设备在敲门
./scripts/member.py authorize <地址>      # 授权（可一次多个）
./scripts/member.py list                 # 全部成员 + 授权状态 + 分配地址
./scripts/member.py ip <地址> 172.16.0.50 # 指定固定地址
./scripts/member.py ip <地址> --clear     # 回到自动分配
./scripts/member.py deauthorize <地址>    # 取消授权（保留记录）
```

地址是 10 位十六进制的 ZeroTier 地址，设备上跑 `zerotier-cli info` 就能看到。

---

## 客户端接入

把 planet 分发到每台设备：

| 平台 | 数据目录 |
|---|---|
| Linux | `/var/lib/zerotier-one/planet` |
| macOS | `/Library/Application Support/ZeroTier/One/planet` |
| Windows | `C:\ProgramData\ZeroTier\One\planet` |

替换后重启 ZeroTier 服务。**务必用 md5 校验**（`deploy.sh` 会打印正确的值）：

```bash
md5sum /var/lib/zerotier-one/planet       # 必须与 deploy.sh 输出的值一致
```

一个容易忽略的点：**官方 planet 是编译在二进制里的**（`node/Topology.cpp` 的 `ZT_DEFAULT_WORLD`）。
磁盘上的 planet 和它同 ID（都是 149604618），靠时间戳和签名区分。所以：

- 自建 planet 的时间戳必须**比设备已缓存的那个新**
- 且必须能用**设备已缓存 planet 的签名密钥**验证通过

两条都满足才会被接受。这也是下面那条铁律的来源。

---

## 三条硬性约束

### 1. planet 的签名密钥绝对不能变

`current.c25519` / `previous.c25519`（在 `data/one/` 下）是 planet 的签名密钥。
节点替换 planet 的判据（`node/World.hpp:149`）：

```cpp
if ((_id == update._id) && (_ts < update._ts) && (_type == update._type)) {
    return C25519::verify(_updatesMustBeSignedBy, ..., update._signature);
}
```

注意 `_updatesMustBeSignedBy` 取自**节点当前缓存的 planet**。所以一旦换了签名密钥，
新 planet 无法通过旧 planet 的验签 —— **已经缓存旧 planet 的节点永远不会接受新的**，
只能逐台手工删掉 planet 文件。

同理，`identity.secret` 也不能丢：planet 里 root 的公钥就是它，换了所有设备都连不回来。

`entrypoint.sh` 里有一个灾难守卫：数据目录曾被初始化过（存在 `.ztio-initialized`）
但 `identity.secret` 不见了，会**拒绝启动**而不是"顺手"生成一套新的。

### 2. 只能在 `via` 全网统一的前提下下发路由

网络配置里的 `routes[].via` 对**所有成员**生效，而 **member 对象没有任何路由字段**。
向成员 POST `routes` / `nextHop` 会被静默丢弃（已核对 1.14.1 的成员字段全集）。

需要「每个节点走不同的出口」时，只能用 [应用层代理](../docs/exit-proxy.md)，不能用 IP 路由。

### 3. 不要依赖自动探测公网 IP

目标服务器上 `icanhazip` 之类的服务不可达（实测超时）。`ZTIO_PUBLIC_IP4` 必须显式填，
`deploy.sh` 也会拒绝内网地址和文档示例地址。

---

## 备份

```bash
./scripts/backup.sh              # 默认写到 ../backups/，保留 14 份
```

归档里有两样性质完全不同的东西：

- `identity.secret` + `current/previous.c25519` —— **整套系统的根**。丢了所有设备永久失联，
  且无法用备份以外的方式恢复
- `controller.d/` —— 网络定义与成员授权。丢了要重建，但设备还在网里，重新授权即可

脚本会验证归档确实包含关键文件且能解开，而不是"看起来成功了"才收工。

> ⚠️ 归档含 planet 签名私钥 —— 拿到它的人可以冒充你的 root 并签发世界更新。
> 存到受控位置，不要进公开仓库。`backup.sh` 会把权限设为 600。

---

## 安全

### 端口

| 端口 | 用途 | 是否对公网开放 |
|---|---|---|
| UDP 9993 | ZeroTier 线协议 | ✅ **唯一需要放行的** |
| TCP 9993 | 管理 API | ❌ 绑在 `0.0.0.0`（host 网络所致），由应用层 `allowManagementFrom` 限制为仅本机 |

安全组只需要一条入站规则：**UDP 9993**。

因为用了 `network_mode: host`，TCP 9993 会绑到 `0.0.0.0`。ZeroTier 在应用层拒绝非本机请求
（`service/OneService.cpp:1742` 按源地址过滤），但如果主机的 `iptables -P INPUT` 是 `ACCEPT`
且安全组放行了 9993/tcp，这层就只剩应用一层防护。建议在安全组或主机防火墙上额外关掉它。

**不要**放行 3443 / 3000 —— 本编排不提供任何 HTTP 服务。

### 容器权限

刻意**没有**下面这些。第三方镜像 `xubiaolin/zerotier-planet` 要求它们，但 planet + controller
不需要：

```yaml
# devices: ["/dev/net/tun:/dev/net/tun"]
# cap_add: [NET_ADMIN, SYS_ADMIN]
```

去掉之后，即使容器被攻破也无法建虚拟网卡、改路由、挂载。

### 为什么用 host 网络

ZeroTier 的 VL1 依据**源地址**判断对端路径，而 root 的职责恰恰是协助两端打洞 ——
走 Docker 的 bridge + DNAT 会让它看到的地址与真实路径不一致，地址错了就打不成。

---

## 升级

planet 的签名密钥和 identity 都在数据卷里，所以换镜像不影响已入网的设备。

```bash
$EDITOR Dockerfile            # 改 ARG ZT_VERSION
./scripts/deploy.sh
```

升级后 `zerotier-cli` / 管理 API 的字段可能有变化，`apply-network.py` 的回读比对会发现。

> 注意：1.14.1 之前的版本不在 Apache 2.0 的 Change Date 覆盖范围内，授权不同。
