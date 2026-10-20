"""The filters and sorting of the public portal ``/tenderi`` (TASK portal_filters).

Parsing of the GET parameters — ONLY by allow-list: an unknown key / value is silently ignored (the portal is public, input from anonymous users). The module is pure (no DB): the SQL assembly lives in :mod:`src.api.repo`, and the ready :class:`PortalFilters` comes here.

The normalization of the search text is the same :func:`src.domain.text_norm.normalize_text` that ingest writes to ``title_normalized`` / ``description_normalized`` and that folds the matching keywords: “škola” / “skola” / the Cyrillic spelling of the same word give one token ``skola``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from src.domain.regions import OKRUG_CODES
from src.domain.text_norm import normalize_text

#: The valid sort keys (key → a fixed ORDER BY in ``repo``).
SORT_KEYS: tuple[str, ...] = ("deadline", "published_desc", "amount_desc", "amount_asc")
DEFAULT_SORT = "deadline"

MAX_Q_LEN = 120
MAX_Q_TOKENS = 5
#: Shorter tokens are noise (`strpos` on one character matches almost everywhere and is heavy).
MIN_Q_TOKEN_LEN = 2
MAX_CUSTOMER_LEN = 80
#: The amount ceiling (EUR): anything larger is input garbage, not a filter.
MAX_AMOUNT = Decimal("1000000000000")


@dataclass(frozen=True)
class PortalFilters:
    """The validated search parameters. An empty value = “not set”."""

    q: str = ""
    #: The folded ``q`` tokens (AND between the tokens).
    q_tokens: tuple[str, ...] = ()
    #: The same tokens WITHOUT the iekavian step — for comparison with the SQL fold of the customer.
    q_tokens_plain: tuple[str, ...] = ()
    customer: str = ""
    customer_folded: str = ""
    region: str = ""
    source: str = ""
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    no_amount: bool = False
    published_from: date | None = None
    published_to: date | None = None
    actual: bool = True
    sort: str = DEFAULT_SORT
    #: The default sorting (not written in the links). The portal has ``deadline``,
    #: the cabinet feed has ``date``.
    default_sort: str = DEFAULT_SORT
    #: The cabinet feed only (``/app/feed``): the bid deadline not later than the date, and the template id.
    deadline_to: date | None = None
    profile_id: str = ""
    #: It is kept in the links but is not counted as a filter (the interface language axis).
    lang: str = ""
    #: Auxiliary: the labels of the sources for the chips (source_key → label).
    source_labels: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def active(self) -> bool:
        """Whether at least one filter / sorting differs from the default values."""
        return bool(self._pairs(include_lang=False))

    @property
    def min_amount_str(self) -> str:
        return _fmt_amount(self.min_amount) if self.min_amount is not None else ""

    @property
    def max_amount_str(self) -> str:
        return _fmt_amount(self.max_amount) if self.max_amount is not None else ""

    @property
    def has_amount_bounds(self) -> bool:
        return self.min_amount is not None or self.max_amount is not None

    def _pairs(
        self, *, drop: tuple[str, ...] = (), include_lang: bool = True
    ) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.q and "q" not in drop:
            pairs.append(("q", self.q))
        if self.customer and "customer" not in drop:
            pairs.append(("customer", self.customer))
        if self.region and "region" not in drop:
            pairs.append(("region", self.region))
        if self.source and "source" not in drop:
            pairs.append(("source", self.source))
        if self.min_amount is not None and "min_amount" not in drop:
            pairs.append(("min_amount", _fmt_amount(self.min_amount)))
        if self.max_amount is not None and "max_amount" not in drop:
            pairs.append(("max_amount", _fmt_amount(self.max_amount)))
        if self.no_amount and self.has_amount_bounds and "no_amount" not in drop:
            pairs.append(("no_amount", "1"))
        if self.published_from is not None and "published_from" not in drop:
            pairs.append(("published_from", self.published_from.isoformat()))
        if self.published_to is not None and "published_to" not in drop:
            pairs.append(("published_to", self.published_to.isoformat()))
        if self.deadline_to is not None and "deadline_to" not in drop:
            pairs.append(("deadline_to", self.deadline_to.isoformat()))
        if self.profile_id and "profile_id" not in drop:
            pairs.append(("profile_id", self.profile_id))
        if not self.actual and "actual" not in drop:
            pairs.append(("actual", "0"))
        if self.sort != self.default_sort and "sort" not in drop:
            pairs.append(("sort", self.sort))
        if include_lang and self.lang:
            pairs.append(("lang", self.lang))
        return pairs

    def query(self, *, drop: tuple[str, ...] = (), page: int | None = None) -> str:
        """The query string from a link (“?…” or empty) without the default parameters."""
        pairs = self._pairs(drop=drop)
        if page is not None and page > 1:
            pairs.append(("page", str(page)))
        return f"?{urlencode(pairs)}" if pairs else ""

    def chips(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """The active filters: ``(kind, value, keys for drop)`` — a label in the template."""
        out: list[tuple[str, str, tuple[str, ...]]] = []
        if self.q:
            out.append(("q", self.q, ("q",)))
        if self.customer:
            out.append(("customer", self.customer, ("customer",)))
        if self.region:
            out.append(("region", self.region, ("region",)))
        if self.source:
            label = self.source_labels.get(self.source) or self.source
            out.append(("source", label, ("source",)))
        if self.min_amount is not None:
            out.append(("min_amount", _fmt_amount(self.min_amount), ("min_amount",)))
        if self.max_amount is not None:
            out.append(("max_amount", _fmt_amount(self.max_amount), ("max_amount",)))
        if self.no_amount and self.has_amount_bounds:
            out.append(("no_amount", "", ("no_amount",)))
        if self.published_from is not None:
            out.append(
                (
                    "published_from",
                    self.published_from.isoformat(),
                    ("published_from",),
                )
            )
        if self.published_to is not None:
            out.append(
                ("published_to", self.published_to.isoformat(), ("published_to",))
            )
        if self.deadline_to is not None:
            out.append(("deadline_to", self.deadline_to.isoformat(), ("deadline_to",)))
        if self.profile_id:
            out.append(("profile", self.profile_id, ("profile_id",)))
        if not self.actual:
            out.append(("all_dates", "", ("actual",)))
        if self.sort != self.default_sort:
            out.append(("sort", self.sort, ("sort",)))
        return out

    @property
    def details_open(self) -> bool:
        """Whether to expand the “Filters” block (at least one of the nested fields is set)."""
        return any(
            (
                self.customer,
                self.region,
                self.source,
                self.has_amount_bounds,
                self.published_from,
                self.published_to,
                self.deadline_to,
                self.profile_id,
                not self.actual,
            )
        )


def _fmt_amount(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def _parse_amount(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    try:
        value = Decimal(raw.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def parse_filters(
    params,
    *,
    source_keys: dict[str, str] | None = None,
    allow_region: bool = True,
    sort_keys: tuple[str, ...] = SORT_KEYS,
    default_sort: str = DEFAULT_SORT,
    profile_ids: frozenset[str] | None = None,
    allow_deadline_to: bool = False,
) -> PortalFilters:
    """GET parameters → :class:`PortalFilters` (allow-list; the unknown is ignored).

    ``params`` — ``request.query_params`` or any mapping with ``get``. ``source_keys`` — the enabled sources ``source_key → label``; a key outside the set is ignored. ``allow_region=False`` — on ``/tenderi/region/{code}`` the region is set by the path, so the ``region`` parameter is not accepted.
    The cabinet feed passes its own ``sort_keys`` / ``default_sort``, ``profile_ids`` (the tenant’s templates — a foreign / unknown id is ignored) and ``allow_deadline_to``; for the portal all of this stays at the default, and the ``profile_id`` / ``deadline_to`` parameters are not read from the query.
    """

    def get(key: str) -> str:
        value = params.get(key)
        return value if isinstance(value, str) else ""

    q = " ".join(get("q").split())[:MAX_Q_LEN]
    folded = normalize_text(q) or ""
    plain_words = (normalize_text(q, ijekavica=False) or "").split()
    pairs = [
        (w, p) for w, p in zip(folded.split(), plain_words) if len(w) >= MIN_Q_TOKEN_LEN
    ][:MAX_Q_TOKENS]
    tokens = tuple(w for w, _ in pairs)
    tokens_plain = tuple(p for _, p in pairs)

    customer = " ".join(get("customer").split())[:MAX_CUSTOMER_LEN]
    customer_folded = normalize_text(customer, ijekavica=False) or ""

    region = get("region").strip()
    if not allow_region or region not in OKRUG_CODES:
        region = ""

    labels = source_keys or {}
    source = get("source").strip()
    if source not in labels:
        source = ""

    min_amount = _parse_amount(get("min_amount"))
    max_amount = _parse_amount(get("max_amount"))
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        min_amount, max_amount = max_amount, min_amount

    sort = get("sort").strip()
    if sort not in sort_keys:
        sort = default_sort

    profile_id = get("profile_id").strip()
    if profile_ids is None or profile_id not in profile_ids:
        profile_id = ""
    deadline_to = _parse_date(get("deadline_to")) if allow_deadline_to else None

    lang = get("lang").strip()
    if lang not in ("sr", "ru", "en"):
        lang = ""

    return PortalFilters(
        q=q if tokens else "",
        q_tokens=tokens,
        q_tokens_plain=tokens_plain,
        customer=customer if customer_folded else "",
        customer_folded=customer_folded,
        region=region,
        source=source,
        min_amount=min_amount,
        max_amount=max_amount,
        no_amount=get("no_amount") == "1",
        published_from=_parse_date(get("published_from")),
        published_to=_parse_date(get("published_to")),
        actual=get("actual") != "0",
        sort=sort,
        default_sort=default_sort,
        deadline_to=deadline_to,
        profile_id=profile_id,
        lang=lang,
        source_labels=dict(labels),
    )
