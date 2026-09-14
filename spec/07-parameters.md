# 07 · 参数表

所有超时、重试、阈值集中在此，便于调优与跨平台统一。

## 7.1 引导

| 参数 | 默认值 | 说明 |
|---|---|---|
| `BOOTSTRAP_TIMEOUT` | 3000 ms | 单次查花名册的超时 |
| `BOOTSTRAP_RETRIES` | 3 | 失败重试次数 |
| `BOOTSTRAP_BACKOFF_BASE` | 500 ms | 指数退避基数 |
| `BOOTSTRAP_BACKOFF_MAX` | 60000 ms | 退避上限 |
| `ROSTER_CACHE_TTL` | 300 s | `peerName → ZT 地址` 映射缓存时长 |
| `ZT_JOIN_TIMEOUT` | 15000 ms | 等待 libzt 网络就绪 |

> **注意**：`ROSTER_CACHE_TTL` 只适用于 **ZT 地址**。
> **局域网候选永不缓存。**

## 7.2 信令

| 参数 | 默认值 | 说明 |
|---|---|---|
| `SIGNALING_TIMEOUT` | 5000 ms | 建立信令会话 |
| `SIGNALING_MSG_TIMEOUT` | 3000 ms | 单条消息的确认超时 |
| `SIGNALING_MSG_RETRIES` | 2 | 消息重发次数 |
| `SIGNALING_PING_INTERVAL` | 15000 ms | 保活间隔 |
| `SIGNALING_DEAD_MULTIPLIER` | 3 | 连续 3 次 ping 无响应判定会话死亡 |
| `SIGNALING_MAX_MSG_SIZE` | 65536 B | 单条消息上限（防滥用） |

## 7.3 候选枚举

| 参数 | 默认值 | 说明 |
|---|---|---|
| `CANDIDATE_RESCAN_INTERVAL` | 30000 ms | 定期重扫接口 |
| `CANDIDATE_MAX_COUNT` | 16 | 单次上报的候选上限 |
| `CANDIDATE_DEBOUNCE` | 500 ms | 网络变化去抖窗口 |

## 7.4 探测与选路

| 参数 | 默认值 | 说明 |
|---|---|---|
| `PROBE_TIMEOUT` | 1500 ms | **单候选探测超时** |
| `PROBE_WINDOW` | 2000 ms | **整体探测窗口** |
| **`LAN_PROBE_BUDGET`** | **2000 ms** | **局域网路径的总预算（硬约束）** |
| `PROBE_MAX_CONCURRENCY` | 16 | 并发探测上限 |
| `PROBE_CONNECT_RETRIES` | 1 | 单候选重试次数 |
| `SWITCH_COOLDOWN` | 3000 ms | 切换后冷却期 |
| `SWITCH_HYSTERESIS` | 1 档 | 需严格更优一个档位才切换 |

> **`LAN_PROBE_BUDGET` 是最重要的一个参数。**
> 它是「用户感知」与「探测充分性」之间的平衡点。
> 超过它收益极小，代价是明显的卡顿感。

## 7.5 自愈

| 参数 | 默认值 | 说明 |
|---|---|---|
| `RESELECT_MIN_INTERVAL` | 2000 ms | 两次完整重探测的最小间隔 |
| `PATH_HEALTH_PING` | 5000 ms | 数据面健康检查间隔 |
| `PATH_LOST_THRESHOLD` | 3 | 连续失败次数达到即判定路径失效 |
| `WARM_PROBE_INTERVAL` | 30000 ms | 温候选的轻量重探测间隔 |

## 7.6 应用层（非 ztio 参数，供上层参考）

| 参数 | 建议值 | 说明 |
|---|---|---|
| 出口接口绑定 | 平台相关 | 见 08 §8.4 |
| 数据面心跳 | 10000 ms | 与信令保活相互独立 |

---

## 7.7 调优指引

| 场景 | 调整 | 代价 |
|---|---|---|
| 局域网探测总失败，想确认是不是探测太快 | 提高 `PROBE_TIMEOUT` 到 3000 ms | 连接建立变慢；**若 3000 ms 仍失败，基本可确认不是超时问题** |
| 移动端切换过于频繁 | 提高 `SWITCH_COOLDOWN` | 网络恢复后切回变慢 |
| 电量敏感 | 降低 `CANDIDATE_RESCAN_INTERVAL` 频率、提高 `WARM_PROBE_INTERVAL` | 自愈变慢 |
| 大量设备 | 提高 `CANDIDATE_MAX_COUNT` | 信令报文变大 |

## 7.8 诊断上报

建议 SDK 暴露以下计数（用于判断降级原因）：

| 指标 | 用途 |
|---|---|
| `probe.lan.success` / `probe.lan.timeout` / `probe.lan.refused` | **区分隔离与权限问题** |
| `probe.zt.direct` / `probe.zt.relay` | 判断 ZT 是否退化到中继 |
| `switch.count` / `switch.reason` | 判断网络是否不稳定 |
| `bootstrap.cache_hit` | 判断锚点是否健康 |

**`probe.lan.timeout` 与 `probe.lan.refused` 的区别是关键**：

- `refused`（收到 RST）→ 主机可达，端口没开 → **配置/端口问题**
- `timeout`（无响应）→ 包被丢弃 → **客户端隔离**（或权限被拒）

这个区分直接决定用户该做什么。
