#!/bin/bash
# ============================================================
# 部署 ztio planet + controller，并**验证它真的在提供正确的 planet**。
#
# 为什么要验证而不是只看容器起没起：
#   这套东西最常见的失败不是"容器挂了"，而是"容器好好的，但提供的 planet
#   不是你要的那个" —— 比如端点 IP 写错、planet 用了旧的缓存、root 身份对不上。
#   这些情况下 curl /status 一样返回 200，节点一样显示 ONLINE，但设备之间
#   永远连不通。所以这里把 planet 解出来逐项核对。
#
# 用法:
#   ./deploy.sh            构建并启动
#   ./deploy.sh --no-build 不重新构建（改了 .env 或只是重启时用）
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$HERE/.." && pwd)"
cd "$SERVER_DIR"

BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

die() { printf '\n❌ %s\n' "$*" >&2; exit 1; }
ok()  { printf '   ✅ %s\n' "$*"; }

# ------------------------------------------------------------
# 1. 前置检查
# ------------------------------------------------------------
echo "==> 前置检查"

command -v docker >/dev/null || die "没有 docker"
docker compose version >/dev/null 2>&1 || die "没有 docker compose 插件"

[ -f .env ] || die "缺少 .env。先 \`cp .env.example .env\` 并填好 ZTIO_PUBLIC_IP4。"
# shellcheck disable=SC1091
set -a; . ./.env; set +a

[ -n "${ZTIO_PUBLIC_IP4:-}" ] || die ".env 里的 ZTIO_PUBLIC_IP4 是空的"
if [ "$ZTIO_PUBLIC_IP4" = "203.0.113.10" ]; then
    die ".env 里还是 .env.example 的示例地址 203.0.113.10（RFC 5737 文档用址）。
    请填本机真实的公网 IPv4。阿里云 ECS 上填弹性公网 IP。"
fi
case "$ZTIO_PUBLIC_IP4" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
        die "ZTIO_PUBLIC_IP4=$ZTIO_PUBLIC_IP4 是内网地址。
    planet 里写内网地址，外网设备永远找不到这台 root。" ;;
esac
ok "公网 IPv4 = $ZTIO_PUBLIC_IP4"

ZT_PORT="${ZTIO_ZT_PORT:-9993}"

# 端口占用检查（network_mode: host，所以直接看主机）
if command -v ss >/dev/null; then
    if ss -lunp 2>/dev/null | grep -q ":${ZT_PORT}\b"; then
        OWNER="$(ss -lunp 2>/dev/null | grep ":${ZT_PORT}\b" | head -1)"
        if ! echo "$OWNER" | grep -q ztplanet; then
            die "UDP ${ZT_PORT} 已被占用：$OWNER
    如果是旧的 ztplanet 容器，先 \`docker compose down\` 或 \`docker rm -f ztplanet\`。"
        fi
    fi
fi
ok "UDP ${ZT_PORT} 可用"

# ------------------------------------------------------------
# 2. 构建并启动
# ------------------------------------------------------------
if [ "$BUILD" = "1" ]; then
    echo "==> 构建镜像（从源码编译 ZeroTier 1.14.1，首次约数分钟）"
    docker compose build
fi

echo "==> 启动"
docker compose up -d

# ------------------------------------------------------------
# 3. 等待就绪
# ------------------------------------------------------------
TOKEN_FILE="$SERVER_DIR/data/one/authtoken.secret"
echo "==> 等待 controller 就绪"
for i in $(seq 1 60); do
    if [ -f "$TOKEN_FILE" ] && curl -fsS -m 2 \
        -H "X-ZT1-Auth: $(cat "$TOKEN_FILE")" \
        http://127.0.0.1:9993/status >/dev/null 2>&1; then
        ok "管理 API 已响应（${i}s）"
        break
    fi
    [ "$i" = "60" ] && die "60 秒内管理 API 没有响应。看日志：docker compose logs"
    sleep 1
done

# ------------------------------------------------------------
# 4. 验证 planet
# ------------------------------------------------------------
echo "==> 验证 planet"

PLANET="$SERVER_DIR/data/one/planet"
[ -f "$PLANET" ] || die "planet 不存在：$PLANET"

python3 - "$PLANET" "$SERVER_DIR/data/one/identity.public" "$ZTIO_PUBLIC_IP4" "$ZT_PORT" <<'PY' || exit 1
import ipaddress, sys

planet_path, ident_path, expect_ip, expect_port = sys.argv[1:5]
raw = open(planet_path, "rb").read()
ident = open(ident_path).read().strip()
# identity.public 的格式是 "<10位地址>:0:<公钥hex>" —— 必须按前两个冒号切，
# 用 partition(":") 会在第一个冒号处切开，拿到 "0:<hex>"，fromhex 会直接抛异常。
parts = ident.split(":", 2)
if len(parts) != 3:
    print("   ❌ identity.public 格式不对：%r" % ident[:40]); sys.exit(1)
addr, pubkey = parts[0], parts[2]
want_key = bytes.fromhex(pubkey)
want_ip = ipaddress.IPv4Address(expect_ip).packed
want_port = int(expect_port)

ok = True

wtype = raw[0]
if wtype != 1:
    print("   ❌ planet 类型是 %d，应为 1（PLANET）" % wtype); ok = False
else:
    print("   ✅ 类型 PLANET")

wid = int.from_bytes(raw[1:9], "big")
if wid != 149604618:
    print("   ❌ 世界 ID 是 %d，应为 149604618（ZT_WORLD_ID_EARTH）" % wid); ok = False
else:
    print("   ✅ 世界 ID 149604618（与官方一致 —— 这是必须的，客户端只在同 ID 下比较新旧）")

ts = int.from_bytes(raw[9:17], "big")
import datetime
print("   ℹ️  世界时间戳 %d（%s）" % (ts, datetime.datetime.utcfromtimestamp(ts/1000).strftime("%Y-%m-%d")))

if want_key not in raw:
    print("   ❌ planet 里找不到 root 的公钥 %s…" % pubkey[:24]); ok = False
else:
    print("   ✅ root 公钥与 identity.public 一致（root 地址 %s）" % addr)

if want_ip not in raw:
    print("   ❌ planet 里找不到公网 IP %s 的字节" % expect_ip); ok = False
else:
    print("   ✅ 端点含 %s" % expect_ip)

# 端点端口：planet 里以 2 字节大端跟随 IPv4 地址
found_port = False
for i in range(len(raw) - 5):
    if raw[i:i+4] == want_ip and raw[i+4:i+6] == want_port.to_bytes(2, "big"):
        found_port = True; break
if not found_port:
    print("   ❌ %s 后面跟的端口不是 %d" % (expect_ip, want_port)); ok = False
else:
    print("   ✅ 端点端口 %d" % want_port)

sys.exit(0 if ok else 1)
PY

# ------------------------------------------------------------
# 5. 汇总
# ------------------------------------------------------------
FINGERPRINT="$(md5sum "$PLANET" | cut -d' ' -f1)"
NODE_ADDR="$(cut -d: -f1 "$SERVER_DIR/data/one/identity.public")"

echo
echo "════════════════════════════════════════════════════════"
echo " ztio planet + controller 已就绪"
echo "════════════════════════════════════════════════════════"
echo " 节点地址（= planet 的 root）  : $NODE_ADDR"
echo " 世界 ID                       : 149604618（与官方相同，靠签名区分）"
echo " 端点                          : $ZTIO_PUBLIC_IP4/$ZT_PORT"
echo " planet md5                    : $FINGERPRINT"
echo " 分发副本                      : $SERVER_DIR/data/dist/planet"
echo
echo " 下一步：创建网络"
echo "   ./scripts/apply-network.py"
echo
echo " 客户端接入：把 data/dist/planet 覆盖到设备的 ZeroTier 数据目录，"
echo " 然后校验 md5 必须是 $FINGERPRINT"
echo
echo " 安全组只需放行：UDP $ZT_PORT 入站"
echo " 确认**没有**放行：TCP 9993 / 3443 / 3000"
echo "════════════════════════════════════════════════════════"
