#!/usr/bin/env python3
"""
把 ZeroTierOne 1.14.1 的 attic/world/mkworld.cpp 改成从环境变量读 root 配置。

为什么需要它
------------
上游的 mkworld 把 root 列表硬编码在源码里，注释写得很直白：
    "If you want to make your own World you must edit this file."
所以它没法在容器启动时按当前 identity 生成 planet —— 除非改源码。

为什么是脚本而不是 .patch 文件
------------------------------
mkworld.cpp 的文件头是 GPLv3（仓库其余部分是 BSL），把改过的整份文件或大段
diff 收进本仓库会带来许可问题。这里只保留我们**新写入的十几行**，在构建时
就地操作上游源码。

改动范围
--------
只替换 `// EDIT BELOW HERE` 与 `// END WORLD DEFINITION` 之间的硬编码 root 段。

生成 planet 需要的三个输入（全部来自环境变量）：
    ZTIO_PLANET_ROOT        root 节点的 identity 字符串，即 identity.public 的内容
                            形如 "<10位地址>:0:<96字节公钥hex>"
    ZTIO_PLANET_ENDPOINTS  逗号分隔的 "<ip>/<port>" 列表
    ZTIO_PLANET_TS          （可选）世界时间戳，毫秒。缺省取当前时间。

用法
----
    python3 mkworld-env.py /path/to/ZeroTierOne/attic/world/mkworld.cpp
"""

import re
import sys

BEGIN = "\tstd::vector<World::Root> roots;"
END = "\t// END WORLD DEFINITION"

REPLACEMENT = r"""	std::vector<World::Root> roots;

	const uint64_t id = ZT_WORLD_ID_EARTH;

	// 时间戳必须是「毫秒」，且新 planet 要比节点已缓存的旧 planet 更新，
	// 否则 shouldBeReplacedBy() 会拒绝替换（见 node/World.hpp:149）。
	// 注意：即使时间戳更新，也必须在签名密钥一致的前提下才可能被接受 ——
	// 这就是 previous.c25519 / current.c25519 必须持久化的原因。
	uint64_t ts = (uint64_t)::time((time_t *)0) * 1000ULL;
	{
		const char *env_ts = ::getenv("ZTIO_PLANET_TS");
		if ((env_ts)&&(env_ts[0])) {
			ts = (uint64_t)::strtoull(env_ts,(char **)0,10);
		}
	}

	{
		const char *env_root = ::getenv("ZTIO_PLANET_ROOT");
		if ((!env_root)||(!env_root[0])) {
			fprintf(stderr,"FATAL: ZTIO_PLANET_ROOT is not set (expected the contents of identity.public)" ZT_EOL_S);
			return 1;
		}

		const char *env_ep = ::getenv("ZTIO_PLANET_ENDPOINTS");
		if ((!env_ep)||(!env_ep[0])) {
			fprintf(stderr,"FATAL: ZTIO_PLANET_ENDPOINTS is not set (expected comma-separated ip/port)" ZT_EOL_S);
			return 1;
		}

		roots.push_back(World::Root());
		// Identity 只接受 const char*（node/Identity.hpp:56），传 std::string 会编译失败
		roots.back().identity = Identity(env_root);

		// 逗号分隔，容忍空格与空项
		std::string eps(env_ep);
		std::string cur;
		for (unsigned long i=0;i<=eps.length();++i) {
			const char c = (i < eps.length()) ? eps[i] : ',';
			if (c == ',') {
				if (cur.length() > 0) {
					// InetAddress 只接受 const char*（node/InetAddress.hpp:93），
					// 传 std::string 会编译失败
					roots.back().stableEndpoints.push_back(InetAddress(cur.c_str()));
					cur.clear();
				}
			} else if ((c != ' ')&&(c != '\t')) {
				cur.push_back(c);
			}
		}

		if (roots.back().stableEndpoints.empty()) {
			fprintf(stderr,"FATAL: ZTIO_PLANET_ENDPOINTS contained no usable endpoint" ZT_EOL_S);
			return 1;
		}
	}

	// END WORLD DEFINITION"""


def patch(path: str) -> None:
    src = open(path, "r", encoding="utf-8").read()

    begin = src.find(BEGIN)
    if begin < 0:
        sys.exit("[FAIL] 找不到锚点 `%s`，上游源码可能已变" % BEGIN.strip())

    end = src.find(END, begin)
    if end < 0:
        sys.exit("[FAIL] 找不到锚点 `%s`，上游源码可能已变" % END.strip())

    old_len = end + len(END) - begin
    out = src[:begin] + REPLACEMENT + src[end + len(END):]

    # 需要 <time.h> 和 <stdlib.h>；stdlib.h 上游已 include，time.h 补上。
    if "#include <time.h>" not in out:
        out = out.replace("#include <stdio.h>", "#include <stdio.h>\n#include <time.h>", 1)

    open(path, "w", encoding="utf-8").write(out)
    print("[OK] 已替换硬编码 root 段：%d 字节 -> %d 字节" % (old_len, len(REPLACEMENT)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法: mkworld-env.py <path/to/mkworld.cpp>")
    patch(sys.argv[1])
