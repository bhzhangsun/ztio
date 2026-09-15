#!/bin/bash
# ============================================================
# ztio-dns 的启动脚本
#
# 职责（除最后一步外全部幂等）：
#   1. 灾难守卫 —— 数据卷意外为空时不要"顺手"生成新身份
#   2. 生成/加载自己的节点 identity
#   3. 装上**我们自己的** planet
#   4. 写 local.conf（换端口、API 锁在容器内）
#   5. 后台启动 zerotier-one，加入网络
#   6. 把本节点地址写到共享文件，供控制面脚本读取
#   7. 前台启动 DNS（成为容器的主进程）
#
# 和 planet 容器的三处关键差异：
#   - 用**另一个** UDP 端口：9993 已被 planet 占用
#   - 必须装我们自己的 planet：否则节点会去连官方 root，永远找不到我们的网络
#   - 会加入网络，因此需要 /dev/net/tun 与 NET_ADMIN（在 compose 里给）
# ============================================================
set -euo pipefail

ZT_HOME="${ZT_HOME:-/var/lib/zerotier-one}"
DIST_DIR="${DIST_DIR:-/dist}"
CTR_DIR="${CTR_DIR:-/ctr}"
RUN_DIR="${RUN_DIR:-/run/ztio}"
MARKER_FILE="${ZT_HOME}/.ztio-initialized"
ADDR_FILE="${RUN_DIR}/dns-node-address"

ZT_PORT="${ZTIO_DNS_ZT_PORT:-9994}"
NWID="${ZTIO_NWID:-}"

log() { printf '[ztio-dns] %s\n' "$*"; }
die() { printf '[ztio-dns] FATAL: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------
# 1. 灾难守卫
# ------------------------------------------------------------
mkdir -p "$ZT_HOME" "$RUN_DIR"

if [ -f "$MARKER_FILE" ] && [ ! -f "$ZT_HOME/identity.secret" ]; then
    die "数据目录曾初始化过，但 identity.secret 不见了。
    这通常意味着数据卷挂载错误或被清空。
    本节点的身份丢失后可以重建（重新加入网络即可），但**地址会变**，
    控制面上的 dns.servers 和成员记录都要跟着改。
    确定要继续的话，删掉 ${MARKER_FILE} 再启动。"
fi

# ------------------------------------------------------------
# 2. 身份
# ------------------------------------------------------------
if [ ! -f "$ZT_HOME/identity.secret" ]; then
    log "首次启动：生成节点 identity"
    ( cd "$ZT_HOME" && zerotier-idtool generate identity.secret identity.public >/dev/null )
fi

[ -f "$ZT_HOME/identity.public" ] || die "identity.public 缺失"
IDENTITY="$(cat "$ZT_HOME/identity.public")"
NODE_ADDR="${IDENTITY%%:*}"
log "节点地址 ${NODE_ADDR}"

# 控制面脚本（ztnet.py dns-setup）需要知道这个地址才能把成员建对。
# 写文件而不是让它 docker exec 进来读：脚本在 planet 容器里跑，跨容器执行很别扭。
printf '%s' "$NODE_ADDR" > "$ADDR_FILE"
chmod 0644 "$ADDR_FILE"

# ------------------------------------------------------------
# 3. 装我们自己的 planet
# ------------------------------------------------------------
# 这一条漏了，节点会去连**官方** ZeroTier root，然后永远找不到我们的网络 ——
# 症状是 listpeers 里出现一堆官方 IP，join 一直不成功。
[ -f "$DIST_DIR/planet" ] || die "${DIST_DIR}/planet 不存在。
    本节点必须用我们自己的世界文件，否则连不到自建 controller。
    确认 planet 容器已经跑过（它负责生成并分发 planet）。"

if ! cmp -s "$DIST_DIR/planet" "$ZT_HOME/planet"; then
    cp -f "$DIST_DIR/planet" "$ZT_HOME/planet"
    log "已装入 planet（$(wc -c < "$ZT_HOME/planet") 字节）"
else
    log "planet 已是最新，跳过"
fi

# ------------------------------------------------------------
# 4. local.conf
# ------------------------------------------------------------
# primaryPort 换到 9994：宿主机上 9993 已被 planet 占用（host 网络）。
#
# allowManagementFrom 锁在容器内 —— 这个节点的管理 API 没有理由被别的容器
# 或宿主机访问。控制面只在需要时 docker exec 进来。
#
# 关掉 secondary/tertiary 端口：叶子节点只需要一个端口可被访问。
cat > "$ZT_HOME/local.conf" <<EOF
{
  "settings": {
    "primaryPort": ${ZT_PORT},
    "allowSecondaryPort": false,
    "portMappingEnabled": false,
    "allowManagementFrom": [ "127.0.0.1", "::1" ]
  }
}
EOF

chmod 700 "$ZT_HOME" 2>/dev/null || true
for _f in identity.secret authtoken.secret metricstoken.secret; do
    [ -f "$ZT_HOME/$_f" ] && chmod 600 "$ZT_HOME/$_f" 2>/dev/null || true
done

touch "$MARKER_FILE"

# ------------------------------------------------------------
# 5. 后台启动 zerotier-one
# ------------------------------------------------------------
log "启动 zerotier-one（UDP ${ZT_PORT}，bridge 网络）"
zerotier-one "$ZT_HOME" &
ZT_PID=$!

# 容器收到 TERM 时把子进程一起带走，否则 docker stop 要等超时才强杀。
cleanup() { kill "$ZT_PID" 2>/dev/null || true; }
trap cleanup TERM INT EXIT

# 等本地管理 API 可用
for _ in $(seq 1 60); do
    if zerotier-cli info >/dev/null 2>&1; then break; fi
    sleep 1
done
zerotier-cli info >/dev/null 2>&1 || die "60 秒内 zerotier-one 的管理 API 没起来"

# ------------------------------------------------------------
# 6. 加入网络
# ------------------------------------------------------------
if [ -n "$NWID" ]; then
    if zerotier-cli listnetworks 2>/dev/null | grep -q "$NWID"; then
        log "已在网络 ${NWID} 中"
    else
        log "加入网络 ${NWID}"
        zerotier-cli join "$NWID" || log "join 返回非零（可能已经在加入中）"
    fi
else
    log "未设置 ZTIO_NWID，跳过加入网络 —— DNS 将没有可服务的接口"
fi

# ------------------------------------------------------------
# 7. 前台启动 DNS
# ------------------------------------------------------------
# DNS 绑定在 ZT 接口的地址上（网段第一个 IP），那个地址要等控制面授权之后
# 才会出现 —— 所以 ztio_dns.py 自己会重试绑定，这里不阻塞等待。
#
# 参数：
#   --ctr      controller.d/network 的只读挂载点，记录来源
#   --run-dir  共享目录（节点地址文件）
exec python3 /usr/local/bin/ztio_dns.py \
    --ctr "$CTR_DIR" \
    --nwid "$NWID"
