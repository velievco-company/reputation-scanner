"""
search_engine.py
Модуль поиска упоминаний компании из открытых источников.

Текущая конфигурация (без API-ключей на старте):
DuckDuckGo (основной, без ключа) -> Reddit JSON (без ключа, публичный)

Brave Search оставлен как ОПЦИОНАЛЬНЫЙ модуль — если в будущем добавишь
BRAVE_API_KEY в st.secrets, он подключится автоматически как fallback,
когда DuckDuckGo не сработает. Без ключа просто пропускается.

Каждая функция возвращает единый формат:
{
    "status": "ok" | "fallback" | "failed",
    "source": str,
    "data": list[dict],
    "error": str | None
}
"""

import re

import httpx
import streamlit as st
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None


REDDIT_SEARCH_ENDPOINT = "https://www.reddit.com/search.json"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = "Mozilla/5.0 (compatible; ReputationScanner/1.0; +https://streamlit.app)"

NEGATIVE_KEYWORDS = [
    "scam", "fraud", "lawsuit", "complaint", "complaints",
    "sued", "court case", "ripoff", "warning",
    "мошенник", "жалоба", "суд", "развод", "обман",
]


def _get_secret(key: str) -> str | None:
    """Безопасно достаёт ключ из st.secrets. Если секретов нет — не падает."""
    try:
        return st.secrets.get(key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DUCKDUCKGO (ОСНОВНОЙ источник на старте — без ключа)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _duckduckgo_search_raw(query: str, count: int) -> list[dict]:
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=count):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
                "age": "",
            })
    return results


def duckduckgo_search(query: str, count: int = 10) -> dict:
    """Основной поиск через duckduckgo_search. Без ключа, бесплатно."""
    if DDGS is None:
        return {
            "status": "failed",
            "source": "duckduckgo",
            "data": [],
            "error": "Библиотека duckduckgo_search не установлена.",
        }

    try:
        results = _duckduckgo_search_raw(query, count)
        if not results:
            return {
                "status": "failed",
                "source": "duckduckgo",
                "data": [],
                "error": "Пустой результат (возможна временная блокировка облачного IP).",
            }
        return {"status": "ok", "source": "duckduckgo", "data": results, "error": None}
    except Exception as exc:
        return {"status": "failed", "source": "duckduckgo", "data": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# BRAVE SEARCH (опциональный fallback — активируется автоматически при наличии ключа)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _brave_search_raw(query: str, api_key: str, count: int = 10) -> list[dict]:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": count}

    with httpx.Client(timeout=10.0) as client:
        resp = client.get(BRAVE_ENDPOINT, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()

    results = []
    for item in payload.get("web", {}).get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
            "age": item.get("age", ""),
        })
    return results


def brave_search(query: str, count: int = 10) -> dict:
    """
    Поиск через Brave Search API. Работает ТОЛЬКО если BRAVE_API_KEY
    добавлен в st.secrets. Если ключа нет — возвращает failed,
    и search_with_fallback просто продолжит использовать DuckDuckGo.
    """
    api_key = _get_secret("BRAVE_API_KEY")

    if not api_key:
        return {
            "status": "failed",
            "source": "brave",
            "data": [],
            "error": "BRAVE_API_KEY не задан — используется бесплатный DuckDuckGo.",
        }

    try:
        results = _brave_search_raw(query, api_key, count)
        return {"status": "ok", "source": "brave", "data": results, "error": None}
    except Exception as exc:
        return {"status": "failed", "source": "brave", "data": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# ЕДИНАЯ ТОЧКА ВХОДА С FALLBACK-ЦЕПОЧКОЙ
# ---------------------------------------------------------------------------

def search_with_fallback(query: str, count: int = 10) -> dict:
    """
    Порядок попыток: DuckDuckGo (основной, бесплатный, без ключа) -> Brave (если есть ключ).
    """
    ddg_result = duckduckgo_search(query, count)
    if ddg_result["status"] == "ok":
        return ddg_result

    brave_result = brave_search(query, count)
    if brave_result["status"] == "ok":
        brave_result["status"] = "fallback"
        brave_result["error"] = f"DuckDuckGo недоступен ({ddg_result['error']}), использован Brave."
        return brave_result

    return {
        "status": "failed",
        "source": "none",
        "data": [],
        "error": f"DuckDuckGo: {ddg_result['error']} | Brave: {brave_result['error']}",
    }


# ---------------------------------------------------------------------------
# REDDIT (публичный JSON endpoint, без OAuth/ключа)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def reddit_search(company_name: str, limit: int = 15) -> dict:
    """
    Поиск упоминаний на Reddit через публичный .json endpoint.
    Без ключа, но с мягким rate limit — используем корректный User-Agent,
    чтобы снизить риск временной блокировки.
    """
    headers = {"User-Agent": USER_AGENT}
    params = {"q": company_name, "sort": "new", "limit": limit}

    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            resp = client.get(REDDIT_SEARCH_ENDPOINT, params=params)
            resp.raise_for_status()
            payload = resp.json()

        posts = []
        for child in payload.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append({
                "title": post.get("title", ""),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "snippet": post.get("selftext", "")[:300],
                "created_utc": post.get("created_utc"),
                "score": post.get("score", 0),
                "subreddit": post.get("subreddit", ""),
            })

        return {"status": "ok", "source": "reddit", "data": posts, "error": None}
    except Exception as exc:
        return {"status": "failed", "source": "reddit", "data": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# СПЕЦИАЛИЗИРОВАННЫЕ ПОИСКОВЫЕ ЗАПРОСЫ ПО КАТЕГОРИЯМ
# ---------------------------------------------------------------------------

def search_negative_mentions(company_name: str) -> dict:
    """Ищет негативные упоминания (скам, суды, жалобы)."""
    keyword_group = " OR ".join(f'"{kw}"' for kw in NEGATIVE_KEYWORDS[:6])
    query = f'"{company_name}" ({keyword_group})'
    return search_with_fallback(query, count=10)


def search_trustpilot(company_name: str) -> dict:
    query = f'site:trustpilot.com "{company_name}"'
    return search_with_fallback(query, count=5)


def search_glassdoor(company_name: str) -> dict:
    query = f'site:glassdoor.com "{company_name}" reviews'
    return search_with_fallback(query, count=5)


def search_google_maps(company_name: str, city: str = "") -> dict:
    """
    Возвращает только рейтинг и число отзывов через сниппет поисковой выдачи.
    Тексты отзывов НЕ извлекаются (недоступно без платного Google Places API).
    """
    query = f'site:google.com/maps "{company_name}" {city}'.strip()
    result = search_with_fallback(query, count=3)

    if result["status"] in ("ok", "fallback"):
        for item in result["data"]:
            rating, review_count = _extract_rating_from_snippet(item["snippet"])
            if rating is not None:
                item["parsed_rating"] = rating
                item["parsed_review_count"] = review_count

    return result


def _extract_rating_from_snippet(snippet: str) -> tuple[float | None, int | None]:
    """
    Извлекает рейтинг и число отзывов из текста сниппета,
    например: "4.2 ★ · 89 reviews" или "Rated 4.2 out of 5 · 89 отзывов".
    """
    rating_match = re.search(r"(\d\.\d)\s*(?:★|/5|out of 5|звезд)", snippet, re.IGNORECASE)
    review_match = re.search(r"(\d+)\s*(?:reviews|отзыв)", snippet, re.IGNORECASE)

    rating = float(rating_match.group(1)) if rating_match else None
    review_count = int(review_match.group(1)) if review_match else None
    return rating, review_count


def search_news(company_name: str) -> dict:
    """Поиск новостных упоминаний (скандалы, судебные иски) — замена GDELT."""
    query = f'"{company_name}" news lawsuit OR scandal OR investigation'
    return search_with_fallback(query, count=10)


def search_industry_competitors(niche: str, city: str = "", count: int = 8) -> dict:
    """
    Ищет конкурентов в нише для расчёта среднего рейтинга по отрасли —
    нужно для context-aware сравнения вместо жёсткого порога.
    """
    query = f'best {niche} companies {city} reviews rating'.strip()
    return search_with_fallback(query, count=count)
