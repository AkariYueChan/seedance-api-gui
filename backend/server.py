#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Akari_Zeng
"""
seedance-api-gui 本地后端

  双击「一键启动.command」          # 自动安装依赖并启动（macOS）
  python3 backend/server.py                 # 启动并自动打开 http://127.0.0.1:6547/
  python3 backend/server.py --no-browser    # 只启动，不打开浏览器

功能
  * 提供 index.html 页面。
  * /proxy/<path>：把页面收集好的请求转发到火山方舟 API（Base URL + path）。
    每个请求在发出“之前”先完整写入数据库；连接断开、超时或后端中途退出后都可以重发。
  * 数据库可选 SQLite（默认，本地文件）、MySQL、PostgreSQL，在网页「数据库」页配置。
  * 网络代理可选：跟随系统 / 直连 / 自定义 HTTP(S)、SOCKS5，在网页「网络代理」页配置。
  * 后台线程持续轮询未完成的视频任务。

数据库与代理的连接信息保存在 data/settings.json（权限 600）；
API Key、页面状态、请求记录、任务记录保存在所选数据库中。

只用 SQLite 和 HTTP 代理时仅依赖 Python 3.8+ 标准库；
MySQL 需要 PyMySQL，PostgreSQL 需要 psycopg，SOCKS5 代理需要 PySocks（一键脚本会自动安装）。
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
try:
    import socks
    from sockshandler import SocksiPyHandler
except ImportError:
    socks = None

# 目录结构（都相对项目根目录）：
#   backend/server.py    本文件
#   frontend/index.html  页面
#   docs/         帮助页使用的文档
#   data/         运行时数据：数据库、设置、保存到本地的视频（不放进发布包）
#   log/          每次启动时导出的上一次运行日志（不放进发布包）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(ROOT, "frontend")
INDEX = os.path.join(FRONTEND_DIR, "index.html")
DOCS_DIR = os.path.join(ROOT, "docs")
DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(ROOT, "log")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
APP_VERSION = "1.9.2"

DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
# 只允许把 API Key 转发到这些上游域名，防止被当作开放代理、把 Key 发到任意地址。
# 使用第三方中转时，在「🌐 网络代理」页的「额外允许的上游域名」里添加。
UPSTREAM_ALLOW = ("volces.com", "volcengineapi.com", "byteplus.com", "bytepluses.com", "127.0.0.1", "localhost")
MEDIA_HOST_ALLOW = ("volces.com", "volccdn.com", "byteimg.com", "bytepluses.com", "byteplus.com", "127.0.0.1", "localhost")
MAX_MEDIA_BYTES = 500 * 1024 * 1024
UPSTREAM_TIMEOUT = 300          # 秒；上传技能包、大请求体可能较慢
MAX_TEXT_STORE = 5 * 1024 * 1024
MAX_BLOB_STORE = 50 * 1024 * 1024
POLL_INTERVAL = 15              # 后台跟踪任务的查询间隔（秒）；只对用户点了「自动跟踪」的任务生效
POLL_MAX_AGE = 3 * 3600         # 自动跟踪最长持续 3 小时，到期自动停止，避免空闲时持续请求
TASK_PATH_RE = re.compile(r"^/contents/generations/tasks/([^/?#]+)")
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,63}$")
TABLES = ("app_config", "app_state", "api_keys", "snapshots", "requests", "tasks", "events", "folders", "conv_meta")
SERIAL_TABLES = ("api_keys", "snapshots", "requests", "events", "folders")
KEY_REF_RE = re.compile(r"^Bearer\s+@key:(\d+)$")

DEFAULT_SETTINGS = {
    "db": {"type": "sqlite", "sqlite_path": "data/seedance.db",
           "host": "127.0.0.1", "port": 0, "database": "seedance",
           "user": "", "password": "", "sslmode": "prefer"},
    "proxy": {"mode": "system", "scheme": "http", "host": "", "port": 0,
              "username": "", "password": "", "no_proxy": "localhost,127.0.0.1", "extra_upstreams": ""},
}
DEFAULT_PORTS = {"mysql": 3306, "postgres": 5432}


# ============================================================================ 设置文件
def load_settings():
    s = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for sec in s:
            if isinstance(saved.get(sec), dict):
                s[sec].update(saved[sec])
    except FileNotFoundError:
        pass
    except Exception as e:
        print("读取 settings.json 失败，使用默认设置:", e)
    return s


def save_settings(s):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_PATH)


def masked(section):
    d = dict(section)
    d["password_set"] = bool(d.get("password"))
    d["password"] = ""
    return d


def merge_secret(new, old):
    """前端不回传已保存的密码：留空且 keep_password 为真时沿用旧密码。"""
    new = dict(new or {})
    keep = new.pop("keep_password", True)
    if not new.get("password") and keep:
        new["password"] = old.get("password", "")
    return new


# ============================================================================ 数据库
class Store:
    """对 SQLite / MySQL / PostgreSQL 的统一封装。SQL 一律用 ? 作为占位符。"""

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.kind = self.cfg.get("type", "sqlite")
        self.lock = threading.Lock()
        if self.kind == "sqlite":
            p = (self.cfg.get("sqlite_path") or "data/seedance.db").strip()
            self.path = p if os.path.isabs(p) else os.path.join(ROOT, p)
        elif self.kind == "mysql":
            if pymysql is None:
                raise RuntimeError("未安装 MySQL 驱动 PyMySQL。请双击「一键启动.command」，或执行：pip install PyMySQL")
        elif self.kind == "postgres":
            if psycopg is None:
                raise RuntimeError("未安装 PostgreSQL 驱动 psycopg。请双击「一键启动.command」，或执行：pip install \"psycopg[binary]\"")
        else:
            raise ValueError(f"不支持的数据库类型：{self.kind}")
        if self.kind != "sqlite":
            if not (self.cfg.get("host") or "").strip():
                raise ValueError("请填写数据库主机")
            if not DB_NAME_RE.match(self.cfg.get("database") or ""):
                raise ValueError("数据库名只能包含字母、数字和下划线（1～63 位）")
            if not (self.cfg.get("user") or "").strip():
                raise ValueError("请填写数据库用户名")

    # ---- 描述 ----
    def port(self):
        return int(self.cfg.get("port") or DEFAULT_PORTS.get(self.kind, 0))

    def describe(self):
        if self.kind == "sqlite":
            return f"SQLite · {os.path.relpath(self.path, ROOT) if self.path.startswith(ROOT) else self.path}"
        name = "MySQL" if self.kind == "mysql" else "PostgreSQL"
        return f"{name} · {self.cfg.get('user')}@{self.cfg.get('host')}:{self.port()}/{self.cfg.get('database')}"

    def identity(self):
        if self.kind == "sqlite":
            return ("sqlite", os.path.realpath(self.path))
        return (self.kind, self.cfg.get("host"), self.port(), self.cfg.get("database"))

    # ---- 连接 ----
    def _connect(self, use_db=True, autocommit=False):
        if self.kind == "sqlite":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            return c
        if self.kind == "mysql":
            return pymysql.connect(
                host=self.cfg["host"].strip(), port=self.port(), user=self.cfg["user"].strip(),
                password=self.cfg.get("password") or "", database=self.cfg["database"] if use_db else None,
                charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, binary_prefix=True,
                connect_timeout=8, read_timeout=120, write_timeout=120, autocommit=autocommit)
        return psycopg.connect(
            host=self.cfg["host"].strip(), port=self.port(), user=self.cfg["user"].strip(),
            password=self.cfg.get("password") or "", dbname=self.cfg["database"] if use_db else "postgres",
            sslmode=self.cfg.get("sslmode") or "prefer", connect_timeout=8,
            row_factory=dict_row, autocommit=autocommit)

    @contextmanager
    def conn(self):
        with (self.lock if self.kind == "sqlite" else nullcontext()):
            c = self._connect()
            try:
                yield c
                c.commit()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                raise
            finally:
                c.close()

    def _sql(self, sql):
        return sql if self.kind == "sqlite" else sql.replace("?", "%s")

    # ---- 基本操作 ----
    def all(self, sql, params=()):
        with self.conn() as c:
            cur = c.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def run(self, sql, params=()):
        with self.conn() as c:
            cur = c.cursor()
            cur.execute(self._sql(sql), tuple(params))
            return cur.rowcount

    def _insert_sql(self, table, cols):
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        return sql + (" RETURNING id" if self.kind == "postgres" and table in SERIAL_TABLES else "")

    def insert(self, table, data):
        cols = list(data)
        with self.conn() as c:
            cur = c.cursor()
            cur.execute(self._sql(self._insert_sql(table, cols)), tuple(data[k] for k in cols))
            if self.kind == "postgres" and table in SERIAL_TABLES:
                return cur.fetchone()["id"]
            return cur.lastrowid

    def upsert(self, table, key, data):
        cols = list(data)
        others = [c for c in cols if c != key]
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        if self.kind == "mysql":
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(f"{c}=VALUES({c})" for c in others)
        else:
            sql += f" ON CONFLICT({key}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in others)
        self.run(sql, [data[c] for c in cols])

    # ---- 维护 ----
    def version(self):
        q = {"sqlite": "SELECT sqlite_version() AS v", "mysql": "SELECT VERSION() AS v",
             "postgres": "SELECT version() AS v"}[self.kind]
        return str(self.one(q)["v"])

    def ensure_database(self):
        """MySQL / PostgreSQL：数据库不存在时创建。"""
        name = self.cfg["database"]
        if self.kind == "mysql":
            c = self._connect(use_db=False, autocommit=True)
            try:
                c.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{name}` "
                                   "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            finally:
                c.close()
        elif self.kind == "postgres":
            c = self._connect(use_db=False, autocommit=True)
            try:
                cur = c.cursor()
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{name}"')
            finally:
                c.close()

    def init_schema(self):
        T = {
            "sqlite": dict(ID="INTEGER PRIMARY KEY AUTOINCREMENT", KEY="TEXT", HASH="TEXT", TXT="TEXT", BIG="TEXT",
                           BLOB="BLOB", NUM="REAL", INT="INTEGER", TAIL=""),
            "mysql": dict(ID="BIGINT AUTO_INCREMENT PRIMARY KEY", KEY="VARCHAR(191)", HASH="VARCHAR(64)", TXT="TEXT", BIG="LONGTEXT",
                          BLOB="LONGBLOB", NUM="DOUBLE", INT="BIGINT",
                          TAIL=" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"),
            "postgres": dict(ID="BIGSERIAL PRIMARY KEY", KEY="TEXT", HASH="TEXT", TXT="TEXT", BIG="TEXT",
                             BLOB="BYTEA", NUM="DOUBLE PRECISION", INT="BIGINT", TAIL=""),
        }[self.kind]
        idx = ", INDEX idx_req_created (created_at)" if self.kind == "mysql" else ""
        ddl = [
            "CREATE TABLE IF NOT EXISTS app_config(k {KEY} PRIMARY KEY, v {BIG}, updated_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS app_state(k {KEY} PRIMARY KEY, v {BIG}, updated_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS requests(id {ID}, created_at {NUM}, finished_at {NUM}, "
            "method {TXT}, path {TXT}, url {TXT}, kind {TXT}, req_content_type {TXT}, req_body {BLOB}, "
            "req_size {INT}, state {TXT}, status_code {INT}, error {TXT}, resp_content_type {TXT}, "
            "resp_body {BLOB}, resp_size {INT}, retry_of {INT}, snapshot_id {INT}, key_id {INT}, summary {TXT}"
            + idx + "){TAIL}",
            "CREATE TABLE IF NOT EXISTS api_keys(id {ID}, name {TXT}, api_key {TXT}, base_url {TXT}, note {TXT}, "
            "created_at {NUM}, updated_at {NUM}, last_used_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS snapshots(id {ID}, hash {HASH} UNIQUE, v {BIG}, created_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS folders(id {ID}, name {TXT}, sort {NUM}, collapsed {INT}, color {TXT}, "
            "created_at {NUM}, updated_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS conv_meta(request_id {INT} PRIMARY KEY, title {TXT}, folder_id {INT}, "
            "sort {NUM}, updated_at {NUM}){TAIL}",
            "CREATE TABLE IF NOT EXISTS events(id {ID}, created_at {NUM}, level {TXT}, category {TXT}, title {TXT}, "
            "detail {BIG}, task_id {TXT}, request_id {INT}, source {TXT}){TAIL}",
            "CREATE TABLE IF NOT EXISTS tasks(id {KEY} PRIMARY KEY, model {TXT}, prompt {BIG}, status {TXT}, "
            "video_url {TXT}, last_frame_url {TXT}, error {BIG}, raw {BIG}, request_id {INT}, "
            "created_at {NUM}, updated_at {NUM}, key_id {INT}, watch_until {NUM}){TAIL}",
        ]
        with self.conn() as c:
            cur = c.cursor()
            if self.kind == "sqlite":
                cur.execute("PRAGMA journal_mode=WAL")
            for s in ddl:
                cur.execute(s.format(**T))
            if self.kind != "mysql":
                cur.execute("CREATE INDEX IF NOT EXISTS idx_req_created ON requests(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_evt_created ON events(created_at)")
            if self.kind == "sqlite":
                self._migrate_legacy_sqlite(cur)
            # 旧版本创建的表补齐新增列
            for table, cols in (("requests", {"snapshot_id": T["INT"], "key_id": T["INT"], "summary": T["TXT"]}),
                                ("tasks", {"key_id": T["INT"], "watch_until": T["NUM"]})):
                have = self._columns(cur, table)
                for col, typ in cols.items():
                    if col not in have:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            # 上次运行时还没收到响应就退出的请求，标记为 interrupted，方便重发
            cur.execute(self._sql("UPDATE requests SET state='interrupted', error=? WHERE state='pending'"),
                        ("后端在请求完成前退出",))

    def _columns(self, cur, table):
        if self.kind == "sqlite":
            return {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        if self.kind == "mysql":
            cur.execute("SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        else:
            cur.execute("SELECT column_name AS c FROM information_schema.columns "
                        "WHERE table_schema=current_schema() AND table_name=%s", (table,))
        return {r["c"] for r in cur.fetchall()}

    @staticmethod
    def _migrate_legacy_sqlite(cur):
        """旧版本使用 config / ui_state 表（key, value 列），迁移到 app_config / app_state。"""
        for old, new in (("config", "app_config"), ("ui_state", "app_state")):
            if cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old,)).fetchone():
                cur.execute(f"INSERT OR IGNORE INTO {new}(k, v, updated_at) SELECT key, value, updated_at FROM {old}")
                cur.execute(f"DROP TABLE {old}")

    def counts(self):
        return {t: int(self.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]) for t in TABLES}


def copy_data(src, dst):
    """把 src 的全部数据复制到 dst（dst 必须为空）。返回说明文字。"""
    if any(dst.counts().values()):
        return "目标数据库里已经有数据，为避免冲突没有复制。"
    total = {}
    for t, key in (("app_config", "k"), ("app_state", "k"), ("tasks", "id"), ("api_keys", "id"), ("snapshots", "id"), ("events", "id"), ("folders", "id"), ("conv_meta", "request_id")):
        rows = src.all(f"SELECT * FROM {t}")
        for r in rows:
            dst.upsert(t, key, r)
        total[t] = len(rows)
    n, last = 0, 0
    while True:   # requests 可能带大请求体，分批复制
        rows = src.all("SELECT * FROM requests WHERE id > ? ORDER BY id LIMIT 100", (last,))
        if not rows:
            break
        with dst.conn() as c:
            cur = c.cursor()
            cols = list(rows[0])
            sql = dst._sql(f"INSERT INTO requests({','.join(cols)}) VALUES({','.join('?' * len(cols))})")
            for r in rows:
                cur.execute(sql, tuple(bytes(r[k]) if isinstance(r[k], memoryview) else r[k] for k in cols))
        n += len(rows)
        last = rows[-1]["id"]
    if dst.kind == "postgres":
        for t in SERIAL_TABLES:
            dst.all(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)")
    total["requests"] = n
    return "已复制：" + "，".join(f"{k} {v} 条" for k, v in total.items())


STATE = {"store": None, "net": None, "db_error": None, "proxy_error": None}
STATE_LOCK = threading.Lock()


def store():
    return STATE["store"]


def net():
    return STATE["net"]


# ============================================================================ 网络代理
class Net:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.mode = self.cfg.get("mode", "system")
        redirect = urllib.request.HTTPRedirectHandler
        self.direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect())
        self.no_proxy = []
        if self.mode == "none":
            self.opener = self.direct
        elif self.mode == "system":
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(), redirect())
        elif self.mode == "custom":
            scheme = self.cfg.get("scheme", "http")
            host = (self.cfg.get("host") or "").strip()
            port = int(self.cfg.get("port") or 0)
            if not host or not (0 < port < 65536):
                raise ValueError("请填写代理主机和端口（1～65535）")
            user, pw = self.cfg.get("username") or "", self.cfg.get("password") or ""
            self.no_proxy = [h.strip().lower().lstrip(".") for h in (self.cfg.get("no_proxy") or "").split(",") if h.strip()]
            if scheme in ("http", "https"):
                auth = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
                url = f"{scheme}://{auth}{host}:{port}"
                self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}), redirect())
            elif scheme in ("socks5", "socks5h"):
                if socks is None:
                    raise RuntimeError("SOCKS5 代理需要 PySocks。请双击「一键启动.command」，或执行：pip install PySocks")
                h = SocksiPyHandler(socks.SOCKS5, host, port, rdns=(scheme == "socks5h"),
                                    username=user or None, password=pw or None)
                # 显式加入空的 ProxyHandler，避免再叠加系统 HTTP 代理
                self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), h, redirect())
            else:
                raise ValueError(f"不支持的代理协议：{scheme}")
        else:
            raise ValueError(f"不支持的代理模式：{self.mode}")

    def describe(self):
        if self.mode == "none":
            return "不使用代理（直连）"
        if self.mode == "system":
            p = urllib.request.getproxies()
            return "跟随系统代理（" + (", ".join(f"{k}={v}" for k, v in p.items() if k in ("http", "https")) or "当前系统未设置代理，直连") + "）"
        return f"自定义代理 {self.cfg.get('scheme')}://{self.cfg.get('host')}:{self.cfg.get('port')}" + \
            ("（带账号）" if self.cfg.get("username") else "")

    def opener_for(self, url):
        host = (urlsplit(url).hostname or "").lower()
        for np in self.no_proxy:
            if np == "*" or host == np or host.endswith("." + np):
                return self.direct
        return self.opener

    def send(self, method, url, ctype, body, auth):
        req = urllib.request.Request(url, data=body if body else None, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        req.add_header("Accept", "application/json, */*")
        if auth:
            # unredirected：跟随 302（如技能包下载地址）时不会把 API Key 带给跳转目标
            req.add_unredirected_header("Authorization", auth)
        try:
            with self.opener_for(url).open(req, timeout=UPSTREAM_TIMEOUT) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read(), None
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("Content-Type", "") if e.headers else ""), e.read() or b"", None
        except Exception as e:  # 连接失败 / 超时 / 代理错误 等
            return None, "", b"", f"{type(e).__name__}: {e}"


# ============================================================================ 业务数据
def get_config():
    rows = {r["k"]: r["v"] for r in store().all("SELECT k, v FROM app_config")}
    return {"api_key": rows.get("api_key", "") or "", "base_url": rows.get("base_url") or DEFAULT_BASE,
            "active_key_id": int(rows["active_key_id"]) if (rows.get("active_key_id") or "").isdigit() else None}


def set_config(values):
    for k in ("api_key", "base_url", "active_key_id"):
        if values.get(k) is not None:
            store().upsert("app_config", "k", {"k": k, "v": str(values[k]).strip(), "updated_at": time.time()})


EVENT_LEVELS = ("info", "success", "warn", "error")
EVENT_CATEGORIES = ("submit", "response", "query", "timeout", "error", "task", "retry", "config", "system", "client")
TASK_STATUS_CN = {"queued": "排队中", "running": "生成中", "succeeded": "已完成", "failed": "生成失败",
                  "expired": "已超时", "cancelled": "已取消", "deleted": "已删除", "not_found": "任务不存在"}


def log_event(level, category, title, detail=None, task_id=None, request_id=None, source="server"):
    """写入事件日志。任何异常都不影响主流程。"""
    try:
        if not isinstance(detail, (str, type(None))):
            detail = json.dumps(detail, ensure_ascii=False, indent=2, default=str)
        st = STATE.get("store")
        if st is None:
            return
        st.insert("events", {"created_at": time.time(), "level": level if level in EVENT_LEVELS else "info",
                             "category": category if category in EVENT_CATEGORIES else "system",
                             "title": str(title)[:300], "detail": (detail or "")[:20000] or None,
                             "task_id": task_id, "request_id": request_id, "source": source})
    except Exception as e:
        print("写事件日志失败:", e)


def err_text(body):
    d = parse_json(body)
    if isinstance(d, dict) and isinstance(d.get("error"), dict):
        e = d["error"]
        return ((e.get("code") or "") + "：" + (e.get("message") or "")).strip("：")
    try:
        return bytes(body or b"")[:500].decode("utf-8", "replace")
    except Exception:
        return ""


def is_timeout(err):
    e = (err or "").lower()
    return "timed out" in e or "timeout" in e


def mask_key(k):
    k = k or ""
    return (k[:4] + "…" + k[-4:]) if len(k) > 12 else "****"


def get_key(kid):
    return store().one("SELECT * FROM api_keys WHERE id=?", (kid,)) if kid else None


def key_public(k, active_id=None):
    return {"id": k["id"], "name": k["name"], "masked": mask_key(k["api_key"]), "base_url": k["base_url"] or DEFAULT_BASE,
            "note": k["note"] or "", "created_at": k["created_at"], "updated_at": k["updated_at"],
            "last_used_at": k["last_used_at"], "active": k["id"] == active_id}


def activate_key(kid):
    k = get_key(kid) if kid else None
    if k:
        set_config({"active_key_id": str(k["id"]), "api_key": k["api_key"], "base_url": k["base_url"] or DEFAULT_BASE})
    else:
        set_config({"active_key_id": "", "api_key": ""})
    return k


def host_allowed(url, allow):
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and any(host == a or host.endswith("." + a) for a in allow)


def upstream_allow():
    raw = load_settings()["proxy"].get("extra_upstreams") or ""
    extra = [h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()]
    return tuple(UPSTREAM_ALLOW) + tuple(extra)


def resolve_auth(auth, base_hint=None):
    """把前端的 Authorization 解析成真正的鉴权头。返回 (auth, base_url, key_id, error)。
    Bearer @key:<id> 表示使用数据库里保存的 Key，浏览器不接触明文。"""
    cfg = get_config()
    base = base_hint if re.match(r"^https?://", base_hint or "") else None
    m = KEY_REF_RE.match(auth or "")
    if m:
        k = get_key(int(m.group(1)))
        if not k:
            return None, base or cfg["base_url"], None, "所选的 API Key 已被删除，请重新选择"
        store().run("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), k["id"]))
        return f"Bearer {k['api_key']}", base or k["base_url"] or DEFAULT_BASE, k["id"], None
    if auth:
        return auth, base or cfg["base_url"], None, None
    k = get_key(cfg["active_key_id"])
    if k:
        return f"Bearer {k['api_key']}", base or k["base_url"] or DEFAULT_BASE, k["id"], None
    if cfg["api_key"]:
        return f"Bearer {cfg['api_key']}", base or cfg["base_url"], None, None
    return None, base or cfg["base_url"], None, "请先在右上角选择或添加 API Key"


def save_snapshot(raw):
    h = hashlib.sha256(raw).hexdigest()
    r = store().one("SELECT id FROM snapshots WHERE hash=?", (h,))
    if r:
        return r["id"]
    try:
        return store().insert("snapshots", {"hash": h, "v": raw.decode("utf-8"), "created_at": time.time()})
    except Exception:   # 并发写入同一快照
        return store().one("SELECT id FROM snapshots WHERE hash=?", (h,))["id"]


def make_summary(method, path, ctype, body):
    """为历史记录生成一句话摘要。"""
    if method == "GET":
        return None
    p = path.split("?")[0]
    if "multipart" in (ctype or ""):
        names = [n.decode("utf-8", "replace") for n in re.findall(rb'filename="([^"]+)"', (body or b"")[:500000])]
        return f"上传 {len(names)} 个文件：" + "、".join(names[:3]) + ("…" if len(names) > 3 else "")
    d = parse_json(body)
    if p == "/contents/generations/tasks" and isinstance(d, dict):
        content = [x for x in d.get("content") or [] if isinstance(x, dict)]
        text = next((x.get("text") or "" for x in content if x.get("type") == "text"), "")
        names = {"image_url": "图片", "video_url": "视频", "audio_url": "音频", "draft_task": "样片"}
        cnt = Counter(names.get(x.get("type"), x.get("type")) for x in content if x.get("type") != "text")
        res = "、".join(f"{k}×{v}" for k, v in cnt.items())
        return " · ".join(x for x in (d.get("model"), (text[:60] + ("…" if len(text) > 60 else "")) if text else "", res) if x)
    if method == "DELETE":
        return "删除 / 取消 " + p.rsplit("/", 1)[-1]
    if p.startswith("/agents") and isinstance(d, dict):
        if "skills" in d:
            return f"绑定 {len(d.get('skills') or [])} 个技能" + (f"（智能体版本 {d['version']}）" if d.get("version") else "")
        return d.get("name")
    if p == "/skills/github/scan" and isinstance(d, dict):
        return f"扫描 {d.get('input')}"
    if p == "/skills/github/import" and isinstance(d, dict):
        repo = d.get("repo") or {}
        return f"导入 {len(d.get('selected') or [])} 个技能 · {repo.get('owner')}/{repo.get('name')}"
    if isinstance(d, dict) and "protection_enabled" in d:
        return f"内容保护 → {'开启' if d['protection_enabled'] else '关闭'}"
    return None


def slim(obj, depth=0):
    """把请求体里的 Base64 素材缩短，便于在列表里展示。"""
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        return {k: slim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [slim(v, depth + 1) for v in obj]
    if isinstance(obj, str) and obj.startswith("data:") and len(obj) > 300:
        head = obj.split(",", 1)[0]
        return f"{head},…（{len(obj)} 字符）"
    return obj


def conversations(limit=100, rid=None):
    """视频生成会话：每次「创建任务」请求 + 对应的任务状态。"""
    where = "r.kind='video' AND r.method='POST' AND r.path='/contents/generations/tasks'"
    args = []
    if rid:
        where += " AND r.id=?"
        args.append(rid)
    rows = store().all(
        "SELECT r.id, r.created_at, r.finished_at, r.state, r.status_code, r.error, r.summary, r.snapshot_id, "
        "r.key_id, r.retry_of, r.req_body, r.resp_body, t.id AS task_id, t.status AS task_status, t.video_url, "
        "t.last_frame_url, t.error AS task_error, t.raw AS task_raw, t.updated_at AS task_updated, t.watch_until, "
        "cm.title, cm.folder_id, cm.sort "
        "FROM requests r LEFT JOIN tasks t ON t.request_id = r.id LEFT JOIN conv_meta cm ON cm.request_id = r.id WHERE " + where +
        f" ORDER BY r.id DESC LIMIT {int(limit)}", args)
    names = {r["id"]: r["name"] for r in store().all("SELECT id, name FROM api_keys")}
    out = []
    for r in rows:
        resp = parse_json(r["resp_body"]) if r["resp_body"] is not None and len(r["resp_body"]) < 200000 else None
        d = {k: r[k] for k in ("id", "created_at", "finished_at", "state", "status_code", "error", "summary",
                               "snapshot_id", "key_id", "retry_of", "task_id", "task_status", "video_url",
                               "last_frame_url", "task_error", "task_updated", "watch_until", "title", "folder_id", "sort")}
        d.update(request=slim(parse_json(r["req_body"])), response=resp, task_raw=parse_json((r["task_raw"] or "").encode()),
                 key_name=names.get(r["key_id"]))
        if d.get("task_id"):
            d["local_video"] = os.path.isfile(media_path(d["task_id"], "video"))
            d["local_frame"] = os.path.isfile(media_path(d["task_id"], "frame"))
        out.append(d)
    return out


def classify(path):
    p = path.split("?")[0]
    if p.startswith("/contents/generations/tasks"):
        return "video"
    if p.startswith("/skills"):
        return "skill"
    if p.startswith("/agents"):
        return "agent"
    return "other"


def is_text(ctype):
    ctype = (ctype or "").lower()
    return "json" in ctype or ctype.startswith("text/") or "xml" in ctype or "x-www-form-urlencoded" in ctype


def record_request(method, path, url, ctype, body, retry_of=None, snapshot_id=None, key_id=None):
    try:
        summary = make_summary(method, path, ctype, body)
    except Exception:
        summary = None
    return store().insert("requests", {
        "created_at": time.time(), "method": method, "path": path, "url": url, "kind": classify(path),
        "req_content_type": ctype, "req_body": body or b"", "req_size": len(body or b""),
        "state": "pending", "retry_of": retry_of, "snapshot_id": snapshot_id, "key_id": key_id, "summary": summary})


def finish_request(rid, status, rctype, rbody, err):
    size = len(rbody or b"")
    keep = (is_text(rctype) and size <= MAX_TEXT_STORE) or size <= MAX_BLOB_STORE
    store().run("UPDATE requests SET finished_at=?, state=?, status_code=?, error=?, "
                "resp_content_type=?, resp_body=?, resp_size=? WHERE id=?",
                (time.time(), "failed" if err else "done", status, err, rctype,
                 rbody if keep else None, size, rid))


def parse_json(b):
    try:
        return json.loads(bytes(b).decode("utf-8")) if b else None
    except Exception:
        return None


def upsert_task(t, request_id=None, model=None, prompt=None, key_id=None):
    if not isinstance(t, dict) or not t.get("id"):
        return
    content = t.get("content") or {}
    err = t.get("error")
    now = time.time()
    old = store().one("SELECT id, status FROM tasks WHERE id=?", (t["id"],))
    new_status = t.get("status") or (None if old else "queued")
    if new_status and (old is None or old["status"] != new_status) and not (old is None and new_status == "queued"):
        cn = TASK_STATUS_CN.get(new_status, new_status)
        if new_status == "succeeded":
            log_event("success", "task", f"视频生成完成 {t['id']}", (content or {}).get("video_url"), t["id"])
        elif new_status == "expired":
            log_event("error", "timeout", f"任务超时 {t['id']}", "任务处于排队或生成中的时间超过 execution_expires_after，已被自动终止", t["id"])
        elif new_status == "failed":
            log_event("error", "error", f"视频生成失败 {t['id']}", err, t["id"])
        else:
            log_event("info", "task", f"任务状态 → {cn} {t['id']}", None, t["id"])
    if old is None:
        store().insert("tasks", {
            "id": t["id"], "model": t.get("model") or model, "prompt": prompt,
            "status": t.get("status") or "queued", "video_url": content.get("video_url"),
            "last_frame_url": content.get("last_frame_url"),
            "error": json.dumps(err, ensure_ascii=False) if err else None,
            "raw": json.dumps(t, ensure_ascii=False), "request_id": request_id,
            "created_at": t.get("created_at") or now, "updated_at": now, "key_id": key_id})
    else:
        store().run("UPDATE tasks SET model=COALESCE(?,model), status=COALESCE(?,status), "
                    "video_url=COALESCE(?,video_url), last_frame_url=COALESCE(?,last_frame_url), "
                    "error=?, raw=?, updated_at=? WHERE id=?",
                    (t.get("model"), t.get("status"), content.get("video_url"), content.get("last_frame_url"),
                     json.dumps(err, ensure_ascii=False) if err else None,
                     json.dumps(t, ensure_ascii=False), now, t["id"]))


def after_response(method, path, req_body, status, rbody, rid, key_id=None):
    """根据视频任务接口的响应，同步本地 tasks 表。"""
    if status is None or not (200 <= status < 300):
        return
    p = path.split("?")[0]
    data = parse_json(rbody)
    if p == "/contents/generations/tasks":
        if method == "POST" and isinstance(data, dict) and data.get("id"):
            req = parse_json(req_body) or {}
            prompt = next((x.get("text") for x in req.get("content", [])
                           if isinstance(x, dict) and x.get("type") == "text"), None)
            upsert_task({"id": data["id"], "status": "queued"}, rid, req.get("model"), prompt, key_id)
        elif method == "GET" and isinstance(data, dict):
            for item in data.get("items") or []:
                upsert_task(item)
        return
    m = TASK_PATH_RE.match(p)
    if m:
        if method == "GET" and isinstance(data, dict):
            upsert_task(data)
        elif method == "DELETE":
            store().run("UPDATE tasks SET status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'deleted' END, "
                        "updated_at=? WHERE id=?", (time.time(), m.group(1)))


def do_proxy(method, path, ctype, body, auth, retry_of=None, base=None, snapshot_id=None):
    auth, base_url, key_id, auth_err = resolve_auth(auth, base)
    url = base_url.rstrip("/") + path
    if not host_allowed(url, upstream_allow()):
        msg = (f"上游地址 {urlsplit(url).hostname} 不在允许列表里，已拒绝发送（防止 API Key 被转发到未知地址）。"
               "如确需使用，请在「网络代理」页添加到额外允许的上游域名。")
        log_event("error", "error", "拒绝转发到未知上游", url)
        return None, 403, "application/json", json.dumps({"error": {"code": "UpstreamNotAllowed", "message": msg}}, ensure_ascii=False).encode(), None
    rid = record_request(method, path, url, ctype, body, retry_of, snapshot_id, key_id)   # 先落库，再发送
    is_query = method == "GET"
    if not is_query:
        log_event("info", "retry" if retry_of else "submit",
                  f"{'重发 #'+str(retry_of)+' · ' if retry_of else ''}{method} {path.split('?')[0]}",
                  make_summary(method, path, ctype, body), request_id=rid)
    if auth_err:
        log_event("error", "error", f"未发送：{auth_err}", f"{method} {path}", request_id=rid)
        finish_request(rid, None, "", b"", auth_err)
        return rid, 400, "application/json", json.dumps(
            {"error": {"code": "NoApiKey", "message": auth_err}}, ensure_ascii=False).encode(), None
    t0 = time.time()
    status, rctype, rbody, err = net().send(method, url, ctype, body, auth)
    finish_request(rid, status, rctype, rbody, err)
    ms = int((time.time() - t0) * 1000)
    p0 = path.split("?")[0]
    if err:
        log_event("error", "timeout" if is_timeout(err) else "error",
                  f"{'请求超时' if is_timeout(err) else '网络错误'} · {method} {p0}", f"{url}\n{err}", request_id=rid)
    elif status >= 400:
        log_event("error" if status >= 500 or status in (401, 403) else "warn", "error",
                  f"HTTP {status} · {method} {p0}", err_text(rbody), request_id=rid)
    else:
        data = parse_json(rbody)
        tid = data.get("id") if isinstance(data, dict) and p0 == "/contents/generations/tasks" and method == "POST" else None
        note = f"任务 ID：{tid}" if tid else (f"共 {data.get('total')} 条" if isinstance(data, dict) and "total" in data else None)
        log_event("success" if not is_query else "info", "query" if is_query else "response",
                  f"HTTP {status} · {method} {p0}（{ms} ms）", note, task_id=tid, request_id=rid)
    try:
        after_response(method, path, body, status, rbody, rid, key_id)
    except Exception as e:  # 记录同步失败不影响返回
        print("tasks 同步失败:", e)
    return rid, status, rctype, rbody, err


def poller():
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            # 只查询用户主动开启「自动跟踪」、且还在跟踪期限内的任务；空闲时不会向方舟发任何请求
            rows = store().all("SELECT id, key_id FROM tasks WHERE status IN ('queued','running') AND watch_until > ?",
                               (time.time(),))
            for row in rows:
                tid = row["id"]
                auth, base, _kid, err = resolve_auth(f"Bearer @key:{row['key_id']}" if get_key(row["key_id"]) else None)
                if err:
                    continue
                url = base.rstrip("/") + "/contents/generations/tasks/" + tid
                status, _ct, rbody, err = net().send("GET", url, None, None, auth)
                if status and 200 <= status < 300:
                    t = parse_json(rbody) or {}
                    upsert_task(t)
                    if t.get("status") not in ("queued", "running"):
                        store().run("UPDATE tasks SET watch_until=NULL WHERE id=?", (tid,))
                elif status == 404:
                    store().run("UPDATE tasks SET status='not_found', watch_until=NULL, updated_at=? WHERE id=?", (time.time(), tid))
        except Exception as e:
            print("轮询出错:", e)
            log_event("error", "system", "后台轮询出错", f"{type(e).__name__}: {e}")


# ============================================================================ 视频本地保存
def media_path(task_id, kind):
    safe = re.sub(r"[^\w.-]", "_", task_id)[:120]
    return os.path.join(MEDIA_DIR, safe + (".mp4" if kind == "video" else ".jpg"))


def save_media(task_id, kind):
    """把任务的视频 / 尾帧下载到 data/media。只在用户点击「保存到本地」时调用。"""
    row = store().one("SELECT video_url, last_frame_url FROM tasks WHERE id=?", (task_id,))
    if not row:
        return False, "任务不存在"
    url = row["video_url"] if kind == "video" else row["last_frame_url"]
    if not url:
        return False, "这个任务还没有可保存的地址"
    if not host_allowed(url, MEDIA_HOST_ALLOW):
        return False, f"下载地址的域名 {urlsplit(url).hostname} 不在允许列表里"
    req = urllib.request.Request(url, headers={"User-Agent": "seedance-api-gui"})
    try:
        with net().opener_for(url).open(req, timeout=UPSTREAM_TIMEOUT) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(MAX_MEDIA_BYTES + 1)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}（视频地址 24 小时后会过期）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not data:
        return False, "下载到的内容为空"
    if len(data) > MAX_MEDIA_BYTES:
        return False, "文件超过 500 MB，已放弃"
    if "html" in ctype or "json" in ctype:
        return False, f"下载到的不是媒体文件（{ctype}）"
    os.makedirs(MEDIA_DIR, exist_ok=True)
    fp = media_path(task_id, kind)
    with open(fp + ".tmp", "wb") as f:
        f.write(data)
    os.replace(fp + ".tmp", fp)
    log_event("success", "task", f"已保存{'视频' if kind == 'video' else '尾帧'}到本地 {task_id}", f"{fp}（{len(data) // 1024} KB）", task_id)
    return True, fp
# ============================================================================ 设置相关
def drivers():
    return {"pymysql": pymysql is not None, "psycopg": psycopg is not None, "pysocks": socks is not None}


def settings_payload():
    s = load_settings()
    payload = {"db": masked(s["db"]), "proxy": masked(s["proxy"]), "drivers": drivers(),
               "python": sys.version.split()[0], "system_proxies": urllib.request.getproxies(),
               "active_db": store().describe(), "active_db_type": store().kind,
               "db_error": STATE["db_error"], "active_proxy": net().describe(),
               "proxy_error": STATE["proxy_error"]}
    try:
        payload["db_counts"] = store().counts()
    except Exception as e:
        payload["db_counts"] = None
        payload["db_error"] = payload["db_error"] or f"{type(e).__name__}: {e}"
    return payload


def test_db(cfg, create_db):
    st = Store(cfg)
    if create_db:
        st.ensure_database()
    return st, st.version()


def test_proxy(n, auth=None, base=None):
    if auth is None and base is None:
        auth, base, _kid, _err = resolve_auth(None)
    url = (base or DEFAULT_BASE).rstrip("/") + "/contents/generations/tasks?page_size=1"
    t0 = time.time()
    status, _ct, body, err = n.send("GET", url, None, None, auth)
    ms = int((time.time() - t0) * 1000)
    if err:
        return {"ok": False, "message": f"无法连接 {urlsplit(url).hostname}（{ms} ms）：{err}"}
    if status in (401, 403):
        note = "，但 API Key 无效或没有权限" if auth else "（还没有填写 API Key，所以返回未授权）"
        return {"ok": True, "message": f"网络已连通：HTTP {status}，耗时 {ms} ms{note}"}
    return {"ok": True, "message": f"网络已连通：HTTP {status}，耗时 {ms} ms" + ("，API Key 有效" if 200 <= status < 300 else "")}


# ============================================================================ HTTP
def row_summary(r):
    keys = ("id", "created_at", "finished_at", "method", "path", "kind", "state",
            "status_code", "error", "req_size", "resp_size", "retry_of")
    return {k: r[k] for k in keys}


def body_preview(b, ctype, limit=200_000):
    if b is None:
        return None
    b = bytes(b)
    if is_text(ctype) or "multipart" in (ctype or ""):
        t = b[:limit].decode("utf-8", errors="replace")
        return t + (f"\n…（已截断，共 {len(b)} 字节）" if len(b) > limit else "")
    return f"<二进制内容 {len(b)} 字节，类型 {ctype or '未知'}>"


class Handler(BaseHTTPRequestHandler):
    server_version = "seedance-api-gui/" + APP_VERSION

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, fp, ctype):
        size = os.path.getsize(fp)
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        partial = bool(rng and (rng.group(1) or rng.group(2)))
        start, end = 0, size - 1
        if partial:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        with open(fp, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, obj, status=200):
        self.send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def jbody(self):
        try:
            d = json.loads(self.body().decode("utf-8") or "{}")
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PATCH(self):
        self.route("PATCH")

    def route(self, method):
        try:
            u = urlsplit(self.path)
            p = u.path
            if p.startswith("/proxy/") or p == "/proxy":
                return self.proxy(method, self.path[len("/proxy"):] or "/")
            if p in ("/", "/index.html") and method == "GET":
                with open(INDEX, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if p.startswith("/docs/") and method == "GET":
                name = os.path.basename(p)
                fp = os.path.join(DOCS_DIR, name)
                if not re.fullmatch(r"[\w.-]+\.md", name) or not os.path.isfile(fp):
                    return self.json({"error": "文档不存在"}, 404)
                with open(fp, "rb") as f:
                    return self.send(200, f.read(), "text/markdown; charset=utf-8", {"Cache-Control": "max-age=3600"})
            mm = re.fullmatch(r"/media/([\w.-]+)\.(mp4|jpg)", p)
            if mm and method == "GET":
                fp = media_path(mm.group(1), "video" if mm.group(2) == "mp4" else "frame")
                if not os.path.isfile(fp):
                    return self.json({"error": "本地没有这个文件"}, 404)
                return self.send_file(fp, "video/mp4" if mm.group(2) == "mp4" else "image/jpeg")
            if p == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
            if p.startswith("/api/"):
                return self.api(method, p[len("/api"):], parse_qs(u.query))
            self.json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    def proxy(self, method, path):
        body = self.body()
        snap = self.headers.get("X-SAG-Snapshot")
        rid, status, rctype, rbody, err = do_proxy(method, path, self.headers.get("Content-Type"), body,
                                                   self.headers.get("Authorization"),
                                                   base=self.headers.get("X-SAG-Base"),
                                                   snapshot_id=int(snap) if (snap or "").isdigit() else None)
        if err:
            return self.send(502, json.dumps({"error": {"code": "UpstreamUnreachable", "message": err,
                                                        "record_id": rid}}, ensure_ascii=False).encode(),
                             extra={"X-Record-Id": str(rid)})
        self.send(status, rbody, rctype or "application/octet-stream", {"X-Record-Id": str(rid)})

    def api(self, method, p, q):
        if p == "/health":
            ak = get_key(get_config()["active_key_id"])
            return self.json({"ok": True, "version": APP_VERSION, "db": store().describe(), "db_type": store().kind,
                              "active_key": key_public(ak, ak["id"]) if ak else None,
                              "db_error": STATE["db_error"], "proxy": net().describe()})

        # ---------------- 设置：数据库 / 代理 ----------------
        if p == "/settings" and method == "GET":
            return self.json(settings_payload())

        if p in ("/settings/db/test", "/settings/db") and method in ("POST", "PUT"):
            d = self.jbody() or {}
            s = load_settings()
            cfg = merge_secret(d.get("db"), s["db"])
            try:
                new, ver = test_db(cfg, bool(d.get("create_db")))
            except Exception as e:
                return self.json({"ok": False, "message": f"连接失败：{type(e).__name__}: {e}"})
            if p.endswith("/test"):
                return self.json({"ok": True, "message": f"连接成功：{new.describe()}，版本 {ver}"})
            try:
                new.init_schema()
                note = ""
                with STATE_LOCK:
                    old = store()
                    if d.get("migrate") and new.identity() != old.identity():
                        note = copy_data(old, new)
                    STATE["store"], STATE["db_error"] = new, None
                    s["db"] = cfg
                    save_settings(s)
            except Exception as e:
                return self.json({"ok": False, "message": f"切换失败（仍使用原数据库）：{type(e).__name__}: {e}"})
            log_event("info", "config", f"切换数据库 → {new.describe()}", note or None)
            return self.json({"ok": True, "message": f"已切换到 {new.describe()}。{note}", "counts": new.counts()})

        if p in ("/settings/proxy/test", "/settings/proxy") and method in ("POST", "PUT"):
            d = self.jbody() or {}
            s = load_settings()
            cfg = merge_secret(d.get("proxy"), s["proxy"])
            try:
                n = Net(cfg)
            except Exception as e:
                return self.json({"ok": False, "message": f"代理配置无效：{e}"})
            if p.endswith("/test"):
                return self.json(test_proxy(n))
            with STATE_LOCK:
                STATE["net"], STATE["proxy_error"] = n, None
                s["proxy"] = cfg
                save_settings(s)
            log_event("info", "config", f"网络代理 → {n.describe()}")
            return self.json({"ok": True, "message": f"已生效：{n.describe()}"})

        # ---------------- API Key 管理 ----------------
        if p == "/keys":
            if method == "GET":
                active = get_config()["active_key_id"]
                rows = store().all("SELECT * FROM api_keys ORDER BY id")
                return self.json({"items": [key_public(r, active) for r in rows], "active_key_id": active})
            if method == "POST":
                d = self.jbody() or {}
                name, key = (d.get("name") or "").strip(), (d.get("api_key") or "").strip()
                if not name or not key:
                    return self.json({"ok": False, "message": "名称和 API Key 都必须填写"}, 400)
                now = time.time()
                kid = store().insert("api_keys", {"name": name[:100], "api_key": key,
                                                  "base_url": (d.get("base_url") or DEFAULT_BASE).strip(),
                                                  "note": (d.get("note") or "")[:500], "created_at": now,
                                                  "updated_at": now, "last_used_at": None})
                if d.get("activate"):
                    activate_key(kid)
                log_event("info", "config", f"保存 API Key「{name}」", mask_key(key))
                return self.json({"ok": True, "id": kid, "message": f"已保存「{name}」"})

        if p == "/keys/test" and method == "POST":
            d = self.jbody() or {}
            if d.get("id"):
                k = get_key(int(d["id"]))
                if not k:
                    return self.json({"ok": False, "message": "这个 Key 不存在"})
                auth, base = f"Bearer {k['api_key']}", d.get("base_url") or k["base_url"]
            else:
                key = (d.get("api_key") or "").strip()
                if not key:
                    return self.json({"ok": False, "message": "请先填写 API Key"})
                auth, base = f"Bearer {key}", d.get("base_url")
            r = test_proxy(net(), auth, base or DEFAULT_BASE)
            if r["ok"] and "HTTP 2" not in r["message"]:
                r["ok"] = False
            return self.json(r)

        m = re.fullmatch(r"/keys/(\d+)(/activate)?", p)
        if m:
            kid = int(m.group(1))
            k = get_key(kid)
            if not k:
                return self.json({"ok": False, "message": "这个 Key 不存在"}, 404)
            if m.group(2) and method == "POST":
                activate_key(kid)
                log_event("info", "config", f"切换 API Key →「{k['name']}」")
                return self.json({"ok": True, "message": f"当前使用「{k['name']}」"})
            if method == "PUT":
                d = self.jbody() or {}
                store().run("UPDATE api_keys SET name=?, api_key=?, base_url=?, note=?, updated_at=? WHERE id=?",
                            ((d.get("name") or k["name"]).strip()[:100], (d.get("api_key") or "").strip() or k["api_key"],
                             (d.get("base_url") or k["base_url"] or DEFAULT_BASE).strip(),
                             (d.get("note") if d.get("note") is not None else k["note"] or "")[:500], time.time(), kid))
                if get_config()["active_key_id"] == kid:
                    activate_key(kid)
                return self.json({"ok": True, "message": "已更新"})
            if method == "DELETE":
                store().run("DELETE FROM api_keys WHERE id=?", (kid,))
                if get_config()["active_key_id"] == kid:
                    activate_key(None)
                log_event("warn", "config", f"删除 API Key「{k['name']}」")
                return self.json({"ok": True, "message": f"已删除「{k['name']}」"})

        if p == "/keys/deactivate" and method == "POST":
            activate_key(None)
            return self.json({"ok": True})

        # ---------------- 配置快照（历史调用一键回填） ----------------
        if p == "/snapshots" and method == "POST":
            raw = self.body()
            json.loads(raw.decode("utf-8"))
            return self.json({"id": save_snapshot(raw)})
        m = re.fullmatch(r"/snapshots/(\d+)", p)
        if m and method == "GET":
            r = store().one("SELECT v, created_at FROM snapshots WHERE id=?", (int(m.group(1)),))
            if not r:
                return self.json({"error": "快照不存在"}, 404)
            return self.json({"value": json.loads(r["v"]), "created_at": r["created_at"]})

        # ---------------- API 配置 / 页面状态 ----------------
        if p == "/config":
            if method == "GET":
                return self.json(get_config())
            if method in ("PUT", "POST"):
                data = self.jbody()
                if data is None:
                    return self.json({"error": "bad json"}, 400)
                set_config(data)
                return self.json(get_config())

        m = re.fullmatch(r"/state/([\w.-]+)", p)
        if m:
            key = m.group(1)
            if method == "GET":
                r = store().one("SELECT v, updated_at FROM app_state WHERE k=?", (key,))
                return self.json({"value": json.loads(r["v"]) if r else None, "updated_at": r["updated_at"] if r else None})
            if method in ("PUT", "POST"):          # POST 供 navigator.sendBeacon 使用
                raw = self.body().decode("utf-8")
                json.loads(raw)
                store().upsert("app_state", "k", {"k": key, "v": raw, "updated_at": time.time()})
                return self.json({"ok": True})
            if method == "DELETE":
                store().run("DELETE FROM app_state WHERE k=?", (key,))
                return self.json({"ok": True})

        # ---------------- 请求记录 ----------------
        if p == "/requests":
            if method == "GET":
                limit = max(1, min(int((q.get("limit") or ["50"])[0]), 500))
                offset = max(0, int((q.get("offset") or ["0"])[0]))
                where, args = [], []
                for f in ("kind", "state"):
                    v = (q.get(f) or [""])[0]
                    if v:
                        where.append(f"{f}=?")
                        args.append(v)
                if (q.get("writes") or [""])[0] == "1":
                    where.append("method<>'GET'")
                w = (" WHERE " + " AND ".join(where)) if where else ""
                total = store().one("SELECT COUNT(*) AS n FROM requests" + w, args)["n"]
                rows = store().all("SELECT id, created_at, finished_at, method, path, kind, state, status_code, "
                                   "error, req_size, resp_size, retry_of, snapshot_id, key_id, summary FROM requests" + w +
                                   f" ORDER BY id DESC LIMIT {limit} OFFSET {offset}", args)
                names = {r["id"]: r["name"] for r in store().all("SELECT id, name FROM api_keys")}
                items = []
                for r in rows:
                    d = row_summary(r)
                    d.update(snapshot_id=r["snapshot_id"], key_id=r["key_id"], key_name=names.get(r["key_id"]),
                             summary=r["summary"])
                    items.append(d)
                return self.json({"total": int(total), "items": items})
            if method == "DELETE":
                store().run("DELETE FROM conv_meta")
                store().run("DELETE FROM requests")
                return self.json({"ok": True})

        m = re.fullmatch(r"/requests/(\d+)(/retry)?", p)
        if m:
            rid = int(m.group(1))
            r = store().one("SELECT * FROM requests WHERE id=?", (rid,))
            if not r:
                return self.json({"error": "记录不存在"}, 404)
            if m.group(2) and method == "POST":
                base = r["url"][:-len(r["path"])] if r["url"] and r["url"].endswith(r["path"]) else None
                auth = f"Bearer @key:{r['key_id']}" if get_key(r["key_id"]) else None
                nid, status, rctype, rbody, err = do_proxy(r["method"], r["path"], r["req_content_type"],
                                                           bytes(r["req_body"] or b""), auth, retry_of=rid,
                                                           base=base, snapshot_id=r["snapshot_id"])
                return self.json({"record_id": nid, "status_code": status, "error": err,
                                  "response": body_preview(rbody, rctype, 20000)})
            if method == "GET":
                d = row_summary(r)
                k = get_key(r["key_id"])
                d.update(url=r["url"], req_content_type=r["req_content_type"], resp_content_type=r["resp_content_type"],
                         snapshot_id=r["snapshot_id"], key_id=r["key_id"], key_name=k["name"] if k else None,
                         summary=r["summary"],
                         req_body=body_preview(r["req_body"], r["req_content_type"]),
                         resp_body=body_preview(r["resp_body"], r["resp_content_type"]))
                return self.json(d)
            if method == "DELETE":
                store().run("DELETE FROM conv_meta WHERE request_id=?", (rid,))
                store().run("DELETE FROM requests WHERE id=?", (rid,))
                return self.json({"ok": True})

        # ---------------- 事件日志 ----------------
        if p == "/events":
            if method == "GET":
                limit = max(1, min(int((q.get("limit") or ["100"])[0]), 1000))
                offset = max(0, int((q.get("offset") or ["0"])[0]))
                where, args = [], []
                lv = [x for x in (q.get("level") or [""])[0].split(",") if x in EVENT_LEVELS]
                if lv:
                    where.append("level IN (" + ",".join("?" * len(lv)) + ")"); args += lv
                cats = [x for x in (q.get("cats") or [""])[0].split(",") if x in EVENT_CATEGORIES]
                if cats:
                    where.append("category IN (" + ",".join("?" * len(cats)) + ")"); args += cats
                kw = (q.get("q") or [""])[0].strip()
                if kw:
                    where.append("(title LIKE ? OR detail LIKE ? OR task_id LIKE ?)"); args += [f"%{kw}%"] * 3
                since = (q.get("since") or [""])[0]
                if since:
                    where.append("created_at > ?"); args.append(float(since))
                w = (" WHERE " + " AND ".join(where)) if where else ""
                total = int(store().one("SELECT COUNT(*) AS n FROM events" + w, args)["n"])
                rows = store().all("SELECT * FROM events" + w + f" ORDER BY id DESC LIMIT {limit} OFFSET {offset}", args)
                cnt = {r["category"]: int(r["n"]) for r in store().all("SELECT category, COUNT(*) AS n FROM events GROUP BY category")}
                return self.json({"total": total, "items": rows, "counts": cnt})
            if method == "POST":   # 前端事件（如未连接时的校验、终止提示等）
                d = self.jbody() or {}
                log_event(d.get("level") or "info", d.get("category") or "client", d.get("title") or "前端事件",
                          d.get("detail"), d.get("task_id"), None, "client")
                return self.json({"ok": True})
            if method == "DELETE":
                store().run("DELETE FROM events")
                return self.json({"ok": True})

        # ---------------- 文件夹 / 会话归类 ----------------
        if p == "/folders":
            if method == "GET":
                return self.json({"items": store().all("SELECT * FROM folders ORDER BY sort, id")})
            if method == "POST":
                d = self.jbody() or {}
                name = (d.get("name") or "").strip()[:100] or "新建文件夹"
                top = store().one("SELECT MIN(sort) AS s FROM folders")
                now = time.time()
                fid = store().insert("folders", {"name": name, "sort": ((top or {}).get("s") or 0) - 1, "collapsed": 0,
                          "color": (d.get("color") or "")[:20], "created_at": now, "updated_at": now})
                log_event("info", "config", f"新建文件夹「{name}」")
                return self.json({"ok": True, "id": fid})
        m = re.fullmatch(r"/folders/(\d+)", p)
        if m:
            fid = int(m.group(1))
            f = store().one("SELECT * FROM folders WHERE id=?", (fid,))
            if not f:
                return self.json({"ok": False, "message": "文件夹不存在"}, 404)
            if method == "PUT":
                d = self.jbody() or {}
                name = (d.get("name") if d.get("name") is not None else f["name"]).strip()[:100] or f["name"]
                store().run("UPDATE folders SET name=?, collapsed=?, color=?, updated_at=? WHERE id=?",
                            (name, 1 if d.get("collapsed", f["collapsed"]) else 0,
                      (d.get("color") if d.get("color") is not None else f["color"] or "")[:20], time.time(), fid))
                return self.json({"ok": True})
            if method == "DELETE":
                # 删除文件夹不删除里面的会话，会话移回「未归类」
                store().run("UPDATE conv_meta SET folder_id=NULL WHERE folder_id=?", (fid,))
                store().run("DELETE FROM folders WHERE id=?", (fid,))
                log_event("info", "config", f"删除文件夹「{f['name']}」", "里面的会话已移回「未归类」")
                return self.json({"ok": True})
        if p == "/folders/order" and method == "PUT":
            ids = [int(x) for x in ((self.jbody() or {}).get("ids") or []) if str(x).isdigit()]
            for i, fid in enumerate(ids):
                store().run("UPDATE folders SET sort=? WHERE id=?", (i, fid))
            return self.json({"ok": True})
        m = re.fullmatch(r"/conversations/(\d+)/meta", p)
        if m and method == "PUT":
            rid = int(m.group(1))
            if not store().one("SELECT id FROM requests WHERE id=?", (rid,)):
                return self.json({"ok": False, "message": "会话不存在"}, 404)
            d = self.jbody() or dict()
            old = store().one("SELECT * FROM conv_meta WHERE request_id=?", (rid,)) or dict()
            title = old.get("title")
            if "title" in d:
                title = (d.get("title") or "").strip()[:200] or None   # 空字符串表示恢复为默认标题
            folder = old.get("folder_id")
            if "folder_id" in d:
                folder = int(d["folder_id"]) if str(d.get("folder_id") or "").isdigit() else None
                if folder and not store().one("SELECT id FROM folders WHERE id=?", (folder,)):
                    return self.json({"ok": False, "message": "文件夹不存在"}, 404)
            sort = d.get("sort", old.get("sort"))
            store().upsert("conv_meta", "request_id", {"request_id": rid, "title": title, "folder_id": folder,
                    "sort": float(sort) if sort is not None else None, "updated_at": time.time()})
            return self.json({"ok": True, "title": title, "folder_id": folder})
        if p == "/conversations/order" and method == "PUT":
            # 一次性写入一组会话的文件夹和顺序（拖动排序后调用）
            d = self.jbody() or {}
            folder = int(d["folder_id"]) if str(d.get("folder_id") or "").isdigit() else None
            ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]
            now = time.time()
            for i, rid in enumerate(ids):
                old = store().one("SELECT title FROM conv_meta WHERE request_id=?", (rid,)) or {}
                store().upsert("conv_meta", "request_id", {"request_id": rid, "title": old.get("title"), "folder_id": folder,
                        "sort": float(i), "updated_at": now})
            return self.json({"ok": True})
        # ---------------- 视频生成会话（左侧历史栏） ----------------
        if p == "/conversations" and method == "GET":
            limit = max(1, min(int((q.get("limit") or ["100"])[0]), 500))
            return self.json({"items": conversations(limit)})
        m = re.fullmatch(r"/conversations/(\d+)", p)
        if m and method == "GET":
            items = conversations(1, int(m.group(1)))
            return self.json(items[0] if items else {"error": "不存在"}, 200 if items else 404)
        if m and method == "DELETE":
            rid = int(m.group(1))
            for t in store().all("SELECT id FROM tasks WHERE request_id=?", (rid,)):
                for kind in ("video", "frame"):
                    if os.path.isfile(media_path(t["id"], kind)):
                        os.remove(media_path(t["id"], kind))
            store().run("DELETE FROM tasks WHERE request_id=?", (rid,))
            store().run("DELETE FROM conv_meta WHERE request_id=?", (rid,))
            store().run("DELETE FROM requests WHERE id=?", (rid,))
            return self.json({"ok": True})

        # ---------------- 视频任务 ----------------
        if p == "/tasks" and method == "GET":
            rows = store().all("SELECT id, model, prompt, status, video_url, last_frame_url, error, request_id, "
                               "created_at, updated_at FROM tasks ORDER BY created_at DESC LIMIT 200")
            return self.json({"items": rows})

        m = re.fullmatch(r"/tasks/([^/]+)/save", p)
        if m and method == "POST":
            kind = "frame" if (q.get("kind") or [""])[0] == "frame" else "video"
            ok, info = save_media(m.group(1), kind)
            return self.json({"ok": ok, "message": "已保存" if ok else info, "path": info if ok else None})

        m = re.fullmatch(r"/tasks/([^/]+)/watch", p)
        if m and method in ("POST", "DELETE"):
            until = time.time() + POLL_MAX_AGE if method == "POST" else None
            store().run("UPDATE tasks SET watch_until=? WHERE id=?", (until, m.group(1)))
            log_event("info", "task", f"{'开启' if until else '关闭'}自动跟踪 {m.group(1)}",
                      f"每 {POLL_INTERVAL} 秒查询一次，最长 {POLL_MAX_AGE // 3600} 小时" if until else None, m.group(1))
            return self.json({"ok": True, "watch_until": until})

        m = re.fullmatch(r"/tasks/([^/]+)", p)
        if m and method == "DELETE":
            store().run("DELETE FROM tasks WHERE id=?", (m.group(1),))
            return self.json({"ok": True})

        self.json({"error": "not found"}, 404)


# ============================================================================ 启动
# ============================================================================ 启动时导出上一次的日志
LV_CN = {"info": "信息", "success": "成功", "warn": "警告", "error": "错误"}
CAT_CN = {"submit": "提交", "response": "返回", "query": "查询", "timeout": "超时", "error": "错误", "task": "任务",
"retry": "重发", "config": "配置", "system": "系统", "client": "前端"}


def fmt_event(e):
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["created_at"] or 0))
    line = f"[{t}] [{LV_CN.get(e['level'], e['level'])}] [{CAT_CN.get(e['category'], e['category'])}] {e['title']}"
    if e.get("task_id"):
        line += f"  task={e['task_id']}"
    if e.get("request_id"):
        line += f"  req=#{e['request_id']}"
    if e.get("source") == "client":
        line += "  (前端)"
    detail = (e.get("detail") or "").strip()
    if detail:
        line += "\n" + "\n".join("    " + x for x in detail.split("\n"))
    return line


def export_previous_logs():
    """把上次导出之后新增的日志（也就是上一次运行期间的日志）导出到 log/yyyy-mm-dd_NN.txt。
    日期取这批日志里第一条的日期；同一天有多个文件时，编号 NN 依次递增。没有新日志时不生成文件。"""
    try:
        row = store().one("SELECT v FROM app_config WHERE k=?", ("log_export_last_id",))
        last = int(row["v"]) if row and str(row["v"]).isdigit() else 0
        r = store().one("SELECT MIN(id) AS a, MAX(id) AS b, COUNT(*) AS n FROM events WHERE id > ?", (last,))
        if not r or not r["n"]:
            return None
        lo, hi, n = int(r["a"]), int(r["b"]), int(r["n"])
        t0 = store().one("SELECT created_at FROM events WHERE id=?", (lo,))["created_at"] or time.time()
        t1 = store().one("SELECT created_at FROM events WHERE id=?", (hi,))["created_at"] or time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(t0))
        os.makedirs(LOG_DIR, exist_ok=True)
        seq = 1
        while os.path.exists(os.path.join(LOG_DIR, f"{day}_{seq:02d}.txt")):
            seq += 1
        fp = os.path.join(LOG_DIR, f"{day}_{seq:02d}.txt")
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        with open(fp + ".tmp", "w", encoding="utf-8") as f:
            f.write(f"seedance-api-gui {APP_VERSION} · 上一次运行的日志\n")
            f.write(f"时间范围：{fmt(t0)} — {fmt(t1)}\n")
            f.write(f"共 {n} 条（事件编号 {lo}–{hi}） · 数据库：{store().describe()} · 导出于 {fmt(time.time())}\n")
            f.write("=" * 72 + "\n\n")
            cur = last
            while True:
                rows = store().all("SELECT * FROM events WHERE id > ? AND id <= ? ORDER BY id LIMIT 1000", (cur, hi))
                if not rows:
                    break
                for e in rows:
                    f.write(fmt_event(e) + "\n")
                cur = rows[-1]["id"]
        os.replace(fp + ".tmp", fp)
        store().upsert("app_config", "k", {"k": "log_export_last_id", "v": str(hi), "updated_at": time.time()})
        print(f"已导出上一次的日志：{os.path.relpath(fp, ROOT)}（{n} 条）")
        return fp
    except Exception as e:
        print("导出日志失败：", e)
        return None
def boot():
    os.makedirs(DATA_DIR, exist_ok=True)
    s = load_settings()
    try:
        st = Store(s["db"])
        st.init_schema()
    except Exception as e:
        STATE["db_error"] = f"无法连接已配置的数据库（{type(e).__name__}: {e}），已临时改用本地 SQLite。"
        print("⚠", STATE["db_error"])
        st = Store(DEFAULT_SETTINGS["db"])
        st.init_schema()
    STATE["store"] = st
    STATE["log_export"] = export_previous_logs()
    if STATE["db_error"]:
        log_event("error", "system", "数据库连接失败，已临时改用本地 SQLite", STATE["db_error"])
    try:
        STATE["net"] = Net(s["proxy"])
    except Exception as e:
        STATE["proxy_error"] = f"代理配置无效（{e}），已临时改为跟随系统代理。"
        print("⚠", STATE["proxy_error"])
        STATE["net"] = Net(DEFAULT_SETTINGS["proxy"])


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 实时输出到终端或日志文件
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="seedance-api-gui 本地后端")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机访问）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SAG_PORT", 6547)))
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    boot()
    threading.Thread(target=poller, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    exported = os.path.relpath(STATE["log_export"], ROOT) if STATE.get("log_export") else None
    log_event("info", "system", f"后端启动 · v{APP_VERSION}", f"地址 {url}\n数据库 {store().describe()}\n网络 {net().describe()}"
+ (f"\n已导出上一次的日志：{exported}" if exported else ""))
    print(f"seedance-api-gui 已启动：{url}")
    print(f"数据库：{store().describe()}")
    print(f"网络：{net().describe()}")
    print(f"驱动：{', '.join(k + ('✓' if v else '✗') for k, v in drivers().items())}")
    print("按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
