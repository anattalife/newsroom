"""Press releases from an email inbox (IMAP). Unread messages are read and marked read."""
import email
import imaplib
from email.header import decode_header, make_header
from email.utils import parseaddr

from bs4 import BeautifulSoup


def _body(msg):
    plain, htmlpart = "", ""
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_type() == "text/plain":
            plain += text
        elif part.get_content_type() == "text/html":
            htmlpart += BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return (plain or htmlpart).strip()


def fetch(source, config, secret, mark_read=True):
    host, user = config.get("host"), config.get("username")
    if not host or not user or not secret:
        raise ValueError("Fill in the email server, username and password.")
    official = [d.strip().lower().lstrip("@") for d in config.get("official_domains", []) if d.strip()]
    try:
        m = imaplib.IMAP4_SSL(host, timeout=30)
        m.login(user, secret)
    except imaplib.IMAP4.error as e:
        raise ValueError("The email server rejected the login. For Gmail, use an App Password.") from e
    except OSError as e:
        raise ValueError(f"Couldn't reach the email server {host}.") from e
    try:
        m.select(config.get("folder") or "INBOX", readonly=not mark_read)
        _, data = m.search(None, "UNSEEN")
        items = []
        for num in data[0].split()[:25]:
            _, msgdata = m.fetch(num, "(RFC822)" if mark_read else "(BODY.PEEK[])")
            msg = email.message_from_bytes(msgdata[0][1])
            sender = str(make_header(decode_header(msg.get("From", ""))))
            addr = parseaddr(sender)[1].lower()
            domain = addr.split("@")[-1] if "@" in addr else ""
            is_official = any(domain == d or domain.endswith("." + d) for d in official)
            items.append({
                "key": msg.get("Message-ID") or f"{sender}|{msg.get('Date')}|{msg.get('Subject')}",
                "title": str(make_header(decode_header(msg.get("Subject", "(no subject)")))),
                "url": "",
                "text": _body(msg)[:8000],
                "published": msg.get("Date", ""),
                "trust": "official" if is_official else "tip",
                "extra": {"from": sender},
            })
        return items
    finally:
        try:
            m.logout()
        except Exception:
            pass
