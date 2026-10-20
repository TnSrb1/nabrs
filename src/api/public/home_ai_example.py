"""A static snapshot of a real AI analysis for the home page (UX audit P1, owner 10.10).

Taken ONLY by reading production (``tender_ai_analyses``, level='bid', id a089c9c7-79d7-4d75-8509-316870cd3d57, created 2026-07-23): there are no new paid calls, and at display there are no DB requests. The title and the customer — in Latin (Serbian is in Latin everywhere, owner 10.10).

The pros / cons are given in three languages. ``ru`` — AS IN THE SOURCE (the analysis was made in Russian: the profile's ``target_language``). ``sr`` and ``en`` — a MANUALLY translated fixture (without a model call), marked in the template as “Primer, skraćeno” / “Example, abridged”. To update the snapshot = replace the values below with a new real analysis from production (and translate again).
"""

from __future__ import annotations

from typing import Final

HOME_AI_EXAMPLE: Final[dict[str, object]] = {
    "tender_id": "ede75f22-15c0-4cf2-8d57-354b29a7f5a8",
    "title": (
        "Nabavka IT opreme za obradu i skladištenje podataka pri "
        "arheološkim istraživanjima na području RHE „Bistrica“"
    ),
    "customer": "Republički zavod za zaštitu spomenika kulture",
    "amount": "320.000 RSD",
    "analysis_date": "23.07.2026",
    "verdict": "caution",
    "score": 48,
    "texts": {
        "ru": {
            "pros": [
                "Открытая процедура (Open tender) — доступна любому поставщику, "
                "без ограничений на участие",
                "Критерий отбора — только цена, что упрощает конкурентную ситуацию "
                "и не требует сложных доказательств компетенций",
            ],
            "cons": [
                "Требование финансового обеспечения (бланко-вексель 10% от стоимости) — "
                "поставщик должен иметь банковское обслуживание и возможность оформить меницу",
                "Платежные условия до 45 дней — риск кассового разрыва для малого поставщика",
            ],
        },
        "sr": {
            "pros": [
                "Otvoreni postupak (Open tender) — dostupan svakom ponuđaču, "
                "bez ograničenja za učešće",
                "Kriterijum izbora je samo cena, što pojednostavljuje konkurentsku situaciju "
                "i ne zahteva složene dokaze o kompetencijama",
            ],
            "cons": [
                "Zahtev za finansijsko obezbeđenje (blanko menica 10% od vrednosti) — "
                "ponuđač mora imati bankarsku uslugu i mogućnost izdavanja menice",
                "Rok plaćanja do 45 dana — rizik nedostatka likvidnosti za mali subjekt",
            ],
        },
        "en": {
            "pros": [
                "Open procedure (Open tender) — open to any supplier, "
                "with no restrictions on participation",
                "Selection criterion is price only, which simplifies the competitive situation "
                "and does not require complex proof of competence",
            ],
            "cons": [
                "Financial security requirement (blank promissory note, 10% of the value) — "
                "the supplier must have a bank account and be able to issue a promissory note",
                "Payment terms up to 45 days — cash-flow gap risk for a small supplier",
            ],
        },
    },
}
