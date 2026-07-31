import httpx
from newsapi import NewsApiClient
from geopy.geocoders import Nominatim

def get_reddit_reviews(company_name: str) -> list:
    """Парсинг Reddit через публичный JSON API без OAuth ключей"""
    url = f"https://www.reddit.com/search.json?q={company_name}+reviews+OR+complaints&limit=5"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ReputationBot/1.0"}
    findings = []
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                for post in resp.json().get("data", {}).get("children", []):
                    p = post.get("data", {})
                    findings.append({
                        "source": "reddit",
                        "title": p.get("title", ""),
                        "text": p.get("selftext", "")[:300],
                        "url": f"https://reddit.com{p.get('permalink')}"
                    })
    except Exception:
        pass
    return findings

def get_news_api(company_name: str, api_key: str) -> list:
    """Сбор последних упоминаний из СМИ через NewsAPI.org"""
    if not api_key:
        return []
    findings = []
    try:
        newsapi = NewsApiClient(api_key=api_key)
        articles = newsapi.get_everything(q=f'"{company_name}"', language='en', page_size=5)
        for a in articles.get("articles", []):
            findings.append({
                "source": "news",
                "title": a.get("title", ""),
                "text": a.get("description", "") or "",
                "url": a.get("url", "")
            })
    except Exception:
        pass
    return findings

def check_osm_address(company_name: str) -> str:
    """Проверка географического наличия компании на картах OpenStreetMap"""
    try:
        geolocator = Nominatim(user_agent="reputation_scanner_app")
        location = geolocator.geocode(company_name)
        if location:
            return f"Найден адрес на карте: {location.address}"
    except Exception:
        pass
    return "Адрес на OpenStreetMap не найден"

