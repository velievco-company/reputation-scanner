"""
ai_analyzer.py
AI-анализ тональности и генерация текстовых рекомендаций.

Приоритет:
    Groq API (llama-3.3-70b, бесплатно до 14400 req/день) — основной
    TextBlob (offline, без ключа) — fallback для sentiment, если Groq недоступен

ВАЖНО: GROQ_API_KEY нужно добавить в st.secrets. Пока ключа нет —
модуль автоматически откатывается на TextBlob и работает без AI-резюме,
показывая базовую статистику вместо развёрнутых рекомендаций.
"""

import json

import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    from groq import Groq
except ImportError:
    Groq = None

try:
    from textblob import TextBlob
except ImportError:
    TextBlob = None


GROQ_MODEL = "llama-3.3-70b-versatile"


def _get_secret(key: str) -> str | None:
    try:
        return st.secrets.get(key)
    except Exception:
        return None


def _get_groq_client():
    """Возвращает Groq-клиент, если ключ есть, иначе None."""
    if Groq is None:
        return None
    api_key = _get_secret("GROQ_API_KEY")
    if not api_key:
        return None
    return Groq(api_key=api_key)


# ---------------------------------------------------------------------------
# SENTIMENT ANALYSIS
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _groq_sentiment_batch(texts: list[str]) -> list[dict]:
    """Отправляет пачку текстов в Groq для классификации тональности одним вызовом."""
    client = _get_groq_client()
    if client is None:
        raise RuntimeError("GROQ_API_KEY не задан.")

    numbered_texts = "\n".join(f"{i+1}. {t[:500]}" for i, t in enumerate(texts))

    prompt = f"""Classify sentiment of each numbered text as exactly one of: positive, neutral, negative.
Return ONLY a JSON array, no other text, no markdown fences.
Format: [{{"index": 1, "sentiment": "positive"}}, ...]

Texts:
{numbered_texts}"""

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)

    return parsed


def analyze_sentiment_batch(texts: list[str]) -> dict:
    """
    Анализирует тональность списка текстов.
    Пробует Groq первым, при недоступности — TextBlob (менее точный, но offline).
    """
    if not texts:
        return {"status": "ok", "source": "none", "results": []}

    try:
        parsed = _groq_sentiment_batch(texts)
        results = [
            {
                "text": texts[item["index"] - 1] if 0 < item["index"] <= len(texts) else "",
                "sentiment": item.get("sentiment", "neutral"),
            }
            for item in parsed
        ]
        return {"status": "ok", "source": "groq", "results": results}
    except Exception as exc:
        return _textblob_sentiment_batch(texts, fallback_reason=str(exc))


def _textblob_sentiment_batch(texts: list[str], fallback_reason: str = "") -> dict:
    """Offline fallback без ключа. Менее точен для сарказма/сложных формулировок."""
    if TextBlob is None:
        return {
            "status": "failed",
            "source": "none",
            "results": [{"text": t, "sentiment": "neutral"} for t in texts],
            "error": "TextBlob не установлен, и Groq недоступен: " + fallback_reason,
        }

    results = []
    for text in texts:
        polarity = TextBlob(text).sentiment.polarity
        if polarity > 0.1:
            sentiment = "positive"
        elif polarity < -0.1:
            sentiment = "negative"
        else:
            sentiment = "neutral"
        results.append({"text": text, "sentiment": sentiment})

    return {
        "status": "fallback",
        "source": "textblob",
        "results": results,
        "error": f"Groq недоступен ({fallback_reason}), использован offline-анализ TextBlob.",
    }


# ---------------------------------------------------------------------------
# ГЕНЕРАЦИЯ РЕКОМЕНДАЦИЙ (только Groq — TextBlob не умеет генерировать текст)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def generate_recommendations(company_name: str, score_summary: dict) -> dict:
    """
    Генерирует текстовое резюме и рекомендации на основе итогового скора.
    Требует GROQ_API_KEY. Без ключа возвращает статичный шаблонный текст.
    """
    client = _get_groq_client()

    if client is None:
        return _template_recommendations(company_name, score_summary)

    confidence_note = ""
    if score_summary.get("confidence_warning"):
        confidence_note = (
            "\nIMPORTANT: This company has LOW DATA AVAILABILITY (few sources found), "
            "not necessarily poor reputation. A low score here often means insufficient "
            "online presence to verify reputation, NOT confirmed negative sentiment. "
            "Do not claim 'negative sentiment' or 'poor reviews' if negative findings = 0 "
            "and reviews score is neutral (around 50) due to missing data."
        )

    prompt = f"""You are a reputation management analyst. Based on this data for "{company_name}":

Final Reputation Score: {score_summary.get('final_score')}/100
Risk Level: {score_summary.get('risk_level')}
Reviews Score: {score_summary['breakdown']['reviews']['score']}/100
Visibility Score: {score_summary['breakdown']['visibility']['score']}/100
Negative Score: {score_summary['breakdown']['negative']['score']}/100
Number of negative findings: {score_summary['breakdown']['negative']['total_findings']}
{confidence_note}

Write a concise 2-3 sentence summary and 2-3 actionable recommendations.
Be precise: distinguish between "low visibility / insufficient data" and "confirmed negative reputation" —
these require different recommendations (build online presence vs. address specific complaints).
Return ONLY valid JSON, no markdown fences:
{{"summary": "...", "recommendations": ["...", "..."]}}"""

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)

        return {
            "status": "ok",
            "source": "groq",
            "summary": parsed.get("summary", ""),
            "recommendations": parsed.get("recommendations", []),
        }
    except Exception as exc:
        fallback = _template_recommendations(company_name, score_summary)
        fallback["error"] = f"Groq недоступен: {exc}"
        return fallback


def _template_recommendations(company_name: str, score_summary: dict) -> dict:
    """Статичный шаблон без AI — используется если GROQ_API_KEY не задан."""
    risk = score_summary.get("risk_level", "MEDIUM_RISK")
    findings_count = score_summary["breakdown"]["negative"]["total_findings"]
    low_data = bool(score_summary.get("confidence_warning"))

    if low_data and findings_count == 0:
        summary = (
            f"{company_name} имеет низкий скор из-за недостатка данных в открытых источниках, "
            f"а не из-за подтверждённого негатива — конкретных жалоб или скандалов не найдено."
        )
        recommendations = [
            "Проверить название компании на опечатки — возможно, поиск не находит верную страницу.",
            "Создать/заполнить профили на Trustpilot, Google Maps, Glassdoor для повышения видимости.",
            "Повторить анализ позже — поисковые источники иногда временно недоступны.",
        ]
    else:
        templates = {
            "LOW_RISK": f"{company_name} демонстрирует стабильную онлайн-репутацию с минимальными рисками.",
            "MEDIUM_RISK": f"{company_name} имеет умеренные репутационные риски, требующие внимания.",
            "HIGH_RISK": f"{company_name} демонстрирует значительные репутационные риски, требующие немедленных действий.",
        }
        summary = templates.get(risk, templates["MEDIUM_RISK"])
        recommendations = ["Настроить мониторинг упоминаний компании на регулярной основе."]
        if findings_count > 0:
            recommendations.append(f"Проработать {findings_count} найденных негативных упоминаний.")
        recommendations.append("Увеличить количество позитивных отзывов на профильных платформах.")

    return {
        "status": "template",
        "source": "static_template",
        "summary": summary,
        "recommendations": recommendations,
    }
