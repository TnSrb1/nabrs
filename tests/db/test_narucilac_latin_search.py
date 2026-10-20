"""Integration: the search “Narucioci” in Latin over the names lying in CYRILLIC.

Prod 02.10: ``?q=beograd`` → “not found”, ``?q=`` with the Cyrillic word “Beograd” → 22 KB of output. The mocks do not check the SQL fold of the column (``fold_sql``) — here a real PG.

The fixture (the suffix ``sfx`` in Cyrillic-immutable: digits/Latin at the end of the name
isolate the rows; the query always includes it):

    UNIVERZITET U BEOGRADU-sfx        2 decisions
    Opstina Cacak-sfx                 1
    Dom zdravlja Djurdjevo-sfx        1
    Opstina Ljig-sfx                  1
    Cacak latin-sfx                   1   (Latin in the column, with diacritics: Čačak)

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.analytics.awards_analytics import (
    _CUSTOMER_NAME_FOLDED,
    normalize_customer_name,
    search_customers_page,
)
from src.domain.text_norm import fold

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция поиска наручиоца пропускается",
)

_FIXTURE = [
    ("УНИВЕРЗИТЕТ У БЕОГРАДУ", "100000001"),
    ("УНИВЕРЗИТЕТ У БЕОГРАДУ", "100000001"),
    ("Општина Чачак", "100000002"),
    ("Дом здравља Ђурђево", "100000003"),
    ("Општина Љиг", "100000004"),
    ("Opština Čačak lat", None),
]


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:8]
    tender_ids: list[uuid.UUID] = []
    async with eng.begin() as c:
        for i, (name, pib) in enumerate(_FIXTURE):
            tid = uuid.uuid4()
            tender_ids.append(tid)
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, first_seen_at, title_normalized) "
                    "VALUES (:id, :k, :uid, 'RS', 'active', now(), 'nabavka')"
                ),
                {"id": tid, "k": f"lat-{sfx}-{i}", "uid": f"lat-{sfx}-{i}"},
            )
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, "
                    "document_type_id, customer_name, customer_pib) "
                    "VALUES (:t, :uid, 1, :n, :p)"
                ),
                {
                    "t": tid,
                    "uid": f"lat-{sfx}-{i}",
                    "n": f"{name} {sfx}",
                    "p": pib,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield sfx, app_eng, eng
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": tender_ids},
            )
        await app_eng.dispose()
        await eng.dispose()


async def _found(app_eng, term, sfx) -> list[tuple[str, int]]:
    """The groups of the fixture (by the suffix) among those found by ``term``."""
    async with AsyncSession(app_eng) as s:
        page = await search_customers_page(s, term=term, sort="name_asc", limit=200)
    return [(r.name, r.awards) for r in page.rows if r.name.endswith(sfx)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "term",
    ["beograd", "Beograd", "BEOGRAD", "београд", "БЕОГРАД", "  beograd  "],
)
async def test_beograd_any_script_same_group(world, term):
    sfx, eng, _ = world
    assert await _found(eng, term, sfx) == [(f"УНИВЕРЗИТЕТ У БЕОГРАДУ {sfx}", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["čačak", "cacak", "чачак", "Чачак", "CACAK"])
async def test_cacak_diacritics_and_scripts(world, term):
    """Searches for both the Cyrillic “Opstina Cacak” and the Latin “Opstina Cacak lat”."""
    sfx, eng, _ = world
    got = {n for n, _ in await _found(eng, term, sfx)}
    assert got == {f"Општина Чачак {sfx}", f"Opština Čačak lat {sfx}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["djurdjevo", "đurđevo", "ђурђево", "Đurđevo"])
async def test_dj_variants(world, term):
    sfx, eng, _ = world
    assert await _found(eng, term, sfx) == [(f"Дом здравља Ђурђево {sfx}", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["ljig", "љиг", "Љиг"])
async def test_digraph_lj(world, term):
    sfx, eng, _ = world
    assert await _found(eng, term, sfx) == [(f"Општина Љиг {sfx}", 1)]


@pytest.mark.asyncio
async def test_pib_prefix_still_works(world):
    _, eng, _ = world
    async with AsyncSession(eng) as s:
        page = await search_customers_page(s, term="100000001")
    assert [(r.pib, r.awards) for r in page.rows] == [("100000001", 2)]


@pytest.mark.asyncio
async def test_group_key_unchanged_by_search(world):
    """Grouping stays by ``normalize_customer_name`` (not by the fold):
    “Opstina Cacak” and «Opština Čačak lat» are DIFFERENT groups."""
    sfx, eng, _ = world
    assert len(await _found(eng, "cacak", sfx)) == 2
    assert normalize_customer_name("  Општина   Чачак ") == "општина чачак"


@pytest.mark.asyncio
async def test_sql_fold_equals_python_fold(world):
    """``fold_sql`` in the DB ≡ ``text_norm.fold`` in Python on a mix of alphabets."""
    _, _, sup = world
    samples = [
        "УНИВЕРЗИТЕТ У БЕОГРАДУ",
        "Општина Љиг Њега Џамија",
        "Ђурђево Ћићевац Шабац Жабаљ",
        "Čačak Šabac Žabalj Đakovo ćevap",
        "Mixed КИРИЛИЦА latin 123",
        # Prod 02.10: Hungarian diacritics in 2 of 1520 names (owner review).
        "Сзекерес Лáсзлó ÁЛЛАТКЕРТ éüőñç",
        # Ijekavica/RS words with «je»: the SQL fold of step 5 does not have them (see fold_sql).
        "Bijelo Polje Bijeljina Mirijevo cijevi pjesma mlijeko",
        "projekat objekat dejstvo prijava Sjenica Pljevlja",
    ]
    async with sup.connect() as c:
        for s in samples:
            got = (
                await c.execute(
                    text(
                        "SELECT "
                        + _CUSTOMER_NAME_FOLDED.replace(
                            "customer_name", "CAST(:s AS text)"
                        )
                    ),
                    {"s": s},
                )
            ).scalar()
            assert got == fold(normalize_customer_name(s), ijekavica=False), s


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "src, expected",
    [
        ("Bijelo Polje", "bijelo polje"),
        ("Bijeljina", "bijeljina"),
        ("Mirijevo", "mirijevo"),
        ("cijevi", "cijevi"),
        ("pjesma", "pjesma"),
        ("mlijeko", "mlijeko"),
        ("projekat", "projekat"),
        ("objekat", "objekat"),
        ("dejstvo", "dejstvo"),
        ("prijava", "prijava"),
        ("Sjenica", "sjenica"),
        ("Pljevlja", "pljevlja"),
        ("ОПШТИНА БИЈЕЛО ПОЉЕ", "opstina bijelo polje"),
    ],
)
async def test_sql_fold_has_no_ijekavica_step(world, src, expected):
    """The SQL fold of the column does NOT fold the ijekavica, and the input for it too (``fold(..., ijekavica=False)``): «Bijelo Polje» finds «Bijelo Polje»."""
    _, _, sup = world
    async with sup.connect() as c:
        got = (
            await c.execute(
                text(
                    "SELECT "
                    + _CUSTOMER_NAME_FOLDED.replace("customer_name", "CAST(:s AS text)")
                ),
                {"s": src},
            )
        ).scalar()
    assert got == expected
    assert fold(src, ijekavica=False) == expected
