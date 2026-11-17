"""notify.py — Telegram + email delivery (stdlib only).

Telegram: Bot API sendMessage (HTML parse mode, split under the 4096-char limit without breaking
<pre> blocks) and sendDocument (PDF).
Email: the Brevo transactional API (HTTPS, api-key header) when BREVO_API_KEY is set — it works on servers
that block mail ports — otherwise SMTP: SSL on port 465 (Gmail with an app password), STARTTLS on any
other port (Brevo's relay on 587 or 2525). PDFs go along as attachments either way.
Every message is also written to state/outbox/ so the UI can show what was sent — and so nothing is
lost when a channel is not configured (sim/replay runs never send anything).
"""
from __future__ import annotations

import base64
import html
import json
import logging
import smtplib
import ssl
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Iterable, List, Optional

import config
import timeutil as tu

TG_LIMIT = 3900
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


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
                 brevo_key: str = config.BREVO_API_KEY, smtp_user: str = config.SMTP_USER,
                 smtp_password: str = config.SMTP_PASSWORD, email_from: str = config.EMAIL_FROM,
                 email_to: str = config.EMAIL_TO, email_from_name: str = config.EMAIL_FROM_NAME,
                 smtp_host: str = config.SMTP_HOST, smtp_port: int = config.SMTP_PORT, clock=tu.now_ms):
        self.log = log
        self.outbox = outbox
        self.enabled = enabled
        self.token, self.chat_id = token, chat_id
        self.brevo_key = brevo_key
        self.smtp_user, self.smtp_password = smtp_user, smtp_password
        self.smtp_host, self.smtp_port = smtp_host, int(smtp_port)
        self.email_from = email_from or smtp_user
        self.email_from_name = email_from_name
        self.email_to = [a.strip() for a in (email_to or self.email_from or "").split(",") if a.strip()]
        self.clock = clock

    @property
    def has_telegram(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)

    @property
    def email_via(self) -> str:
        """"brevo" (API), "smtp", or "" when email is off or not configured."""
        if not (self.enabled and self.email_to and self.email_from):
            return ""
        if self.brevo_key:
            return "brevo"
        return "smtp" if self.smtp_user and self.smtp_password else ""

    @property
    def has_email(self) -> bool:
        return bool(self.email_via)

    def channels(self) -> str:
        on = [n for n, ok in (("telegram", self.has_telegram), (f"email ({self.email_via})", self.has_email)) if ok]
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
        if self.email_via == "brevo":
            return self._brevo(subject, body_html, attachments)
        return self._smtp(subject, body_html, attachments)

    def _brevo(self, subject: str, body_html: str, attachments: List[Path]) -> bool:
        payload = {"sender": {"name": self.email_from_name, "email": self.email_from},
                   "to": [{"email": a} for a in self.email_to], "subject": subject, "htmlContent": body_html}
        if attachments:
            payload["attachment"] = [{"name": a.name, "content": base64.b64encode(a.read_bytes()).decode()}
                                     for a in attachments]
        req = urllib.request.Request(BREVO_URL, data=json.dumps(payload).encode(), method="POST",
                                     headers={"api-key": self.brevo_key, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                return True
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code < 500:            # bad key, unverified sender, … — retrying will not help
                    self.log.warning("brevo email rejected (HTTP %s): %s", exc.code, detail)
                    return False
                self.log.warning("brevo email failed (HTTP %s, attempt %d): %s", exc.code, attempt + 1, detail)
            except Exception as exc:  # network trouble must never stop trading jobs
                self.log.warning("brevo email failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
        return False

    def _smtp(self, subject: str, body_html: str, attachments: List[Path]) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((self.email_from_name, self.email_from))
        msg["To"] = ", ".join(self.email_to)
        msg.set_content("This message is best viewed as HTML.\n")
        msg.add_alternative(body_html, subtype="html")
        for a in attachments:
            msg.add_attachment(a.read_bytes(), maintype="application", subtype="pdf", filename=a.name)
        ctx = ssl.create_default_context()
        try:
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=30) as smtp:
                    smtp.login(self.smtp_user, self.smtp_password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                    smtp.starttls(context=ctx)
                    smtp.login(self.smtp_user, self.smtp_password)
                    smtp.send_message(msg)
            return True
        except Exception as exc:
            self.log.warning("smtp email via %s:%s failed: %s", self.smtp_host, self.smtp_port, exc)
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
