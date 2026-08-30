"""HTML rendering helpers for the operator console.

No template engine and no client-side JavaScript. Every interpolated value goes
through :func:`esc`, which is the only way a value reaches the page — a
subscriber-supplied ``friendly_device_name`` or a management object value read off
a handset is untrusted input, and this console renders both.

The Content-Security-Policy sent with every page forbids scripts outright, so even
a missed escape cannot become script execution.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; img-src 'none'"
)

SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The console displays IMSI, MSISDN and IMEI. None of it may be cached by an
    # intermediary or written to a browser's disk cache.
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
}

STYLE = """
:root{--fg:#1a1a1a;--muted:#5a5a5a;--line:#d8d8d8;--bg:#fff;--accent:#0b5fa5;
--warn:#8a4b00;--bad:#b00020;--ok:#0a6b3d;--chip:#f2f4f7}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
color:var(--fg);background:var(--bg)}
a{color:var(--accent)}
header{border-bottom:1px solid var(--line);padding:.75rem 1.25rem;display:flex;
gap:1.25rem;align-items:baseline;flex-wrap:wrap}
header h1{font-size:1.05rem;margin:0}
nav{display:flex;gap:1rem;flex-wrap:wrap}
nav a{text-decoration:none}
nav a[aria-current=page]{font-weight:700;text-decoration:underline}
header form{margin-left:auto}
main{padding:1.25rem;max-width:76rem;margin:0 auto}
h2{font-size:1.3rem;margin:0 0 .25rem}
h3{font-size:1.05rem;margin:1.75rem 0 .5rem;padding-bottom:.25rem;
border-bottom:1px solid var(--line)}
p.hint{color:var(--muted);margin:.25rem 0 1rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem;font-size:.94rem}
caption{text-align:left;color:var(--muted);padding-bottom:.4rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid var(--line);
vertical-align:top}
th{background:var(--chip);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
code,td.mono,input,select{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
form.inline{display:inline}
fieldset{border:1px solid var(--line);margin:0 0 1.25rem;padding:1rem}
legend{padding:0 .4rem;font-weight:600}
label{display:block;margin:.6rem 0 .15rem;font-weight:600;font-size:.92rem}
input,select{padding:.4rem;font-size:.95rem;max-width:34rem;width:100%}
input[type=checkbox]{width:auto}
.row{display:flex;gap:1rem;flex-wrap:wrap}
.row>div{flex:1 1 16rem}
button{margin-top:.9rem;padding:.45rem 1rem;font-size:.95rem;cursor:pointer}
button.danger{color:var(--bad)}
.flash{padding:.6rem .9rem;border-left:4px solid var(--ok);background:#eef7f1;
margin-bottom:1rem}
.flash.error{border-color:var(--bad);background:#fdeef0}
.pill{display:inline-block;padding:.05rem .5rem;border-radius:999px;
font-size:.8rem;background:var(--chip)}
.pill.bad{background:#fdeef0;color:var(--bad)}
.pill.ok{background:#eef7f1;color:var(--ok)}
.pill.warn{background:#fdf3e7;color:var(--warn)}
.skip{position:absolute;left:-9999px}
.skip:focus{left:.5rem;top:.5rem;background:#fff;padding:.5rem;z-index:1}
footer{border-top:1px solid var(--line);margin-top:2rem;padding:1rem 1.25rem;
color:var(--muted);font-size:.85rem}
"""


def esc(value: object) -> str:
    """Escape any value for HTML text or an attribute.

    Everything rendered by the console passes through here, including values a
    handset reported over OMA-DM.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def page(
    title: str,
    body: str,
    *,
    current: str = "",
    flash: str = "",
    flash_error: bool = False,
    signed_in: bool = True,
    csrf: str = "",
    environment: str = "",
) -> str:
    """Wrap a body fragment in the console chrome."""
    nav_items = (
        ("/admin/ui", "Overview", "overview"),
        ("/admin/ui/subscribers", "Numbers", "subscribers"),
        ("/admin/ui/devices", "Devices", "devices"),
        ("/admin/ui/catalog", "Parameters", "catalog"),
        ("/admin/ui/conformance", "Conformance", "conformance"),
    )
    nav = ""
    logout = ""
    if signed_in:
        nav = "".join(
            f'<a href="{esc(href)}"'
            + (' aria-current="page"' if key == current else "")
            + f">{esc(label)}</a>"
            for href, label, key in nav_items
        )
        logout = (
            '<form method="post" action="/admin/ui/logout">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            '<button type="submit">Sign out</button></form>'
        )

    banner = ""
    if flash:
        css = "flash error" if flash_error else "flash"
        role = 'role="alert"' if flash_error else 'role="status"'
        banner = f'<div class="{css}" {role}>{esc(flash)}</div>'

    env_pill = f'<span class="pill">{esc(environment)}</span>' if environment else ""

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} — RCS ACS console</title>"
        f"<style>{STYLE}</style></head><body>"
        '<a class="skip" href="#main">Skip to content</a>'
        f"<header><h1>RCS ACS console</h1>{env_pill}"
        f'<nav aria-label="Sections">{nav}</nav>{logout}</header>'
        f'<main id="main">{banner}{body}</main>'
        "<footer>Displays IMSI, MSISDN and IMEI. Pages are never cached and the "
        "session cookie expires. Nothing here is a GSMA certification; see "
        "docs/conformance.md.</footer>"
        "</body></html>"
    )


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    caption: str = "",
    empty: str = "Nothing to show.",
    numeric: Sequence[int] = (),
) -> str:
    """Render a table. Cell contents must already be escaped or safe markup."""
    body_rows = [
        "<tr>"
        + "".join(
            f'<td class="{"num" if index in numeric else ""}">{cell}</td>'
            for index, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    ]
    if not body_rows:
        return f"<p>{esc(empty)}</p>"
    head = "".join(f'<th scope="col">{esc(h)}</th>' for h in headers)
    caption_html = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        f"<table>{caption_html}<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def definition_list(pairs: Sequence[tuple[str, str]]) -> str:
    rows = "".join(
        f'<tr><th scope="row">{esc(name)}</th><td>{value}</td></tr>' for name, value in pairs
    )
    return f"<table><tbody>{rows}</tbody></table>"


def pill(text: str, kind: str = "") -> str:
    css = f"pill {kind}".strip()
    return f'<span class="{css}">{esc(text)}</span>'


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def text_field(
    name: str,
    label: str,
    value: str = "",
    *,
    hint: str = "",
    input_type: str = "text",
    required: bool = False,
    placeholder: str = "",
) -> str:
    field_id = f"f-{esc(name)}"
    described = f' aria-describedby="{field_id}-hint"' if hint else ""
    hint_html = f'<p class="hint" id="{field_id}-hint">{esc(hint)}</p>' if hint else ""
    required_attr = " required" if required else ""
    placeholder_attr = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    return (
        f'<label for="{field_id}">{esc(label)}</label>'
        f'<input id="{field_id}" name="{esc(name)}" type="{esc(input_type)}" '
        f'value="{esc(value)}"{required_attr}{placeholder_attr}{described}>'
        f"{hint_html}"
    )


def select_field(
    name: str,
    label: str,
    options: Sequence[tuple[str, str]],
    selected: str = "",
    hint: str = "",
) -> str:
    field_id = f"f-{esc(name)}"
    rendered = "".join(
        f'<option value="{esc(value)}"'
        + (" selected" if value == selected else "")
        + f">{esc(text)}</option>"
        for value, text in options
    )
    hint_html = f'<p class="hint">{esc(hint)}</p>' if hint else ""
    return (
        f'<label for="{field_id}">{esc(label)}</label>'
        f'<select id="{field_id}" name="{esc(name)}">{rendered}</select>{hint_html}'
    )


def checkbox_field(name: str, label: str, checked: bool) -> str:
    field_id = f"f-{esc(name)}"
    return (
        f'<label for="{field_id}">'
        f'<input id="{field_id}" name="{esc(name)}" type="checkbox" value="1"'
        f'{" checked" if checked else ""}> {esc(label)}</label>'
    )
