"""
search_engine.py
Модуль поиска упоминаний компании из открытых источников.

Текущая конфигурация:
Bright Data SERP API (основной, платный — уже подключен) -> DuckDuckGo (fallback, без ключа)
-> Reddit JSON (без ключа, публичный)

Bright Data SERP API стабильнее DuckDuckGo на облачных IP Streamlit Cloud,
так как запросы идут через резидентные прокси, а не напрямую с забаненного
диапазона адресов облачных провайдеров.

Brave Search оставлен как ДОПОЛНИТЕЛЬНЫЙ fallback — если добавишь
BRAVE_API_KEY в st.secrets, он попробуется, если и Bright Data, и DuckDuckGo
недоступны.

Каждая функция возвращает единый формат:
{
    "status": "ok" | "fallback" | "failed",
    "source": str,
    "data": list[dict],
    "error": str | None
}
"""

import json
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
NEWSAPI_ENDPOINT = "https://newsapi.org/v2/everything"
BRIGHTDATA_ENDPOINT = "https://api.brightdata.com/request"
BRIGHTDATA_ZONE = "serp_api"
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
# BRIGHT DATA SERP API (ОСНОВНОЙ источник — стабильнее DuckDuckGo на облаке)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _brightdata_search_raw(query: str, api_key: str, count: int = 10) -> list[dict]:
    """
    Запрос через Bright Data SERP API — прокси-парсинг реальной Google-выдачи.
    Формат запроса зафиксирован Bright Data:
    POST https://api.brightdata.com/request
    body: {"zone": "serp_api", "url": "<google search url>", "format": "json", "data_format": "parsed"}
    """
    import urllib.parse

    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&num={count}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "zone": BRIGHTDATA_ZONE,
        "url": search_url,
        "format": "json",
        "data_format": "parsed",
    }

    with httpx.Client(timeout=20.0) as client:
        resp = client.post(BRIGHTDATA_ENDPOINT, headers=headers, json=payload)
        resp.raise_for_status()
        payload_response = resp.json()

    # Bright Data оборачивает распарсенный результат в поле "body" (JSON-строка) или отдаёт напрямую,
    # в зависимости от data_format — обрабатываем оба варианта на всякий случай.
    parsed_body = payload_response
    if isinstance(payload_response, dict) and "body" in payload_response:
        raw_body = payload_response["body"]
        parsed_body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

    organic_results = parsed_body.get("organic", []) if isinstance(parsed_body, dict) else []

    results = []
    for item in organic_results[:count]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", item.get("url", "")),
            "snippet": item.get("description", item.get("snippet", "")),
            "age": "",
        })
    return results


def brightdata_search(query: str, count: int = 10) -> dict:
    """
    Поиск через Bright Data SERP API. Требует BRIGHTDATA_API_KEY в st.secrets.
    Это ОСНОВНОЙ источник — стабильнее DuckDuckGo на облачных IP Streamlit Cloud,
    так как идёт через резидентные прокси Bright Data, а не напрямую.
    """
    api_key = _get_secret("BRIGHTDATA_API_KEY")

    if not api_key:
        return {
            "status": "failed",
            "source": "brightdata",
            "data": [],
            "error": "BRIGHTDATA_API_KEY не задан в st.secrets.",
        }

    try:
        results = _brightdata_search_raw(query, api_key, count)
        if not results:
            return {
                "status": "failed",
                "source": "brightdata",
                "data": [],
                "error": "Bright Data вернул пустой результат (возможно, изменился формат ответа).",
            }
        return {"status": "ok", "source": "brightdata", "data": results, "error": None}
    except Exception as exc:
        return {"status": "failed", "source": "brightdata", "data": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# DUCKDUCKGO (fallback, без ключа — используется только если Bright Data недоступен)
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
    Порядок попыток: Bright Data SERP (основной, платный, стабилен на облаке)
    -> DuckDuckGo (бесплатный, без ключа, но может банить облачные IP)
    -> Brave (если есть ключ, дополнительный резерв).
    """
    brightdata_result = brightdata_search(query, count)
    if brightdata_result["status"] == "ok":
        return brightdata_result

    ddg_result = duckduckgo_search(query, count)
    if ddg_result["status"] == "ok":
        ddg_result["status"] = "fallback"
        ddg_result["error"] = f"Bright Data недоступен ({brightdata_result['error']}), использован DuckDuckGo."
        return ddg_result

    brave_result = brave_search(query, count)
    if brave_result["status"] == "ok":
        brave_result["status"] = "fallback"
        brave_result["error"] = (
            f"Bright Data: {brightdata_result['error']} | DuckDuckGo: {ddg_result['error']}. Использован Brave."
        )
        return brave_result

    return {
        "status": "failed",
        "source": "none",
        "data": [],
        "error": (
            f"Bright Data: {brightdata_result['error']} | "
            f"DuckDuckGo: {ddg_result['error']} | Brave: {brave_result['error']}"
        ),
    }


# ---------------------------------------------------------------------------
# NEWSAPI (структурированные новости с точными датами публикации)
# ---------------------------------------------------------------------------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
def _newsapi_search_raw(query: str, api_key: str, page_size: int = 15) -> list[dict]:
    headers = {"X-Api-Key": api_key}
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
    }

    with httpx.Client(timeout=10.0) as client:
        resp = client.get(NEWSAPI_ENDPOINT, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()

    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("message", "NewsAPI вернул ошибку."))

    results = []
    for article in payload.get("articles", []):
        results.append({
            "title": article.get("title", ""),
            "url": article.get("url", ""),
            "snippet": article.get("description") or article.get("content", "") or "",
            "source_name": article.get("source", {}).get("name", ""),
            "published_at": article.get("publishedAt", ""),  # ISO 8601, напр. "2026-07-15T10:30:00Z"
        })
    return results


def newsapi_search(company_name: str, extra_terms: str = "lawsuit OR scandal OR fraud OR complaint") -> dict:
    """
    Поиск новостных упоминаний через NewsAPI.org. Требует NEWSAPI_KEY в st.secrets.
    Даёт точные даты публикации (в отличие от DuckDuckGo-сниппетов), что критично
    для корректного recency decay в scoring.py — свежие негативные новости
    должны весить заметно больше старых.

    Работает НЕЗАВИСИМО и ПАРАЛЛЕЛЬНО с DuckDuckGo — это не замена, а
    дополнительный источник: DuckDuckGo покрывает форумы/блоги/отзывы,
    NewsAPI — только настоящие новостные издания.
    """
    api_key = _get_secret("NEWSAPI_KEY")

    if not api_key:
        return {
            "status": "failed",
            "source": "newsapi",
            "data": [],
            "error": "NEWSAPI_KEY не задан в st.secrets — новостной поиск через NewsAPI пропущен.",
        }

    query = f'"{company_name}" {extra_terms}'

    try:
        results = _newsapi_search_raw(query, api_key)
        return {"status": "ok", "source": "newsapi", "data": results, "error": None}
    except Exception as exc:
        return {"status": "failed", "source": "newsapi", "data": [], "error": str(exc)}


def newsapi_days_ago(published_at: str) -> float:
    """Конвертирует ISO 8601 дату публикации NewsAPI в количество дней назад."""
    if not published_at:
        return 365.0
    try:
        from datetime import datetime, timezone
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0.0, (now - published).total_seconds() / 86400.0)
    except Exception:
        return 365.0


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
    """
    Поиск новостных упоминаний (скандалы, судебные иски) — замена GDELT.
    Объединяет DuckDuckGo (широкий охват, без ключа) и NewsAPI (точные даты,
    если NEWSAPI_KEY задан) — это ДВА ПАРАЛЛЕЛЬНЫХ источника, не замена друг друга.
    """
    query = f'"{company_name}" news lawsuit OR scandal OR investigation'
    ddg_result = search_with_fallback(query, count=10)

    newsapi_result = newsapi_search(company_name)

    combined_data = list(ddg_result.get("data", []))
    newsapi_errors = None

    if newsapi_result["status"] == "ok":
        for article in newsapi_result["data"]:
            combined_data.append({
                "title": article["title"],
                "url": article["url"],
                "snippet": article["snippet"],
                "age": "",
                "source_name": article.get("source_name", ""),
                "published_at": article.get("published_at", ""),
            })
    else:
        newsapi_errors = newsapi_result["error"]

    if ddg_result["status"] == "failed" and newsapi_result["status"] == "failed":
        return {
            "status": "failed",
            "source": "none",
            "data": [],
            "error": f"DuckDuckGo: {ddg_result['error']} | NewsAPI: {newsapi_errors}",
        }

    return {
        "status": "ok" if combined_data else "failed",
        "source": "duckduckgo+newsapi",
        "data": combined_data,
        "error": newsapi_errors if newsapi_result["status"] == "failed" else None,
    }


def search_industry_competitors(niche: str, city: str = "", count: int = 8) -> dict:
    """
    Ищет конкурентов в нише для расчёта среднего рейтинга по отрасли —
    нужно для context-aware сравнения вместо жёсткого порога.
    """
    query = f'best {niche} companies {city} reviews rating'.strip()
    return search_with_fallback(query, count=count)
