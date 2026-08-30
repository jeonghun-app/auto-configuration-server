"""Operator console — a server-rendered admin UI.

Deployed with the service, so an installation comes with a management page rather
than only a JSON API. It manages three things the operator actually works with:

* **numbers** — a subscriber keyed by MSISDN or IMSI: entitlement, profile, VoLTE,
  the configuration version, and the operational actions;
* **parameters** — per-subscriber overrides of any of the OMA-CP provisioning
  parameters or OMA-DM management nodes, chosen from the catalogues rather than
  typed free-hand;
* **devices** — the inventory built from RCC.14 request parameters and from what a
  handset reported over OMA-DM, including every management node value it returned.

Security posture, since this page renders subscriber data over the network:

* it does not exist until ``ACS_ADMIN_TOKEN`` is set — the same fail-closed rule as
  the JSON admin API, and with no token every route answers 503;
* sign-in exchanges the admin token for an HMAC-signed, expiring session cookie
  that is ``HttpOnly`` and ``SameSite=Strict``, and ``Secure`` over HTTPS;
* every mutating form carries a CSRF token bound to that cookie;
* no JavaScript at all, and a Content-Security-Policy that forbids scripts;
* every rendered value is escaped;
* pages are ``no-store``, because they contain IMSI, MSISDN and IMEI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Sequence
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Cookie, Form, HTTPException, Query, Request, Response

from acs.api.deps import AppState
from acs.api.html import (
    SECURITY_HEADERS,
    checkbox_field,
    definition_list,
    esc,
    hidden,
    page,
    pill,
    select_field,
    table,
    text_field,
)
from acs.auth import token as token_mod
from acs.conformance import load_all as load_conformance
from acs.domain.models import Subscriber
from acs.observability import get_logger
from acs.protocol import vers as vers_mod
from acs.protocol.omacp.catalog import available_profiles, get_catalog
from acs.protocol.omadm.motree import get_tree
from acs.security.pii import normalise_msisdn
from acs.specscope import load_families as load_spec_families

log = get_logger(__name__)
router = APIRouter(prefix="/admin/ui", tags=["console"])

SESSION_COOKIE = "acs_console"
CSRF_COOKIE = "acs_console_csrf"
SESSION_TTL_SECONDS = 3600


# --------------------------------------------------------------- session
def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_session(secret: str, now: int | None = None) -> str:
    """Mint a signed, expiring session value.

    Signed with the admin token itself, so revoking the token invalidates every
    live console session at once.
    """
    expires = (now or int(time.time())) + SESSION_TTL_SECONDS
    payload = str(expires)
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload, secret)}".encode()).decode()


def session_valid(cookie: str | None, secret: str, now: int | None = None) -> bool:
    if not cookie or not secret:
        return False
    try:
        raw = base64.urlsafe_b64decode(cookie.encode()).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return False
    payload, _, signature = raw.rpartition(":")
    if not payload.isdigit() or not signature:
        return False
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return False
    return int(payload) > (now or int(time.time()))


def _state(request: Request) -> AppState:
    return request.app.state.acs  # type: ignore[no-any-return]


def _require_console(request: Request) -> tuple[AppState, str]:
    """Return the app state and the CSRF token, or raise."""
    app_state = _state(request)
    if not app_state.settings.admin_token:
        raise HTTPException(
            status_code=503,
            detail="console disabled: ACS_ADMIN_TOKEN is not configured",
        )
    cookie = request.cookies.get(SESSION_COOKIE)
    if not session_valid(cookie, app_state.settings.admin_token):
        raise _redirect("/admin/ui/login?next=" + quote(request.url.path))
    csrf = request.cookies.get(CSRF_COOKIE, "")
    return app_state, csrf


class _Redirect(HTTPException):
    def __init__(self, location: str) -> None:
        super().__init__(status_code=303)
        self.location = location


def _redirect(location: str) -> _Redirect:
    return _Redirect(location)


def _html_response(
    body: str, status_code: int = 200, cookies: Sequence[tuple[str, str, bool]] = ()
) -> Response:
    response = Response(
        content=body,
        media_type="text/html; charset=utf-8",
        status_code=status_code,
        headers=dict(SECURITY_HEADERS),
    )
    for name, value, secure in cookies:
        if value:
            response.set_cookie(
                name,
                value,
                httponly=True,
                samesite="strict",
                secure=secure,
                max_age=SESSION_TTL_SECONDS,
                path="/admin/ui",
            )
        else:
            response.delete_cookie(name, path="/admin/ui")
    return response


def _see_other(location: str) -> Response:
    return Response(status_code=303, headers={"Location": location, **dict(SECURITY_HEADERS)})


def _check_csrf(form_token: str, cookie_token: str | None) -> None:
    if not cookie_token or not hmac.compare_digest(form_token, cookie_token):
        raise HTTPException(status_code=400, detail="stale form; reload and try again")


def _flash(location: str, message: str, error: bool = False) -> Response:
    query = urlencode({"m": message, **({"e": "1"} if error else {})})
    joiner = "&" if "?" in location else "?"
    return _see_other(f"{location}{joiner}{query}")


def _render(
    request: Request,
    app_state: AppState,
    csrf: str,
    title: str,
    body: str,
    current: str,
) -> Response:
    return _html_response(
        page(
            title,
            body,
            current=current,
            flash=request.query_params.get("m", ""),
            flash_error=request.query_params.get("e") == "1",
            csrf=csrf,
            environment=app_state.settings.env,
        )
    )


# ------------------------------------------------------------------ sign in
@router.get("/login", summary="Console sign-in")
def login_form(request: Request, next: str = Query(default="/admin/ui")) -> Response:
    app_state = _state(request)
    if not app_state.settings.admin_token:
        return _html_response(
            page(
                "Console unavailable",
                "<h2>Console unavailable</h2><p>The admin token is not configured, so "
                "the console and the admin API are both disabled. Set "
                "<code>ACS_ADMIN_TOKEN</code> — there is deliberately no default.</p>",
                signed_in=False,
            ),
            status_code=503,
        )

    csrf = secrets.token_urlsafe(24)
    body = (
        "<h2>Sign in</h2>"
        '<p class="hint">Paste the admin token. In AWS it is held in Secrets '
        "Manager; the deploy output prints the command to read it.</p>"
        '<form method="post" action="/admin/ui/login">'
        + hidden("csrf", csrf)
        + hidden("next", next)
        + text_field(
            "token",
            "Admin token",
            input_type="password",
            required=True,
            hint="Sent once and exchanged for a session that expires in one hour.",
        )
        + '<button type="submit">Sign in</button></form>'
    )
    return _html_response(
        page("Sign in", body, signed_in=False),
        cookies=[(CSRF_COOKIE, csrf, request.url.scheme == "https")],
    )


@router.post("/login", summary="Exchange the admin token for a session")
def login(
    request: Request,
    token: str = Form(...),
    csrf: str = Form(...),
    next: str = Form(default="/admin/ui"),
    acs_console_csrf: str | None = Cookie(default=None),
) -> Response:
    app_state = _state(request)
    expected = app_state.settings.admin_token
    if not expected:
        raise HTTPException(status_code=503, detail="console disabled")
    _check_csrf(csrf, acs_console_csrf)

    if not hmac.compare_digest(token, expected):
        # Never log the supplied value.
        log.info("console sign-in rejected")
        body = (
            "<h2>Sign in</h2>"
            '<div class="flash error" role="alert">That token was not accepted.</div>'
            '<p><a href="/admin/ui/login">Try again</a></p>'
        )
        return _html_response(page("Sign in", body, signed_in=False), status_code=401)

    secure = request.url.scheme == "https"
    destination = next if next.startswith("/admin/ui") else "/admin/ui"
    response = _see_other(destination)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(expected),
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=SESSION_TTL_SECONDS,
        path="/admin/ui",
    )
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(24),
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=SESSION_TTL_SECONDS,
        path="/admin/ui",
    )
    log.info("console sign-in accepted")
    return response


@router.post("/logout", summary="End the console session")
def logout(request: Request, csrf: str = Form(default="")) -> Response:
    cookie = request.cookies.get(CSRF_COOKIE)
    if csrf and cookie:
        _check_csrf(csrf, cookie)
    response = _see_other("/admin/ui/login")
    response.delete_cookie(SESSION_COOKIE, path="/admin/ui")
    response.delete_cookie(CSRF_COOKIE, path="/admin/ui")
    return response


# ----------------------------------------------------------------- overview
@router.get("", summary="Console overview")
def overview(request: Request) -> Response:
    app_state, csrf = _require_console(request)
    store = app_state.store
    subscribers = store.list_subscribers(limit=500)
    devices = store.list_devices(limit=500)
    catalog = get_catalog(app_state.settings.default_rcs_profile)
    tree = get_tree()

    disabled = [s for s in subscribers if s.forced_vers is not None]
    not_entitled = [s for s in subscribers if not s.entitled]
    overridden = [s for s in subscribers if s.overrides]

    cards = definition_list(
        [
            ("Numbers", f"{len(subscribers)}"),
            (
                "Not entitled",
                f"{len(not_entitled)} "
                + (pill("attention", "warn") if not_entitled else pill("none", "ok")),
            ),
            (
                "Forced disable / dormant",
                f"{len(disabled)} "
                + (pill("attention", "warn") if disabled else pill("none", "ok")),
            ),
            ("With parameter overrides", f"{len(overridden)}"),
            ("Devices seen", f"{len(devices)}"),
            ("OMA-CP parameters available", f"{len(catalog.entries)}"),
            ("OMA-DM nodes available", f"{len(tree.all_nodes())}"),
            ("Default profile", esc(app_state.settings.default_rcs_profile)),
            ("Store backend", esc(app_state.settings.store_backend)),
            ("SMS provider", esc(app_state.sms.name)),
        ]
    )

    recent = sorted(devices, key=lambda d: d.last_seen_at, reverse=True)[:10]
    recent_table = table(
        ["Device", "Model", "Software", "DM nodes", "Last seen"],
        [
            (
                f'<a href="/admin/ui/devices/{quote(d.device_id)}">{esc(d.device_id)}</a>',
                esc(d.model or "—"),
                esc(d.sw_version or "—"),
                str(len(d.mo_values)),
                esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(d.last_seen_at))),
            )
            for d in recent
        ],
        caption="Most recently seen devices",
        empty="No device has contacted the server yet.",
        numeric=(3,),
    )

    body = (
        "<h2>Overview</h2>"
        '<p class="hint">Manage subscribers by number, their provisioning '
        "parameters, and the devices that have contacted this server.</p>"
        '<form method="get" action="/admin/ui/subscribers">'
        + text_field(
            "q",
            "Find a number or IMSI",
            hint="Enter a full or partial MSISDN or IMSI.",
            placeholder="+821012345678",
        )
        + '<button type="submit">Search</button></form>'
        "<h3>Status</h3>" + cards + "<h3>Devices</h3>" + recent_table
    )
    return _render(request, app_state, csrf, "Overview", body, "overview")


# -------------------------------------------------------------- subscribers
def _subscriber_row(subscriber: Subscriber) -> tuple[str, ...]:
    if not subscriber.entitled:
        state = pill("not entitled", "bad")
    elif subscriber.forced_vers is not None:
        rule = vers_mod.rule_for(subscriber.forced_vers)
        state = pill(f"{subscriber.forced_vers} {rule.action.value}", "warn")
    else:
        state = pill("active", "ok")
    link = f"/admin/ui/subscribers/{quote(subscriber.imsi)}"
    return (
        f'<a href="{link}">{esc(subscriber.msisdn)}</a>',
        f'<span class="mono">{esc(subscriber.imsi)}</span>',
        state,
        esc(subscriber.rcs_profile or "(default)"),
        pill("on", "ok") if subscriber.volte_enabled else pill("off"),
        str(subscriber.provisioning_version),
        str(len(subscriber.overrides)),
    )


@router.get("/subscribers", summary="Subscribers by number")
def subscriber_list(request: Request, q: str = Query(default="")) -> Response:
    app_state, csrf = _require_console(request)
    needle = q.strip()
    subscribers = app_state.store.list_subscribers(limit=500)

    if needle:
        digits = "".join(ch for ch in needle if ch.isdigit())
        subscribers = [
            s for s in subscribers if digits and (digits in s.msisdn or digits in s.imsi)
        ]

    listing = table(
        ["Number", "IMSI", "State", "Profile", "VoLTE", "Version", "Overrides"],
        [_subscriber_row(s) for s in sorted(subscribers, key=lambda s: s.msisdn)],
        caption=(f"{len(subscribers)} matching" if needle else f"{len(subscribers)} total"),
        empty="No subscriber matched." if needle else "No subscribers yet. Add one below.",
        numeric=(5, 6),
    )

    profiles = [("", "(server default)")] + [(p, p) for p in available_profiles()]
    add_form = (
        "<h3>Add a number</h3>"
        '<form method="post" action="/admin/ui/subscribers">'
        + hidden("csrf", csrf)
        + '<div class="row"><div>'
        + text_field("imsi", "IMSI", required=True, hint="5 to 15 digits.")
        + "</div><div>"
        + text_field("msisdn", "Number (MSISDN)", required=True, placeholder="+821012345678")
        + "</div><div>"
        + select_field("rcs_profile", "RCS profile", profiles)
        + "</div></div>"
        + checkbox_field("entitled", "Entitled to RCS", True)
        + checkbox_field("volte_enabled", "VoLTE enabled", True)
        + '<button type="submit">Add</button></form>'
    )

    body = (
        "<h2>Numbers</h2>"
        '<form method="get" action="/admin/ui/subscribers">'
        + text_field("q", "Search", needle, hint="Full or partial MSISDN or IMSI.")
        + '<button type="submit">Search</button></form>'
        + listing
        + add_form
    )
    return _render(request, app_state, csrf, "Numbers", body, "subscribers")


@router.post("/subscribers", summary="Create a subscriber")
def subscriber_create(
    request: Request,
    imsi: str = Form(...),
    msisdn: str = Form(...),
    rcs_profile: str = Form(default=""),
    csrf: str = Form(...),
    entitled: str = Form(default=""),
    volte_enabled: str = Form(default=""),
) -> Response:
    app_state, cookie_csrf = _require_console(request)
    _check_csrf(csrf, cookie_csrf)

    imsi = imsi.strip()
    normalised = normalise_msisdn(
        msisdn,
        app_state.settings.default_country_code,
        app_state.settings.national_trunk_prefix,
    )
    if not imsi.isdigit() or not 5 <= len(imsi) <= 15:
        return _flash("/admin/ui/subscribers", "IMSI must be 5 to 15 digits.", True)
    if normalised is None:
        return _flash("/admin/ui/subscribers", "That number is not valid E.164.", True)
    if app_state.store.get_subscriber(imsi) is not None:
        return _flash("/admin/ui/subscribers", f"IMSI {imsi} already exists.", True)

    app_state.store.put_subscriber(
        Subscriber(
            imsi=imsi,
            msisdn=normalised,
            entitled=bool(entitled),
            volte_enabled=bool(volte_enabled),
            rcs_profile=rcs_profile,
        )
    )
    log.info("console created subscriber", extra={"imsi": imsi})
    return _flash(f"/admin/ui/subscribers/{quote(imsi)}", "Number added.")


@router.get("/subscribers/{imsi}", summary="Subscriber detail")
def subscriber_detail(request: Request, imsi: str) -> Response:
    app_state, csrf = _require_console(request)
    subscriber = app_state.store.get_subscriber(imsi)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="subscriber not found")

    settings = app_state.settings
    profile = subscriber.rcs_profile or settings.default_rcs_profile
    catalog = get_catalog(profile)
    tree = get_tree()

    device = app_state.store.get_device(subscriber.imsi)
    devices = [d for d in app_state.store.list_devices(limit=500) if d.imsi == subscriber.imsi]

    forced_options = [("", "not forced (serve configuration)")] + [
        (str(v), f"{v} — {vers_mod.rule_for(v).action.value}") for v in vers_mod.FORCEABLE_VERSIONS
    ]
    profiles = [("", "(server default)")] + [(p, p) for p in available_profiles()]

    core = (
        "<h3>Number</h3>"
        '<form method="post" action="'
        f'/admin/ui/subscribers/{quote(subscriber.imsi)}">'
        + hidden("csrf", csrf)
        + '<div class="row"><div>'
        + text_field("msisdn", "Number (MSISDN)", subscriber.msisdn, required=True)
        + "</div><div>"
        + select_field("rcs_profile", "RCS profile", profiles, subscriber.rcs_profile)
        + "</div><div>"
        + select_field(
            "forced_vers",
            "Force a configuration version",
            forced_options,
            "" if subscriber.forced_vers is None else str(subscriber.forced_vers),
            hint="-2 and -4 stop the client asking again until a factory reset or SIM swap.",
        )
        + "</div></div>"
        + text_field(
            "imei_allowlist",
            "IMEI allowlist",
            ", ".join(subscriber.imei_allowlist),
            hint="Comma separated. Empty means any handset.",
        )
        + checkbox_field("entitled", "Entitled to RCS", subscriber.entitled)
        + checkbox_field("volte_enabled", "VoLTE enabled", subscriber.volte_enabled)
        + '<button type="submit">Save</button></form>'
    )

    facts = definition_list(
        [
            ("IMSI", f'<span class="mono">{esc(subscriber.imsi)}</span>'),
            ("Configuration version", str(subscriber.provisioning_version)),
            (
                "Effective profile",
                esc(profile)
                + (" " + pill("from server default") if not subscriber.rcs_profile else ""),
            ),
            (
                "OMA-DM bootstrapped",
                pill("yes", "ok") if subscriber.dm_password else pill("not yet"),
            ),
            (
                "Devices seen",
                ", ".join(
                    f'<a href="/admin/ui/devices/{quote(d.device_id)}">{esc(d.device_id)}</a>'
                    for d in devices
                )
                or "—",
            ),
            (
                "Last updated",
                esc(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(subscriber.updated_at))),
            ),
        ]
    )

    def action_button(action: str, label: str, danger: bool) -> str:
        css = ' class="danger"' if danger else ""
        return (
            '<form class="inline" method="post" action="'
            f'/admin/ui/subscribers/{quote(subscriber.imsi)}/action">'
            + hidden("csrf", csrf)
            + hidden("action", action)
            + f'<button type="submit"{css}>{esc(label)}</button></form> '
        )

    actions = (
        "<h3>Actions</h3>"
        + "".join(
            action_button(action, label, danger)
            for action, label, danger in (
                ("invalidate", "Bump version (force re-provision)", False),
                ("enable", "Enable", False),
                ("revoke-tokens", "Revoke tokens", True),
                ("issue-token", "Issue a token", False),
                ("delete", "Delete subscriber", True),
            )
        )
        + '<p class="hint">Bumping the version makes the next request return a full '
        "document. Clients pick it up when their validity expires, not instantly.</p>"
    )

    # ---- parameter overrides
    cp_options = [("", "— choose an OMA-CP parameter —")] + [
        (e.key, f"{e.key}  [{e.type}{' ' + e.unit if e.unit else ''}]")
        for e in catalog.for_profile(profile)
    ]
    dm_options = [("", "— choose an OMA-DM node —")] + [
        (n.uri, f"{n.uri}  [{n.format}]") for n in tree.server_nodes(["rcs", "volte"])
    ]
    known = {e.key: e for e in catalog.entries}
    dm_known = {n.uri: n for n in tree.all_nodes()}

    override_rows = []
    for key, value in sorted(subscriber.overrides.items()):
        entry = known.get(key)
        node = dm_known.get(key)
        if entry is not None:
            kind, default = "OMA-CP", entry.default or "(omitted)"
        elif node is not None:
            kind, default = "OMA-DM", node.default or "(none)"
        else:
            kind, default = pill("unknown key", "bad"), "—"
        delete_form = (
            '<form class="inline" method="post" action="'
            f'/admin/ui/subscribers/{quote(subscriber.imsi)}/override">'
            + hidden("csrf", csrf)
            + hidden("key", key)
            + hidden("value", "")
            + '<button class="danger" type="submit">Remove</button></form>'
        )
        override_rows.append(
            (
                f'<span class="mono">{esc(key)}</span>',
                kind if isinstance(kind, str) and kind.startswith("<") else esc(kind),
                f'<span class="mono">{esc(value)}</span>',
                f'<span class="mono">{esc(default)}</span>',
                delete_form,
            )
        )

    overrides = (
        "<h3>Parameter overrides</h3>"
        '<p class="hint">An override replaces the catalogue default for this '
        "subscriber only, on either plane. Everything else comes from the profile.</p>"
        + table(
            ["Parameter", "Plane", "Override", "Catalogue default", ""],
            override_rows,
            empty="No overrides. This subscriber gets the profile defaults.",
        )
        + '<form method="post" action="'
        f'/admin/ui/subscribers/{quote(subscriber.imsi)}/override">'
        + hidden("csrf", csrf)
        + select_field("key", "OMA-CP provisioning parameter", cp_options)
        + select_field("dm_key", "or OMA-DM management node", dm_options)
        + text_field("value", "Value", hint="Leave empty to remove an existing override.")
        + '<button type="submit">Set override</button></form>'
    )

    dm_summary = ""
    if device is not None and device.mo_values:
        dm_summary = "<h3>Last reported by the handset</h3>" + table(
            ["Management node", "Value"],
            [
                (f'<span class="mono">{esc(k)}</span>', esc(v))
                for k, v in sorted(device.mo_values.items())
            ],
            caption="Collected over OMA-DM",
        )

    body = (
        f"<h2>{esc(subscriber.msisdn)}</h2>"
        '<p class="hint"><a href="/admin/ui/subscribers">Back to numbers</a></p>'
        + facts
        + core
        + overrides
        + actions
        + dm_summary
    )
    return _render(request, app_state, csrf, subscriber.msisdn, body, "subscribers")


@router.post("/subscribers/{imsi}", summary="Update a subscriber")
def subscriber_update(
    request: Request,
    imsi: str,
    msisdn: str = Form(...),
    csrf: str = Form(...),
    rcs_profile: str = Form(default=""),
    forced_vers: str = Form(default=""),
    imei_allowlist: str = Form(default=""),
    entitled: str = Form(default=""),
    volte_enabled: str = Form(default=""),
) -> Response:
    app_state, cookie_csrf = _require_console(request)
    _check_csrf(csrf, cookie_csrf)
    subscriber = app_state.store.get_subscriber(imsi)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="subscriber not found")

    destination = f"/admin/ui/subscribers/{quote(imsi)}"
    normalised = normalise_msisdn(
        msisdn,
        app_state.settings.default_country_code,
        app_state.settings.national_trunk_prefix,
    )
    if normalised is None:
        return _flash(destination, "That number is not valid E.164.", True)

    forced: int | None = None
    if forced_vers:
        try:
            forced = int(forced_vers)
        except ValueError:
            return _flash(destination, "Forced version must be a number.", True)
        if forced not in vers_mod.FORCEABLE_VERSIONS:
            return _flash(destination, f"{forced} is not a valid disable value.", True)

    allowlist = [item.strip() for item in imei_allowlist.split(",") if item.strip()]
    bad = [item for item in allowlist if not item.isdigit() or not 14 <= len(item) <= 16]
    if bad:
        return _flash(destination, f"Not a valid IMEI: {', '.join(bad)}", True)

    subscriber.msisdn = normalised
    subscriber.rcs_profile = rcs_profile
    subscriber.forced_vers = forced
    subscriber.imei_allowlist = allowlist
    subscriber.entitled = bool(entitled)
    subscriber.volte_enabled = bool(volte_enabled)
    app_state.store.put_subscriber(subscriber)
    log.info("console updated subscriber", extra={"imsi": imsi, "forced_vers": forced})
    return _flash(destination, "Saved.")


@router.post("/subscribers/{imsi}/override", summary="Set or clear a parameter override")
def subscriber_override(
    request: Request,
    imsi: str,
    csrf: str = Form(...),
    key: str = Form(default=""),
    dm_key: str = Form(default=""),
    value: str = Form(default=""),
) -> Response:
    app_state, cookie_csrf = _require_console(request)
    _check_csrf(csrf, cookie_csrf)
    subscriber = app_state.store.get_subscriber(imsi)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="subscriber not found")

    destination = f"/admin/ui/subscribers/{quote(imsi)}"
    chosen = (key or dm_key).strip()
    if not chosen:
        return _flash(destination, "Choose a parameter or a management node.", True)

    profile = subscriber.rcs_profile or app_state.settings.default_rcs_profile
    valid_keys = {e.key for e in get_catalog(profile).entries}
    valid_nodes = {n.uri for n in get_tree().all_nodes()}
    if chosen not in valid_keys and chosen not in valid_nodes:
        # Only catalogued keys are accepted: a typo would otherwise sit in the
        # record forever, silently doing nothing.
        return _flash(destination, "That key is not in either catalogue.", True)

    if value.strip():
        subscriber.overrides[chosen] = value.strip()
        message = "Override set."
    elif chosen in subscriber.overrides:
        del subscriber.overrides[chosen]
        message = "Override removed."
    else:
        return _flash(destination, "Nothing to remove.", True)

    app_state.store.put_subscriber(subscriber)
    log.info("console changed override", extra={"imsi": imsi, "key": chosen})
    return _flash(destination, message)


@router.post("/subscribers/{imsi}/action", summary="Operational action")
def subscriber_action(
    request: Request,
    imsi: str,
    action: str = Form(...),
    csrf: str = Form(...),
) -> Response:
    app_state, cookie_csrf = _require_console(request)
    _check_csrf(csrf, cookie_csrf)
    store = app_state.store
    subscriber = store.get_subscriber(imsi)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="subscriber not found")

    destination = f"/admin/ui/subscribers/{quote(imsi)}"

    if action == "invalidate":
        subscriber.provisioning_version = vers_mod.next_version(subscriber.provisioning_version)
        subscriber.forced_vers = None
        store.put_subscriber(subscriber)
        return _flash(destination, f"Version bumped to {subscriber.provisioning_version}.")

    if action == "enable":
        subscriber.forced_vers = None
        subscriber.entitled = True
        subscriber.provisioning_version = vers_mod.next_version(subscriber.provisioning_version)
        store.put_subscriber(subscriber)
        return _flash(destination, "Enabled and version bumped.")

    if action == "revoke-tokens":
        count = store.revoke_tokens_for_imsi(imsi)
        return _flash(destination, f"{count} token(s) revoked; the client must re-authenticate.")

    if action == "issue-token":
        issued = token_mod.issue_token(
            store=store,
            imsi=imsi,
            imei=None,
            ttl_seconds=app_state.settings.token_ttl_seconds,
            bind_imei=False,
        )
        # Shown once. It is a bearer credential, so it is never stored in clear
        # text and never logged.
        return _flash(destination, f"Token issued (shown once): {issued}")

    if action == "delete":
        store.revoke_tokens_for_imsi(imsi)
        store.delete_subscriber(imsi)
        log.info("console deleted subscriber", extra={"imsi": imsi})
        return _flash("/admin/ui/subscribers", "Subscriber deleted.")

    return _flash(destination, f"Unknown action {action}.", True)


# ------------------------------------------------------------------ devices
@router.get("/devices", summary="Device inventory")
def device_list(request: Request, q: str = Query(default="")) -> Response:
    app_state, csrf = _require_console(request)
    needle = q.strip()
    devices = app_state.store.list_devices(limit=500)
    if needle:
        lowered = needle.lower()
        devices = [
            d
            for d in devices
            if lowered in d.device_id.lower()
            or lowered in d.model.lower()
            or lowered in d.manufacturer.lower()
            or lowered in d.imsi
        ]

    rows = []
    for device in sorted(devices, key=lambda d: d.last_seen_at, reverse=True):
        subscriber = app_state.store.get_subscriber(device.imsi) if device.imsi else None
        number = (
            f'<a href="/admin/ui/subscribers/{quote(subscriber.imsi)}">'
            f"{esc(subscriber.msisdn)}</a>"
            if subscriber
            else "—"
        )
        rows.append(
            (
                f'<a href="/admin/ui/devices/{quote(device.device_id)}">'
                f'<span class="mono">{esc(device.device_id)}</span></a>',
                number,
                esc(device.manufacturer or "—"),
                esc(device.model or "—"),
                esc(device.sw_version or "—"),
                esc(device.client_version or "—"),
                str(len(device.mo_values)),
                esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(device.last_seen_at))),
            )
        )

    body = (
        "<h2>Devices</h2>"
        '<p class="hint">Built from RCC.14 request parameters and from what each '
        "handset reported over OMA-DM.</p>"
        '<form method="get" action="/admin/ui/devices">'
        + text_field("q", "Search", needle, hint="Device id, model, vendor or IMSI.")
        + '<button type="submit">Search</button></form>'
        + table(
            [
                "Device id",
                "Number",
                "Vendor",
                "Model",
                "Software",
                "RCS client",
                "DM nodes",
                "Last seen",
            ],
            rows,
            caption=f"{len(devices)} device(s)",
            empty="No device has contacted the server yet.",
            numeric=(6,),
        )
    )
    return _render(request, app_state, csrf, "Devices", body, "devices")


@router.get("/devices/{device_id}", summary="Device detail")
def device_detail(request: Request, device_id: str) -> Response:
    app_state, csrf = _require_console(request)
    device = app_state.store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")

    subscriber = app_state.store.get_subscriber(device.imsi) if device.imsi else None
    tree = get_tree()

    number = (
        f'<a href="/admin/ui/subscribers/{quote(subscriber.imsi)}">' f"{esc(subscriber.msisdn)}</a>"
        if subscriber
        else "— not linked to a subscriber"
    )
    facts = definition_list(
        [
            ("Device id", f'<span class="mono">{esc(device.device_id)}</span>'),
            ("Number", number),
            ("Manufacturer", esc(device.manufacturer or "—")),
            ("Model", esc(device.model or "—")),
            ("Software", esc(device.sw_version or "—")),
            ("RCS client", esc(f"{device.client_vendor} {device.client_version}".strip() or "—")),
            ("DM client version", esc(device.dm_client_version or "—")),
            (
                "Last seen",
                esc(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(device.last_seen_at))),
            ),
        ]
    )

    reported_rows = []
    for uri, value in sorted(device.mo_values.items()):
        node = tree.node(uri)
        reported_rows.append(
            (
                f'<span class="mono">{esc(uri)}</span>',
                esc(node.format if node else "?"),
                esc(value),
                esc(node.spec if node else "not in any loaded management object"),
            )
        )

    body = (
        f"<h2>{esc(device.device_id)}</h2>"
        '<p class="hint"><a href="/admin/ui/devices">Back to devices</a></p>'
        + facts
        + "<h3>Reported management nodes</h3>"
        + table(
            ["Node", "Format", "Value", "Reference"],
            reported_rows,
            caption="Values this handset returned over OMA-DM",
            empty="This device has not completed an OMA-DM session yet.",
        )
        + (
            "<h3>Parameters</h3>"
            '<p class="hint">Parameters are set per number. '
            f'<a href="/admin/ui/subscribers/{quote(subscriber.imsi)}">'
            f"Manage {esc(subscriber.msisdn)}</a>.</p>"
            if subscriber
            else ""
        )
    )
    return _render(request, app_state, csrf, device.device_id, body, "devices")


# ----------------------------------------------------------------- catalogue
@router.get("/catalog", summary="Browse the parameter catalogues")
def catalog_view(
    request: Request, profile: str = Query(default=""), q: str = Query(default="")
) -> Response:
    app_state, csrf = _require_console(request)
    chosen = profile or app_state.settings.default_rcs_profile
    catalog = get_catalog(chosen)
    tree = get_tree()
    needle = q.strip().lower()

    cp_entries = [
        e
        for e in catalog.for_profile(chosen)
        if not needle or needle in e.key.lower() or needle in e.spec.lower()
    ]
    dm_nodes = [
        n
        for n in tree.all_nodes()
        if not needle or needle in n.uri.lower() or needle in n.spec.lower()
    ]

    profiles = [(p, p) for p in available_profiles()]
    body = (
        "<h2>Parameters</h2>"
        '<p class="hint">Everything this server can send. Values are declared in '
        "YAML, so adding a parameter or a management object is a data change. "
        "<code>verified</code> means cross-checked against a specification edition "
        "— see docs/spec-coverage.md for what that currently means.</p>"
        '<form method="get" action="/admin/ui/catalog">'
        + select_field("profile", "RCS profile", profiles, chosen)
        + text_field("q", "Filter", q, hint="Match on parameter path or reference.")
        + '<button type="submit">Apply</button></form>'
        + "<h3>OMA-CP provisioning parameters</h3>"
        + table(
            ["Path", "Parameter", "Type", "Default", "Reference", "Verified"],
            [
                (
                    f'<span class="mono">{esc(e.path)}</span>',
                    f'<span class="mono">{esc(e.parm)}</span>',
                    esc(e.type + (f" ({e.unit})" if e.unit else "")),
                    f'<span class="mono">{esc(e.default or "(omitted)")}</span>',
                    esc(e.spec),
                    pill("yes", "ok") if e.verified else pill("no"),
                )
                for e in cp_entries
            ],
            caption=(
                f"{len(cp_entries)} of {len(catalog.entries)} parameters, "
                f"{catalog.verified_count} cross-checked overall, profile {chosen}"
            ),
            empty="Nothing matched.",
        )
        + "<h3>OMA-DM management nodes</h3>"
        + table(
            ["Node", "Format", "Owner", "Default", "Feature", "Verified"],
            [
                (
                    f'<span class="mono">{esc(n.uri)}</span>',
                    esc(n.format),
                    esc(n.source),
                    f'<span class="mono">{esc(n.default or "—")}</span>',
                    esc(n.feature or "—"),
                    pill("yes", "ok") if n.verified else pill("no"),
                )
                for n in dm_nodes
            ],
            caption=(
                f"{len(dm_nodes)} of {len(tree.all_nodes())} nodes across "
                f"{len(tree.objects)} management objects"
            ),
            empty="Nothing matched.",
        )
    )
    return _render(request, app_state, csrf, "Parameters", body, "catalog")


# --------------------------------------------------------------- conformance
@router.get("/conformance", summary="Conformance registry")
def conformance_view(request: Request) -> Response:
    app_state, csrf = _require_console(request)
    families = load_conformance()

    sections = []
    for family in families:
        counts = family.counts()
        sections.append(
            f"<h3>{esc(family.title)}</h3>"
            f'<p class="hint">{esc(family.spec)} — edition pinned: '
            f"{'yes' if family.spec_edition_pinned else 'no'}</p>"
            + table(
                ["Id", "Requirement", "Level", "Status", "Evidence"],
                [
                    (
                        f'<span class="mono">{esc(r.id)}</span>',
                        esc(r.title),
                        esc(r.level),
                        pill(r.status, "ok" if r.status == "implemented" else "warn")
                        if r.status != "not-implemented"
                        else pill(r.status, "bad"),
                        esc(r.verification),
                    )
                    for r in family.requirements
                ],
                caption=(
                    f"{counts['implemented']} implemented, {counts['partial']} partial, "
                    f"{counts['not-implemented']} not implemented, "
                    f"{len(family.mandatory_gaps)} mandatory gaps"
                ),
            )
        )

    pending = [f for f in load_spec_families() if f.blocks_assessment]
    unassessed = ""
    if pending:
        unassessed = (
            "<h3>Specification families not assessed</h3>"
            '<p class="hint">Asked for, but the document is not held, so no '
            "requirement can be stated. These deliberately carry no requirement "
            "rows — a row would imply a requirement had been read.</p>"
            + table(
                ["Family", "Jurisdiction", "State", "What only the document can answer"],
                [
                    (
                        f'<span class="mono">{esc(f.id)}</span><br>{esc(f.title)}',
                        esc(f.jurisdiction),
                        pill("not assessed", "bad"),
                        "<br>".join(esc(q) for q in f.open_questions),
                    )
                    for f in pending
                ],
                caption=f"{len(pending)} family(ies) awaiting a document",
            )
        )

    body = (
        "<h2>Conformance</h2>"
        '<p class="hint">Requirement by requirement, for both specifications. '
        "Levels are this project&#39;s engineering judgement, not citations. "
        "<strong>Nothing here is certified</strong> and no real handset has been "
        "tested.</p>" + unassessed + "".join(sections)
    )
    return _render(request, app_state, csrf, "Conformance", body, "conformance")
