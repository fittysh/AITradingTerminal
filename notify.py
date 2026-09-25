"""notify.py — Telegram + Gmail delivery (stdlib only).

Telegram: Bot API sendMessage (HTML parse mode, split under the 4096-char limit without breaking
<pre> blocks) and sendDocument (PDF). Gmail: SMTP over SSL with an app password.
Every message is also written to state/outbox/ so the UI can show what was sent — and so nothing is
lost when a channel is not configured (sim/replay runs never send anything).
"""
from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
import time
import urllib.request
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, List, Optional

import config
import timeutil as tu

TG_LIMIT = 3900


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def split_telegram(text: str, limit: int = TG_LIMIT) -> List[str]:
    """Chunk on line boundaries; a chunk that ends inside <pre> is closed and the next reopened."""
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    in_pre = False
    for line in text.split("\n"):
        pieces = [line[i:i + limit - 20] for i in range(0, len(line), limit - 20)] or [""]
        for piece in pieces:
            if size + len(piece) + 1 > limit and cur:
                chunk = "\n".join(cur)
                if in_pre:
                    chunk += "</pre>"
                chunks.append(chunk)
                cur, size = (["<pre>"] if in_pre else []), (5 if in_pre else 0)
            cur.append(piece)
            size += len(piece) + 1
        in_pre = (in_pre or "<pre>" in line) and not (line.rfind("</pre>") > line.rfind("<pre>"))
    if cur and "\n".join(cur).strip():
        chunks.append("\n".join(cur))
    return chunks


class Notifier:
    def __init__(self, log: logging.Logger, outbox: Optional[Path] = None, enabled: bool = True,
                 token: str = config.TELEGRAM_BOT_TOKEN, chat_id: str = config.TELEGRAM_CHAT_ID,
                 gmail_user: str = config.GMAIL_USER, gmail_password: str = config.GMAIL_APP_PASSWORD,
                 gmail_to: str = config.GMAIL_TO, clock=tu.now_ms):
        self.log = log
        self.outbox = outbox
        self.enabled = enabled
        self.token, self.chat_id = token, chat_id
        self.gmail_user, self.gmail_password = gmail_user, gmail_password
        self.gmail_to = gmail_to or gmail_user
        self.clock = clock

    @property
    def has_telegram(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)

    @property
    def has_email(self) -> bool:
        return bool(self.enabled and self.gmail_user and self.gmail_password and self.gmail_to)

    def channels(self) -> str:
        on = [n for n, ok in (("telegram", self.has_telegram), ("gmail", self.has_email)) if ok]
        return ", ".join(on) if on else "none (outbox only)"

    # -- public ------------------------------------------------------------------------------------
    def send(self, subject: str, tg_html: str, email_html: Optional[str] = None,
             attachments: Iterable[Path] = (), telegram: bool = True, email: bool = True) -> None:
        attachments = [Path(a) for a in attachments]
        self._to_outbox(subject, tg_html, email_html, attachments)
        if telegram and self.has_telegram:
            for chunk in split_telegram(tg_html):
                self._telegram("sendMessage", {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
            for a in attachments:
                self._telegram_file(a, subject)
        if email and self.has_email:
            self._email(subject, email_html or f"<pre>{tg_html}</pre>", attachments)

    # -- transports ----------------------------------------------------------------------------------
    def _telegram(self, method: str, payload: dict) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        return self._post(req, f"telegram {method}")

    def _telegram_file(self, path: Path, caption: str) -> bool:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", self.chat_id), ("caption", caption[:1000])):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                      f"Content-Type: application/pdf\r\n\r\n").encode())
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        url = f"https://api.telegram.org/bot{self.token}/sendDocument"
        req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._post(req, "telegram sendDocument")

    def _post(self, req: urllib.request.Request, what: str) -> bool:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read().decode() or "{}")
                    if body.get("ok", True):
                        return True
                    self.log.warning("%s rejected: %s", what, str(body)[:200])
                    return False
            except Exception as exc:  # network trouble must never stop trading jobs
                self.log.warning("%s failed (attempt %d): %s", what, attempt + 1, exc)
                time.sleep(1.5 * (attempt + 1))
        return False

    def _email(self, subject: str, body_html: str, attachments: List[Path]) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.gmail_user
        msg["To"] = self.gmail_to
        msg.set_content("This message is best viewed as HTML.\n")
        msg.add_alternative(body_html, subtype="html")
        for a in attachments:
            msg.add_attachment(a.read_bytes(), maintype="application", subtype="pdf", filename=a.name)
        try:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context(),
                                  timeout=30) as smtp:
                smtp.login(self.gmail_user, self.gmail_password)
                smtp.send_message(msg)
            return True
        except Exception as exc:
            self.log.warning("gmail send failed: %s", exc)
            return False

    def _to_outbox(self, subject: str, tg_html: str, email_html: Optional[str], attachments: List[Path]) -> None:
        if not self.outbox:
            return
        stamp = tu.fmt(self.clock(), "%Y%m%d-%H%M%S")
        slug = "".join(ch if ch.isalnum() else "-" for ch in subject.lower())[:48].strip("-")
        doc = (f"<!doctype html><meta charset=utf-8><title>{esc(subject)}</title>"
               f"<h3>{esc(subject)}</h3><pre style='white-space:pre-wrap'>{tg_html}</pre>"
               + (f"<hr>{email_html}" if email_html else "")
               + "".join(f"<p>attachment: {esc(a.name)}</p>" for a in attachments))
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            (self.outbox / f"{stamp}-{slug}.html").write_text(doc, encoding="utf-8")
        except OSError as exc:
            self.log.debug("outbox write failed: %s", exc)
