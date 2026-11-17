"""Email delivery: Brevo API request shape, rejected keys, SMTP SSL vs STARTTLS — all with fakes, nothing sent."""
import base64
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402

LOG = logging.getLogger("test-notify")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


def brevo_notifier(**kw):
    args = dict(token="", chat_id="", brevo_key="xkeysib-test", email_from="bot@example.com",
                email_to="a@example.com, b@example.com", smtp_user="", smtp_password="")
    args.update(kw)
    return notify.Notifier(LOG, None, **args)


class FakeResp:
    def __init__(self, body=b'{"messageId":"<1@smtp-relay.brevo.com>"}'):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Brevo(unittest.TestCase):
    def test_sends_html_and_pdf_through_the_api(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "report.pdf"
            pdf.write_bytes(b"%PDF-1.4 test")
            n = brevo_notifier()
            self.assertEqual(n.channels(), "email (brevo)")
            with mock.patch.object(notify.urllib.request, "urlopen", lambda req, timeout: calls.append(req) or FakeResp()):
                n.send("Signals 12:02", "<b>tg</b>", "<p>email</p>", attachments=[pdf])
        self.assertEqual(len(calls), 1)
        req = calls[0]
        self.assertEqual(req.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(req.get_header("Api-key"), "xkeysib-test")
        body = json.loads(req.data)
        self.assertEqual(body["sender"], {"name": "AI Trading Terminal", "email": "bot@example.com"})
        self.assertEqual(body["to"], [{"email": "a@example.com"}, {"email": "b@example.com"}])
        self.assertEqual((body["subject"], body["htmlContent"]), ("Signals 12:02", "<p>email</p>"))
        self.assertEqual(body["attachment"][0]["name"], "report.pdf")
        self.assertEqual(base64.b64decode(body["attachment"][0]["content"]), b"%PDF-1.4 test")

    def test_a_rejected_key_is_logged_once_not_retried(self):
        calls = []

        def reject(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"code":"unauthorized","message":"Key not found"}'))
        n = brevo_notifier()
        with mock.patch.object(notify.urllib.request, "urlopen", reject), self.assertLogs(LOG, "WARNING") as logs:
            self.assertFalse(n._email("s", "<p>x</p>", []))
        self.assertEqual(len(calls), 1)
        self.assertIn("Key not found", logs.output[0])

    def test_not_configured_without_a_sender(self):
        self.assertFalse(brevo_notifier(email_from="", email_to="").has_email)
        self.assertEqual(brevo_notifier(email_to="").email_to, ["bot@example.com"])   # defaults to the sender


class FakeSMTP:
    instances = []

    def __init__(self, host, port, **kw):
        self.host, self.port, self.kw, self.events = host, port, kw, []
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.events.append("starttls")

    def login(self, user, password):
        self.events.append(("login", user, password))

    def send_message(self, msg):
        self.events.append(("send", msg["From"], msg["To"]))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Smtp(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []

    def send(self, port):
        n = notify.Notifier(LOG, None, token="", chat_id="", brevo_key="", smtp_user="123abc@smtp-brevo.com",
                            smtp_password="xsmtpsib-key", email_from="bot@example.com", email_to="me@example.com",
                            smtp_host="smtp-relay.brevo.com", smtp_port=port)
        self.assertEqual(n.channels(), "email (smtp)")
        with mock.patch.object(notify.smtplib, "SMTP", FakeSMTP), mock.patch.object(notify.smtplib, "SMTP_SSL", FakeSMTP):
            self.assertTrue(n._email("s", "<p>x</p>", []))
        return FakeSMTP.instances[-1]

    def test_port_587_uses_starttls_and_the_verified_sender(self):
        s = self.send(587)
        self.assertEqual((s.host, s.port), ("smtp-relay.brevo.com", 587))
        self.assertEqual(s.events[0], "starttls")
        self.assertEqual(s.events[1], ("login", "123abc@smtp-brevo.com", "xsmtpsib-key"))
        self.assertEqual(s.events[2], ("send", "AI Trading Terminal <bot@example.com>", "me@example.com"))

    def test_port_465_uses_ssl(self):
        s = self.send(465)
        self.assertNotIn("starttls", s.events)
        self.assertIn("context", s.kw)


if __name__ == "__main__":
    unittest.main()
