"""Licensed wire service: your provider's feed (RSS or Atom), read with your account key.
Wire stories are published as the wire wrote them, with its credit, so no AI is used."""
from . import rss


def fetch(source, config, secret):
    headers = {}
    name = (config.get("key_header") or "").strip()
    if secret and name:
        headers[name] = secret
    return rss.fetch(source, config, secret, headers=headers, keep_html=True)
