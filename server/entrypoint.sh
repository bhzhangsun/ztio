#!/bin/bash
# ============================================================
# ztio planet + controller 的启动脚本
#
# 职责（全部幂等，容器可反复重启）：
#   1. 灾难守卫 —— 数据卷意外为空时拒绝重建 identity
#   2. 生成/加载节点 identity
#   3. 由该 identity 生成 planet（自签名 root），并对外分发
#   4. 写 local.conf，把管理 API 锁在容器内
#   5. 启动 zerotier-one
#
# 需要持久化的三样东西，缺一不可：
#   identity.secret / identity.public  —— 节点身份，planet 的 root 就是它
#   current.c25519 / previous.c25519   —— planet 的签名密钥
#   controller.d/                      —— 网络定义与成员授权
# ============================================================
set -euo pipefail

ZT_HOME="${ZT_HOME:-/var/lib/zerotier-one}"
DIST_DIR="${DIST_DIR:-/dist}"
STATE_FILE="${ZT_HOME}/.ztio-planet-state"
MARKER_FILE="${ZT_HOME}/.ztio-initialized"

ZT_PORT="${ZTIO_ZT_PORT:-9993}"
PUBLIC_IP4="${ZTIO_PUBLIC_IP4:-}"
PUBLIC_IP6="${ZTIO_PUBLIC_IP6:-}"
FORCE_REGEN="${ZTIO_FORCE_PLANET_REGEN:-0}"

log() { printf '[ztio] %s\n' "$*"; }
die() { printf '[ztio] FATAL: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------
# 1. 灾难守卫
# ------------------------------------------------------------
# 数据卷被误挂成空目录时，绝不能让脚本"顺手"生成一套新 identity —— 那会让
# 全网已加入的设备全部失联，而且因为 planet 的 root 变了，它们连不回来。
mkdir -p "$ZT_HOME" "$DIST_DIR"

if [ -f "$MARKER_FILE" ] && [ ! -f "$ZT_HOME/identity.secret" ]; then
    die "数据目录曾初始化过（存在 ${MARKER_FILE}），但 identity.secret 不见了。
    这通常意味着数据卷挂载错误或卷被清空。
    如果确定要重建一个全新的 planet（会让所有已入网设备失联），
    请先手工删除 ${MARKER_FILE} 和 ${ZT_HOME}/controller.d 再启动。"
fi

# ------------------------------------------------------------
# 2. 节点 identity
# ------------------------------------------------------------
if [ ! -f "$ZT_HOME/identity.secret" ]; then
    log "首次启动：生成节点 identity"
    ( cd "$ZT_HOME" && zerotier-idtool generate identity.secret identity.public >/dev/null )
fi

[ -f "$ZT_HOME/identity.public" ] || die "identity.public 缺失"
IDENTITY="$(cat "$ZT_HOME/identity.public")"
NODE_ADDR="${IDENTITY%%:*}"
log "节点地址 ${NODE_ADDR}"

# ------------------------------------------------------------
# 3. 生成 planet
# ------------------------------------------------------------
if [ -z "$PUBLIC_IP4" ]; then
    die "必须设置 ZTIO_PUBLIC_IP4（本机的公网 IPv4）。
    不要依赖自动探测：目标服务器上 icanhazip 之类的服务不可达。
    阿里云 ECS 上填弹性公网 IP。"
fi

ENDPOINTS="${PUBLIC_IP4}/${ZT_PORT}"
if [ -n "$PUBLIC_IP6" ]; then
    ENDPOINTS="${ENDPOINTS},${PUBLIC_IP6}/${ZT_PORT}"
fi

# 只在 planet 缺失、端点变化或显式要求时重生成。
# 不无条件重生成的理由：每次都会产生新的时间戳，导致所有客户端看到一个新
# world 并重新握手 —— 没必要。
NEED_REGEN=1
if [ -f "$ZT_HOME/planet" ] && [ -f "$STATE_FILE" ] && [ "$FORCE_REGEN" != "1" ]; then
    if [ "$(cat "$STATE_FILE")" = "${IDENTITY}|${ENDPOINTS}" ]; then
        NEED_REGEN=0
    fi
fi

if [ "$NEED_REGEN" = "1" ]; then
    log "生成 planet：root=${NODE_ADDR} 端点=${ENDPOINTS}"

    # current.c25519 / previous.c25519 由 mkworld 在 CWD 创建并复用。
    # 它们是 planet 的签名密钥：一旦丢失或更换，已经缓存了旧 planet 的节点
    # 将永远无法接受新 planet（shouldBeReplacedBy 验签失败），只能逐台手工清理。
    ( cd "$ZT_HOME" && ZTIO_PLANET_ROOT="$IDENTITY" ZTIO_PLANET_ENDPOINTS="$ENDPOINTS" mkworld >/dev/null )

    [ -f "$ZT_HOME/world.bin" ] || die "mkworld 没有产出 world.bin"
    mv -f "$ZT_HOME/world.bin" "$ZT_HOME/planet"
    printf '%s' "${IDENTITY}|${ENDPOINTS}" > "$STATE_FILE"

    log "planet 已生成（$(wc -c < "$ZT_HOME/planet") 字节）"
else
    log "planet 已存在且端点未变，跳过生成"
fi

# 对外分发副本
cp -f "$ZT_HOME/planet" "$DIST_DIR/planet"

# ------------------------------------------------------------
# 4. local.conf
# ------------------------------------------------------------
# 管理 API（TCP 9993）只允许容器内访问。compose 刻意不发布 TCP 9993，
# 控制面脚本通过 docker exec 在容器内执行，源地址就是 127.0.0.1。
#
# 关掉 secondary/tertiary UDP 端口与 uPnP：这台机器是 root，不是叶子节点，
# 只需要 9993 一个端口可被访问。少开一个端口就少一份对外暴露面。
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

touch "$MARKER_FILE"

log "启动 zerotier-one（UDP ${ZT_PORT}）"
exec zerotier-one "$ZT_HOME"
