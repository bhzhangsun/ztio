# ztio-dns —— ZeroTier 网络内部的静态 DNS
#
# 为什么和 planet 分开成两个容器
# ------------------------------
# planet 容器刻意不带 /dev/net/tun 和 NET_ADMIN，那条注释写得很清楚：
#   「planet + controller 不加入任何网络，不需要虚拟网卡」
# 这个权限收窄是对的，而且在 planet 只做控制面时完全成立。
#
# 但 DNS 是**数据面**服务：客户端要往一个 IP（<网段第一个地址>:53）发 UDP 包，
# 而这个 IP 要可达，节点就必须真的在那个网络里 —— 于是它需要建虚拟网卡。
#
# 两条路：给 planet 加 TUN + NET_ADMIN，或者拆一个容器出来。
# 选后者，因为 planet 持有 planet 的**签名私钥** —— 泄露等于把所有用户的设备
# 指向攻击者的根。它是最不该拥有「改网络」能力的组件。
# 拆开之后还有一个额外收益：用 bridge 网络时 NET_ADMIN 被限制在容器自己的
# 网络命名空间里，够不到宿主机的路由和网卡。
#
# 为什么用 bridge 而不是 host
# ---------------------------
# planet 必须用 host：它是 root，VL1 依据源地址判断对端路径，bridge + DNAT
# 会让它看到的地址与真实路径不一致，而 root 的职责恰恰是协助两端打洞。
#
# DNS 是**叶子节点**，跟普通客户端一样。普通客户端躲在 NAT 后面是常态，
# ZeroTier 本来就为此设计。所以 bridge 足够，而且权限更收敛。
#
# 代价是必须发布 UDP 端口，否则只能走 relay —— 而 DNS 在每次名字解析的
# 关键路径上，绕中继意味着每次查询都多一跳公网延迟。

FROM debian:12-slim

ARG ZT_VERSION=1.14.2
ARG ZT_DEB_URL=https://mirrors.sustech.edu.cn/zerotier/RELEASES/${ZT_VERSION}/dist/debian/bookworm/zerotier-one_${ZT_VERSION}_amd64.deb
# 与 planet 镜像同一个包、同一个哈希。换版本时必须同时改这里和 planet 的
# Dockerfile —— 宁可构建失败，也不要装上未校验的二进制。
ARG ZT_DEB_SHA256=4be52f75df3557da57cf09979366a316cbe9bde618008d1031ccaa3babd163a0

LABEL org.opencontainers.image.title="ztio dns" \
      org.opencontainers.image.description="Static DNS served inside a self-hosted ZeroTier network" \
      org.opencontainers.image.source="https://github.com/bhzhangsun/ztio"

# iproute2 用来读接口地址；python3 跑 DNS 服务。
# adduser / openssl 是 zerotier-one 的声明依赖。
# iputils-ping / tcpdump / netcat 是**诊断**工具，不是运行依赖。
# 放在这里是有理由的：这个容器的全部工作就是网络，而出问题时最常见的症状是
# "客户端超时"—— 此时唯一能区分"包没到"和"包到了但没回"的手段就是抓包。
# 一个网络服务容器没有抓包能力，等于每次故障都只能靠猜。
# 它们的体积代价很小（tcpdump 约 1 MB），远小于一次误判的代价。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl iproute2 tini adduser openssl \
      python3 python3-pip \
      tcpdump iputils-ping netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "${ZT_DEB_URL}" -o /tmp/zt.deb \
 && echo "${ZT_DEB_SHA256}  /tmp/zt.deb" | sha256sum -c - \
 && dpkg -i /tmp/zt.deb \
 && rm -f /tmp/zt.deb \
 && test -x /usr/sbin/zerotier-one \
 && test -L /usr/sbin/zerotier-cli

# dnslib 处理 DNS 报文的边角：压缩指针、EDNS0、TCP fallback、大小写随机化。
# 这些自己写会烧掉大量时间，而且**测不出来** —— 因为测试客户端是 dig，
# 不是 macOS/iOS 的 mDNSResponder。要用的是「静态表 + 隔离策略」，
# 不是 DNS 协议栈。
#
# --break-system-packages：Debian 12 的 PEP668 保护。容器里没有系统 Python
# 的其它使用者，装进系统 site-packages 是合适的。
RUN pip install --no-cache-dir --break-system-packages dnslib \
 && python3 -c "import dnslib; print('dnslib', dnslib.version)"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY ztio_dns.py   /usr/local/bin/ztio_dns.py
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/ztio_dns.py

# 自己的身份和数据目录 —— 与 planet 的完全分开。
# 丢了只是重新加入网络（地址会变，dns.servers 要跟着改），
# 不像 planet 的 identity.secret 那样一丢就全网失联。
VOLUME ["/var/lib/zerotier-one"]

# ZeroTier 线协议端口。**必须发布**，否则节点只能走 relay。
EXPOSE 9994/udp

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
