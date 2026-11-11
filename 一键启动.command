#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Akari_Zeng
# seedance-api-gui · macOS 一键安装依赖并启动
#   在 Finder 中双击本文件；或在终端运行：bash 一键启动.command
#   可选环境变量：SAG_PORT=6547  SAG_NO_BROWSER=1  SAG_PIP_MIRROR=<镜像地址>

cd "$(dirname "$0")" || exit 1
PORT="${SAG_PORT:-6547}"
URL="http://127.0.0.1:${PORT}/"
VENV=".venv"
MIRROR="${SAG_PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

say()  { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m  ✓ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m  ! %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31m✗ %s\033[0m\n" "$*"; read -r -p "按回车键关闭窗口…" _; exit 1; }
open_browser() { [ -z "$SAG_NO_BROWSER" ] && open "$URL"; }

echo "seedance-api-gui · 一键启动"

# ---------------------------------------------------------------- 0. 是否已在运行
if curl -fs --max-time 2 --noproxy '*' "${URL}api/health" >/dev/null 2>&1; then
  say "服务已经在运行，直接打开浏览器：$URL"
  open_browser
  exit 0
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "端口 $PORT 被其他程序占用：
$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2)
可以关闭该程序，或用 SAG_PORT=其他端口 bash 一键启动.command 启动。"
fi

# ---------------------------------------------------------------- 1. Python 3.8+
say "检查 Python"
find_python() {
  for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      command -v "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python)"
if [ -z "$PY" ]; then
  warn "没有找到 Python 3.8 及以上版本"
  if command -v brew >/dev/null 2>&1; then
    say "使用 Homebrew 安装 Python（可能需要几分钟）"
    brew install python || fail "Homebrew 安装 Python 失败，请检查网络后重试。"
    PY="$(find_python)"
  else
    xcode-select --install >/dev/null 2>&1
    fail "请在弹出的窗口中安装「命令行开发者工具」（包含 Python 3），或到 https://www.python.org/downloads/ 下载安装。
安装完成后，再双击本文件。"
  fi
fi
[ -z "$PY" ] && fail "仍然找不到可用的 Python。"
ok "$("$PY" --version 2>&1)（$PY）"

# ---------------------------------------------------------------- 2. 虚拟环境
say "准备虚拟环境 $VENV"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  rm -rf "$VENV"
  "$PY" -m venv "$VENV" || fail "创建虚拟环境失败。"
  ok "已创建"
else
  ok "已存在"
fi
VPY="$VENV/bin/python"

# ---------------------------------------------------------------- 3. 依赖
pip_install() {   # 先用默认源，失败再用国内镜像
  "$VPY" -m pip install -q --disable-pip-version-check --timeout 20 "$@" 2>/dev/null || \
  "$VPY" -m pip install -q --disable-pip-version-check --timeout 20 -i "$MIRROR" "$@"
}
REQ="backend/requirements.txt"
REQ_HASH="$(shasum "$REQ" 2>/dev/null | cut -d' ' -f1)"
if [ "$(cat "$VENV/.req-hash" 2>/dev/null)" = "$REQ_HASH" ]; then
  say "依赖已是最新"
else
  say "安装依赖（PyMySQL、psycopg、PySocks）"
  pip_install --upgrade pip >/dev/null 2>&1 || warn "pip 升级失败，继续使用当前版本"
  all_ok=1
  while IFS= read -r line; do
    pkg="$(echo "${line%%#*}" | xargs)"
    [ -z "$pkg" ] && continue
    if pip_install "$pkg"; then ok "$pkg"; else warn "$pkg 安装失败（相关功能不可用，其余功能不受影响）"; all_ok=0; fi
  done < "$REQ"
  [ "$all_ok" = 1 ] && echo "$REQ_HASH" > "$VENV/.req-hash"
fi

# ---------------------------------------------------------------- 4. 启动
say "启动服务：$URL"
echo "  关闭这个终端窗口，或按 Ctrl+C，即可停止服务。"
ARGS=(--port "$PORT")
[ -n "$SAG_NO_BROWSER" ] && ARGS+=(--no-browser)
exec "$VPY" backend/server.py "${ARGS[@]}"
