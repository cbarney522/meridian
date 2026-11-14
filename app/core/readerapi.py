"""Reads reader facts from the R700's own REST API (Impinj IoT Device Interface) on localhost.

The API needs the reader's admin login (the one used for the Impinj web UI). Field names are
taken defensively: several plausible names are tried for each item, and the raw responses are
kept so the Developer › Debug log page can show exactly what this firmware returns.
Standard library only.
"""
import base64
import json
import ssl
import time
import urllib.error
import urllib.request

ENDPOINTS = {
    "system": "/api/v1/system",
    "image": "/api/v1/system/image",
    "region": "/api/v1/system/region",
    "rfid_interface": "/api/v1/system/rfid/interface",
    # probes: not documented; kept raw so we can see whether this firmware exposes NTP settings
    "ntp": "/api/v1/system/ntp",
    "time": "/api/v1/system/time",
}
OPTIONAL = {"ntp", "time"}       # a 404 here is expected and isn't reported as a problem

# item -> (endpoint, candidate field names in order of preference)
FIELDS = {
    "description": ("system", ["productDescription", "description", "productModel"]),
    "model": ("system", ["productModel", "model"]),
    "sku": ("system", ["productSku", "sku"]),
    "hla": ("system", ["productHla", "productHLA", "hla"]),
    "pcba": ("system", ["productPcba", "productPCBA", "pcba"]),
    "serial": ("system", ["serialNumber", "serial"]),
    "firmware": ("image", ["primaryFirmware", "firmwareVersion", "version", "primaryImage"]),
    "region": ("region", ["operatingRegion", "region"]),
    "rfid_interface": ("rfid_interface", ["rfidInterface", "interface"]),
}
INTERFACE_LABELS = {"rest": "Impinj IoT Device Interface", "llrp": "LLRP"}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(f"HTTP {code}: {message}" if code else message)
        self.code, self.message = code, message


class ReaderApi:
    def __init__(self, base="https://localhost", user=None, password=None, timeout=4):
        self.base = base.rstrip("/")
        self.user, self.password, self.timeout = user, password, timeout
        # The reader's own self-signed certificate on localhost: nothing to verify against.
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Accept": "application/json"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.user:
            tok = base64.b64encode(f"{self.user}:{self.password or ''}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        return req

    def get(self, path):
        with urllib.request.urlopen(self._request("GET", path), context=self.ctx, timeout=self.timeout) as r:
            return json.loads(r.read() or b"null")

    def send(self, method, path, body=None):
        """POST/PUT/DELETE. Returns the parsed reply (or None). Raises ApiError with the reader's message."""
        try:
            with urllib.request.urlopen(self._request(method, path, body), context=self.ctx, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                j = json.loads(e.read() or b"null")
                detail = (j.get("message") or j.get("detail") or j.get("error") or "") if isinstance(j, dict) else str(j)
                if isinstance(j, list):
                    detail = "; ".join(str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in j)
            except (ValueError, OSError):
                pass
            raise ApiError(e.code, detail or e.reason) from None
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, f"can't reach the reader API: {getattr(e, 'reason', e)}") from None

    def open_stream(self, path, timeout=30):
        """A long-lived streaming response (caller reads lines and closes it)."""
        return urllib.request.urlopen(self._request("GET", path), context=self.ctx, timeout=timeout)

    def fetch(self):
        """Return {"ok", "error", "items": {...}, "raw": {endpoint: json or error}, "fetched": ts}."""
        raw, errors = {}, []
        for name, path in ENDPOINTS.items():
            try:
                raw[name] = self.get(path)
            except urllib.error.HTTPError as e:
                raw[name] = {"_error": f"HTTP {e.code}"}
                if name in OPTIONAL and e.code == 404:
                    continue
                errors.append("login rejected (check the reader user name and password)" if e.code in (401, 403)
                              else f"{path}: HTTP {e.code}")
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", e)
                raw[name] = {"_error": str(reason)}
                errors.append(f"can't reach the reader API: {reason}")
        items = {}
        for item, (ep, names) in FIELDS.items():
            src = raw.get(ep)
            val = None
            if isinstance(src, dict):
                val = next((src[n] for n in names if isinstance(src.get(n), (str, int, float)) and src.get(n) != ""), None)
            elif isinstance(src, str) and item in ("region", "rfid_interface", "firmware"):
                val = src
            items[item] = val
        if items.get("rfid_interface"):
            items["rfid_interface"] = INTERFACE_LABELS.get(str(items["rfid_interface"]).lower(), items["rfid_interface"])
        ok = any(v is not None for v in items.values())
        # one message per distinct problem
        uniq = list(dict.fromkeys(errors))
        return {"ok": ok, "error": "; ".join(uniq) if uniq else None, "items": items, "raw": raw,
                "fetched": int(time.time())}
