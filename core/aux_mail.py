# -*- coding: utf-8 -*-
"""辅助邮箱池：微软登录「Help us protect your account」验证码的收码邮箱。

微软账号（hotmail/outlook）在 Codex 补跑登录时，输入邮箱密码后可能要求
向注册时预留的恢复邮箱（如 sb*****@163.com）发送验证码。这些恢复邮箱
不在本项目的邮箱池里，单独维护：用户在「辅助邮箱」页导入
`邮箱----密码/授权码`，登录流程按页面上的掩码邮箱匹配到池里的账号，
直接 IMAP 取码完成验证。
"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_DB_PATH = Path(__file__).resolve().parent.parent / "turb.sqlite3"

# 常见邮箱域名的 IMAP 服务器；未收录的域名退化为 imap.<域名>
_IMAP_SERVERS = {
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "yeah.net": "imap.yeah.net",
    "qq.com": "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "sina.com": "imap.sina.com",
    "sohu.com": "imap.sohu.com",
    "139.com": "imap.139.com",
    "189.cn": "imap.189.cn",
    "aliyun.com": "imap.aliyun.com",
    "139.com.cn": "imap.139.com",
}

# 微软「保护账号」验证码邮件的发件人/主题特征；163 等国内邮箱也可能转发
_MS_MAIL_HINT = re.compile(
    r"microsoft|account.?protection|outlook team|security code|verify your"
    r"|验证代码|安全代码|セキュリティ コード", re.I)
# 验证码本体：优先取 “code/验证代码/コード” 关键词附近的 4-8 位数字
_CODE_WITH_HINT = re.compile(
    r"(?:code(?:\s+is)?|验证代码|验证码|安全代码|コード)[^0-9]{0,24}(\d{4,8})", re.I)
_CODE_FALLBACK = re.compile(r"\b(\d{4,8})\b")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aux_emails (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL DEFAULT '',
            server TEXT NOT NULL DEFAULT '',
            port INTEGER NOT NULL DEFAULT 993,
            status TEXT NOT NULL DEFAULT 'available',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)


def guess_imap_server(email_addr: str) -> str:
    domain = str(email_addr or "").split("@")[-1].strip().lower()
    return _IMAP_SERVERS.get(domain) or f"imap.{domain}"


def list_emails() -> list[dict]:
    with _LOCK, closing(_conn()) as conn:
        _ensure_table(conn)
        rows = conn.execute("SELECT * FROM aux_emails ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def add_email(email_addr: str, password: str, server: str = "") -> dict:
    email_addr = str(email_addr or "").strip()
    password = str(password or "").strip()
    if "@" not in email_addr or not password:
        return {"ok": False, "error": "邮箱或密码为空"}
    server = str(server or "").strip() or guess_imap_server(email_addr)
    now = datetime.now().isoformat(timespec="seconds")
    with _LOCK, closing(_conn()) as conn:
        _ensure_table(conn)
        with conn:
            conn.execute(
                "INSERT INTO aux_emails(email,password,server,status,created_at,updated_at) "
                "VALUES(?,?,?,'available',?,?) "
                "ON CONFLICT(email) DO UPDATE SET password=excluded.password, "
                "server=excluded.server, status='available', updated_at=excluded.updated_at",
                (email_addr, password, server, now, now),
            )
    return {"ok": True, "email": email_addr, "server": server}


def delete_email(key) -> bool:
    """按 id（数字）或邮箱删除。返回是否删到。"""
    with _LOCK, closing(_conn()) as conn:
        _ensure_table(conn)
        with conn:
            if str(key).isdigit():
                cur = conn.execute("DELETE FROM aux_emails WHERE id=?", (int(key),))
            else:
                cur = conn.execute("DELETE FROM aux_emails WHERE email=?", (str(key).strip(),))
            return cur.rowcount > 0


def parse_import_lines(text: str) -> dict:
    """导入 `邮箱----密码[----IMAP服务器]`，与邮箱池素材行格式一致。"""
    added, failed = [], []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("----")]
        if len(parts) < 2:
            failed.append({"line": line[:60], "reason": "格式应为 邮箱----密码[----服务器]"})
            continue
        result = add_email(parts[0], parts[1], parts[2] if len(parts) > 2 else "")
        (added if result.get("ok") else failed).append(
            result if result.get("ok") else {"line": parts[0], "reason": result.get("error")})
    return {"added": added, "failed": failed}


_MASKED_RE = re.compile(r"^([A-Za-z0-9._%+-]{1,3})\*+@([A-Za-z0-9.-]+)$")


def match_masked(masked: str) -> dict | None:
    """把微软页面上的掩码邮箱（sb*****@163.com）匹配到池里的辅助邮箱。

    匹配规则：域名完全一致，且真实邮箱的 local part 以掩码前缀开头。
    命中多条时返回创建时间最新的（后导入的通常是新配的恢复邮箱）。
    """
    m = _MASKED_RE.match(str(masked or "").strip())
    if not m:
        # 没打码的完整邮箱：直接按地址找
        target = str(masked or "").strip().lower()
        for row in list_emails():
            if str(row.get("email") or "").lower() == target:
                return row
        return None
    prefix, domain = m.group(1).lower(), m.group(2).lower()
    hits = []
    for row in list_emails():
        addr = str(row.get("email") or "").strip().lower()
        local, _, row_domain = addr.partition("@")
        if row_domain == domain and local.startswith(prefix):
            hits.append(row)
    if not hits:
        return None
    hits.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return hits[0]


def _looks_like_ms_mail(msg: dict) -> bool:
    haystack = " ".join(str(msg.get(k) or "") for k in ("from", "subject"))
    return bool(_MS_MAIL_HINT.search(haystack))


def _extract_ms_code(msg: dict) -> str:
    for text in (str(msg.get("subject") or ""), str(msg.get("body") or ""),
                 str(msg.get("text") or "")):
        m = _CODE_WITH_HINT.search(text)
        if m:
            return m.group(1)
    for text in (str(msg.get("subject") or ""), str(msg.get("body") or ""),
                 str(msg.get("text") or "")):
        m = _CODE_FALLBACK.search(text)
        if m:
            return m.group(1)
    return ""


def fetch_microsoft_code(
    email_addr: str,
    after_ts: float,
    max_wait: int = 150,
    poll_interval: int = 5,
) -> str:
    """轮询辅助邮箱，取微软「保护账号」验证码。

    与通用 IMAP 池不同：这里找的是微软安全邮件（不是 OpenAI 验证码），
    且匹配键只有邮箱地址（登录流程按掩码匹配后传入完整地址）。
    """
    row = next((r for r in list_emails()
                if str(r.get("email") or "").lower() == str(email_addr).lower()), None)
    if row is None:
        raise RuntimeError(f"辅助邮箱池中没有 {email_addr}")
    server = str(row.get("server") or "") or guess_imap_server(email_addr)
    port = int(row.get("port") or 993)
    password = str(row.get("password") or "")
    after_dt = datetime.fromtimestamp(after_ts - 120, tz=timezone.utc)

    deadline = time.time() + max_wait
    logger.info("[辅助邮箱] 开始轮询 %s (%s:%s) 的微软验证码，最长 %ss", email_addr, server, port, max_wait)
    while time.time() < deadline:
        mail = None
        messages: list[dict] = []
        try:
            mail = imaplib.IMAP4_SSL(server, port)
            mail.login(email_addr, password)
            status, _ = mail.select("INBOX")
            if status != "OK":
                raise RuntimeError("无法打开 INBOX")
            status, result = mail.search(None, f'(SINCE {after_dt.strftime("%d-%b-%Y")})')
            if status == "OK" and result and result[0]:
                for message_id in result[0].split()[-20:]:
                    status2, data = mail.fetch(message_id, "(RFC822)")
                    if status2 != "OK" or not data:
                        continue
                    raw = next((p[1] for p in data if isinstance(p, tuple) and len(p) > 1), None)
                    if not raw:
                        continue
                    try:
                        from core.qqmail_client import _msg_to_dict
                        messages.append(_msg_to_dict(email_lib.message_from_bytes(raw)))
                    except Exception as exc:
                        logger.debug("[辅助邮箱] 邮件解析失败 id=%r: %s", message_id, exc)
        except Exception as exc:
            logger.warning("[辅助邮箱] %s 读取失败: %s", email_addr, str(exc)[:160])
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

        messages.sort(key=lambda item: item.get("date") or "", reverse=True)
        for item in messages:
            if not _looks_like_ms_mail(item):
                continue
            code = _extract_ms_code(item)
            if code:
                logger.info("[辅助邮箱] %s 收到微软验证码 %s（主题: %s）",
                            email_addr, code, str(item.get("subject") or "")[:60])
                return code
        time.sleep(max(2, poll_interval))
    raise RuntimeError(f"等待 {email_addr} 的微软验证码超时（>{max_wait}s）")
