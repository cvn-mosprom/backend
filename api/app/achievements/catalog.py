"""Achievement catalog (C1, docs/hr-crm-plan.md "Каталог достижений"): data only, no logic.

Every rule is grounded in a field the server already validates (`factories.state` after the sim = the frontend core's
FactoryState v2, tables `shifts`, `season_results`, `quiz_answers`, `users.source`). Weights and thresholds live HERE and can be
changed without a migration; `CATALOG_VERSION` goes up with every change that adds / re-tunes an entry (earned rows keep the
version they were earned under; nothing earned is ever revoked).

A rule is evaluated by `engine.RULES[entry.rule]`; `source` says which data the rule needs:
  state   - the factory state only (the cheap hook after every command)
  shifts  - aggregates of the `shifts` table (hook after submitShift + recompute)
  season  - `season_results` (recompute / backfill)
  quiz    - `quiz_answers` (recompute / backfill)
`target` is the progress bar target (`current/target`); composite rules (several conditions) use target 100 = percent of the
weakest condition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CATALOG_VERSION = 1

TIERS = ("bronze", "silver", "gold")
COMPETENCIES = ("speed", "accuracy", "attention", "endurance", "reliability", "company")
CATEGORIES = ("learning", "production", "quality", "professions", "speed", "endurance", "reliability", "company",
              "season", "team")
SOURCES = ("state", "shifts", "season", "quiz")
COUNTERS_ID = "_counters"  # internal achievement_progress row (engine counters); never part of the catalog

# ---- game content the rules stand on (api/tests/test_achievements_catalog.py checks them against vendor/frontend)
PROFESSIONS: Mapping[str, str] = {  # ProfessionId (content/buildings.ts) -> short Russian name
    "kvasovar": "Квасовар",
    "operator": "Оператор линии",
    "adjuster": "Наладчик",
    "lab": "Лаборант ОТК",
    "storekeeper": "Кладовщик",
    "forklift": "Водитель погрузчика",
    "kipia": "Инженер КИПиА",
}
SKU_IDS = ("kvas_ochakovsky_15", "lemonade_tarhun_15", "lemonade_duchess_15", "kvas_semeyny_15", "coolcola_05",
           "fancy_05", "street_05", "kvas_ochakovsky_can")  # content/products.ts SKUS
FACT_IDS = ("f1978", "f1979", "f1996", "f1999", "f2000", "f2003", "f2005", "f2013", "f2015", "f2022")  # content/facts.ts
QUEST_LINE = "q_line"  # content/dialogs.ts: the training line is assembled
QUEST_FIRST_SHIPMENT = "q_dock"  # content/dialogs.ts: the first shipment (core: the end of the tutorial)
SHIFTS_PER_DAY = 5  # content/balance.ts
LOAN_OVERDUE_MS = 48 * 3_600_000  # content/balance.ts LOAN_OVERDUE_H
OTK_MIN_QUALITY = 0.9  # an order "of the quality control kind": minQuality >= 0.9
FAST_REACTION_MS = 400  # shifts.metrics.reaction_ms below this is a "fast reaction"
PERFECT_SCORE = 0.99


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    description: str
    category: str
    tier: str  # bronze | silver | gold
    points: int
    rule: str  # key of engine.RULES
    source: str  # state | shifts | season | quiz
    target: float
    competency: str | None = None  # one of COMPETENCIES (what it proves), None = none
    hidden: bool = False  # shown as "???" until earned
    seasonal: bool = False  # earned once PER SEASON (season_key = season id), else once ever (season_key 0)
    progress: bool = True  # False: no meaningful progress bar (placements)
    profession: str | None = None  # ProfessionId the achievement belongs to (feeds the per-profession fit, C2)
    params: Mapping[str, Any] = field(default_factory=dict)  # thresholds read by the rule

    def public(self) -> dict[str, Any]:
        """The catalog entry as the player / HR API shows it (no rule internals). A hidden entry keeps its id and points but
        callers should mask title / description until it is earned."""
        return {"id": self.id, "title": self.title, "description": self.description, "category": self.category,
                "tier": self.tier, "points": self.points, "competency": self.competency, "hidden": self.hidden,
                "seasonal": self.seasonal, "target": self.target, "profession": self.profession}


def _a(**kw: Any) -> Achievement:
    return Achievement(**kw)


def _professions() -> list[Achievement]:
    out: list[Achievement] = []
    for pid, name in PROFESSIONS.items():
        out.append(_a(id=f"{pid}_practice", title=f"Практика: {name}", category="professions", tier="bronze", points=10,
                      description=f"Отработай 5 смен по профессии «{name}»", rule="prof_played", source="state",
                      target=5, profession=pid, params={"profession": pid}))
        out.append(_a(id=f"{pid}_ace", title=f"Ас: {name}", category="professions", tier="gold", points=40,
                      description=f"{name}: 10+ смен, не меньше 70% побед и средний балл от 0,85",
                      rule="prof_ace", source="state", target=100, profession=pid,
                      params={"profession": pid, "min_played": 10, "win_rate": 0.7, "avg_score": 0.85}))
    return out


CATALOG: tuple[Achievement, ...] = (
    # --- Обучение
    _a(id="first_line", title="Первая линия", category="learning", tier="bronze", points=10,
       description="Собери учебную линию от приёмки до упаковки", rule="quest", source="state", target=1,
       params={"quest": QUEST_LINE}),
    _a(id="first_order", title="Первый заказ", category="learning", tier="bronze", points=10,
       description="Отгрузи первый заказ клиенту", rule="first_order", source="state", target=1),
    # --- Производство
    _a(id="bottles_1k", title="Тысяча бутылок", category="production", tier="bronze", points=10,
       description="Выпусти 1 000 бутылок", rule="produced", source="state", target=1_000, competency="reliability"),
    _a(id="bottles_10k", title="10 000 бутылок", category="production", tier="silver", points=25,
       description="Выпусти 10 000 бутылок", rule="produced", source="state", target=10_000, competency="reliability"),
    _a(id="bottles_100k", title="100 000 бутылок", category="production", tier="gold", points=50,
       description="Выпусти 100 000 бутылок", rule="produced", source="state", target=100_000, competency="reliability"),
    _a(id="sku_5", title="Пять напитков", category="production", tier="silver", points=20,
       description="Отгрузи 5 разных напитков", rule="skus", source="state", target=5, competency="reliability"),
    _a(id="sku_all", title="Весь ассортимент", category="production", tier="gold", points=50,
       description="Отгрузи все 8 напитков коллекции", rule="skus", source="state", target=len(SKU_IDS),
       competency="reliability"),
    # --- Качество
    _a(id="no_defects", title="Без брака", category="quality", tier="gold", points=30,
       description="Держи качество не ниже 98% на 1 000+ бутылок", rule="no_defects", source="state", target=100,
       competency="attention", params={"min_bottles": 1_000, "quality": 0.98}),
    _a(id="otk_control", title="Контроль ОТК", category="quality", tier="silver", points=25,
       description="Выполни 10 заказов с требованием качества от 90%", rule="orders_otk", source="state", target=10,
       competency="attention"),
    # --- Профессии (по две на профессию: практика и «ас») + универсал
    *_professions(),
    _a(id="all_rounder", title="Универсал", category="professions", tier="gold", points=40,
       description="Отработай не меньше 5 смен в каждой из 7 профессий", rule="all_rounder", source="state",
       target=len(PROFESSIONS), params={"min_played": 5}),
    # --- Скорость
    _a(id="reaction_fast", title="Реакция", category="speed", tier="silver", points=25,
       description="10 смен с реакцией быстрее 400 мс", rule="reaction_fast", source="shifts", target=10,
       competency="speed"),
    # --- Выносливость
    _a(id="week_streak", title="Неделя на смене", category="endurance", tier="gold", points=30,
       description="Выходи на смену 7 дней подряд", rule="week_streak", source="shifts", target=7,
       competency="endurance"),
    _a(id="full_days", title="Полный день", category="endurance", tier="silver", points=20,
       description="Отработай все 5 смен за день, и так 5 дней", rule="full_days", source="shifts", target=5,
       competency="endurance"),
    # --- Надёжность
    _a(id="on_time_10", title="Точно в срок", category="reliability", tier="silver", points=25,
       description="Выполни 10 заказов в срок (просроченный заказ не засчитывается)", rule="orders_done", source="state",
       target=10, competency="reliability"),
    _a(id="no_debts", title="Без долгов", category="reliability", tier="bronze", points=15,
       description="Верни аванс от завода до срока (48 часов)", rule="loans_repaid", source="state", target=1,
       competency="reliability"),
    # --- Знание компании
    _a(id="museum_all", title="Музей", category="company", tier="silver", points=20,
       description="Прочитай все факты музея «Очаково»", rule="museum", source="state", target=len(FACT_IDS),
       competency="company"),
    _a(id="quiz_6", title="Знаток завода", category="company", tier="silver", points=20,
       description="Ответь верно на 6 вопросов викторины из 8", rule="quiz_correct", source="quiz", target=6,
       competency="company"),
    _a(id="quiz_8", title="Эксперт завода", category="company", tier="gold", points=40,
       description="Ответь верно на все 8 вопросов викторины", rule="quiz_correct", source="quiz", target=8,
       competency="company"),
    # --- Сезон
    _a(id="season_top50", title="Топ-50 сезона", category="season", tier="silver", points=30,
       description="Закончи сезон в первой полусотне", rule="season_place", source="season", target=1,
       seasonal=True, progress=False, params={"max_place": 50}),
    _a(id="season_top10", title="Топ-10 сезона", category="season", tier="gold", points=60,
       description="Закончи сезон в десятке лучших", rule="season_place", source="season", target=1,
       seasonal=True, progress=False, params={"max_place": 10}),
    _a(id="season_top3", title="Призёр сезона", category="season", tier="gold", points=100,
       description="Закончи сезон в тройке лучших", rule="season_place", source="season", target=1,
       seasonal=True, progress=False, params={"max_place": 3}),
    _a(id="season_regular", title="Сезонник", category="season", tier="silver", points=25,
       description="Отработай 15 смен за один сезон", rule="season_shifts", source="shifts", target=15,
       competency="endurance", seasonal=True),
    # --- Команда
    _a(id="mentor", title="Наставник", category="team", tier="silver", points=20,
       description="Пригласи 3 друзей в цех", rule="referrals", source="state", target=3),
    # --- Скрытое
    _a(id="perfect_shift", title="Идеальная смена", category="speed", tier="silver", points=20,
       description="Выиграй смену с результатом от 99%", rule="perfect_shift", source="shifts", target=1,
       competency="accuracy", hidden=True),
)

BY_ID: dict[str, Achievement] = {a.id: a for a in CATALOG}
assert len(BY_ID) == len(CATALOG), "duplicate achievement id"
assert all(a.tier in TIERS and a.category in CATEGORIES and a.source in SOURCES for a in CATALOG)
assert all(a.competency is None or a.competency in COMPETENCIES for a in CATALOG)
assert all(a.profession is None or a.profession in PROFESSIONS for a in CATALOG)
assert not any(a.id.startswith("_") for a in CATALOG)


def public_catalog() -> list[dict[str, Any]]:
    return [a.public() for a in CATALOG]
