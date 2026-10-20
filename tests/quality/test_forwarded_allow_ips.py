"""The guard of the CONFIG (not of the behavior): the api behind Caddy must receive FORWARDED_ALLOW_IPS.

The honest name: the test reads ``deploy/docker-compose.yml`` as data and checks that the service ``api`` has in ``environment`` the variable ``FORWARDED_ALLOW_IPS`` declared with a non-empty value. It does NOT prove that uvicorn really substituted the client’s IP: this is visible only in the log of api on production (see docs/reports/TESTING_real-client-ip.md).

Why. Without the variable uvicorn trusts X-Forwarded-For only from 127.0.0.1, while Caddy goes from an IP of the docker bridge subnet. Then ``request.client.host`` is the same for all clients, and the limits “per IP” (login, contact, admin) become shared across all.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"


def _api_environment() -> dict:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return compose["services"]["api"]["environment"]


def test_api_declares_forwarded_allow_ips():
    env = _api_environment()
    assert (
        "FORWARDED_ALLOW_IPS" in env
    ), "api without FORWARDED_ALLOW_IPS: the client’s IP behind Caddy is not visible, the limits are shared"


def test_forwarded_allow_ips_default_covers_docker_bridge_caddy():
    # The default in compose is of the form ${FORWARDED_ALLOW_IPS:-<CIDR>}; we take the CIDR as a literal.
    value = _api_environment()["FORWARDED_ALLOW_IPS"]
    assert value == "${FORWARDED_ALLOW_IPS:-172.16.0.0/12}", value
    # Caddy on the VPS: 172.18.0.13 (the network deploy_tendersrb, the fact 13.10).
    assert ipaddress.ip_address("172.18.0.13") in ipaddress.ip_network("172.16.0.0/12")
