"""Keyboard-wedge badge readers: turning key events into badge IDs.

Two parts, both standard library only:

1. KeyAssembler — feed it Linux input events (from /dev/input/eventN after EVIOCGRAB) and it
   returns the typed string when the reader sends Enter, or after a quiet gap if it doesn't.
2. normalize() — turn that raw string into the badge ID the access list uses, with per-site
   settings (strip "I"/"O" prefixes, split facility:card, drop leading zeros, validate).

The reader-side driver that opens the device and calls these lives in the engine (not written yet).
"""
import re
import time

EV_KEY = 1
KEY_DOWN = 1
KEY_ENTER, KEY_KPENTER, KEY_TAB = 28, 96, 15
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54

# US layout, (unshifted, shifted). Wedge readers almost always emit digits, letters and a few separators.
KEYMAP = {
    2: ("1", "!"), 3: ("2", "@"), 4: ("3", "#"), 5: ("4", "$"), 6: ("5", "%"), 7: ("6", "^"),
    8: ("7", "&"), 9: ("8", "*"), 10: ("9", "("), 11: ("0", ")"), 12: ("-", "_"), 13: ("=", "+"),
    16: ("q", "Q"), 17: ("w", "W"), 18: ("e", "E"), 19: ("r", "R"), 20: ("t", "T"), 21: ("y", "Y"),
    22: ("u", "U"), 23: ("i", "I"), 24: ("o", "O"), 25: ("p", "P"), 26: ("[", "{"), 27: ("]", "}"),
    30: ("a", "A"), 31: ("s", "S"), 32: ("d", "D"), 33: ("f", "F"), 34: ("g", "G"), 35: ("h", "H"),
    36: ("j", "J"), 37: ("k", "K"), 38: ("l", "L"), 39: (";", ":"), 40: ("'", '"'), 43: ("\\", "|"),
    44: ("z", "Z"), 45: ("x", "X"), 46: ("c", "C"), 47: ("v", "V"), 48: ("b", "B"), 49: ("n", "N"),
    50: ("m", "M"), 51: (",", "<"), 52: (".", ">"), 53: ("/", "?"), 57: (" ", " "),
    # keypad digits (some readers use these)
    71: ("7", "7"), 72: ("8", "8"), 73: ("9", "9"), 75: ("4", "4"), 76: ("5", "5"), 77: ("6", "6"),
    79: ("1", "1"), 80: ("2", "2"), 81: ("3", "3"), 82: ("0", "0"), 83: (".", "."), 74: ("-", "-"),
    98: ("/", "/"), 55: ("*", "*"), 78: ("+", "+"), 117: ("=", "="), 121: (",", ","),   # keypad / * + = ,
    41: ("`", "~"),
}


class KeyAssembler:
    """Collects key presses into one scan. end='enter' finishes on Enter/Tab; a gap longer than
    timeout_ms also finishes a scan (needed for readers configured without a terminator)."""

    def __init__(self, timeout_ms=100, max_len=64, clock=time.monotonic):
        self.timeout = timeout_ms / 1000.0
        self.max_len = max_len
        self.clock = clock
        self.buf = []
        self.shift = False
        self.last = None

    def feed(self, etype, code, value):
        """Feed one input event. Returns a finished scan string or None."""
        if etype != EV_KEY:
            return None
        if code in (KEY_LEFTSHIFT, KEY_RIGHTSHIFT):
            self.shift = value != 0
            return None
        if value != KEY_DOWN:
            return None
        done = self.poll()                      # a long gap ends the previous scan first
        if code in (KEY_ENTER, KEY_KPENTER, KEY_TAB):
            out = "".join(self.buf) or None
            self.buf, self.last = [], None
            return out or done
        ch = KEYMAP.get(code)
        if ch and len(self.buf) < self.max_len:
            self.buf.append(ch[1] if self.shift else ch[0])
        self.last = self.clock()
        return done

    def poll(self):
        """Call periodically; returns a scan if the quiet-gap timeout has passed."""
        if self.buf and self.last is not None and self.clock() - self.last > self.timeout:
            out = "".join(self.buf)
            self.buf, self.last = [], None
            return out
        return None


DEFAULT_SETTINGS = {
    "strip_prefixes": ["I", "O"],   # removed if the scan starts with one (readers programmed to tag in/out)
    "strip_suffixes": [],
    "separator": ":",               # facility/card separator in the raw scan; "" if none
    "output": "card",               # "card" | "facility_card" | "raw"
    "strip_leading_zeros": True,
    "pattern": r"^\d{1,10}$",        # final ID must match
    "end_of_scan": "enter",         # "enter" | "timeout"
    "timeout_ms": 100,
}

OUTPUTS = ("card", "facility_card", "raw")


def validate_settings(s):
    errors = []
    if not isinstance(s, dict):
        return ["settings must be an object"]
    for key in ("strip_prefixes", "strip_suffixes"):
        v = s.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and 0 < len(x) <= 8 for x in v) or len(v) > 8:
            errors.append(f"{key}: up to 8 entries of 1–8 characters")
    sep = s.get("separator", "")
    if not isinstance(sep, str) or len(sep) > 3:
        errors.append("separator: up to 3 characters")
    if s.get("output") not in OUTPUTS:
        errors.append("output: must be card, facility_card or raw")
    if not isinstance(s.get("strip_leading_zeros"), bool):
        errors.append("strip_leading_zeros: true or false")
    pat = s.get("pattern", "")
    if not isinstance(pat, str) or len(pat) > 200:
        errors.append("pattern: up to 200 characters")
    else:
        try:
            re.compile(pat)
        except re.error as e:
            errors.append(f"pattern: not a valid regular expression ({e})")
    if s.get("end_of_scan") not in ("enter", "timeout"):
        errors.append("end_of_scan: enter or timeout")
    t = s.get("timeout_ms")
    if not isinstance(t, int) or isinstance(t, bool) or not 20 <= t <= 2000:
        errors.append("timeout_ms: 20–2000")
    return errors


def normalize(raw, settings=None):
    """Return {"ok", "badge_id", "facility", "card", "steps", "error"} for a raw scan string.
    `steps` records each transformation so the UI can show the technician what happened."""
    s = dict(DEFAULT_SETTINGS, **(settings or {}))
    steps = []
    v = (raw or "").strip()
    if not v:
        return {"ok": False, "badge_id": None, "facility": None, "card": None, "steps": steps, "error": "empty scan"}
    for p in s["strip_prefixes"]:
        if v.startswith(p):
            v = v[len(p):]
            steps.append(f"removed prefix '{p}'")
            break
    for suf in s["strip_suffixes"]:
        if v.endswith(suf):
            v = v[: -len(suf)]
            steps.append(f"removed suffix '{suf}'")
            break
    facility, card = None, v
    if s["separator"] and s["separator"] in v:
        facility, card = v.split(s["separator"], 1)
        steps.append(f"split at '{s['separator']}': facility {facility}, card {card}")
    if s["strip_leading_zeros"]:
        stripped = card.lstrip("0") or "0"
        if stripped != card:
            steps.append("removed leading zeros")
        card = stripped
        if facility is not None:
            facility = facility.lstrip("0") or "0"
    if s["output"] == "raw":
        badge = (raw or "").strip()
    elif s["output"] == "facility_card" and facility is not None:
        badge = f"{facility}{s['separator']}{card}"
    else:
        badge = card
    if s["pattern"] and not re.fullmatch(s["pattern"], badge):
        return {"ok": False, "badge_id": badge, "facility": facility, "card": card, "steps": steps,
                "error": "doesn't match the validation pattern"}
    return {"ok": True, "badge_id": badge, "facility": facility, "card": card, "steps": steps, "error": None}
