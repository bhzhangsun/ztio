# server —— 自建 ZeroTier planet + controller

这台机器做两件事：

| 角色 | 作用 |
|---|---|
| **planet（root）** | ZeroTier 的根服务器。所有节点靠它互相找到对方、协助打洞、必要时中继 |
| **controller** | 网络定义、成员授权、地址分配。你的网络不再依赖 ZeroTier Central |

它**自己不加入任何网络** —— 所以不需要虚拟网卡、不需要 `NET_ADMIN`、不需要 `/dev/net/tun`。

---

## 部署

### 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| **公网 IP** | 必须有 | root 的地址要写进 planet，所有节点靠它互相找到。**这是唯一需要你提供的信息** |
| Docker | 20.10+ | 需要 `docker compose` 插件（`docker compose version` 能跑） |
| 入站端口 | **UDP 9993** | 唯一需要开放的端口。TCP 9993 是本地管理 API，**不要**对外 |
| 内存 | 512 MB 够 | 实测在 1.6 GB / 2 vCPU 的机器上构建 + 运行无压力 |
| 磁盘 | 1 GB 够 | 镜像 112 MB + 数据几百 KB |

**不需要** `/dev/net/tun`、`NET_ADMIN`、`SYS_ADMIN`、`--privileged`。
这台机器**不加入任何网络** —— 它只是根服务器和控制器，不承载业务流量。

### 三步部署

```bash
# 1. 取代码
git clone https://github.com/bhzhangsun/ztio.git
cd ztio/server

# 2. 配置 —— 只有一个必填项
cp .env.example .env
$EDITOR .env                    # 填 ZTIO_PUBLIC_IP4=你的公网 IP

# 3. 一条命令：构建 + 启动 + 验证
./scripts/deploy.sh
```

**就这些。** 第 3 步会自己构建镜像、起容器、等管理 API 就绪，然后**把生成的 planet
解出来逐项核对**（类型、世界 ID、root 公钥、端点 IP 与端口）—— 核对不过直接报错退出。

**部署到此结束 —— 不会创建任何网络。** 需要网络时见下面「创建网络」，成员授权见「让设备入网」。

### 关于那个公网 IP

这是唯一必须手填的东西，因为**服务器不可能知道自己对外的公网 IP**（云主机的网卡上
只有内网地址，公网 IP 是 NAT 映射出来的）。

- 阿里云 ECS 上填**弹性公网 IP**
- **不要**填 `172.19.x.x` 这类内网地址
- `deploy.sh` 会主动拒绝 `203.0.113.10`（示例值）和 RFC1918 私网地址

填错的后果值得强调：**容器正常、API 正常、`zerotier-cli` 正常、`/status` 返回 200**，
但设备之间**永远连不通** —— 因为它们拿着一个错误的地址去找 root。这类故障从日志上
看不出来，所以 `deploy.sh` 才要解包核对而不是只看容器起没起。

### `deploy.sh` 背后做了什么

```bash
./scripts/deploy.sh              # 完整流程
./scripts/deploy.sh --no-build   # 跳过构建（只改了 .env 或单纯重启时用）
```

1. 前置检查：docker、compose 插件、`.env` 存在且 IP 合法、9993 端口没被占
2. `docker compose build` → `up -d`
3. 轮询等待管理 API 可应答
4. 解析容器里的 planet 二进制，逐项断言（见上）

**首次启动时容器内部会自动**（`entrypoint.sh`）：

1. 生成节点 identity
2. 用 `mkworld` 按当前 identity + 你的公网 IP 生成 planet，同时放到 `/var/lib/ztio/dist/planet`
3. 写 `local.conf`：`allowSecondaryPort:false`、`portMappingEnabled:false`、
   `allowManagementFrom:["127.0.0.1","::1"]` —— 把管理 API 锁在容器内
4. 启动 `zerotier-one`

planet 只在**首次**或**端点变化**时生成 —— 重启不会重新生成（幂等，已实测）。
只有换公网 IP 时才需要 `ZTIO_FORCE_PLANET_REGEN=1`。

### 创建网络（手动，按需）

**部署不会创建任何网络。** planet 只解决「节点怎么找到彼此」；网络定义（网段、DNS、
路由、谁能加入）属于 controller，由你在需要时手动创建。

管理脚本装进了镜像，所以**在容器内跑** —— 服务器上不需要装 python：

```bash
docker compose exec ztplanet ztnet.py create --name homenet --private --mtu 2800 --pool 172.16.0.100-172.16.0.200 --route 172.16.0.0/24
docker compose exec ztplanet ztnet.py set --dns-domain ztio.internal --dns-server 172.16.0.1 --v4-zt --v6-rfc4193
```

`create` 会打印出 nwid（形如 `b021fa09d579af0f`）—— **记下来**，授权成员要用。

> ### ⚠️ `--pool` 必须配一条覆盖它的 `--route`，否则谁都拿不到 IP
>
> 这不是可选的美化项，是 ZeroTier 控制器的硬逻辑。`controller/EmbeddedNetworkController.cpp` 里：
>
> ```cpp
> // 成员被手动指定的地址
> int routedNetmaskBits = -1;
> for (rk...) if (routes[rk].target.containsAddress(ip)) routedNetmaskBits = ...;
> if (routedNetmaskBits >= 0) { nc->staticIps[nc->staticIpCount++] = ip; }
>
> // 自动分配
> if ((routedNetmaskBits > 0) && ...) { 分配 }
> ```
>
> **地址只有落在某条 route 的覆盖范围内才会被下发。** 没有覆盖 pool 的路由时：
>
> - 网段、DNS、MTU **照常下发** —— 客户端 `status: OK`，`netconfRevision` 也对得上
> - 但 `assignedAddresses` **永远是空数组**，`zerotier-cli listnetworks` 最后一列是 `-`
> - 服务端**不报任何错**，`member.py` 里手写的 `ipAssignments` 也照样存着、照样不下发
>
> 也就是说这是个**完全静默**的失败：每一层看起来都正常，只有最终没有 IP。
> 排查它花了整整一轮，所以 `ztnet.py` 现在会在**发请求之前**拦住这种情况并告诉你该加什么路由。


其它子命令：

```bash
docker compose exec ztplanet ztnet.py ls       # 有哪些网络
docker compose exec ztplanet ztnet.py show     # 某个网络的完整状态
docker compose exec ztplanet ztnet.py fields   # 可写字段（从 API 自己推导）
docker compose exec ztplanet ztnet.py set --mtu 2800
docker compose exec ztplanet ztnet.py rm --yes
```

不带 nwid 时，controller 上只有一个网络就自动选它；有多个则**要求显式指定，不替你猜**。

> 主机上如果有 git checkout，同一份脚本也能直接跑（`./scripts/ztnet.py ...`）——
> 它自己找得到 token（容器内 `/var/lib/zerotier-one/`，主机上 `/var/lib/ztio/one/`）。

**controller 没有配置文件** —— 它自己的存储就是状态：

```
/var/lib/ztio/one/controller.d/network/<16位nwid>.json            网络定义
/var/lib/ztio/one/controller.d/network/<16位nwid>/member/*.json   成员授权
```

那是 ZeroTier 的原生布局（`EmbeddedNetworkController` + `FileDB`），`backup.sh` 备份的就是它。
所以这里**不维护第二份配置** —— 两个数据源必然漂移，理由见
[状态在 controller 自己那里](#状态在-controller-自己那里不在这里)。

**为什么要有这个脚本而不是直接 curl**：controller 的 API 只校验 JSON 语法，
**不校验语义**，会静默改写。实测（1.14.2）：

| 你写的 | 它存下的 |
|---|---|
| `mtu: "abc"` | `1280`（悄悄换成默认值） |
| `mtu: 999999` | `10000`（悄悄截断到上限） |
| `mtu: {...}`（类型都错） | **HTTP 200，静默忽略** |
| `private: "yes"` | `false` |
| `multicastLimit: -5` | `18446744073709552000` |
| `v4AssignMode: {zt:true, dhcp:true}` | `{zt:true}`（dhcp 直接丢） |
| `routes: 10.0.0.0/abc` | `10.0.0.0/0`（前缀吞成 0） |

所以脚本**在发请求之前**校验（非法掩码、超范围、类型不符一律拦住，请求根本不发出去），
写完再**逐字段读回比对**，不一致就报错。

### 让设备入网

网络是 `private=true`，设备 join 之后处于**未授权**状态，什么都做不了。用 `member.py`
授权 —— 同样在容器内：

```bash
docker compose exec ztplanet member.py pending              # 看谁在敲门
docker compose exec ztplanet member.py authorize <地址>      # 授权
docker compose exec ztplanet member.py list                 # 全部成员与状态
```

详见 [成员管理](#成员管理)。

### 把 planet 发给客户端

```bash
scp root@<服务器>:/var/lib/ztio/dist/planet  ./planet
```

各平台的确切路径、校验方法，以及**为什么官方 planet 的存在不会让自建 planet 失效**，
见 [客户端接入](#客户端接入)。

> planet 内容对所有人可见（它就是个公开的根列表），**但 `/var/lib/ztio/one/identity.secret`
> 和 `current.c25519` 是密钥，绝对不能外传** —— `identity.secret` 泄露等于别人可以
> 冒充你的 root。

### 日常操作

```bash
```bash
docker compose logs -f                        # 看日志
docker compose restart                        # 重启（不会重新生成 planet）
docker compose down && docker compose up -d   # 重建容器，数据在 data/ 里不受影响
./scripts/backup.sh                           # 备份密钥与控制器状态
docker compose exec ztplanet member.py list   # 成员状态
```

`backup.sh` 会把 `identity.secret`、`authtoken.secret`、`current.c25519`、
`previous.c25519`、`planet`、`local.conf`、`controller.d/` 打包，校验归档可解，
并按 `KEEP=14` 轮转。**这些是丢了就无法恢复的东西** —— 尤其是 `current.c25519`，
它是 planet 的签名密钥；丢了之后所有已入网设备都要重新分发 planet。

### 升级到新版本

见下面的 [升级](#升级) 一节。

---

## 目录

```
server/
├── Dockerfile                  固定 1.14.2，装预编译包 + 只编译 mkworld
├── docker-compose.yml
├── .env.example
├── entrypoint.sh               生成 identity / planet / local.conf
├── patches/
│   └── mkworld-env.py          把 mkworld 的硬编码 root 改成读环境变量
└── scripts/
    ├── deploy.sh               构建 + 启动 + 验证 planet
    ├── ztnet.py                网络的增删改查，请求前校验 + 写后回读比对
    ├── member.py               成员授权 / 固定地址
    └── backup.sh               备份密钥与 controller 状态
```

`scripts/` 会被 `COPY` 进镜像（`/opt/ztio/`，并在 `/usr/local/bin` 建软链），
所以**同一份脚本在容器内和主机上都能跑** —— 它自己会找 token：

| 在哪跑 | 怎么调 | 数据目录 |
|---|---|---|
| 容器内（推荐） | `docker compose exec ztplanet ztnet.py ...` | `/var/lib/zerotier-one/` |
| 主机上（有 checkout 时） | `./scripts/ztnet.py ...` | `/var/lib/ztio/one/` |

### ⚠️ 数据不在仓库里

节点身份、planet 签名密钥、网络定义、分发用 planet —— **全部在 `/var/lib/ztio/`**，
由 `.env` 的 `ZTIO_DATA_ROOT` 决定（compose 与主机脚本读的是**同一个值**，改一处即可）。

```
/var/lib/ztio/one/      ← 挂进容器的 /var/lib/zerotier-one
/var/lib/ztio/dist/     ← 挂进容器的 /dist
```

**为什么不放在 `./data` 下**：gitignore 只防误提交，**防不了误删**。
删代码目录、`git clean -xfd`、重做一次 checkout —— 任何一种都会连数据一起清掉，
而 `identity.secret` 和 `current.c25519` 丢了就是全网设备永久失联、无法恢复。

> 备份同理，落在 `/var/backups/ztio/`，也在仓库外。


---

## 镜像从哪里取 ZeroTier

**不从源码构建。** 一开始是源码构建的，但那个选择是错的：在 2 vCPU / 1.6 GB 的机器上，
`make -j2` 编译 ZeroTier 的 `node/*.cpp` 内存峰值很高，**实测把整台机器压进 swap 抖动到
SSH 都无法完成握手**，只能靠控制台重启。而它换来的只是「不依赖第三方包」。

实测过的各条来源：

| 来源 | 从目标服务器可达 | 结论 |
|---|---|---|
| `download.zerotier.com`（官方 apt 源 / 安装脚本） | ❌ 超时 | 不可用 |
| GitHub Releases 的 `.deb` | ❌ 各版本 release 都**没有任何附件** | 不存在 |
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

### mkworld 是什么，凭什么信它

**`mkworld` 不是 ZeroTier 的运行时组件** —— ZeroTier 从不执行它。它只是生成 `planet`
文件的运维工具，而 `planet` 才是 ZeroTier 真正读的东西。所以「mkworld 对不对」
等价于「**它产出的 planet 能不能被别的节点接受**」。

它编译自与运行阶段**完全相同的 tag** 的 `attic/world/mkworld.cpp`，只做了一处改动：
把硬编码的 root 列表改成读环境变量（见 `patches/mkworld-env.py`）。上游那行注释原话是
「If you want to make your own World you must edit this file.」

**验证方式不是结构比对，是端到端行为验证** —— 起一个真实 leaf 节点，只喂给它我们生成的
planet，看它是否把我们的 root 认成 `PLANET`。实测（1.14.2）：

```
=== leaf peers ===
  4570a54054 1.14.2 PLANET    23 DIRECT   82   81   10.99.0.2/9993
=== root peers ===
  5619e0a634 1.14.2 LEAF       0 DIRECT   178  178  10.99.0.3/31842
```

leaf 认出的 `4570a54054` 正是 root 的 identity，路径 `10.99.0.2/9993` 正是烤进 planet
的端点，且是 **DIRECT** 直连（23 ms）—— 说明 planet 的签名被接受、root 被信任、
双向 VL1 会话建立完成。

### 三个必须知道的坑

**1. 换 `ZT_VERSION` 时必须同时换 `ZT_DEB_SHA256`。** 这是刻意的：宁可构建失败，
也不要在不知情的情况下装上一个未校验的二进制。

**2. 启动时会看到一条「降权失败」警告 —— 这是预期行为，不是故障。**

```
zerotier-one: WARNING: failed to drop privileges (kernel may not support required
prctl features), running as root
```

**这句文案是误导性的。** 它说「kernel may not support required prctl features」，
但真实原因和内核无关。完整机制（读了 `one.cpp` 源码 + 实测确认）：

1. ZeroTier 在 Linux 上想把权限从 root 降到一个无特权用户 `zerotier-one`，
   **同时保留 `CAP_NET_ADMIN` 和 `CAP_NET_RAW`** —— 因为它要创建和配置 TUN 虚拟网卡
2. 为此它调用 `capset()`（`_setCapabilities()`）去设置含 **`CAP_NET_ADMIN`（bit 12）**
   在内的能力位
3. **Docker 默认能力集（`CapBnd = 0xa80425fb`）不含 bit 12** —— 这是刻意的安全默认值
4. `capset()` 因 `EPERM` 失败 → ZeroTier 放弃降权 → 打这条警告 → 继续以 root 运行

**验证**：加 `--cap-add NET_ADMIN` 后警告立刻消失，数据目录也被 chown 给
`zerotier-one`（证明降权真的执行了）。而 `--security-opt seccomp=unconfined`
**消不掉**它 —— 这是**能力**限制，不是 seccomp 限制。

**所以这条警告恰恰是我们安全选择的直接结果**：我们故意不给 `CAP_NET_ADMIN`，
因为**这台机器不加入任何网络、永远不创建 TUN 设备**，根本不需要它。
为了消掉一行警告去授予 `CAP_NET_ADMIN` 是得不偿失的。

**对系统的影响：没有。** 容器进程是 root，但那是**容器内的 root**，不是你主机上的 root ——
没有 `--privileged`、没有 `CAP_NET_ADMIN`/`CAP_SYS_ADMIN`、没有 `/dev/net/tun`、
用默认 seccomp 和自己的 namespace，且只有 `/var/lib/ztio/one` 与 `/var/lib/ztio/dist`
两个目录被挂进来。

唯一实际代价：少了一层纵深防御 —— 万一 ZeroTier 被攻破，攻击者拿到的是**容器内的 root**
而非 `zerotier-one` 用户，因此能以 root 读写那两个挂载目录（其中 `identity.secret`
在主机上本来就是 root 所有）。

**想彻底消除它**（可选的加固，当前未做）：让容器整体以 `zerotier-one` 用户运行。
源码里 `dropPrivileges()` 第一行就是 `if (getuid() != 0) return;` —— 已经非 root 就直接
跳过，不会有警告，且**不需要任何 capability**。代价是要额外处理数据卷属主
（启动时 chown，或预先在主机上改好属主）。

**3. 不要用 `dpkg-deb -x <deb> /`。** 实测会把镜像的 `/usr/bin` 清空 —— 解包后
`ls`、`grep`、`dpkg-deb` 全部 "not found"，而那个 deb 里根本没有 `usr/bin/`。
现在用 `dpkg -i`，不需要它了，但别改回去。

---

## 状态在 controller 自己那里，不在这里

**没有配置文件。** 这是刻意的 —— 早先有过一个 `network.json`，实测它已经和 controller 漂了：

| 字段 | 文件里声明 | controller 实际 |
|---|---|---|
| `ipAssignmentPools` | `172.16.0.100–200` | `[]` |
| `v4AssignMode` | `{zt: true}` | `{zt: false}` |
| `v6AssignMode` | `{rfc4193: true}` | 全 `false` |
| `dns` | `ztio.internal` / `[172.16.0.1]` | `testdns.lo` / `[]` |

**两个数据源必然漂移，而且漂了没有任何东西会告诉你** —— 网络照常工作，
只是行为和你以为的不一样。

所以唯一的事实来源是 ZeroTier 自己的存储：

```
/var/lib/ztio/one/controller.d/network/<16位nwid>.json            网络定义（网段/DNS/路由/MTU）
/var/lib/ztio/one/controller.d/network/<16位nwid>/member/*.json   每个成员一个文件
```

`backup.sh` 备份的就是它，有它就能完整恢复 —— 不需要再往 git 里放一份。

**一个 controller 可以托管任意多个网络**，没有数量限制。多网络不需要任何额外机制：
再 `create` 一个，就多一个 `<nwid>.json`。

### 校验怎么做（不另写一份 schema）

自己维护一份 schema 必然漏字段。实测 API 返回里有 `capabilities`、`tags`、`rules`、
`ssoEnabled`、`authorizationEndpoint`、`rulesSource`、`remoteTraceTarget`、`clientId` ——
手写的定义**一个都没覆盖**。所以：

1. **可写字段集从 API 自己推导** —— 先 GET 一个网络，响应里的键就是字段全集，
   减去只读的 `id`/`nwid`/`objtype`/`creationTime`/`revision`。
   `./scripts/ztnet.py fields` 展示的就是这个
2. **值的类型必须与现状一致** —— 挡的是 `mtu:"abc"`、`private:"yes"` 这类。
   例外：**空容器不携带类型信息**（新建网络的 `dns` 是 `[]`，设置后才变对象），
   这时只要求"是容器"
3. **只对"说错话会变危险"的字段做前置检查** —— CIDR 掩码、`mtu`/`multicastLimit` 范围。
   特别是 CIDR：非法掩码会被存成 `/0`，等于整个 IPv4 空间
4. **回读比对作总兜底** —— 写进去的和读回来不一致就报错。这条覆盖所有意料之外的字段，
   **且不需要预先知道 schema**

### 为什么必须「回读比对」

controller 的管理 API **只校验 JSON 语法，不校验语义**。不合法的值会被**静默改写**而不是报错。
实测（1.14.1 上首测，1.14.2 上逐项复现，结果完全一致）：

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
而你不会收到任何提示。所以上面第 3、4 条缺一不可。

> `create` 是先建网络再写配置（API 要求如此）。所以**初始配置校验失败时会自动回滚删掉**
> 刚建的网络 —— 不会在 controller 上留下用着默认值的孤儿网络。

---

## 成员管理

网络是 `private=true`，设备 join 之后处于**未授权**状态，什么都做不了。

```bash
docker compose exec ztplanet member.py pending               # 看哪些设备在敲门
docker compose exec ztplanet member.py authorize <地址>       # 授权（可一次多个）
docker compose exec ztplanet member.py list                  # 全部成员 + 授权状态 + 分配地址
docker compose exec ztplanet member.py ip <地址> 172.16.0.50  # 指定固定地址
docker compose exec ztplanet member.py ip <地址> --clear      # 回到自动分配
docker compose exec ztplanet member.py deauthorize <地址>     # 取消授权（保留记录）
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
| iOS / Android | 官方 app 没有可写的数据目录 —— 用内嵌 libzt 的 app，planet 随包分发 |

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

`current.c25519` / `previous.c25519`（在 `/var/lib/ztio/one/` 下）是 planet 的签名密钥。
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
向成员 POST `routes` / `nextHop` 会被静默丢弃（已核对 1.14.2 的成员字段全集）。

需要「每个节点走不同的出口」时，只能用 [应用层代理](../docs/exit-proxy.md)，不能用 IP 路由。

### 3. 不要依赖自动探测公网 IP

目标服务器上 `icanhazip` 之类的服务不可达（实测超时）。`ZTIO_PUBLIC_IP4` 必须显式填，
`deploy.sh` 也会拒绝内网地址和文档示例地址。

---

## 备份

```bash
./scripts/backup.sh              # 默认写到 /var/backups/ztio/，保留 14 份
```

归档里有两样性质完全不同的东西：

- `identity.secret` + `current/previous.c25519` —— **整套系统的根**。丢了所有设备永久失联，
  且无法用备份以外的方式恢复
- `controller.d/` —— 网络定义与成员授权。丢了要重建，但设备还在网里，重新授权即可

脚本会验证归档确实包含关键文件且能解开，而不是"看起来成功了"才收工。

> ⚠️ 归档含 planet 签名私钥 —— 拿到它的人可以冒充你的 root 并签发世界更新。
> 存到受控位置，不要进公开仓库。`backup.sh` 会把权限设为 600。

**⚠️ 写在 `/var/backups/ztio/` 只是「不在仓库里」，不是「安全」。** 它和 `/var/lib/ztio/one/`
在同一台机器上 —— 机器没了两份一起没。**必须定期把归档拷到本机或其他地方**：

```bash
# 从自己的电脑上拉回来
scp root@8.137.163.130:/var/backups/ztio/ztio-*.tar.gz ./
shasum -a 256 ztio-*.tar.gz     # 与服务器上的 sha256sum 比对
```

> 归档小（<2 KB 起），丢了 `identity.secret` 和 `current.c25519` 才是真的没救 ——
> 那意味着所有已安装的 app 永久失联，只能发新版强制用户更新，而**新版也救不回来**
> （老用户的 libzt 缓存着旧密钥签的 planet，新 planet 验签不过会被静默丢弃）。
> 详见 [`../planet/README.md`](../planet/README.md)。

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

planet 的签名密钥和 identity 都在数据卷里，所以**换镜像不影响已入网的设备**。

```bash
./scripts/backup.sh            # 先备份
$EDITOR Dockerfile             # 1) 改 ARG ZT_VERSION
                               # 2) 同时改 ZT_DEB_SHA256 —— 不改的话构建会失败，
                               #    这是刻意的：宁可失败，也不装未校验的二进制
$EDITOR docker-compose.yml     # 3) 同步 ZT_VERSION 与 image 标签
./scripts/deploy.sh            # 4) 构建 + 启动 + 验证 planet
```

升级后 `zerotier-cli` / 管理 API 的字段可能有变化，`ztnet.py` 的回读比对会发现。

`data/` 里的东西（identity、planet 签名密钥、controller 定义的网络）不受影响，
`docker compose down && up -d` 也不会动它们 —— 所以降级同样安全。

> 注意：1.14.2 是**最后一个** controller 仍在 `controller/`、受 BSL 覆盖的版本。
> 1.16.0 起 controller 移入 `nonfree/`，改为仅限非商业 —— 所以不追新。

### 成员对象支持 `name` 字段（实测确认）

ZeroTier 的成员对象**有名字这个概念**，可以直接写：

```bash
docker compose exec ztplanet member.py set <nwid> <ztaddr> --name macbook
```

实测（1.14.2 controller）：写入 `{"name":"macbook","description":"test","hostname":"mbp"}`，
回读后 **只有 `name` 留下**（revision 7 → 8），`description` / `hostname` 被丢弃。

说明 `name` 不是"任意字段透传"，而是 controller 认识的字段。

**这件事的意义**：成员的名字和地址可以存在同一个地方（controller），
DNS 记录因此能完全派生出来，不需要第二份数据 —— 名字和地址不可能漂移。

### ⚠️ 局部更新必须合并，不能替换

`build_payload` 曾经对 `dns` 这样写：

```python
dns = {"servers": []}        # 从空对象起手
dns["domain"] = args.dns_domain
p["dns"] = dns               # 整个替换
```

后果：`set --dns-domain foo` 会**顺手把已有的 servers 清成 `[]`**。
而且**回读比对发现不了** —— 它只检查你写入的键，不会发现你没写的键被抹掉。

`dns` / `v4AssignMode` / `v6AssignMode` 都是对象，三个都已改为从当前状态合并。
教训：**只给一个子字段时必须先读现状**，这让 `cmd_set` 里的读取顺序变成了硬要求。
