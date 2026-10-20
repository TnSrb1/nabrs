"""Translation of a tender card sr→ru over the original, with a Redis cache (VG-26).

The content from eNabavke arrives in Serbian, while the cabinet and the bot are only ru: the subscriber
reads the tender in a foreign language. The translation is a **display layer**: the original in
``tenders_normalized`` is not overwritten, matching/routing work on the original, the “Original (sr)”
toggle in the card returns the source text.

Two invariants without which the task makes no sense:

1. **The cache is mandatory.** A repeated display of the card costs not a single token:
   the translation goes into Redis (``deploy-redis-1`` is already in the stack — no migration).
   The key is ``(tender_id, target_lang, text fingerprint)``. The fingerprint in the key, and not only the id: the tender on the portal is edited (``last_changed_at``), and an old translation
   next to a toggle showing the new original is a visible lie. The text did not change → the same key → a hit, no tokens spent.
2. **AI failure → the original, not an empty page and not a 500.** The translation is an improvement; any
   error of the model/Redis degrades to the original Serbian text.
3. **The spend is returned upwards.** The module knows neither the tenant nor the DB session,
   so it does not touch the limit/``ai_usage`` — but it must report HOW MUCH was actually spent (:class:`TranslationUsage`), otherwise the translation stays outside
   the accounting contour and burns the budget silently.

The cache key does NOT contain tenant_id — and this is intentional. The translation is a pure function of the
public tender text (``tenders_normalized`` is a shared catalog, not
tenant-scoped, unlike ``tender_ai_analyses``), therefore a shared cache for all
tenants is what makes the task cheap. Access is checked BEFORE the translation
(``get_tender_analyses`` → None → 404): a foreign tender is neither translated nor
given from the cache. Nothing profile- or subscriber-related is mixed into the translation — otherwise the
key would have to include tenant_id.

Dependencies (redis, AI client) are injected — unit tests run without network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.ai import client as ai_client

logger = logging.getLogger(__name__)

# The version in the prefix: a change of the prompt/schema must invalidate the old cache
# (otherwise subscribers see for a month a translation made by the old prompt).
_CACHE_PREFIX = "ai:tender_tr:v1"

# 30 days: a tender lives until its deadline, there is no point in keeping the translation longer.
CACHE_TTL_SECONDS = 30 * 24 * 3600

# Target interface language of the cabinet/bot (the project canon — ru only).
DEFAULT_TARGET_LANG = "ru"

# Language code → name for the prompt (models understand “ru” better as “Russian”).
_LANG_NAMES = {"ru": "русский", "en": "английский", "sr": "сербский"}

# Input ceiling: a long specification is cut — the card shows the essence, not the whole volume.
# Further the subscriber follows the link to the portal (source_url) for the original.
_MAX_INPUT_CHARS = 6000

# The translation is done synchronously when the card is opened, so it has ITS OWN ceiling
# of waiting. Without it the card would hang on the failover backoff of the AI client
# (30s/2m/10m, §14.7) — minutes of a white screen instead of an instant original.
DEFAULT_TIMEOUT_SECONDS = 20.0

# --------------------------------------------------------------------------- #
#  Translation of the tender DESCRIPTION in chunks (VG-26)
#
#  ``translate_tender`` translates the title+description in ONE call. For the description
#  this did not work: real specifications are 46–50 KB, generating 6000 characters on the cheap-
#  model did not fit in 20 s → TimeoutError → silently the original (bug #414).
#
#  Here the description is split by paragraph boundaries into short chunks, the chunks are translated
#  IN PARALLEL (no more than ``_CHUNK_CONCURRENCY`` at a time), each with its own
#  ceiling and its own cache (by the fingerprint of the chunk’s TEXT — without tender_id, as with
#  ``translate_tender``: the translation is a pure function of the public text). The fragment
#  is loaded in the background (htmx ``hx-trigger="load"``), therefore the detail is rendered instantly
#  and always, and a timeout of one chunk does not break the others.
# --------------------------------------------------------------------------- #

# Its own cache version: the key scheme of the description differs from title-only, raising the
# version invalidates the old cache when the prompt/splitting is changed.
_DESC_CACHE_PREFIX = "ai:tender_desc_tr:v1"

# Input ceiling of the description. The card shows the essence; for the full volume the subscriber
# goes to ``source_url`` on the portal. Higher than for title-only (6000): the description is the
# main text of the detail, but still not unlimited (12000 ≈ 5–6 chunks).
_DESC_MAX_INPUT_CHARS = 12000

# Chunk size: a short input for the cheap model fits the ceiling with a margin.
_CHUNK_MAX_CHARS = 2500

# Ceiling for ONE chunk and for the whole fragment. The overall ceiling < the sum of the chunks: with
# parallelism (4 at a time) 5–6 chunks make it in about 40 s, but we insure.
_CHUNK_TIMEOUT_SECONDS = 20.0
_DESC_TOTAL_TIMEOUT_SECONDS = 60.0

# No more than 4 chunks to the model at a time — we spare both the rate limit and the budget.
_CHUNK_CONCURRENCY = 4


@dataclass(frozen=True)
class TenderTranslation:
    """The translated layer of the card: only the text fields, no metadata."""

    title: str
    description: str


@dataclass(frozen=True)
class TranslationUsage:
    """The actual spend of the translation — what was actually paid for.

    Returned as the SECOND element of the tuple from :func:`translate_tender` and
    :func:`translate_description`, so that the caller can debit the limit and write
    ``ai_usage``: without it the translation stayed outside the accounting contour (the module knows
    neither the tenant nor the DB session — and should not).

    Rules:

    - **cache hit → zeros.** No money was spent, the limit must not be spent: otherwise a repeated
      display of an already translated card would eat the quota.
    - **partial failure of the description → the spend for the successful chunks.** The original
      as a whole goes upwards, but the money for the translated chunks is spent, and the limit
      must see it.
    - **a failure of the metrics call gives no spend** (there is no answer) — such a call does not
      get into the spend; this is a deliberate lower estimate, not “free”.

    ``calls`` — the number of REAL calls to the model (chunks), NOT limit units:
    the limit unit (“one user translation”) is counted by the caller.
    """

    calls: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0


# Spend that did not happen: cache hit, redis=None, nothing to translate, model refusal.
_NO_USAGE = TranslationUsage()


def _sum_usage(parts: list[TranslationUsage]) -> TranslationUsage:
    """Total spend over the chunks of the description (an empty list → zeros)."""
    if not parts:
        return _NO_USAGE
    return TranslationUsage(
        calls=sum(p.calls for p in parts),
        tokens_input=sum(p.tokens_input for p in parts),
        tokens_output=sum(p.tokens_output for p in parts),
        cost_usd=sum(p.cost_usd for p in parts),
    )


def _fingerprint(title: str | None, description: str | None) -> str:
    """Short fingerprint of the translated text (see the module docstring, item 1)."""
    payload = f"{title or ''}\x00{description or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def cache_key(tender_id: str, target_lang: str, fingerprint: str) -> str:
    """Translation cache key: ``ai:tender_tr:v1:{tender_id}:{lang}:<fingerprint>``."""
    return f"{_CACHE_PREFIX}:{tender_id}:{target_lang}:{fingerprint}"


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:_MAX_INPUT_CHARS]


async def _cache_get(redis: Any, key: str) -> TenderTranslation | None:
    """Reads the translation from Redis. Unavailability/garbage in the value → a miss."""
    try:
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — Redis is down: this is a miss, not a 500
        logger.warning("translate: Redis GET unavailable (%s)", type(exc).__name__)
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
        return TenderTranslation(
            title=str(data["title"]), description=str(data["description"])
        )
    except (ValueError, TypeError, KeyError):
        logger.warning("translate: corrupt cache value key=%s → miss", key)
        return None


async def _cache_put(redis: Any, key: str, tr: TenderTranslation) -> None:
    """Puts the translation into Redis with a TTL. A write error does not break the display of the card."""
    try:
        await redis.set(
            key,
            json.dumps(
                {"title": tr.title, "description": tr.description}, ensure_ascii=False
            ),
            ex=CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — the translation is ready already, we show it
        logger.warning("translate: Redis SET unavailable (%s)", type(exc).__name__)


_DEFAULT_AI_CLIENT: ai_client.AiClient | None = None


def _get_default_ai_client() -> ai_client.AiClient:
    """A shared client per process: one connection pool for all clicks, not a new one for each.

    Without it every display of “Translate” made ``AiClient()`` → a new ``AsyncAnthropic``
    (the pool and the TLS handshake again). Pattern — ``tender_ai_generate._get_default_ai_client``.
    """
    global _DEFAULT_AI_CLIENT
    if _DEFAULT_AI_CLIENT is None:
        _DEFAULT_AI_CLIENT = ai_client.AiClient()
    return _DEFAULT_AI_CLIENT


def warm_default_client() -> None:
    """Warm-up at process start: import of the SDK and the shared client BEFORE the first click.

    Makes no network calls (money/limits). An error (no key, no package)
    is raised outward — the caller decides: the application start must not fail.
    """
    _get_default_ai_client().warm()


async def translate_tender(
    *,
    tender_id: str,
    title: str | None,
    description: str | None,
    redis: Any | None,
    client: Any | None = None,
    target_lang: str = DEFAULT_TARGET_LANG,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[TenderTranslation | None, TranslationUsage]:
    """Translation of the card into ``target_lang``: cache → model → cache.

    Returns a pair ``(translation | None, spend)``. ``None`` in the first element — in
    any case when there is no translation: nothing to translate, the model is
    unavailable/answered with garbage, the level is not configured. The caller
    shows the original (see the module docstring, item 2) — exceptions do not
    escape.

    The second element (:class:`TranslationUsage`) is needed by the caller to debit
    the limit and write ``ai_usage``: the spend is non-zero ONLY when the model was really
    called (cache hit/refusal/nothing to translate → zeros).

    ``redis=None`` (cache not configured) — also ``None``, WITHOUT calling the model:
    without a cache every display of the card would burn tokens again, which is a direct loss.
    """
    if not (title or description):
        return None, _NO_USAGE

    if redis is None:
        logger.warning("translate: Redis not configured → showing the original")
        return None, _NO_USAGE

    src_title, src_desc = _truncate(title), _truncate(description)
    key = cache_key(str(tender_id), target_lang, _fingerprint(src_title, src_desc))

    cached = await _cache_get(redis, key)
    if cached is not None:
        logger.info("translate: tender=%s part=title cached=1 elapsed_ms=0", tender_id)
        return cached, _NO_USAGE  # a repeated display is free — we do not touch the limit

    ai = client if client is not None else _get_default_ai_client()
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            ai.analyze(
                ai_client.AnalysisRequest(
                    level="translate",
                    title=src_title,
                    description=src_desc,
                    target_language=_LANG_NAMES.get(target_lang, target_lang),
                )
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — the translation is not critical: we degrade
        logger.warning(
            "translate: model unavailable tender=%s (%s) → original",
            tender_id,
            type(exc).__name__,
        )
        return None, _NO_USAGE

    tr = TenderTranslation(
        title=str(result.output.get("title") or ""),
        description=str(result.output.get("description") or ""),
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "translate: tender=%s part=title cached=0 elapsed_ms=%d", tender_id, elapsed_ms
    )
    await _cache_put(redis, key, tr)
    logger.info(
        "translate: tender=%s lang=%s tokens in=%d out=%d cost=$%.4f",
        tender_id,
        target_lang,
        result.tokens_input,
        result.tokens_output,
        result.cost_usd,
    )
    return tr, TranslationUsage(
        calls=1,
        tokens_input=result.tokens_input,
        tokens_output=result.tokens_output,
        cost_usd=result.cost_usd,
    )


# --------------------------------------------------------------------------- #
#  Description in chunks
# --------------------------------------------------------------------------- #


def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Splits the text by paragraph boundaries (``\\n\\n``) into pieces ≤ ``max_chars``.

    Paragraphs are greedily packed into a chunk while they fit; a single paragraph longer than the
    limit is cut at a character boundary (the order is preserved — the join ``"\\n\\n".join`` is reversible).
    An empty/whitespace input → an empty list.
    """
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        # A single paragraph longer than the limit — we cut by characters (we preserve the order).
        while len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        if not para:
            continue
        candidate = f"{buf}\n\n{para}" if buf else para
        if buf and len(candidate) > max_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks


def _desc_cache_key(target_lang: str, fingerprint: str) -> str:
    """Cache key of a description chunk: ``ai:tender_desc_tr:v1:{lang}:<fingerprint>``.

    The fingerprint is from the TEXT of the chunk (not from tender_id): an identical paragraph in two
    tenders is translated once. tenant_id is deliberately not in the key (see the module
    docstring): the translation is a pure function of the public text.
    """
    return f"{_DESC_CACHE_PREFIX}:{target_lang}:{fingerprint}"


async def _cache_get_text(redis: Any, key: str) -> str | None:
    """Reads a translated chunk from Redis. Unavailability/garbage → a miss."""
    try:
        raw = await redis.get(key)
    except Exception as exc:  # noqa: BLE001 — Redis is down: this is a miss, not a 500
        logger.warning("translate: Redis GET unavailable (%s)", type(exc).__name__)
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw


async def _cache_put_text(redis: Any, key: str, value: str) -> None:
    """Puts a translated chunk into Redis with a TTL. A write error is not critical."""
    try:
        await redis.set(key, value, ex=CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the translation is ready already, we show it
        logger.warning("translate: Redis SET unavailable (%s)", type(exc).__name__)


async def _translate_chunk(
    chunk: str,
    *,
    redis: Any,
    ai: Any,
    target_lang: str,
    semaphore: asyncio.Semaphore,
    timeout_seconds: float,
    spent: list[TranslationUsage],
) -> str | None:
    """Translates one chunk: cache → model → cache. Any failure → ``None``.

    ``None`` at the top means a partial failure → the fragment gives the original WHOLE
    (not a mix of languages). A cache hit does not call the model.

    The spend of a successful call is put into ``spent`` IMMEDIATELY (and not returned): with a
    common ceiling ``translate_description`` gather is canceled, and the metrics of the
    already paid chunks would otherwise be lost together with it.
    """
    key = _desc_cache_key(target_lang, _fingerprint(None, chunk))
    cached = await _cache_get_text(redis, key)
    if cached is not None:
        return cached

    async with semaphore:
        try:
            result = await asyncio.wait_for(
                ai.analyze(
                    ai_client.AnalysisRequest(
                        level="translate",
                        title=None,
                        description=chunk,
                        target_language=_LANG_NAMES.get(target_lang, target_lang),
                    )
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — we degrade to the original
            logger.warning(
                "translate: description chunk unavailable (%s) → the original as a whole",
                type(exc).__name__,
            )
            return None

    spent.append(
        TranslationUsage(
            calls=1,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
    )
    translated = str(result.output.get("description") or "")
    await _cache_put_text(redis, key, translated)
    return translated


async def translate_description(
    *,
    tender_id: str,
    description: str | None,
    redis: Any | None,
    client: Any | None = None,
    target_lang: str = DEFAULT_TARGET_LANG,
    chunk_timeout_seconds: float = _CHUNK_TIMEOUT_SECONDS,
    total_timeout_seconds: float = _DESC_TOTAL_TIMEOUT_SECONDS,
) -> tuple[str | None, TranslationUsage]:
    """Translation of the tender DESCRIPTION in chunks into ``target_lang`` (VG-26).

    Returns a pair ``(joined translation | None, spend)``. ``None`` in the first
    element means “show the original WHOLE”: nothing to translate, Redis is not
    configured, ANY chunk failed to translate (partial failure → not a mix of languages),
    the common ceiling is exhausted. Exceptions do not escape.

    ‼️ The spend (the second element) on a partial failure is NOT zero: the original went up,
    but the translated chunks are already paid for — the limit must see it,
    otherwise a refusal of the model would become a free way to burn the budget.

    ``redis=None`` → ``None`` WITHOUT calling the model (without a cache every display would burn
    tokens). The cache is per chunk: a repeated display = 0 tokens.
    """
    if not description or not description.strip():
        return None, _NO_USAGE

    if redis is None:
        logger.warning("translate: Redis not configured → the original description")
        return None, _NO_USAGE

    chunks = _split_into_chunks(description[:_DESC_MAX_INPUT_CHARS], _CHUNK_MAX_CHARS)
    if not chunks:
        return None, _NO_USAGE

    ai = client if client is not None else _get_default_ai_client()
    semaphore = asyncio.Semaphore(_CHUNK_CONCURRENCY)
    spent: list[TranslationUsage] = []
    started = time.perf_counter()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    _translate_chunk(
                        chunk,
                        redis=redis,
                        ai=ai,
                        target_lang=target_lang,
                        semaphore=semaphore,
                        timeout_seconds=chunk_timeout_seconds,
                        spent=spent,
                    )
                    for chunk in chunks
                )
            ),
            timeout=total_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — common ceiling/failure → the original
        logger.warning(
            "translate: translation of the description tender=%s did not fit (%s) → the original",
            tender_id,
            type(exc).__name__,
        )
        return None, _sum_usage(spent)

    # Partial failure → the original WHOLE, not a mix of languages.
    if any(part is None for part in results):
        logger.warning(
            "translate: description tender=%s translated partially → the original as a whole",
            tender_id,
        )
        return None, _sum_usage(spent)

    # All parts are not None (checked above) — a filter and for the mypy narrowing of the type.
    joined = "\n\n".join(part for part in results if part is not None)
    # The model was not called even once → all chunks from the cache (the spend is empty).
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    cached = 0 if spent else 1
    logger.info(
        "translate: tender=%s part=description cached=%d elapsed_ms=%d",
        tender_id,
        cached,
        elapsed_ms,
    )
    return joined, _sum_usage(spent)
