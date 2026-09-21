#!/bin/bash
# ============================================================
# 备份 ztio controller 数据
#
# 备份里有两样东西，性质完全不同，必须分开对待：
#
#   1. identity.secret + current/previous.c25519
#      这是整套系统的根。丢失 = 所有设备永久失联，且**无法用备份以外的方式恢复**。
#      这两个文件还要长期保持一致：
#        - 换 identity  → planet 里 root 的公钥变了，已缓存旧 planet 的节点连不回来
#        - 换 c25519    → 新 planet 无法通过旧 planet 的验签（node/World.hpp:149），
#                         已经缓存旧 planet 的节点永远不会接受新 planet
#      换句话说：**备份不是可选项，是唯一的安全网。**
#
#   2. controller.d/
#      网络定义、成员授权、地址分配。丢了要重建，但设备只要还在网里，
#      重新授权即可恢复。
#
# 用法:
#   ./backup.sh              备份到 /var/backups/ztio/
#   ./backup.sh /path/to/dir 备份到指定目录
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$HERE/.." && pwd)"
# ── 数据根目录 ────────────────────────────────────────────────
# 与 docker-compose.yml 读的是**同一个值**（.env 的 ZTIO_DATA_ROOT）。
# 优先顺序：环境变量 > .env > 系统默认。改路径请只改 .env 一处。
if [ -f "$SERVER_DIR/.env" ]; then
    ENV_DATA_ROOT="$(sed -n 's/^ZTIO_DATA_ROOT=//p' "$SERVER_DIR/.env" | tail -1)"
fi
DATA_ROOT="${ZTIO_DATA_ROOT:-${ENV_DATA_ROOT:-/var/lib/ztio}}"
DATA_DIR="$DATA_ROOT/one"
DIST_DIR="$DATA_ROOT/dist"
BACKUP_DIR="${1:-/var/backups/ztio}"
KEEP="${ZTIO_BACKUP_KEEP:-14}"

[ -d "$DATA_DIR" ] || { echo "FATAL: 数据目录不存在：$DATA_DIR" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/ztio-$TS.tar.gz"

echo "==> 备份 $DATA_DIR"

# controller.d 可能不存在（还没创建过网络），那不是错误
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/one"

for f in identity.secret identity.public authtoken.secret \
         current.c25519 previous.c25519 planet local.conf; do
    [ -f "$DATA_DIR/$f" ] && cp -p "$DATA_DIR/$f" "$TMP/one/"
done

if [ -d "$DATA_DIR/controller.d" ]; then
    cp -rp "$DATA_DIR/controller.d" "$TMP/one/"
else
    echo "    注意：$DATA_DIR/controller.d 不存在（还没创建过网络？）"
fi

# 对外分发的 planet 也留一份，方便重建客户端
if [ -f "$DIST_DIR/planet" ]; then
    mkdir -p "$TMP/dist"
    cp -p "$DIST_DIR/planet" "$TMP/dist/"
fi

cat > "$TMP/MANIFEST.txt" <<EOF
ztio controller 备份
时间: $(date -Iseconds)
主机: $(hostname)
节点地址: $(cat "$DATA_DIR/identity.public" 2>/dev/null | cut -d: -f1)
EOF

tar -czf "$OUT" -C "$TMP" .
chmod 600 "$OUT"

# --- 验证归档真的可用，而不是"看起来成功了" ---
echo "==> 验证归档"
LIST="$(tar -tzf "$OUT")"
MISSING=0
for f in ./one/identity.secret ./one/identity.public ./one/current.c25519 ./one/previous.c25519; do
    if ! grep -qx -- "$f" <<< "$LIST"; then
        echo "    ❌ 归档里缺少 $f" >&2
        MISSING=1
    fi
done
[ "$MISSING" = "0" ] || { echo "FATAL: 备份不完整，已保留在 $OUT 供排查" >&2; exit 1; }

# 校验 tar 本身可解，能发现截断
tar -tzf "$OUT" > /dev/null

echo "    ✅ 关键文件齐全，归档可解"
echo "    大小: $(du -h "$OUT" | cut -f1)"

# --- 轮转 ---
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/ztio-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
if [ "${#OLD[@]}" -gt 0 ]; then
    echo "==> 清理超出 $KEEP 份的旧备份"
    for f in "${OLD[@]}"; do
        rm -f "$f"
        echo "    已删除 $(basename "$f")"
    done
fi

echo
echo "备份完成：$OUT"
echo
echo "⚠️  这个归档里有 identity.secret 和 planet 签名私钥 ——"
echo "    拿到它的人可以冒充你的 root 并签发世界更新。请存在受控位置，不要进公开仓库。"
echo
echo "恢复方式："
echo "    docker compose down"
echo "    tar -xzf $OUT -C $DATA_ROOT"
echo "    docker compose up -d"
echo
echo "    ⚠️  解包目标必须是 $DATA_ROOT（.env 的 ZTIO_DATA_ROOT），"
echo "        不是仓库里的目录 —— 归档内是 ./one/ 与 ./dist/，"
echo "        解错地方会让容器看到空数据目录，进而生成全新的"
echo "        identity 与 planet，所有已入网设备都会失联。"
