"""sr_first: ключевые слова шаблонов библиотеки → сербские ОСНОВЫ латиницей.

Решение owner 30.09.2026 (сербский по умолчанию). Шаблоны ``template_library`` — данные
в БД (правка миграцией = RED), поэтому идемпотентный скрипт; применяет owner.

Режимы (по умолчанию DRY-RUN, БД не меняется):
    python scripts/sr_first_templates.py            # план + счётчик 30 дн ДО/ПОСЛЕ
    python scripts/sr_first_templates.py --apply    # UPDATE + тот же замер
    python scripts/sr_first_templates.py --packages OLD.py   # замер пакетов: OLD.py =
        # `git show origin/main:src/api/cabinet/vertical_packages.py` vs текущий файл

Что меняется у КАЖДОГО шаблона (только публичные/одобренные не отделяем — все строки):
* ``keywords_include``: русский термин → сербские основы (латиница), составные
  группы (AND) разворачиваются в произведение основ. Группа без кириллицы не
  трогается → повторный прогон ничего не меняет.
* ``i18n->'sr'``: name/description/activity_kind кириллица → латиница.

Счётчик — ТОТ ЖЕ ``preview_service.preview_count`` (30 дней, стадии A+B, словарь
включён), что и на витрине /library; свёртка — ``src.domain.text_norm``.
Нужен ``DATABASE_URL``; запуск в образе api (см. TESTING_sr_first.md).
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import re
import sys

CYR = re.compile("[А-Яа-яЁёЂђЈјЉљЊњЋћЏџ]")

# ru-термин → сербские основы (латиница; подстрочный матч по text_norm.fold ловит
# все падежи). Покрытие ≥ прежнего расширения ru→sr словарём keyword_dict.
STEMS: dict[str, list[str]] = {
    "строительство": ["građevin", "izgradnj", "građenj"],
    "ремонт": ["remont", "rekonstrukcij", "održavanj", "popravk", "sanacij"],
    "здание": ["zgrad"],
    "реконструкция": ["rekonstrukcij"],
    "дорога": ["put", "putev"],
    "программное": ["softver", "program"],
    "лицензия": ["licenc"],
    "разработка": ["razvoj", "izrad", "razvija"],
    "компьютер": ["računar", "kompjuter"],
    "медицинское": ["medicinsk"],
    "оборудование": ["oprem", "uređaj"],
    "лабораторное": ["laboratorij"],
    "медтехника": ["medicinsk"],
    "охрана": ["obezbeđ", "čuvanj"],
    "безопасность": ["bezbednost", "sigurnost"],
    "видеонаблюдение": ["videonadzor", "video nadzor"],
    "уборка": ["čišćenj", "čistoć"],
    "клининг": ["čišćenj", "higijen"],
    "чистка": ["čišćenj", "pranj"],
    "электроэнергия": ["električn energij"],
    "электроснабжение": ["snabdevanje električn"],
    "электрическая": ["električn"],
    "энергия": ["energij", "energetsk"],
    "информационная система": ["informacion sistem"],
    "информационная": ["informacion"],
    "система": ["sistem"],
    "обучение": ["obuk", "obrazovanj", "trening"],
    "хостинг": ["hosting"],
    "облако": ["oblak", "oblačn"],
    "проект": ["projekat", "projekt"],
    "интернет": ["internet"],
    "сеть": ["mrež"],
    "асфальт": ["asfalt"],
    "инфраструктура": ["infrastruktur"],
    "канцелярия": ["kancelarij"],
    "бумага": ["papir"],
    "сервер": ["server"],
    "принтер": ["štampač"],
    "лекарство": ["lekov", "leka", "lek van liste", "lek po"],
    "фармацевтика": ["farmaceutsk", "farmacij"],
    "медикамент": ["medikament", "lekov"],
    "мебель": ["namešt"],
    "стол": ["stol"],
    "кресло": ["fotelj"],
    "сигнализация": ["alarm"],
    "продукт": ["prehramben", "namirnic"],
    "питание": ["ishran", "prehran"],
    "продовольствие": ["namirnic", "prehran", "prehramben"],
    "водоснабжение": ["vodosnabdev", "vodovod"],
    "канализация": ["kanalizacij", "kanalizacion"],
    "сантехника": ["sanitarij", "sanitarn", "vodovod"],
    "телефония": ["telefon"],
    "телекоммуникация": ["telekomunikacij"],
    "связь": ["komunikacij", "telekomunikacij"],
    "транспорт": ["transport", "prevoz"],
    "автомобиль": ["vozil", "automobil"],
    "автобус": ["autobus"],
    "электрооборудование": [
        "elektroopre",
        "elektromaterijal",
        "elektro materijal",
        "električn opre",
    ],
    "монтаж": ["montaž", "ugradnj"],
    "электро": ["elektro"],
    "кабель": ["kabl"],
}

_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e",
    "ж": "ž", "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj",
    "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "č",
    "џ": "dž", "ш": "š",
}  # fmt: skip
_LAT.update({k.upper(): v.capitalize() for k, v in list(_LAT.items())})


def latinize(text: str | None) -> str | None:
    """Сербская кириллица → латиница с диакритикой (идемпотентно для латиницы)."""
    if text is None:
        return None
    return "".join(_LAT.get(ch, ch) for ch in text)


def convert_groups(groups: list[dict]) -> list[dict]:
    """OR-группы ``all_of`` с ru-терминами → группы с сербскими основами.

    Составная группа (AND) → произведение основ по позициям. Группа без
    кириллицы возвращается как есть (идемпотентность). Неизвестный ru-термин —
    ошибка (молча оставить кириллицу нельзя: замер это бы скрыл).
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for g in groups:
        terms = list(g.get("all_of") or [])
        if not any(CYR.search(t) for t in terms):
            variants = [tuple(terms)]
        else:
            per_pos = []
            for t in terms:
                if not CYR.search(t):
                    per_pos.append([t])
                elif t.lower() in STEMS:
                    per_pos.append(STEMS[t.lower()])
                else:
                    raise KeyError(f"нет сербской основы для {t!r}")
            variants = [tuple(v) for v in itertools.product(*per_pos)]
        for v in variants:
            flat: list[str] = []
            for term in v:  # «električn energij» → два AND-слова
                flat.extend(term.split(" ") if term in _SPLIT else [term])
            key = tuple(flat)
            if key not in seen:
                seen.add(key)
                out.append({"all_of": list(flat)})
    return out


# Основы-«пары» (два слова AND), в остальных многословных строках пробел — часть фразы.
_SPLIT = {
    "električn energij",
    "snabdevanje električn",
    "električn opre",
    "informacion sistem",
}


def convert_i18n(i18n: dict | None) -> dict | None:
    if not i18n or "sr" not in i18n:
        return i18n
    sr = {k: latinize(v) if isinstance(v, str) else v for k, v in i18n["sr"].items()}
    return {**i18n, "sr": sr}


async def _count(db, payload) -> int:
    from src.api.cabinet import preview_service

    return (await preview_service.preview_count(db, payload)).count


def _payload(groups, cls, exclude=()):
    from src.api.cabinet import template_prefill

    return template_prefill.base_payload(
        keywords_include=groups,
        keywords_exclude=list(exclude),
        classifications={
            "include": template_prefill.as_prefix_filters((cls or {}).get("include")),
            "exclude": template_prefill.as_prefix_filters((cls or {}).get("exclude")),
        },
    )


async def run_templates(apply: bool) -> int:
    from sqlalchemy import text

    from src.db.session import get_sessionmaker

    rows_out = []
    async with get_sessionmaker()() as db:
        res = await db.execute(
            text(
                "SELECT id, name, keywords_include, classifications, i18n "
                "FROM template_library ORDER BY name"
            )
        )
        rows = res.fetchall()
        for r in rows:
            old = list(r.keywords_include or [])
            new = convert_groups(old)
            new_i18n = convert_i18n(r.i18n)
            before = await _count(db, _payload(old, r.classifications))
            after = await _count(db, _payload(new, r.classifications))
            changed = new != old or new_i18n != r.i18n
            rows_out.append((r.name, before, after, changed, old, new))
            if apply and changed:
                await db.execute(
                    text(
                        "UPDATE template_library SET "
                        "keywords_include = CAST(:kw AS jsonb), "
                        "i18n = CAST(:i18n AS jsonb) WHERE id = :id"
                    ),
                    {
                        "kw": json.dumps(new, ensure_ascii=False),
                        "i18n": (
                            json.dumps(new_i18n, ensure_ascii=False)
                            if new_i18n is not None
                            else None
                        ),
                        "id": r.id,
                    },
                )
        if apply:
            await db.commit()
    print("MODE:", "APPLY" if apply else "DRY-RUN")
    print("| шаблон | было (30д) | стало (30д) | изменится |")
    print("|---|---|---|---|")
    for name, b, a, ch, old, new in rows_out:
        print(f"| {name} | {b} | {a} | {'да' if ch else 'нет (уже sr)'} |")
    for name, _, _, ch, old, new in rows_out:
        if ch:
            print(f"\n{name}\n  old: {json.dumps(old, ensure_ascii=False)}")
            print(f"  new: {json.dumps(new, ensure_ascii=False)}")
    return 0


async def run_packages(old_path: str) -> int:
    from src.api.cabinet import template_prefill, vertical_packages
    from src.db.session import get_sessionmaker

    spec = importlib.util.spec_from_file_location("vp_old", old_path)
    assert spec and spec.loader
    old_mod = importlib.util.module_from_spec(spec)
    sys.modules["vp_old"] = old_mod
    spec.loader.exec_module(old_mod)
    print("| пакет | было (30д) | стало (30д) |")
    print("|---|---|---|")
    async with get_sessionmaker()() as db:
        for pkg in vertical_packages.PACKAGES:
            old = old_mod.get_package(pkg.slug)
            b = await _count(db, template_prefill.from_package(old, "ru"))
            a = await _count(db, template_prefill.from_package(pkg, "sr"))
            print(f"| {pkg.slug} | {b} | {a} |")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--packages"]:
        sys.exit(asyncio.run(run_packages(args[1])))
    sys.exit(asyncio.run(run_templates("--apply" in args)))
