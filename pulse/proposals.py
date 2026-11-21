"""Software that isn't in the catalogue yet: finding it, and pre-checking what can be checked safely on the server.

Flow: an employee asks for an app that isn't approved -> the assistant (Gemini with web search) proposes the vendor's official
download page and says whether the software is free to use -> the employee confirms they want IT to look at it -> an admin reviews it
in the admin app, where the file is really downloaded and verified (signature, version, checksum) -> approving adds it to the
catalogue and installs it, and from then on it is "available software" for everyone.

Nothing here is trusted just because a model said it:
  * a proposed link must be a public https address (no IPs, no local/internal names, no odd ports); every redirect is re-checked;
  * the server only reads the first 2 KB of it, to see whether it looks like an installer or a web page, and never passes content back;
  * the host is compared with the vendor name, and a mismatch is flagged for the admin;
  * the licence statement is the model's reading of the vendor's pages: shown as "AI-suggested", and an admin must confirm it
    unless the software is clearly free;
  * the real verification (download, signature, checksum) happens in the admin app when the admin reviews it, never on the server.
"""
import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

UA = "Mozilla/5.0 (compatible; Proglintor-Checker/1.0)"
PROBE_BYTES = 2048

LICENSES = {
    "free": "Free to use",
    "open_source": "Free and open source",
    "freemium": "Free tier, paid plans for more",
    "paid": "Paid licence required",
    "unknown": "Licence not confirmed",
}
CLEARLY_FREE = ("free", "open_source")


class ProposalError(Exception):
    pass


def _resolve(host):
    """All addresses a host name points at (separate so tests can stand in for DNS)."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def safe_public_url(url):
    """Returns the URL if it is a public https address the server may look at, else raises ProposalError."""
    return _checked(url)[0]


def _checked(url):
    """-> (url, addresses). The addresses are the ones that were validated; the caller connects to exactly these (no second lookup)."""
    u = urlparse(str(url or "").strip())
    if u.scheme != "https" or not u.hostname or u.username or u.password or len(str(url)) > 1000:
        raise ProposalError("not a plain https address")
    try:
        port = u.port
    except ValueError as err:
        raise ProposalError("bad port") from err
    if port not in (None, 443):
        raise ProposalError("unusual port")
    host = u.hostname.lower().rstrip(".")
    if "." not in host or host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa", ".corp")):
        raise ProposalError("not a public host name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ProposalError("an IP address, not a host name")
    try:
        addresses = _resolve(host)
    except OSError as err:
        raise ProposalError("host name doesn't resolve") from err
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise ProposalError("points at a private network")
    return u.geturl(), [a.split("%")[0] for a in addresses]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an address we already validated, while TLS/SNI/Host still use the real host name. Closes the DNS-rebinding gap
    between "is this host public?" and "connect to it"."""
    pinned_ip = None

    def connect(self):
        sock = socket.create_connection((self.pinned_ip or self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ip):
        super().__init__()
        self.ip = ip

    def https_open(self, req):
        ip = self.ip

        class Conn(_PinnedHTTPSConnection):
            pinned_ip = ip
        return self.do_open(Conn, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # we follow redirects ourselves so every hop is checked
        return None


def _classify(head, content_type, url):
    ctype = (content_type or "").lower()
    if "text/html" in ctype or re.match(rb"\s*<(!doctype|html|head|body|\?xml)", head[:64], re.I):
        return "page"
    if head[:2] == b"PK" or head[:4] in (b"xar!", b"\x7fELF") or head[:2] == b"MZ" or head[:8] == bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1]):
        return "installer"
    if re.search(r"\.(dmg|pkg|zip|msi|exe|appimage)(\?|$)", url.lower()) or "diskimage" in ctype or "zip" in ctype or "octet-stream" in ctype:
        return "installer"
    return "other"


def probe(url, hops=3):
    """Looks at the start of a link without trusting it. Never raises. -> {ok, status, finalUrl, contentType, size, looksLike}."""
    current = url
    for _ in range(hops + 1):
        try:
            _, addresses = _checked(current)
        except ProposalError as err:
            return {"ok": False, "status": 0, "reason": str(err)}
        opener = urllib.request.build_opener(_NoRedirect, _PinnedHTTPSHandler(addresses[0]))
        req = urllib.request.Request(current, headers={"Range": f"bytes=0-{PROBE_BYTES - 1}", "User-Agent": UA, "Accept": "*/*"})
        try:
            with opener.open(req, timeout=8) as res:
                head = res.read(PROBE_BYTES)
                ctype = res.headers.get("Content-Type", "")
                total = None
                m = re.match(r"bytes \d+-\d+/(\d+)", res.headers.get("Content-Range", ""))
                if m:
                    total = int(m.group(1))
                elif res.headers.get("Content-Length", "").isdigit() and res.status == 200:
                    total = int(res.headers["Content-Length"])
                return {"ok": True, "status": res.status, "finalUrl": current, "contentType": ctype.split(";")[0].strip(),
                        "size": total, "looksLike": _classify(head, ctype, current)}
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308) and err.headers.get("Location"):
                current = urljoin(current, err.headers["Location"])
                continue
            return {"ok": False, "status": err.code, "reason": f"the server answered {err.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"ok": False, "status": 0, "reason": "the server didn't answer"}
    return {"ok": False, "status": 0, "reason": "too many redirects"}


def registrable_host(host):
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


GENERIC_HOSTS = ("github.com", "githubusercontent.com", "amazonaws.com", "cloudfront.net", "googleapis.com", "azureedge.net", "fastly.net",
                 "sourceforge.net", "gitlab.com", "bitbucket.org", "dropbox.com", "akamaized.net", "windows.net")


def host_matches(name, vendor, url):
    """'yes' if the download host looks like the vendor's own, 'unknown' for shared hosting/CDNs (releases live there legitimately), else 'no'."""
    host = registrable_host(urlparse(url).hostname or "")
    if host in GENERIC_HOSTS or any(host.endswith("." + g) for g in GENERIC_HOSTS):
        return "unknown"
    tokens = {t for t in re.findall(r"[a-z0-9]{3,}", f"{name} {vendor}".lower())} - {"inc", "ltd", "llc", "the", "corp", "labs", "technologies", "software", "limited", "gmbh"}
    squashed = re.sub(r"[^a-z0-9]", "", f"{name}{vendor}".lower())
    label = host.split(".")[0]
    return "yes" if any(t in host for t in tokens) or (len(label) >= 4 and label in squashed) else "no"


def parse_model_json(text):
    """The model's JSON, tolerating code fences and chatter around it."""
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean(text, n):
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f*_`#<>]", "", str(text or ""))).strip()[:n]


def build_proposal(data):
    """Validates the model's answer and pre-checks the link. Returns a proposal dict, or None if there is nothing trustworthy to show."""
    if not isinstance(data, dict) or data.get("found") is not True:
        return None
    name, vendor = _clean(data.get("name"), 80), _clean(data.get("vendor"), 80)
    if len(name) < 2:
        return None
    candidates = []
    for key in ("download_url", "homepage"):
        try:
            candidates.append(safe_public_url(data.get(key)))
        except ProposalError:
            continue
    if not candidates:
        return None
    result = url = None
    for url in candidates:  # the download link first; the vendor's home page is the fallback
        result = probe(url)
        if result["ok"]:
            break
    kind = data.get("license_kind") if data.get("license_kind") in LICENSES else "unknown"
    host = urlparse(result.get("finalUrl") or url).hostname or ""
    match = host_matches(name, vendor, result.get("finalUrl") or url)
    checks = [{"ok": True, "text": "The link is a public https address."}]
    if result["ok"]:
        label = {"installer": "an installer file", "page": "a web page that should link to the installer"}.get(result["looksLike"], "something I couldn't classify")
        checks.append({"ok": result["looksLike"] != "other", "text": f"It is reachable and looks like {label}."})
    else:
        checks.append({"ok": False, "text": f"I couldn't open the link ({result.get('reason', 'unknown problem')}). The admin may need to find the right one."})
    checks.append({"ok": match != "no", "text": {
        "yes": f"The download host ({host}) looks like the vendor's own.",
        "unknown": f"The download is on a shared host ({host}), which is normal for releases but can't be matched to the vendor.",
        "no": f"The download host ({host}) doesn't obviously belong to {vendor or name}. Check the link carefully.",
    }[match]})
    return {
        "source": "ai", "name": name, "vendor": vendor, "homepage": candidates[-1] if len(candidates) > 1 else "",
        "downloadUrl": result.get("finalUrl") if result["ok"] and result.get("looksLike") == "installer" else candidates[0],
        "host": host, "licenseKind": kind, "licenseLabel": LICENSES[kind], "licenseClear": kind in CLEARLY_FREE,
        "licenseNote": _clean(data.get("license_note"), 240), "confidence": data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "low",
        "hostMatchesVendor": match, "probe": {k: result.get(k) for k in ("ok", "status", "contentType", "size", "looksLike")}, "checks": checks,
    }
