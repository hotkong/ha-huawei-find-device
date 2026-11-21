"""Device fingerprint for the Huawei account gateway.

The account login registers a device with ``dev`` first, and the gateway only
issues a full ticket (``CASTGC``) when that registration succeeds; without it the
login degrades to ``CASTGCLOW`` and the portal session never settles.

The login page builds the value in ``cas_index-legacy.js``:

1. ``k=v&k=v...`` over the browser attributes, keys sorted
2. append ``&cs=`` + SHA-1 of that text
3. XOR-chain every char code with a state that starts at 211 and becomes the
   previous output byte
4. base64, padding kept

What the gateway checks, measured against the live service:

* A fingerprint whose base64 padding is stripped is refused: ``dev`` answers
  ``isSuccess: 1`` with an empty ``sid``. The page always sends the padding.
* A fingerprint that carries a ``localStorage`` value the gateway has never
  issued is refused the same way, and so is a missing checksum.
* A fingerprint that carries *no* ``localStorage`` key at all is a new device:
  the gateway allocates a fresh 84-character id and returns it as ``sid``.
  The hashes for canvas, installed fonts and WebGL are not validated, so an
  installation can generate every value itself.
* Afterwards the device id travels back in ``localStorage`` and the gateway
  echoes it, which is what keeps the identity stable across logins.

So an installation registers its own device on the first login and stores the
returned id; nothing is shared between installations and nothing is borrowed
from a browser.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import time

# First XOR state the login page uses.
_XOR_SEED = 211

# Device id the gateway issues on registration (``sid``), and the length of the
# per-device hashes the page derives from canvas, fonts and WebGL. Those three
# are not validated -- any 40 hex characters are accepted -- so each install
# generates its own.
DEVICE_ID_LENGTH = 84
_HASH_LENGTHS = {"canvas": 40, "fonts": 40, "webgl": 40}

# Everything else is a plausible desktop Chrome window. Only used to build a
# well-formed parameter list; the values are not validated.
_TEMPLATE = {
    "bsh": "638",
    "bsw": "1019",
    "devs": "",
    "ep": "7b6062d2768e1cbe43f28edb797d0ca1a9b6d542",
    "epl": "5",
    "epls": (
        "P226c8e06e8f3f3c3beed6ed1145927cef5965aaa%2C"
        "C7f67204e925623e4820fe0d288daaad7e4c37ae8%2C"
        "Mad70024da17c63a482b1b58e65af7b1645519302%2C"
        "W22100365de750cfb19746f29f705e03c3e5cd4ee"
    ),
    "ips": "",
    "nacn": "Mozilla",
    "nan": "Netscape",
    "nce": "true",
    "nlg": "zh-CN",
    "npf": "Win32",
    "sah": "720",
    "saw": "1366",
    "sh": "768",
    "sw": "1366",
}


def _canonical(params: dict[str, str]) -> str:
    """The parameter text the checksum is taken over: keys sorted, joined by ``&``."""
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


def _random_hex(length: int) -> str:
    return os.urandom(length).hex()[:length]


def _timezone_minutes() -> str:
    """``Date.prototype.getTimezoneOffset()``, which is what the page sends."""
    return str(int(time.timezone / 60))


def valid_device_id(value: str | None) -> bool:
    """True for a value shaped like an issued device id."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if len(candidate) != DEVICE_ID_LENGTH:
        return False
    try:
        int(candidate, 16)
    except ValueError:
        return False
    return True


def seal(params: dict[str, str]) -> str:
    """Turn a parameter mapping into the opaque fingerprint string."""
    body = _canonical(params)
    text = f"{body}&cs={hashlib.sha1(body.encode()).hexdigest()}"
    state = _XOR_SEED
    payload = bytearray()
    for char in text:
        byte = (ord(char) ^ ((state - 1) & 0xFF)) & 0xFF
        payload.append(byte)
        state = byte
    # Padding is part of the value: the gateway refuses a stripped one.
    return base64.b64encode(bytes(payload)).decode()


def unseal(fingerprint: str) -> dict[str, str]:
    """Recover the parameter mapping, raising ``ValueError`` if it is not intact."""
    value = (fingerprint or "").strip()
    if not value:
        raise ValueError("empty fingerprint")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
    except (binascii.Error, ValueError) as err:
        raise ValueError("fingerprint is not base64") from err
    state = _XOR_SEED
    chars = []
    for byte in raw:
        chars.append(chr(byte ^ ((state - 1) & 0xFF)))
        state = byte
    body, _, digest = "".join(chars).rpartition("&cs=")
    if not body or hashlib.sha1(body.encode()).hexdigest() != digest:
        raise ValueError("fingerprint checksum does not match")
    params: dict[str, str] = {}
    for pair in body.split("&"):
        key, _, value = pair.partition("=")
        params[key] = value
    return params


def device_id_of(fingerprint: str) -> str | None:
    """The device id a fingerprint carries, or None when it carries none."""
    params = unseal(fingerprint)
    value = params.get("localStorage")
    return value if valid_device_id(value) else None


def mint(device_id: str | None = None) -> str:
    """Build a fingerprint, registering a new device when no id is given.

    ``device_id`` is the id a previous login returned. Without one the value
    leaves ``localStorage`` out entirely, which is how the gateway tells a new
    device and allocates an id for it.
    """
    params = dict(_TEMPLATE)
    for field, length in _HASH_LENGTHS.items():
        params[field] = _random_hex(length)
    if device_id is not None:
        if not valid_device_id(device_id):
            raise ValueError("device id must be 84 hexadecimal characters")
        params["localStorage"] = device_id.strip()
    params["ett"] = str(int(time.time() * 1000))
    params["etz"] = _timezone_minutes()
    return seal(params)


def fresh(fingerprint: str | None) -> str:
    """Re-seal a stored fingerprint with the current time.

    The login page stamps ``ett`` on every visit, so a stored value is refreshed
    rather than reused verbatim. An unreadable stored value is an error: the
    caller decides what to do instead of the login quietly presenting a
    different device.
    """
    params = unseal(fingerprint or "")
    device_id = params.get("localStorage")
    if not valid_device_id(device_id):
        raise ValueError("stored fingerprint carries no valid device id")
    for field, length in _HASH_LENGTHS.items():
        if not params.get(field):
            params[field] = _random_hex(length)
    params["ett"] = str(int(time.time() * 1000))
    params["etz"] = _timezone_minutes()
    return seal(params)
