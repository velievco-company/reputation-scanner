class SearchEngine:
    def __init__(self, news_api_key: str = None):
        self.news_key = news_api_key

    def collect_all(self, company_name: str) -> dict:
        findings = []
        
        # 1. Поиск через DuckDuckGo (без ключей)
        try:
            ddgs = DDGS()
            results = list(ddgs.text(f'"{company_name}" reviews complaints отзывы', max_results=7))
            for r in results:
                findings.append({
                    "source": "web",
                    "title": r.get("title", ""),
                    "text": r.get("body", ""),
                    "url": r.get("href", "")
                })
        except Exception:
            pass

        # 2. Reddit API (публичный JSON)
        findings.extend(get_reddit_reviews(company_name))
        
        # 3. NewsAPI
        findings.extend(get_news_api(company_name, self.news_key))
