"""MSISDN entry web flow.

When the ACS answers 511 the client may open this page so the user can type their
number and the OTP. It is deliberately plain server-rendered HTML: no inline
script (a strict CSP is sent), explicit ``<label>`` elements, ``inputmode`` hints
and ``aria-describedby`` for error text, so it works with screen readers and on a
handset's embedded browser.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Cookie, Form, Request, Response

from acs.api.deps import AppState
from acs.auth import otp as otp_mod
from acs.observability import get_logger
from acs.security.pii import normalise_msisdn
from acs.sms.base import SmsRequest, UnsupportedDelivery

log = get_logger(__name__)
router = APIRouter(tags=["msisdn-flow"])

CSRF_COOKIE = "acs_csrf"

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem}"
    "label{display:block;font-weight:600;margin-top:1rem}"
    "input{width:100%;padding:.5rem;font-size:1rem;margin-top:.25rem}"
    "button{margin-top:1.5rem;padding:.6rem 1.2rem;font-size:1rem}"
    ".error{color:#b00020}"
)


def _page(title: str, body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{title}</h1>{body}</main></body></html>"
    )


def _html(
    content: str,
    csrf: str | None = None,
    status_code: int = 200,
    secure_cookie: bool = True,
) -> Response:
    response = Response(
        content=content,
        media_type="text/html; charset=utf-8",
        status_code=status_code,
        headers=dict(_SECURITY_HEADERS),
    )
    if csrf:
        # Secure is set whenever the request itself was HTTPS. Marking the cookie
        # Secure on a plain-HTTP local run would make the browser drop it and the
        # form would fail its own CSRF check.
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            httponly=True,
            samesite="strict",
            secure=secure_cookie,
            max_age=900,
        )
    return response


def _check_csrf(form_token: str, cookie_token: str | None) -> bool:
    return bool(cookie_token) and hmac.compare_digest(form_token, cookie_token or "")


@router.get("/msisdn", summary="MSISDN entry form")
def msisdn_form(request: Request) -> Response:
    csrf = secrets.token_urlsafe(24)
    body = (
        "<p>Enter the phone number of the SIM in this device to activate messaging.</p>"
        '<form method="post" action="/msisdn">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<label for="msisdn">Phone number (international format)</label>'
        '<input id="msisdn" name="msisdn" type="tel" inputmode="tel" autocomplete="tel" '
        'placeholder="+821012345678" required aria-describedby="msisdn-help">'
        '<p id="msisdn-help">Include the country code, for example +821012345678.</p>'
        '<button type="submit">Send code</button></form>'
    )
    return _html(
        _page("Activate messaging", body),
        csrf=csrf,
        secure_cookie=request.url.scheme == "https",
    )


@router.post("/msisdn", summary="Request an OTP for the entered MSISDN")
def msisdn_submit(
    request: Request,
    msisdn: str = Form(...),
    csrf: str = Form(...),
    acs_csrf: str | None = Cookie(default=None),
) -> Response:
    app_state: AppState = request.app.state.acs

    if not _check_csrf(csrf, acs_csrf):
        return _html(
            _page(
                "Activate messaging", '<p class="error">Session expired. Please start again.</p>'
            ),
            status_code=400,
        )

    normalised = normalise_msisdn(
        msisdn,
        app_state.settings.default_country_code,
        app_state.settings.national_trunk_prefix,
    )
    if normalised is None:
        return _html(
            _page(
                "Activate messaging",
                '<p class="error" id="err">That does not look like a valid number.</p>'
                '<p><a href="/msisdn">Try again</a></p>',
            ),
            status_code=400,
        )

    subscriber = app_state.store.get_subscriber_by_msisdn(normalised)
    # The response is identical whether or not the number is known, so this page
    # cannot be used to test which numbers exist on the network.
    if subscriber is not None:
        try:
            _, clear_otp = otp_mod.create_challenge(
                store=app_state.store,
                msisdn=normalised,
                imsi=subscriber.imsi,
                policy=otp_mod.policy_from_settings(app_state.settings),
            )
            app_state.sms.send(
                SmsRequest(
                    msisdn=normalised,
                    body=app_state.settings.sms_otp_template.format(otp=clear_otp),
                    sender_id=app_state.settings.sms_sender_id,
                )
            )
        except (otp_mod.SendBlocked, UnsupportedDelivery) as exc:
            log.info("msisdn flow otp not sent", extra={"reason": str(exc)})

    new_csrf = secrets.token_urlsafe(24)
    body = (
        "<p>If that number is eligible, a code has been sent to it.</p>"
        '<form method="post" action="/msisdn/verify">'
        f'<input type="hidden" name="csrf" value="{new_csrf}">'
        f'<input type="hidden" name="msisdn" value="{normalised}">'
        '<label for="otp">Activation code</label>'
        '<input id="otp" name="otp" type="text" inputmode="numeric" autocomplete="one-time-code" '
        'pattern="[0-9]*" required aria-describedby="otp-help">'
        '<p id="otp-help">The code expires in a few minutes.</p>'
        '<button type="submit">Verify</button></form>'
    )
    return _html(
        _page("Enter your code", body),
        csrf=new_csrf,
        secure_cookie=request.url.scheme == "https",
    )


@router.post("/msisdn/verify", summary="Verify the OTP entered by the user")
def msisdn_verify(
    request: Request,
    msisdn: str = Form(...),
    otp: str = Form(...),
    csrf: str = Form(...),
    acs_csrf: str | None = Cookie(default=None),
) -> Response:
    app_state: AppState = request.app.state.acs

    if not _check_csrf(csrf, acs_csrf):
        return _html(
            _page("Enter your code", '<p class="error">Session expired. Please start again.</p>'),
            status_code=400,
        )

    normalised = normalise_msisdn(
        msisdn,
        app_state.settings.default_country_code,
        app_state.settings.national_trunk_prefix,
    )
    if normalised is None or not otp.isalnum():
        return _html(
            _page("Enter your code", '<p class="error">Invalid submission.</p>'), status_code=400
        )

    outcome = otp_mod.verify_challenge(
        app_state.store, normalised, otp, otp_mod.policy_from_settings(app_state.settings)
    )
    if outcome == otp_mod.VERIFIED:
        body = (
            "<p>Your number is verified. Messaging will finish setting up on this device "
            "shortly.</p>"
        )
        return _html(_page("Verified", body))

    body = (
        '<p class="error">That code was not accepted.</p>'
        '<p><a href="/msisdn">Start again</a></p>'
    )
    return _html(_page("Enter your code", body), status_code=400)
