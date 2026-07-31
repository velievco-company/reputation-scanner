"""
scoring.py
Логика и математика Reputation Score.

Конфигурация весов (без WHOIS/Trust Score — не актуально для B2B/юрфирм):
    Reviews Score:      35%
    Visibility Score:   25%
    Negative Penalty:   40%

Ключевая особенность: рейтинги (Google/Trustpilot/Glassdoor) оцениваются
НЕ по жёсткому порогу (например "ниже 4.4 = плохо"), а через сравнение
со средним по нише (context-aware). Это не наказывает B2B-компании
(юристы, консалтинг), где мало отзывов — норма отрасли, а не проблема.
"""

import math
from datetime import datetime, timezone

WEIGHTS = {
    "reviews": 0.35,
    "visibility": 0.25,
    "negative": 0.40,
}

RECENCY_LAMBDA = 0.01  # коэффициент затухания важности со временем (по дням)
NEGATIVE_PENALTY_PER_HIT = 5  # базовый штраф за одно негативное упоминание
NEGATIVE_PENALTY_MAX = 40  # весь бюджет Negative Score (соответствует весу 40%)

RISK_LEVELS = {
    "LOW_RISK": (70, 100),
    "MEDIUM_RISK": (45, 70),
    "HIGH_RISK": (0, 45),
}


# ---------------------------------------------------------------------------
# RECENCY DECAY — старые негативные упоминания весят меньше
# ---------------------------------------------------------------------------

def recency_decay(days_ago: float, lam: float = RECENCY_LAMBDA) -> float:
    """
    Экспоненциальное затухание значимости события со временем.
    days_ago=0   -> вес 1.0 (максимальная значимость)
    days_ago=100 -> вес ~0.37
    days_ago=365 -> вес ~0.03 (почти не влияет)
    """
    if days_ago < 0:
        days_ago = 0
    return math.exp(-lam * days_ago)


def days_since(timestamp_utc: float | None) -> float:
    """Считает сколько дней прошло с unix-таймстампа. Если даты нет — считаем старым (365 дней)."""
    if timestamp_utc is None:
        return 365.0
    now = datetime.now(timezone.utc).timestamp()
    delta_seconds = max(0, now - timestamp_utc)
    return delta_seconds / 86400.0


# ---------------------------------------------------------------------------
# REVIEWS SCORE (вес 35%) — context-aware сравнение с нишей
# ---------------------------------------------------------------------------

def compute_reviews_score(
    company_ratings: list[dict],
    niche_average_rating: float | None = None,
    niche_average_review_count: float | None = None,
) -> dict:
    """
    company_ratings: список вида [{"source": "trustpilot", "rating": 4.2, "review_count": 47}, ...]
    niche_average_rating: средний рейтинг по нише (из search_industry_competitors)
    niche_average_review_count: средняя медиана числа отзывов по нише

    Логика:
    - Если ниша неизвестна (нет данных конкурентов) -> используем нейтральный бенчмарк 4.0/20 отзывов
    - Компания сравнивается ОТНОСИТЕЛЬНО ниши, а не абсолютным порогом
    """
    if not company_ratings:
        return {
            "score": 50.0,  # нейтральный балл при полном отсутствии данных
            "detail": "Отзывы не найдены ни на одной платформе.",
            "sources_used": [],
        }

    benchmark_rating = niche_average_rating if niche_average_rating else 4.0
    benchmark_reviews = niche_average_review_count if niche_average_review_count else 20

    weighted_scores = []
    source_weights = {
        "trustpilot": 0.40,
        "google_maps": 0.35,
        "glassdoor": 0.25,
    }

    for entry in company_ratings:
        source = entry.get("source", "unknown")
        rating = entry.get("rating")
        review_count = entry.get("review_count", 0)

        if rating is None:
            continue

        # Относительный скор рейтинга: 1.0 = на уровне ниши, >1.0 = лучше ниши.
        # base 50 = "на уровне ниши", ±100 за каждую единицу отклонения relative_rating,
        # потолок 85 оставляет 15 баллов бюджета под volume_component ниже.
        # Раньше формула (relative_rating * 80) утыкалась в потолок 100 почти
        # при любом рейтинге выше бенчмарка — теряя чувствительность к разнице
        # между "немного лучше" и "значительно лучше" ниши.
        relative_rating = rating / benchmark_rating if benchmark_rating > 0 else 1.0
        rating_component = min(85, 50 + (relative_rating - 1.0) * 100)

        # Объём отзывов ОТНОСИТЕЛЬНО ниши — смягчаем через sqrt,
        # чтобы кратно большее число отзывов не давало мгновенный максимум
        volume_ratio = review_count / benchmark_reviews if benchmark_reviews > 0 else 1.0
        volume_component = min(15, (volume_ratio ** 0.5) * 10)

        source_score = max(0, min(100, rating_component + volume_component))
        weight = source_weights.get(source, 0.20)

        weighted_scores.append((source_score, weight))

    if not weighted_scores:
        return {
            "score": 50.0,
            "detail": "Найдены платформы, но без числовых рейтингов.",
            "sources_used": [],
        }

    total_weight = sum(w for _, w in weighted_scores)
    final_score = sum(s * w for s, w in weighted_scores) / total_weight if total_weight else 50.0

    return {
        "score": round(final_score, 1),
        "detail": f"Сравнение с нишевым бенчмарком {benchmark_rating}/5, {benchmark_reviews} отзывов в среднем.",
        "sources_used": [e.get("source") for e in company_ratings],
    }


# ---------------------------------------------------------------------------
# VISIBILITY SCORE (вес 25%)
# ---------------------------------------------------------------------------

def compute_visibility_score(
    has_working_website: bool,
    found_on_maps: bool,
    mention_count: int,
    found_in_news: bool,
) -> dict:
    """Простая композитная метрика цифровой видимости."""
    score = 0
    details = []

    if has_working_website:
        score += 30
        details.append("сайт активен")
    else:
        details.append("⚠ сайт не найден или недоступен")

    if found_on_maps:
        score += 25
        details.append("присутствует в Google Maps")

    if mention_count >= 3:
        score += 25
        details.append(f"найдено {mention_count}+ упоминаний в поиске")
    elif mention_count > 0:
        score += 10
        details.append(f"найдено только {mention_count} упоминаний")

    if found_in_news:
        score += 20
        details.append("есть упоминания в СМИ")

    return {
        "score": min(100, score),
        "detail": "; ".join(details),
    }


# ---------------------------------------------------------------------------
# NEGATIVE PENALTY (вес 40%, база = полный балл, штрафуем за находки)
# ---------------------------------------------------------------------------

def compute_negative_score(negative_findings: list[dict]) -> dict:
    """
    negative_findings: список вида
        [{"title": ..., "url": ..., "days_ago": 45, "severity": "high"|"medium"|"low"}, ...]

    База = 100 (полный балл category-level, потом взвешивается через WEIGHTS["negative"]).
    Каждая находка вычитает штраф, скорректированный на давность (recency decay).
    """
    severity_multiplier = {"high": 2.0, "medium": 1.0, "low": 0.5}

    base_score = 100.0
    findings_detail = []

    for finding in negative_findings:
        days_ago = finding.get("days_ago", 180)
        severity = finding.get("severity", "medium")

        decay = recency_decay(days_ago)
        penalty = NEGATIVE_PENALTY_PER_HIT * severity_multiplier.get(severity, 1.0) * decay
        base_score -= penalty

        findings_detail.append({
            "title": finding.get("title", ""),
            "url": finding.get("url", ""),
            "days_ago": round(days_ago),
            "penalty_applied": round(penalty, 1),
        })

    final_score = max(0.0, base_score)

    if not negative_findings:
        detail = "Негативных упоминаний не найдено."
    else:
        detail = f"Найдено {len(negative_findings)} упоминаний, суммарный штраф {round(100 - final_score, 1)} баллов."

    return {
        "score": round(final_score, 1),
        "detail": detail,
        "findings": findings_detail,
        "total_findings": len(negative_findings),
    }


# ---------------------------------------------------------------------------
# CONFIDENCE MULTIPLIER — понижает итог при недостатке данных
# ---------------------------------------------------------------------------

def compute_confidence_multiplier(sources_found: int, expected_sources: int = 5) -> dict:
    """
    Если по компании найдено мало источников данных — итоговый скор
    умножается на понижающий коэффициент (0.4–1.0), плюс выводится предупреждение.
    """
    ratio = sources_found / expected_sources if expected_sources > 0 else 1.0
    multiplier = max(0.4, min(1.0, ratio))

    warning = None
    if multiplier < 0.7:
        warning = "⚠ Низкая цифровая видимость — недостаточно данных для высокого уровня достоверности."

    return {"multiplier": round(multiplier, 2), "warning": warning}


# ---------------------------------------------------------------------------
# ФИНАЛЬНАЯ АГРЕГАЦИЯ
# ---------------------------------------------------------------------------

def compute_final_score(
    reviews_result: dict,
    visibility_result: dict,
    negative_result: dict,
    sources_found: int,
) -> dict:
    """Собирает все категории в единый Reputation Score с учётом весов и confidence."""

    weighted_sum = (
        reviews_result["score"] * WEIGHTS["reviews"]
        + visibility_result["score"] * WEIGHTS["visibility"]
        + negative_result["score"] * WEIGHTS["negative"]
    )

    confidence = compute_confidence_multiplier(sources_found)
    final_score = round(weighted_sum * confidence["multiplier"], 1)

    risk_level = _get_risk_level(final_score)

    return {
        "final_score": final_score,
        "risk_level": risk_level,
        "confidence_multiplier": confidence["multiplier"],
        "confidence_warning": confidence["warning"],
        "breakdown": {
            "reviews": reviews_result,
            "visibility": visibility_result,
            "negative": negative_result,
        },
        "weights_used": WEIGHTS,
    }


def _get_risk_level(score: float) -> str:
    for level, (low, high) in RISK_LEVELS.items():
        if low <= score <= high:
            return level
    return "MEDIUM_RISK"
