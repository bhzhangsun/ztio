# planet —— 本部署的世界定义

`planet` 就是 ZeroTier 的 **World** 文件（257 字节二进制）。它是**公开信息**：
只有公钥、签名和端点地址，**没有任何私钥**。见下面「里面有什么」。

**它进仓库是有意的** —— 它是 app 的构建资产，不是运行时产物。理由见「为什么放在这里」。

---

## 当前这份

| 项 | 值 |
|---|---|
| 世界 ID | `149604618` —— 与官方相同，这是**必须**的（客户端只在同 ID 下比较新旧） |
| 类型 | `1`（PLANET） |
| root 地址 | `46e6a67371` |
| root 端点 | `8.137.163.130/9993` |
| 时间戳 | `1789381726000` |
| md5 | `dae4cd32e5f8fdfad25b6fabe1a26718` |
| 网络 ID | `46e6a673718e988e`（前 10 位 = controller 地址 = root 地址） |

来自 2026-09-14 的部署，和 `server/data/dist/planet` 逐字节一致。

---

## 里面有什么

按 `node/World.hpp` 的序列化格式逐字节解出来的：

```
[0]         01                 type = PLANET
[1..8]      0000000008eac90a   世界 ID = 149604618
[9..16]     000001a09f764f30   时间戳
[17..80]    10b7d962…          验签公钥      64 字节   ← 公钥
[81..176]   924653d6…          签名          96 字节
[177]       01                 root 数量 = 1
[178..182]  46e6a67371         root 地址
[183]       00                 identity 类型（0 = 公钥身份）
[184..247]  31decfc4…         root 公钥     64 字节   ← 公钥
[248]       00                 ★ 私钥长度 = 0
[249]       01                 端点数量 = 1
[250]       04                 IPv4
[251..254]  0889a382           8.137.163.130
[255..256]  2709               端口 9993
```

**没有私钥，两重证据**：

1. 偏移 248 是 `Identity` 结构的**私钥长度字段**，值是 `0` —— 这个结构本身就不带私钥
2. 用 `data/one/identity.secret` 的**私钥段**（blob 的后 64 字节）全文和分片搜过，**零命中**

> 注意 `identity.secret` 的 blob 是 `公钥(64B) + 私钥(64B)` 拼在一起的。
> 只搜整段会命中**公钥段**（那是应该命中的，planet 必须含 root 公钥）。
> 判断有没有泄露必须**切开只搜私钥段**。

---

## 为什么放在这里

**因为移动端没有别的路。**

官方客户端没有「自定义 planet」这个功能：

| 平台 | 官方客户端 |
|---|---|
| Linux / macOS / Windows | 能生效，但要往数据目录**手工丢文件**、要管理员权限。不是功能，是副作用 |
| **iOS / Android 官方 app** | **做不到** —— 数据目录在 app 私有沙箱里，无处可放 |

所以对用户分发来说，**自带 app 是唯一通道**，planet 必须随包发。既然如此，
它就是 app 的构建资产 —— 和图标、字体同性质 —— 那它就该在仓库里。

顺带它还能当官方桌面客户端的「分发副本」，一份东西两个用途。

---

## ⚠️ 一条硬规则：root 身份和签名密钥不能丢

planet 是**可再生的**，但**不是随便再生**。ZeroTier 接受一个 planet 的条件是
（`node/World.hpp` 的 `shouldBeReplacedBy`）：

```cpp
if ((_id == 0) || (_type == TYPE_NULL)) {
    return true;                     // 空状态：无条件接受，不验签
}
if ((_id == update._id) && (_ts < update._ts) && (_type == update._type)) {
    return C25519::verify(_updatesMustBeSignedBy, ...);   // 才有验签
}
return false;
```

翻译成后果：

| 设备状态 | 换 planet 会怎样 |
|---|---|
| **全新安装** | 无条件接受。谁签的都不看 |
| **已有 planet 缓存** | 必须**时间戳更新** + **用已缓存那把密钥验签通过**，缺一不可 |
| **密钥丢了** | 新 planet 被拒；官方 planet 因为太旧也被拒 → **设备永久卡在旧 planet 上** |

所以下面这三个文件**必须活着**，它们是「以后重新部署生成的 planet 仍被老用户接受」的唯一保证：

```
server/data/one/identity.secret      root 身份；planet 里的 root 公钥就是它
server/data/one/current.c25519       世界更新签名密钥
server/data/one/previous.c25519      上一代，轮换时用
```

`server/scripts/backup.sh` 会把它们连同 `controller.d` 一起打包。
**归档含私钥，绝不能进本仓库**（`.gitignore` 已挡 `backups/` 与 `*.tar.gz`）。

---

## app 怎么用它

### 1. 作为默认 planet

libzt 提供了直接设根的接口（`include/ZeroTierSockets.h`）：

```c
/**
 * @brief Present a root set definition for ZeroTier to use instead of the default.
 * This is an initialization function that can only be called before `zts_node_start()`.
 */
ZTS_API int ZTCALL zts_init_set_roots(const void* roots_data, unsigned int len);
```

**必须在 `zts_node_start()` 之前调用** —— 顺序错了会静默退回官方根，从现象上完全看不出原因。

> ⚠️ **待验证（V6）**：`roots_data` 到底是本文件的 **planet 二进制**，还是扁平的
> `zts_root_set_t`（`{ char* public_id_str[16]; char* endpoint_ip_str[16][32]; }`）。
> 头文件只写了 "binary"。**动手前先确认**，否则会白写一遍。
> 见 `app/README.md` 的待验证表。

### 2. 给用户一个更新 planet 的入口

在 app 里暴露「导入 planet」——从文件选、或粘贴内容，写进 app 可写目录，
下次启动时读它调用 `zts_init_set_roots`。这样不用发新版 app 就能推新 planet。

**但只做这一步，在密钥丢失时会静默失效。** 原因就是上面那张表：老用户的 libzt
缓存着用旧密钥签的 planet，你导入的新 planet 验签不过，**被默默拒掉**，
用户以为更新成功了。

**解法二选一：**

- 调 `zts_init_allow_roots_cache(0)`（libzt 提供，见头文件）—— 节点不再缓存根，
  `_planet` 每次启动都是空的，于是**无条件接受 app 给的那份**
- 或者 app 自己控制 libzt 的存储回调，**不回送**上次存的 planet

**推荐前者**，一行代码，把「planet 由 app 版本决定」这件事变得确定。

---

## 怎么更新这个文件

重新部署后：

```bash
# 服务器上
cd /root/ztio/server
./scripts/deploy.sh                 # 结束时会打印 planet md5
cp data/dist/planet ../../planet/planet

# 本地校验
md5sum planet/planet                # 必须与 deploy.sh 打印的一致
```

然后在 `server/README.md` 的表格里核对该 md5。

> **提交一份「旧」的 planet 无害**：全新安装会接受它，已有缓存的设备会忽略它。
> 但如果 root 身份或签名密钥变了，**它就不是「旧」而是「无效」** —— 老用户直接失联。
> 更新前先确认密钥没变（比对 `identity.public` 的地址）。

---

## 校验

```bash
# 大小必须是 257 字节
wc -c planet/planet

# 世界 ID 必须是 149604618（第 1-8 字节，大端）
xxd -s 1 -l 8 -p planet/planet        # 应为 0000000008eac90a

# root 地址必须是 46e6a67371（第 178-182 字节）
xxd -s 178 -l 5 -p planet/planet      # 应为 46e6a67371

# 私钥长度字段必须是 0（第 248 字节）
xxd -s 248 -l 1 -p planet/planet      # 应为 00
```
