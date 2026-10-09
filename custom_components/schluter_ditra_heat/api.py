"""Minimal async client for the Schluter DITRA-HEAT-E-WiFi cloud API.

Login first tries the direct ``/api/authenticate/user`` call that the web
app's login bundle still contains, then falls back to the OpenID Connect flow
against identity-schluter.mythermostat.info that the site uses today, which
ends with the home page embedding a SessionId. Every API
call then passes that id as the ``sessionid`` query parameter. Temperatures
are integers in hundredths of a degree Celsius.
"""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .const import API_URL, APPLICATION_ID, BASE_URL, NOTIFICATION_TIMEOUT

_LOGGER = logging.getLogger(__name__)

_SESSION_RE = re.compile(r"var\s+session_object\s*=\s*(\{.*?\});", re.S)


class SchluterError(Exception):
    """Generic API error."""


class SchluterAuthError(SchluterError):
    """Credentials were rejected or the session expired."""


class _FormParser(HTMLParser):
    """Collect the first <form>'s action and its input fields."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._in_form = True
            self.action = a.get("action", "")
        elif tag == "input" and self._in_form and a.get("name"):
            if a.get("type", "").lower() in ("checkbox", "radio") and "checked" not in a:
                return
            self.fields[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False
            self._done = True


def _parse_form(html: str) -> _FormParser:
    parser = _FormParser()
    parser.feed(html)
    return parser


class SchluterApi:
    """Talks to ditra-heat-e-wifi.schluter.com."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._session_id: str | None = None
        self._login_lock = asyncio.Lock()

    async def login(self) -> None:
        """Log in and store the resulting SessionId."""
        if session_id := await self._login_direct():
            self._session_id = session_id
        else:
            await self._login_oidc()
        # The server only queues notifications for a session after it has
        # listed the thermostats once, so prime every new session.
        await self._request("GET", "thermostats", retry=False)

    async def _login_direct(self) -> str | None:
        """Try the legacy JSON login; return None if it is unavailable or refused."""
        payload = {
            "Email": self._email,
            "Password": self._password,
            "Application": APPLICATION_ID,
        }
        try:
            async with self._session.post(
                f"{API_URL}/authenticate/user", json=payload, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("ErrorCode"):
            return None
        return data.get("SessionId") or None

    async def _login_oidc(self) -> None:
        """Run the website's OIDC login and scrape the SessionId."""
        self._session.cookie_jar.clear()

        # Unauthenticated, the site redirects to the identity server's login page.
        async with self._session.get(BASE_URL) as resp:
            login_url = str(resp.url)
            html = await resp.text()

        session_id = self._extract_session_id(html)
        if session_id is None:
            form = _parse_form(html)
            if "__RequestVerificationToken" not in form.fields:
                raise SchluterError("Login page did not contain the expected form")
            data = dict(form.fields)
            data.update(
                Username=self._email,
                Password=self._password,
                button="login",
                RememberLogin="false",
            )
            post_url = urljoin(login_url, form.action) if form.action else login_url
            async with self._session.post(post_url, data=data) as resp:
                html = await resp.text()
                final_url = str(resp.url)

            # A failed login re-renders the login form with the password field.
            if 'name="Password"' in html or "name='Password'" in html:
                raise SchluterAuthError("Invalid email or password")

            # The callback returns an auto-submitting form_post back to the site.
            form = _parse_form(html)
            if form.action and "code" in form.fields:
                async with self._session.post(
                    urljoin(final_url, form.action), data=form.fields
                ) as resp:
                    html = await resp.text()

            session_id = self._extract_session_id(html)

        if not session_id:
            raise SchluterAuthError("Login did not return a session id")
        self._session_id = session_id
        _LOGGER.debug("Logged in to Schluter")

    @staticmethod
    def _extract_session_id(html: str) -> str | None:
        match = _SESSION_RE.search(html)
        if not match:
            return None
        try:
            obj = json.loads(match.group(1))
        except ValueError:
            return None
        if obj.get("ErrorCode"):
            raise SchluterAuthError(f"Login error code {obj['ErrorCode']}")
        return obj.get("SessionId") or None

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None, retry: bool = True,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> Any:
        if self._session_id is None:
            async with self._login_lock:
                if self._session_id is None:
                    await self.login()
        session_id = self._session_id
        query = {"sessionid": session_id, **(params or {})}
        kwargs: dict[str, Any] = {"timeout": timeout} if timeout else {}
        async with self._session.request(
            method, f"{API_URL}/{path}", params=query, json=json_body,
            allow_redirects=False, **kwargs,
        ) as resp:
            if resp.status in (401, 403) or 300 <= resp.status < 400:
                if not retry:
                    raise SchluterAuthError(f"Session rejected ({resp.status})")
                # Another call may already have logged in again.
                if self._session_id == session_id:
                    self._session_id = None
                return await self._request(
                    method, path, params, json_body, retry=False, timeout=timeout
                )
            if resp.status >= 400:
                raise SchluterError(f"{method} {path} failed with HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def get_user_account(self) -> dict[str, Any]:
        return await self._request("GET", "useraccount")

    async def get_thermostats(self) -> dict[str, dict[str, Any]]:
        """Return all thermostats keyed by serial number."""
        data = await self._request("GET", "thermostats")
        result: dict[str, dict[str, Any]] = {}
        for group in (data or {}).get("Groups", []):
            for thermostat in group.get("Thermostats", []):
                result[str(thermostat["SerialNumber"])] = thermostat
        return result

    async def update_thermostat(self, serial: str, payload: dict[str, Any]) -> None:
        result = await self._request(
            "POST", "thermostat", params={"serialnumber": serial}, json_body=payload
        )
        if not (result or {}).get("Success"):
            raise SchluterError(f"Thermostat {serial} rejected update: {result}")

    async def wait_for_notification(self) -> dict[str, Any] | None:
        """Long-poll for the next thermostat change on the account.

        The server holds the request open until something changes, then
        returns ``{"Action": n, "Thermostat": {...}}``. Action 2 is an update
        and 3 a removal. With nothing to report it answers with an empty body
        or a null thermostat after about a minute; this returns None then.
        """
        data = await self._request(
            "GET", "notification", params={"sequencenr": 0},
            timeout=aiohttp.ClientTimeout(total=NOTIFICATION_TIMEOUT),
        )
        if not isinstance(data, dict) or not data.get("Thermostat"):
            return None
        return data
