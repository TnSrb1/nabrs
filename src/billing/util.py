"""Small provider-neutral helpers for the connectors (VG-11.1)."""

from __future__ import annotations

from collections.abc import Mapping


def header_get(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive reading of a header.

    Starlette ``Headers`` are already case-insensitive, but a ``dict`` from unit tests
    is not — we normalize both cases.
    """
    if name in headers:
        return headers[name]
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def test_checkout_url(
    provider: str, plan_key: str, tenant_id: str, success_url: str
) -> str:
    """A deterministic test URL (without contacting the provider, BILLING_LIVE=false).

    We return a link to ``success_url`` with markers — the front/cabinet sees "payment
    passed", there is no real charge (live is enabled in VG-11b).
    """
    sep = "&" if "?" in success_url else "?"
    return (
        f"{success_url}{sep}billing_test=1&provider={provider}&plan={plan_key}"
        f"&tenant={tenant_id}"
    )
