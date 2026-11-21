"""Huawei account login that produces the cloud.huawei.com cookie.

The find-device integration only ever needed a Cookie header, and the portal
offers no documented way to obtain one without a browser. This module performs
the account-side login the portal itself performs, so the integration can be
configured with an account and password instead of a pasted cookie.

Protocol, measured against the live pages:

1. ``GET /CAS/portal/login.html`` primes the account session.
2. ``common/getBaseSwitchInfo`` yields the asset version the gateway demands.
3. ``login/getPageInfo`` yields the page token pair.
4. ``chkRisk`` with ``operType`` 0 identifies the account.
5. ``chkRisk`` with ``operType`` 12 returns ``siteInfoList``; when a phone is
   bound to several accounts, each entry carries a ``loginID``.
6. ``chkRisk`` with ``operType`` 13 selects one of those accounts.
7. ``remoteLogin`` submits the password and answers with a ``callbackURL`` that
   carries a CAS ticket.
8. Following that URL makes the portal set ``siteID``, ``loginID`` and
   ``token``; a ``GET /home`` afterwards adds ``CSRFToken`` and the rest.

Once the browser has been registered as trusted, step 7 needs no second factor.
On a fresh device Huawei asks for one, which surfaces here as
:class:`HuaweiLoginCodeRequired`.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import aiohttp

from . import fingerprint

_LOGGER = logging.getLogger(__name__)

CAS_BASE = "https://id1.cloud.huawei.com"
CAS_LOGIN_PAGE = f"{CAS_BASE}/CAS/portal/login.html"
CAS_AJAX = f"{CAS_BASE}/CAS/IDM_W/ajaxHandler"
CAS_CVERSION_BASE = "UP_CAS_6.26.2.100"

PORTAL_BASE = "https://cloud.huawei.com"
PORTAL_LOGIN = f"{PORTAL_BASE}:443/others/login.action"
PORTAL_SCOPE = "https://www.huawei.com/auth/account/pwdlowlogin"

# The portal login ends in an OAuth authorization: the ticket callback lands on
# the OAuth service, which hands back the parameters the client must echo to
# POST /oauth2/ajax/login. Without that call the portal session stays anonymous
# even though token/loginID/siteID were already issued.
OAUTH_BASE = "https://oauth-login.cloud.huawei.com"
OAUTH_LOGIN_BASE = "https://oauth-login1.cloud.huawei.com"
OAUTH_REDIRECT_URI = "https://cloud.huawei.com:443/homeTransit"
# Only the portal session is wanted here, and client 4805300 allows just this
# one scope: asking for the drive, photo and profile scopes as well is refused
# with "The applied scope exceeds the allowed scopes" (error 1101), which the
# OAuth service reports on the ticket exchange rather than on the request.
OAUTH_SCOPE = "openid"

CLIENT_ID = "4805300"
LOGIN_CHANNEL = "1000002"
REQUEST_CLIENT_TYPE = "1"
SITE_ID = "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

# The portal sits behind Huawei's WAF, which treats a request that does not look
# like a browser navigation differently. The ticket callback in particular has to
# be presented the way Chrome presents it.
NAVIGATION_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="157", "Not:A-Brand";v="24", "Google Chrome";v="157"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-User": "?1",
}

XHR_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "x-hw-framework-type": "web",
    "sec-ch-ua": '"Chromium";v="157", "Not:A-Brand";v="24", "Google Chrome";v="157"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Errors that mean "Huawei wants another factor", not "wrong password".
_CHALLENGE_CODES = frozenset(
    {"10012072", "70002080", "70008800", "70008804", "10000004"}
)

# Risk control. Huawei answers these when one account is signed in too often in
# a short window, and after enough of them it can lock the account for a while,
# so the flow refuses further attempts locally instead of hammering it.
_RISK_CODES = frozenset({"10000201", "10000202", "10001201"})
_ATTEMPT_WINDOW = 15 * 60
_ATTEMPT_LIMIT = 3
_attempts: dict[str, list[float]] = {}

_DIGITS = re.compile(r"\D")
# An authorization code, as opposed to a flag or a short status value.
_CODE_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,4096}$")


class HuaweiLoginError(Exception):
    """Login could not be completed."""


class HuaweiLoginAuthError(HuaweiLoginError):
    """Huawei rejected the account or password."""


class HuaweiLoginCodeRequired(HuaweiLoginError):
    """Huawei demands a verification code for this account/device."""


class HuaweiLoginThrottled(HuaweiLoginCodeRequired):
    """Too many attempts in the cooldown window, or Huawei's risk control fired."""


class HuaweiLoginAccountChoice(HuaweiLoginError):
    """The phone is bound to several accounts and one must be chosen."""

    def __init__(self, accounts: list[str]) -> None:
        super().__init__("account choice required: " + ", ".join(accounts))
        self.accounts = accounts


@dataclass(slots=True)
class HuaweiLoginResult:
    """What the portal handed back after a successful login."""

    cookie: str
    user_id: str | None = None
    csrf_token: str | None = None
    login_id: str | None = None
    device_id: str | None = None
    bridge_device_id: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)


def _base_params() -> dict[str, str]:
    return {
        "reqClientType": REQUEST_CLIENT_TYPE,
        "loginChannel": LOGIN_CHANNEL,
        "clientID": CLIENT_ID,
        "lang": "zh-cn",
        "languageCode": "zh-cn",
    }


def normalise_account(account: str) -> str:
    """Return the digits of a phone account, or the text for mail/alias."""
    text = (account or "").strip().replace(" ", "")
    digits = _DIGITS.sub("", text)
    if "@" in text:
        return text
    if len(digits) >= 5 and text.lstrip("+").isdigit():
        return digits
    return text


def _portal_cookies(jar: aiohttp.CookieJar) -> dict[str, str]:
    """Collect the cookies the portal API needs, keyed by name."""
    picked: dict[str, str] = {}
    for morsel in jar:
        domain = str(morsel["domain"] or "").lstrip(".")
        if domain != "cloud.huawei.com":
            continue
        picked[morsel.key] = morsel.value
    return picked


def _query_value(url: str, name: str) -> str | None:
    """Return one query parameter from a URL, or None."""
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


async def _complete_oauth_authorization(
    session: aiohttp.ClientSession,
    client: _Session,
    user_account: str,
    password: str,
    device_id: str | None,
    bridge_device_id: str | None = None,
    login_id: str | None = None,
) -> str | None:
    """Finish the OAuth handshake the portal login page runs in JavaScript.

    The ticket callback does not authenticate the portal session by itself: it
    hands the browser to the OAuth service, which returns the parameters to echo
    back to ``/oauth2/ajax/login`` together with a CAS ticket for its own
    callback. Replicating that exchange is what turns the half-settled session
    into a signed-in one.
    """
    form = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "access_type": "offline",
        "scope": OAUTH_SCOPE,
        "lang": "zh-cn",
    }
    # CAS binds the hop to the authorize request it came from, so the referer
    # has to carry the whole flow query; a bare path sends it to loginAuth.html.
    authorize_url = f"{OAUTH_BASE}/oauth2/v3/authorize?{urlencode(form)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "interfaceVersion": "v3",
        "fromLoginAuth": "false",
        "Referer": authorize_url,
        "Origin": OAUTH_BASE,
    }

    async with session.post(
        f"{OAUTH_BASE}/oauth2/ajax/getLoginWay",
        params={"reflushCode": f"{random.random():.17f}"},
        data=form,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        text = await response.text()
        status = response.status
    client.trace.append({"url": "oauth:getLoginWay", "status": status, "via": "post"})
    if status >= 400:
        raise HuaweiLoginError(f"OAuth service rejected getLoginWay ({status})")
    try:
        payload = json.loads(text)
    except ValueError as err:
        raise HuaweiLoginError("OAuth service returned non-JSON") from err

    signature = payload.get("signatureInfo")
    if not isinstance(signature, dict) or not signature:
        raise HuaweiLoginError("OAuth service did not return signature info")

    interact = payload.get("loginInteractInfo") or {}
    cas_info = interact.get("cas") or {}
    redirect_url = cas_info.get("casLoginRedirectUrl")
    if not isinstance(redirect_url, str) or not redirect_url:
        raise HuaweiLoginError("OAuth service did not return a CAS login URL")

    # CAS answers an XHR differently from a navigation: the portal's own app
    # fetches this URL and reads the ticket out of the reply, so asking as a
    # browser navigation only yields the loginAuth HTML shell.
    cas_headers = dict(headers)
    cas_headers["Sec-Fetch-Dest"] = "empty"
    cas_headers["Sec-Fetch-Mode"] = "cors"
    cas_headers["Sec-Fetch-Site"] = "same-site"

    # CAS issues a ticket for the OAuth callback; this is the silent SSO hop.
    async with session.get(
        redirect_url,
        headers=cas_headers,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as hop:
        final_url = str(hop.url)
        hop_status = hop.status
        hop_text = await hop.text()
    client.trace.append(
        {
            "url": "oauth:casLoginRedirect",
            "status": hop_status,
            "via": "get",
            "final": final_url[:300],
            "hops": [f"{h.status} {h.headers.get('Location', '')[:160]}" for h in hop.history],
            "body": hop_text[:300],
        }
    )

    ticket = _query_value(final_url, "ticket")
    bridge_id = bridge_device_id
    ticket_callback: str | None = None
    if not ticket:
        # CAS hands the OAuth service to its loginAuth bridge, which shows the
        # login form again: the service has to be authorised with a second
        # remoteLogin that names the OAuth callback as its service.
        ticket, bridge_id, ticket_callback = await _authorize_oauth_service(
            client,
            final_url,
            user_account,
            password,
            device_id,
            bridge_device_id,
            login_id,
        )

    if ticket_callback:
        # The service validates the ticket when the redirect is fetched, not when
        # it is posted: CAS hands its ticket over in a navigation, so follow it
        # before anything else touches the ticket. A hop that ends on the portal
        # has already finished the exchange.
        async with session.get(
            ticket_callback,
            headers=NAVIGATION_HEADERS,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as hop:
            consum_status = hop.status
            consum_url = str(hop.url)
            await hop.read()
        client.trace.append(
            {
                "url": "oauth:ticketCallback",
                "status": consum_status,
                "via": "get",
                "final": consum_url[:200],
            }
        )
        if _landed_on_portal(consum_url):
            return bridge_id

    body = {str(key): str(value) for key, value in signature.items()}
    body.update({"ticket": ticket, "siteID": SITE_ID, "countryCode": "CN"})
    login_headers = dict(headers)
    login_headers["Referer"] = final_url
    login_headers["Origin"] = OAUTH_LOGIN_BASE

    async with session.post(
        f"{OAUTH_LOGIN_BASE}/oauth2/ajax/login",
        params={"reflushCode": f"{random.random():.17f}", "display": "page"},
        data=body,
        headers=login_headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        login_status = response.status
        login_text = await response.text()
    client.trace.append(
        {
            "url": "oauth:ajaxLogin",
            "status": login_status,
            "via": "post",
            "body": login_text[:600],
        }
    )
    if login_status >= 400:
        raise HuaweiLoginError(f"OAuth login rejected ({login_status})")

    # The reply carries the redirect that trades the authorization code for a
    # portal session; without it /home answers 401 and no cookie is issued.
    try:
        payload = json.loads(login_text)
    except ValueError:
        payload = {}
    problem = _oauth_error(payload)
    if problem:
        raise HuaweiLoginError(f"OAuth login refused the ticket: {problem}")
    target = _oauth_redirect_target(payload)
    if target is None:
        raise HuaweiLoginError("OAuth login returned no redirect to the portal")
    async with session.get(
        target,
        headers=NAVIGATION_HEADERS,
        allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as hop:
        trans_status = hop.status
        trans_url = str(hop.url)
        await hop.read()
    client.trace.append(
        {
            "url": "oauth:homeTransit",
            "status": trans_status,
            "via": "get",
            "final": trans_url[:200],
        }
    )
    if not _landed_on_portal(trans_url):
        raise HuaweiLoginError("the OAuth redirect did not reach the portal")
    return bridge_id


def _landed_on_portal(url: str) -> bool:
    """Whether a hop ended on the portal, where the sign-in is finished.

    The host has to be the portal itself: ``oauth-login1.cloud.huawei.com`` and
    ``id1.cloud.huawei.com`` also carry ``cloud.huawei.com`` in their names, and
    matching on that substring reported the OAuth callback page as a finished
    sign-in.
    """
    return (urlsplit(url).hostname or "") == "cloud.huawei.com"


def _oauth_error(payload: Any) -> str | None:
    """The error an OAuth reply names, or None when it reports success.

    Only the error code and description are returned: an error redirect is a URL
    and can carry a ticket, so the URL itself is never put in a log line.
    """
    if not isinstance(payload, dict):
        return None
    parts: list[str] = []
    for field in ("errorCode", "errorDesc", "errorMessage", "error"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        if field == "errorCode" and value.strip() in ("0", ""):
            continue
        parts.append(value.strip()[:120])
    for name, value in payload.items():
        if not isinstance(value, str) or "error" not in name.lower():
            continue
        if not value.startswith(("http://", "https://")):
            continue
        query = parse_qs(urlsplit(value).query)
        for key in ("error", "error_code", "errorCode", "error_description", "errorDesc"):
            for item in query.get(key) or []:
                if item.strip():
                    parts.append(f"{key}={item.strip()[:120]}")
    unique = list(dict.fromkeys(parts))
    return " ".join(unique) if unique else None


def _oauth_redirect_target(payload: Any) -> str | None:
    """The URL that finishes the sign-in, or None when the reply carries none.

    The reply field is ``code``. Depending on the account it holds either the
    complete ``https://cloud.huawei.com/homeTransit?code=...`` URL or only the bare
    authorization code, and a bare code has to be wrapped in the redirect URI the
    request declared. Silently skipping this hop is what leaves behind a session
    with no ``userId``, which every find-device call then rejects.
    """
    if not isinstance(payload, dict):
        return None
    for field in ("code", "callbackURL", "redirectURL", "redirectUri", "url"):
        target = _redirect_from_value(payload.get(field))
        if target is not None:
            return target

    # Builds differ in what they name the field, and the page JavaScript only
    # guarantees that its value is a URL: it prefixes a bare host itself. Fall
    # back to the single absolute URL the reply carries, then to the single
    # authorization code, and log the field names -- never a value -- so an
    # unknown name costs one log line instead of one more sign-in.
    urls = [
        (name, value.strip())
        for name, value in payload.items()
        if isinstance(value, str)
        and "error" not in name.lower()
        and value.strip().startswith(("http://", "https://"))
    ]
    if len(urls) == 1:
        _LOGGER.warning(
            "OAuth login put its redirect in an unexpected field: %s", urls[0][0]
        )
        return urls[0][1]
    codes = [
        (name, value.strip())
        for name, value in payload.items()
        if isinstance(value, str)
        and "error" not in name.lower()
        and _CODE_SHAPE.match(value.strip())
    ]
    if not urls and len(codes) == 1:
        _LOGGER.warning(
            "OAuth login put its authorization code in an unexpected field: %s",
            codes[0][0],
        )
        return f"{OAUTH_REDIRECT_URI}?code={quote(codes[0][1], safe='')}"
    _LOGGER.warning(
        "OAuth login reply carried no redirect; fields were: %s", sorted(payload)
    )
    return None


def _redirect_from_value(value: Any) -> str | None:
    """Turn one reply field into the URL that finishes the sign-in."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"{OAUTH_REDIRECT_URI}?code={quote(value, safe='')}"


async def _register_device(
    client: _Session,
    base: dict[str, str],
    tokens: dict[str, str],
    device_id: str | None,
) -> str | None:
    """Register a device for one client type and return the id it issued.

    A client type only knows the ids registered for itself, so the id the portal
    issued is unknown on the bridge and comes back empty; the bridge then has no
    session and the next call reports 10000004. Registering without an id gives
    this client type its own device, which is what the page itself would have
    done on a first visit.
    """
    reply = await client.ajax(
        "dev", {**base, "fp": fingerprint.mint(device_id)}, tokens=tokens
    )
    issued = reply.get("sid")
    if fingerprint.valid_device_id(issued):
        return issued
    if device_id is None:
        return None
    reply = await client.ajax(
        "dev", {**base, "fp": fingerprint.mint(None)}, tokens=tokens
    )
    issued = reply.get("sid")
    return issued if fingerprint.valid_device_id(issued) else None


async def _authorize_oauth_service(
    client: _Session,
    login_auth_url: str,
    user_account: str,
    password: str,
    device_id: str | None,
    bridge_device_id: str | None = None,
    login_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    """Re-authorise the OAuth service on the loginAuth bridge and return its ticket.

    The bridge page is a normal login page whose ``service`` is the OAuth
    callback, so it is driven exactly like the portal login, only with the
    client type the OAuth component asks for (90 / 90000300).
    """
    query = parse_qs(urlsplit(login_auth_url).query)

    def pick(name: str, default: str) -> str:
        values = query.get(name) or []
        return values[0] if values and values[0] else default

    service = pick("service", OAUTH_REDIRECT_URI)
    base = {
        "reqClientType": pick("reqClientType", REQUEST_CLIENT_TYPE),
        "loginChannel": pick("loginChannel", LOGIN_CHANNEL),
        "clientID": pick("clientID", CLIENT_ID),
        "lang": "zh-cn",
        "languageCode": "zh-cn",
        "regionCode": pick("regionCode", "cn"),
        "themeName": pick("themeName", "red"),
    }

    # The bridge page asks for its page info with the service it is authorising;
    # without it the server has no login session to bind and answers 10000004.
    page_info = await client.ajax(
        "login/getPageInfo",
        {
            **base,
            "service": service,
            "pageName": "login",
            "interfaceName": "login/getPageInfo",
            "supportHarmonyTheme": "false",
            "urlParam": urlsplit(login_auth_url).query,
        },
        with_tokens=False,
    )
    token = page_info.get("pageToken")
    token_key = page_info.get("pageTokenKey")
    if not isinstance(token, str) or not isinstance(token_key, str):
        raise HuaweiLoginError("authorization step did not issue a page token")
    tokens = {"pageToken": token, "pageTokenKey": token_key}

    # The bridge page registers the device for its own client type before it
    # touches the account; without it the login session stays blank. Device ids
    # are registered per client type, so the id the portal issued is unknown here:
    # the bridge answers an empty sid and the next call reports a blank session
    # (10000004). Registering one for this client type keeps the session valid.
    bridge_id = await _register_device(client, base, tokens, device_id)

    await client.ajax(
        "chkRisk",
        {
            **base,
            "userAccount": user_account,
            "operType": "0",
            "lowLogin": "",
            "service": service,
            "scope": PORTAL_SCOPE,
        },
        tokens=tokens,
    )

    # The bridge binds its login session to one loginID, exactly like the portal
    # page does: an account that owns several answers "the login session value is
    # blank" (10000004) when remoteLogin names none. The bridge exposes the same
    # list, so the id chosen on the portal is selected here again.
    site = await client.ajax(
        "chkRisk",
        {
            **base,
            "userAccount": "0086" + user_account,
            "operType": "12",
            "lowLogin": "",
            "scope": PORTAL_SCOPE,
            "service": service,
            "registerCountry": "CN",
        },
        tokens=tokens,
    )
    entries = [
        str(item["loginID"])
        for item in (site.get("siteInfoList") or [])
        if isinstance(item, dict) and item.get("loginID")
    ]
    site_id = str(site.get("siteID") or SITE_ID)

    chosen = login_id
    if len(entries) > 1:
        if not chosen or chosen not in entries:
            raise HuaweiLoginAccountChoice(entries)
    elif entries:
        chosen = chosen or entries[0]

    if chosen and len(entries) > 1:
        selected = await client.ajax(
            "chkRisk",
            {
                **base,
                "userAccount": user_account,
                "operType": "13",
                "registerCountry": "CN",
                "anonymousLoginID": chosen,
                "lowLogin": "",
                "service": service,
                "scope": PORTAL_SCOPE,
            },
            tokens=tokens,
        )
        if not _succeeded(selected):
            raise HuaweiLoginError(
                "authorization step could not select the login id: "
                f"{selected.get('errorCode')} {selected.get('errorDesc')}".strip()
            )

    login = await client.ajax(
        "remoteLogin",
        {
            **base,
            **tokens,
            "loginUrl": service,
            "service": service,
            "scenesType": "0",
            "scope": PORTAL_SCOPE,
            "quickAuth": "false",
            "isThirdBind": "0",
            "lowLogin": "false",
            "userAccount": user_account,
            "password": password,
            "siteID": site_id,
        },
        with_tokens=False,
    )

    callback = login.get("callbackURL")
    if not isinstance(callback, str) or not callback:
        raise HuaweiLoginError(
            "authorization step failed: "
            f"{login.get('errorCode')} {login.get('errorDesc')}".strip()
        )
    ticket = _query_value(callback, "ticket")
    if not ticket:
        raise HuaweiLoginError("authorization step returned no ticket")
    client.trace.append({"url": "oauth:authorizeService", "status": 200, "via": "post"})
    return ticket, bridge_id, callback


class _Session:
    """Small wrapper that posts to the account ajax gateway."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.cversion = CAS_CVERSION_BASE
        self.trace: list[dict[str, Any]] = []

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        merged = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        if headers:
            merged.update(headers)
        response = await self._session.get(
            url,
            headers=merged,
            timeout=aiohttp.ClientTimeout(total=30),
            **kwargs,
        )
        self.trace.append({"url": url[:96], "status": response.status, "via": "get"})
        return response

    async def post_json(self, url: str, payload: dict[str, Any]) -> int:
        """POST the way the portal's own single-page app does.

        The portal answers 401 to a POST that does not echo its CSRF cookie in a
        CSRFToken header, so one is always sent when the jar holds it.
        """
        headers = dict(XHR_HEADERS)
        headers["Content-Type"] = "application/json;charset=UTF-8"
        headers["Origin"] = PORTAL_BASE
        headers["Referer"] = f"{PORTAL_BASE}/home"
        csrf = _portal_cookies(self._session.cookie_jar).get("CSRFToken")
        if csrf:
            headers["CSRFToken"] = csrf
        async with self._session.post(
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            await response.read()
            status = response.status
        self.trace.append({"url": url[:96], "status": status, "via": "post-json"})
        return status

    async def ajax(
        self,
        action: str,
        values: dict[str, str],
        *,
        with_tokens: bool = True,
        tokens: dict[str, str] | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        params = _base_params()
        if with_tokens and tokens:
            params.update(tokens)
        params.update(values)
        query = {
            "reflushCode": f"{random.random():.17f}",
            "cVersion": version or self.cversion,
        }
        async with self._session.post(
            f"{CAS_AJAX}/{action}",
            params=query,
            data=params,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": CAS_BASE,
                "Referer": CAS_LOGIN_PAGE,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            text = await response.text()
        self.trace.append(
            {"url": f"ajax:{action}", "status": response.status, "via": "post"}
        )
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise HuaweiLoginError(
                f"account gateway returned non-JSON: {text[:120]}"
            ) from err
        if not isinstance(payload, dict):
            raise HuaweiLoginError("account gateway returned an unexpected shape")
        return payload


def _record_attempt(account: str) -> None:
    """Count one sign-in attempt, refusing it when the window is already full."""
    now = time.monotonic()
    recent = [
        stamp for stamp in _attempts.get(account, []) if now - stamp < _ATTEMPT_WINDOW
    ]
    if len(recent) >= _ATTEMPT_LIMIT:
        raise HuaweiLoginThrottled(
            "too many sign-in attempts; wait a few minutes before trying again"
        )
    recent.append(now)
    _attempts[account] = recent


async def async_login(
    account: str,
    password: str,
    *,
    login_id: str | None = None,
    timeout: int = 30,
    seed_cookies: dict[str, str] | None = None,
    device_id: str | None = None,
    bridge_device_id: str | None = None,
) -> HuaweiLoginResult:
    """Log in and return the cookie header the portal API accepts.

    ``seed_cookies`` primes the account session, which lets a caller carry the
    device-trust cookies of an already trusted browser along. ``device_id`` is the
    id a previous login registered; without one this login registers a new device
    and reports the id it was given, which the caller stores for next time.
    """
    if not account or not password:
        raise HuaweiLoginAuthError("account and password are required")

    user_account = normalise_account(account)
    _record_attempt(user_account)

    jar = aiohttp.CookieJar(unsafe=True)
    if seed_cookies:
        jar.update_cookies(seed_cookies, response_url=aiohttp.client.URL(CAS_BASE))
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        client = _Session(session)

        async with await client.get(CAS_LOGIN_PAGE) as page:
            await page.read()

        switch = await client.ajax(
            "common/getBaseSwitchInfo", {}, with_tokens=False, version=CAS_CVERSION_BASE
        )
        version = switch.get("cookieVersion")
        if isinstance(version, str) and version:
            client.cversion = version

        page_info = await client.ajax(
            "login/getPageInfo", {"pageName": "login"}, with_tokens=False
        )
        token = page_info.get("pageToken")
        token_key = page_info.get("pageTokenKey")
        if not isinstance(token, str) or not isinstance(token_key, str):
            raise HuaweiLoginError("account gateway did not issue a page token")
        tokens = {"pageToken": token, "pageTokenKey": token_key}

        # The login page registers the device before it touches the account. The
        # action is ``dev`` on this module (``common/dev`` is the AMW alias), and
        # the portal-side client type is 7/7000000 here. A fingerprint with no
        # device id is a new device and the gateway allocates one.
        registration = await client.ajax(
            "dev",
            {
                "fp": fingerprint.mint(device_id),
                "reqClientType": "7",
                "loginChannel": "7000000",
            },
            tokens=tokens,
        )
        issued = registration.get("sid")
        issued = issued if isinstance(issued, str) else ""
        if device_id is None:
            if not fingerprint.valid_device_id(issued):
                raise HuaweiLoginError("the account gateway did not register a device")
            device_id = issued
        elif issued != device_id:
            # A stored id the gateway no longer recognises means this install
            # would silently present a different device; stop instead.
            raise HuaweiLoginError("the account gateway rejected the stored device id")
        _LOGGER.debug("Huawei device registered: %s", bool(device_id))

        common = {
            "lowLogin": "",
            "service": PORTAL_LOGIN,
            "scope": PORTAL_SCOPE,
        }

        await client.ajax(
            "chkRisk",
            {"userAccount": user_account, "operType": "0", **common},
            tokens=tokens,
        )

        with_prefix = await client.ajax(
            "chkRisk",
            {
                "userAccount": "0086" + user_account,
                "operType": "12",
                "lowLogin": "",
                "scope": PORTAL_SCOPE,
                "service": PORTAL_LOGIN,
                "registerCountry": "CN",
            },
            tokens=tokens,
        )
        entries = [
            item
            for item in (with_prefix.get("siteInfoList") or [])
            if isinstance(item, dict) and item.get("loginID")
        ]
        accounts = [str(item["loginID"]) for item in entries]
        site_id = str(with_prefix.get("siteID") or SITE_ID)

        chosen = login_id
        if len(accounts) > 1:
            if not chosen:
                raise HuaweiLoginAccountChoice(accounts)
            if chosen not in accounts:
                raise HuaweiLoginAccountChoice(accounts)
        elif accounts:
            chosen = chosen or accounts[0]

        if chosen and len(accounts) > 1:
            selected = await client.ajax(
                "chkRisk",
                {
                    "userAccount": user_account,
                    "operType": "13",
                    "registerCountry": "CN",
                    "anonymousLoginID": chosen,
                    **common,
                },
                tokens=tokens,
            )
            if not _succeeded(selected):
                raise HuaweiLoginError(
                    "account selection failed: "
                    f"{selected.get('errorCode')} {selected.get('errorDesc')}"
                )

        login = await client.ajax(
            "remoteLogin",
            {
                **tokens,
                "loginUrl": PORTAL_LOGIN,
                "service": PORTAL_LOGIN,
                "scenesType": "0",
                "scope": PORTAL_SCOPE,
                "quickAuth": "false",
                "isThirdBind": "0",
                "lowLogin": "false",
                "userAccount": user_account,
                "password": password,
                "siteID": site_id,
            },
            with_tokens=False,
        )

        if not _succeeded(login):
            code = str(login.get("errorCode") or "")
            description = str(login.get("errorDesc") or "")
            if code in _RISK_CODES:
                raise HuaweiLoginThrottled(
                    f"risk control refused the sign-in ({code}); wait and try again"
                )
            if code in _CHALLENGE_CODES or "twoFactorList" in description:
                raise HuaweiLoginCodeRequired(
                    f"verification code required ({code or 'unknown'})"
                )
            raise HuaweiLoginAuthError(
                f"login rejected: {code} {description}".strip()
            )

        callback = login.get("callbackURL")
        if not isinstance(callback, str) or not callback:
            raise HuaweiLoginError("login succeeded without a callback URL")

        async with await client.get(
            callback, headers=NAVIGATION_HEADERS, allow_redirects=True
        ) as hop:
            await hop.read()

        # The ticket only lands on the OAuth service; the authorization itself
        # still has to be completed before the portal knows who signed in.
        bridge_device_id = await _complete_oauth_authorization(
            session,
            client,
            user_account,
            password,
            device_id,
            bridge_device_id,
            chosen,
        )

        # The portal home is what makes the signed-in cookies settle: it issues
        # userId/isLogin/loginSecLevel/CSRFToken on top of the siteID/loginID/
        # token pair the ticket callback already set.
        async with await client.get(f"{PORTAL_BASE}/home", headers=NAVIGATION_HEADERS) as landing:
            await landing.read()

        cookies = _portal_cookies(jar)

        # Measured against the live API: the token, loginID and siteID cookies
        # plus a CSRFToken header equal to the CSRFToken cookie are what the
        # find-device endpoints authenticate with. A missing or mismatched one
        # answers "Unauthorized for user offline".
        missing = [
            name
            for name in ("token", "loginID", "siteID", "CSRFToken")
            if not cookies.get(name)
        ]
        if missing:
            raise HuaweiLoginError(
                "portal did not issue "
                + ", ".join(missing)
                + "; the login did not settle"
            )

        cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        # A completed login proves the account is not being hammered; drop the
        # attempt history so ordinary re-authentication is never refused.
        _attempts.pop(user_account, None)
        _LOGGER.debug(
            "Huawei login ok: user=%s cookies=%d csrf=%s device=%s",
            chosen or user_account,
            len(cookies),
            bool(cookies.get("CSRFToken")),
            bool(device_id),
        )
        return HuaweiLoginResult(
            cookie=cookie_header,
            user_id=cookies.get("userId"),
            csrf_token=cookies.get("CSRFToken"),
            login_id=chosen,
            device_id=device_id,
            bridge_device_id=bridge_device_id,
            cookies=cookies,
        )


def _succeeded(payload: dict[str, Any]) -> bool:
    value = payload.get("isSuccess")
    if value is None:
        return str(payload.get("errorCode", "0")) in ("0", "")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true"}
