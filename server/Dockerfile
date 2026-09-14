# ztio planet + controller
#
# 为什么自建镜像
# --------------
# 之前用第三方镜像 xubiaolin/zerotier-planet:latest，踩到的问题全部源于它：
#   - zerotier-cli 不在 PATH，只能绕过它用 HTTP API
#   - authtoken 用 echo 写入带换行，导致 zerotier-cli 永远 401，还得运行时打补丁
#   - 捆绑 ztncui 并监听所有接口的 3443，曾被公网访问到 200
#   - 捆绑文件服务器监听 3000，同样暴露
#   - 用 latest 而不是固定版本，1.14.1 的 pin 纯属运气
#   - 需要 /dev/net/tun + NET_ADMIN + SYS_ADMIN
#
# 本镜像把这些全部收掉：固定 1.14.1、只暴露 UDP 9993、不需要 TUN、不需要特权。
#
# 为什么不从源码构建 zerotier-one
# ------------------------------
# 一开始是源码构建的（`make ZT_SSO_SUPPORTED=0 one`），但那是错的选择：
#   - 在 2 vCPU / 1.6 GB 的机器上要十几分钟，而且 `make -j2` 编译 ZeroTier 的
#     node/*.cpp 内存峰值很高，实测把整台机器压进 swap 抖动到 SSH 都无法握手
#   - 构建期强依赖 GitHub 可达，迁移性差
#
# 改成直接用预编译包：
#   - 官方 download.zerotier.com 在目标服务器上不可达（实测超时）
#   - 但南方科技大学镜像 mirrors.sustech.edu.cn/zerotier/ 可达，
#     RELEASES/<版本>/dist/debian/<codename>/ 下有各版本的 .deb
#   - 实测 3.2 MB / 1.2 秒，sha256 与官方一致（已核对）
#
# 构建时间因此从十几分钟降到十几秒，内存峰值低一个数量级。
#
# 为什么 1.14.1 而不是最新版
# -------------------------
# 1.16.0 起 ZeroTier 把 Controller 移到了 nonfree/ 目录，换成一份**仅限非商业**的
# Source-Available 许可（它把「Commercial Use」定义得涵盖非营利组织）。
# 1.14.x 仍在 BSL 1.1 下，Change Date 2026-01-01 已过 → **Apache 2.0**。
# 所以自建 controller 就该停在 1.14.x。详见 README。

# ============================================================
# 构建阶段：只编译 mkworld
# ============================================================
# mkworld 不在发行包里（它是 attic 工具），必须自己编译。
# 好在它只依赖 8 个小文件，几秒钟的事。
FROM debian:12-slim AS mkworld

ARG ZT_VERSION=1.14.1
ARG ZT_SRC_URL=https://codeload.github.com/zerotier/ZeroTierOne/tar.gz/refs/tags/${ZT_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl build-essential python3 \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "${ZT_SRC_URL}" -o /tmp/zt.tar.gz \
 && mkdir -p /src \
 && tar -xzf /tmp/zt.tar.gz -C /src --strip-components=1 \
 && rm -f /tmp/zt.tar.gz

# mkworld 上游把 root 列表硬编码在源码里（注释原话：「you must edit this file」），
# 没法在启动时按当前 identity 生成 planet。这个脚本把它改成读环境变量。
COPY patches/mkworld-env.py /tmp/mkworld-env.py
RUN python3 /tmp/mkworld-env.py /src/attic/world/mkworld.cpp

RUN cd /src/attic/world \
 && sh ./build.sh \
 && ls -l mkworld

# ============================================================
# 运行阶段
# ============================================================
FROM debian:12-slim

ARG ZT_VERSION=1.14.1
ARG ZT_DEB_URL=https://mirrors.sustech.edu.cn/zerotier/RELEASES/${ZT_VERSION}/dist/debian/bookworm/zerotier-one_${ZT_VERSION}_amd64.deb
# 换 ZT_VERSION 时**必须**同时换这个哈希，否则构建会失败。
# 这是刻意的：宁可失败，也不要在不知情的情况下装上一个未校验的二进制。
ARG ZT_DEB_SHA256=6f6f8c0ed785b5f05b8d831b4f208f23c6ea8e91220f9f7d4417123873bb0317

LABEL org.opencontainers.image.title="ztio planet+controller" \
      org.opencontainers.image.description="Self-hosted ZeroTier root (planet) and network controller, pinned to ${ZT_VERSION}" \
      org.opencontainers.image.source="https://github.com/bhzhangsun/ztio"

# libstdc++6 与 openssl(libssl3) 是 zerotier-one 的运行时依赖（见包元信息 Depends）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 jq tini libstdc++6 libssl3 \
 && rm -rf /var/lib/apt/lists/*

# 校验后解包。
#
# 注意：**不要**用 `dpkg-deb -x <deb> /` —— 实测会把镜像的 /usr/bin 清空
# （解包后 ls/grep/dpkg-deb 全部 "not found"）。解到暂存目录再挑需要的文件是安全的。
#
# 用 dpkg-deb -x 而不是 dpkg -i：后者会跑 maintainer script，容器里没有 systemd，
# 那些脚本只会报错。
#
# 解出来的 zerotier-cli 与 zerotier-idtool 是指向 zerotier-one 的符号链接
# （ZeroTier 按 argv[0] 分派），cp -a 会保留符号链接。
RUN curl -fsSL "${ZT_DEB_URL}" -o /tmp/zt.deb \
 && echo "${ZT_DEB_SHA256}  /tmp/zt.deb" | sha256sum -c - \
 && dpkg-deb -x /tmp/zt.deb /tmp/ztpkg \
 && cp -a /tmp/ztpkg/usr/sbin/. /usr/sbin/ \
 && rm -rf /tmp/ztpkg /tmp/zt.deb \
 && test -x /usr/sbin/zerotier-one \
 && test -L /usr/sbin/zerotier-cli \
 && test -L /usr/sbin/zerotier-idtool

COPY --from=mkworld /src/attic/world/mkworld /usr/local/bin/mkworld

RUN chmod 0755 /usr/local/bin/mkworld \
 && /usr/sbin/zerotier-one -v

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# ZeroTier 的数据目录。identity、planet 签名密钥、controller 的 network 定义
# 全在这里 —— 必须持久化，容器重建不能丢。
VOLUME ["/var/lib/zerotier-one"]

# 对外分发的 planet / moon。
VOLUME ["/dist"]

# 唯一需要暴露的端口。UDP 9993 是 ZeroTier 线协议，是所有节点找到这台 root 的唯一入口。
# TCP 9993 是本地管理 API —— 刻意不 EXPOSE，由 local.conf 的 allowManagementFrom 锁在容器内。
EXPOSE 9993/udp

# tini 回收僵尸进程并正确转发信号，让 docker stop 能干净地停掉 zerotier-one。
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
