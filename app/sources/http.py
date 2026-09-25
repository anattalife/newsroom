"""One place for outgoing web requests: a polite user agent, timeouts, size limits, and a guard
that refuses internal network addresses (so a source can't be pointed at the server itself)."""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

UA = "LocalNewsroom/1.0 (local news site; contact via website)"
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5


class FetchError(Exception):
    pass


def _check_public(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("The address must start with http:// or https://")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError("Couldn't find that website. Check the address.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise FetchError("That address points to a private network, which isn't allowed.")


def get(url, headers=None, timeout=25):
    if not url or not url.lower().startswith(("http://", "https://")):
        raise FetchError("The address must start with http:// or https://")
    for _ in range(MAX_REDIRECTS + 1):
        _check_public(url)
        try:
            r = requests.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=timeout, stream=True,
                             allow_redirects=False)
        except requests.exceptions.SSLError as e:
            raise FetchError("The site's security certificate couldn't be verified.") from e
        except requests.exceptions.ConnectionError as e:
            raise FetchError("Couldn't connect. Check the address, or the site may be down.") from e
        except requests.exceptions.Timeout as e:
            raise FetchError("The site took too long to respond.") from e
        if r.is_redirect and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        break
    else:
        raise FetchError("That address redirects too many times.")
    if r.status_code == 404:
        raise FetchError("Nothing found at that address (404). Check the link.")
    if r.status_code in (401, 403):
        raise FetchError(f"The site refused access ({r.status_code}). It may block automated readers.")
    if r.status_code >= 400:
        raise FetchError(f"The site returned an error ({r.status_code}).")
    body = r.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(body) > MAX_BYTES:
        raise FetchError("That address returned too much data to be a feed.")
    r._content = body
    return r
