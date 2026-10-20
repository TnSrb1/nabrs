"""Single traversal of the app’s effective routes — shared by all route guards.

WHY ONE HELPER. fastapi below 0.14x flattens `include_router` immediately:
`app.routes` already carries flat `APIRoute` objects with the final `.dependant`/`.methods`.
fastapi 0.14x+ made `include_router` lazy (`_IncludedRouter` in
`app.routes`) — the real paths, methods and dependant of nested routers are visible
only through the public `fastapi.routing.iter_route_contexts`. Without it a guard
walks `app.routes` directly and sees an EMPTY app (routes exist, but they are not
`APIRoute` and have no `.dependant`) — and silently stops checking anything. The gap
has already been caught by two independent guards (`test_admin_login.py`,
`test_mutating_get_invariant.py`); to keep the traversal from drifting through copy-paste,
the source is one — this module.

FastAPI adds docs/openapi/redoc routes as a plain starlette `Route`, not an
`APIRoute` — the `original_route` filter excludes them the same way that
`isinstance(route, APIRoute)` used to on the flattened `app.routes`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


@dataclass(frozen=True)
class EffectiveRoute:
    path: str
    methods: frozenset[str]
    dependant: Any


def iter_effective_routes(application: FastAPI) -> Iterator[EffectiveRoute]:
    """(path, methods, dependant) for each real APIRoute of the app."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        for route in application.routes:
            if not isinstance(route, APIRoute):
                continue
            yield EffectiveRoute(
                route.path, frozenset(route.methods or ()), route.dependant
            )
        return

    for context in iter_route_contexts(application.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        path = context.path
        dependant = getattr(context, "dependant", None)
        if path is None or dependant is None:
            continue
        yield EffectiveRoute(path, frozenset(context.methods or ()), dependant)
