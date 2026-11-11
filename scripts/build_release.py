#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Akari_Zeng
"""打包发布版：只把白名单里的文件放进 zip，并在打包前检查有没有敏感数据。

用法（在项目根目录）：
    python3 scripts/build_release.py        # 输出到 dist/seedance-api-gui-<版本>.zip
    python3 scripts/build_release.py --out 路径.zip

规则：
    * 只打包 INCLUDE 列出的文件；data/、log/、.venv/、dist/ 等运行时目录一律不放进去。
    * 打包前逐个文件扫描：API Key、私钥、密码赋值、数据库文件头、本机用户目录路径等，发现就中止。
    * zip 内文件权限统一（启动脚本和 scripts/ 下的脚本保留可执行权限）。
"""
import argparse
import hashlib
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "seedance-api-gui"

# 白名单：发布包里只有这些文件
INCLUDE = [
    "一键启动.command",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "backend/server.py",
    "backend/requirements.txt",
    "frontend/index.html",
    "docs/images/workspace-light.png",
    "docs/images/workspace-dark.png",
    "docs/images/skills-light.png",
    "docs/images/history-drawer-light.png",
    "docs/images/settings-dark.png",
    "docs/images/help-light.png",
    ".gitignore",
    "scripts/build_release.py",
]
# 运行时目录只放一个说明文件，告诉用户数据会写到哪里
PLACEHOLDERS = {
    "data/README.txt": ("运行时数据目录：数据库（seedance.db）、设置（settings.json）、保存到本地的视频（media/）。\n"
                        "首次启动时自动创建。这些文件包含 API Key 等敏感信息，请不要分享。\n"),
    "log/README.txt": "日志目录：每次启动时，会把上一次运行期间的日志导出为 yyyy-mm-dd_NN.txt。\n",
}

# 敏感内容规则：命中任意一条就中止打包
SENSITIVE = [
    ("SQLite 数据库文件", re.compile(rb"^SQLite format 3\x00")),
    ("私钥", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer 令牌", re.compile(rb"Bearer\s+[A-Za-z0-9._\-]{24,}")),
    ("AWS 访问密钥", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("火山引擎 AK", re.compile(rb"\bAKLT[0-9A-Za-z]{20,}\b")),
    ("OpenAI 风格密钥", re.compile(rb"\bsk-[A-Za-z0-9]{32,}\b")),
    ("GitHub 令牌", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("UUID 形式的 API Key", re.compile(rb"(?i)(api[_-]?key|ARK_API_KEY)\s*[=:]\s*[\"']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
    ("密码赋值", re.compile(rb"(?i)\b(password|passwd|pwd|secret|api_key|apikey|access_key|secret_key|token)[\"']?\s*[=:]\s*[\"'](?!\$|\{|<|\*)[^\"'\s]{6,}[\"']")),
    ("本机用户目录", re.compile(rb"/Users/[A-Za-z0-9._-]+/|C:\\\\Users\\\\[A-Za-z0-9._-]+\\\\")),
]
# 已知是示例或占位的写法（不算泄露）
ALLOW = [
    re.compile(rb"Bearer \$ARK_API_KEY"),
    re.compile(rb"your_api_key|<API_KEY>|<YOUR_API_KEY>|<AGENT_ID>|<SKILL_ID>"),
]
# 文档原文里有大量示例，只对它做「真实密钥」级别的检查
DOC_ONLY_RULES = {"SQLite 数据库文件", "私钥", "AWS 访问密钥", "火山引擎 AK", "GitHub 令牌", "本机用户目录"}


def version():
    src = open(os.path.join(ROOT, "backend", "server.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
    return m.group(1) if m else "0.0.0"


def scan(rel, data):
    hits = []
    rules = [r for r in SENSITIVE if not rel.startswith("docs/") or r[0] in DOC_ONLY_RULES]
    for name, rx in rules:
        for m in rx.finditer(data):
            frag = data[max(0, m.start() - 20):m.end() + 20]
            if any(a.search(frag) for a in ALLOW):
                continue
            line = data.count(b"\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line}  {name}  …{frag.decode('utf-8', 'replace').strip()[:80]}…")
            break
    return hits


def main():
    ap = argparse.ArgumentParser(description="打包 seedance-api-gui 发布版")
    ap.add_argument("--out", help="输出 zip 路径（默认 dist/seedance-api-gui-<版本>.zip）")
    args = ap.parse_args()
    ver = version()
    out = os.path.abspath(args.out or os.path.join(ROOT, "dist", f"{NAME}-{ver}.zip"))
    top = f"{NAME}-{ver}"

    missing = [f for f in INCLUDE if not os.path.isfile(os.path.join(ROOT, f))]
    files = [f for f in INCLUDE if f not in missing]
    if missing:
        print("提示：以下文件不存在，已跳过：", ", ".join(missing))

    problems = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            problems += scan(rel, f.read())
    if problems:
        print("✗ 发现疑似敏感数据，已中止打包：")
        for p in problems:
            print("   ", p)
        sys.exit(1)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    stamp = time.localtime()[:6]
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files + list(PLACEHOLDERS):
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = rel.endswith(".command") or rel.startswith("scripts/")
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            if rel in PLACEHOLDERS:
                data = PLACEHOLDERS[rel].encode("utf-8")
            else:
                with open(os.path.join(ROOT, rel), "rb") as f:
                    data = f.read()
            z.writestr(info, data)
    os.replace(tmp, out)

    with open(out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    print(f"✓ 已生成 {out}（{os.path.getsize(out) // 1024} KB，{len(names)} 个文件）")
    print(f"  SHA-256 {digest}")
    for n in names:
        print("   ", n)


if __name__ == "__main__":
    main()
