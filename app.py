"""
app.py
Reputation Intelligence Platform — главный интерфейс Streamlit.

Деплой: Streamlit Community Cloud (share.streamlit.io)
Секреты (опционально, для расширенного функционала):
    GROQ_API_KEY   -> AI-анализ через Groq (без ключа: шаблонные рекомендации + TextBlob sentiment)
    BRAVE_API_KEY  -> Brave Search как fallback (без ключа: только DuckDuckGo)
"""

import io
from datetime import datetime

import pandas as pd
import streamlit as st

from modules import search_engine, scoring, ai_analyzer, report_generator


st.set_page_config(
    page_title="Reputation Intelligence Platform",
    page_icon="🔍",
    layout="wide",
)


# ---------------------------------------------------------------------------
# ОСНОВНАЯ ЛОГИКА АНАЛИЗА (кэшируется на 24 часа)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def run_analysis(company_name: str, website: str, location: str) -> dict:
    """
    Запускает полный цикл анализа компании.
    Результат кэшируется на 24 часа по ключу (company_name, website, location),
    чтобы повторные запросы не тратили лимиты API.
    """
    warnings = []
    sources_found = 0

    # --- Отзывы: Trustpilot, Glassdoor, Google Maps ---
    trustpilot_result = search_engine.search_trustpilot(company_name)
    glassdoor_result = search_engine.search_glassdoor(company_name)
    maps_result = search_engine.search_google_maps(company_name, location)

    company_ratings = []
    for result, source_name in [
        (trustpilot_result, "trustpilot"),
        (maps_result, "google_maps"),
    ]:
        if result["status"] in ("ok", "fallback") and result["data"]:
            sources_found += 1
            for item in result["data"]:
                rating = item.get("parsed_rating")
                if rating is not None:
                    company_ratings.append({
                        "source": source_name,
                        "rating": rating,
                        "review_count": item.get("parsed_review_count", 0),
                    })
        elif result["status"] == "failed":
            warnings.append(f"{source_name}: {result['error']}")

    if glassdoor_result["status"] in ("ok", "fallback") and glassdoor_result["data"]:
        sources_found += 1

    # --- Ниша: сравнение со средним рейтингом конкурентов (context-aware) ---
    niche_avg_rating, niche_avg_reviews = None, None
    if company_ratings:
        # Пытаемся определить нишу из первого найденного контекста (упрощённо)
        competitors_result = search_engine.search_industry_competitors("professional services", location)
        if competitors_result["status"] in ("ok", "fallback"):
            parsed_competitor_ratings = []
            for item in competitors_result["data"]:
                r, c = search_engine._extract_rating_from_snippet(item["snippet"])
                if r:
                    parsed_competitor_ratings.append((r, c or 0))
            if parsed_competitor_ratings:
                niche_avg_rating = sum(r for r, _ in parsed_competitor_ratings) / len(parsed_competitor_ratings)
                niche_avg_reviews = sum(c for _, c in parsed_competitor_ratings) / len(parsed_competitor_ratings)

    reviews_result = scoring.compute_reviews_score(
        company_ratings, niche_avg_rating, niche_avg_reviews
    )

    # --- Видимость ---
    general_search = search_engine.search_with_fallback(f'"{company_name}"', count=10)
    if general_search["status"] in ("ok", "fallback"):
        sources_found += 1
    else:
        warnings.append(f"Общий поиск: {general_search['error']}")

    news_result = search_engine.search_news(company_name)
    if news_result["status"] in ("ok", "fallback") and news_result["data"]:
        sources_found += 1

    has_website = bool(website)
    found_on_maps = maps_result["status"] in ("ok", "fallback") and len(maps_result["data"]) > 0
    mention_count = len(general_search["data"]) if general_search["status"] in ("ok", "fallback") else 0
    found_in_news = news_result["status"] in ("ok", "fallback") and len(news_result["data"]) > 0

    visibility_result = scoring.compute_visibility_score(
        has_working_website=has_website,
        found_on_maps=found_on_maps,
        mention_count=mention_count,
        found_in_news=found_in_news,
    )

    # --- Негатив ---
    negative_search = search_engine.search_negative_mentions(company_name)
    reddit_result = search_engine.reddit_search(company_name)

    if negative_search["status"] in ("ok", "fallback"):
        sources_found += 1
    else:
        warnings.append(f"Поиск негатива: {negative_search['error']}")

    if reddit_result["status"] == "ok":
        sources_found += 1
    else:
        warnings.append(f"Reddit: {reddit_result['error']}")

    negative_findings = []
    for item in negative_search.get("data", []):
        negative_findings.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "days_ago": 180,  # DuckDuckGo/Brave не всегда дают точную дату в сниппете
            "severity": "medium",
        })

    for post in reddit_result.get("data", []):
        days_ago = scoring.days_since(post.get("created_utc"))
        severity = "high" if post.get("score", 0) < -2 else "medium"
        negative_findings.append({
            "title": post.get("title", ""),
            "url": post.get("url", ""),
            "days_ago": days_ago,
            "severity": severity,
        })

    negative_result = scoring.compute_negative_score(negative_findings)

    # --- Финальный скор ---
    score_summary = scoring.compute_final_score(
        reviews_result, visibility_result, negative_result, sources_found
    )

    # --- AI-анализ ---
    texts_for_sentiment = [item.get("snippet", "") for item in negative_search.get("data", [])][:10]
    sentiment_result = ai_analyzer.analyze_sentiment_batch(texts_for_sentiment)
    ai_summary = ai_analyzer.generate_recommendations(company_name, score_summary)

    return {
        "score_summary": score_summary,
        "ai_summary": ai_summary,
        "sentiment_result": sentiment_result,
        "warnings": warnings,
        "sources_found": sources_found,
        "company_name": company_name,
        "website": website,
        "location": location,
        "analyzed_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# UI: ЗАГОЛОВОК
# ---------------------------------------------------------------------------

st.title("🔍 Reputation Intelligence Platform")
st.caption("Автоматический анализ онлайн-репутации компании из открытых источников.")

with st.expander("ℹ️ Как это работает"):
    st.markdown("""
    Система собирает данные из открытых источников (DuckDuckGo, Reddit, Google Maps,
    Trustpilot, Glassdoor) и рассчитывает Reputation Score (0-100) по трём категориям:

    - **Отзывы (35%)** — рейтинги, сравниваются со средним по нише, а не по жёсткому порогу
    - **Видимость (25%)** — наличие сайта, карточек, упоминаний в СМИ
    - **Негатив (40%)** — найденные жалобы, скандалы, судебные иски (свежие весят больше)

    При недостатке данных итоговый скор корректируется понижающим коэффициентом достоверности.
    """)

groq_available = ai_analyzer._get_groq_client() is not None
brave_available = search_engine._get_secret("BRAVE_API_KEY") is not None

status_cols = st.columns(2)
with status_cols[0]:
    if groq_available:
        st.success("✅ Groq AI подключен")
    else:
        st.warning("⚠️ Groq не подключен — используются шаблонные рекомендации")
with status_cols[1]:
    if brave_available:
        st.success("✅ Brave Search подключен")
    else:
        st.info("ℹ️ Brave не подключен — используется бесплатный DuckDuckGo")

st.divider()


# ---------------------------------------------------------------------------
# UI: РЕЖИМ АНАЛИЗА (одна компания / массовый)
# ---------------------------------------------------------------------------

tab_single, tab_bulk = st.tabs(["🔍 Одна компания", "📊 Массовый анализ (Excel/CSV)"])

with tab_single:
    with st.form("single_analysis_form"):
        col1, col2 = st.columns(2)
        with col1:
            company_name = st.text_input("Название компании *", placeholder="Например: TechFlow Solutions")
        with col2:
            website = st.text_input("Сайт компании", placeholder="https://example.com")

        location = st.text_input("Страна / Город", placeholder="Например: Auckland, New Zealand")

        submitted = st.form_submit_button("🔍 Начать анализ", type="primary", use_container_width=True)

    if submitted:
        if not company_name.strip():
            st.error("Введите название компании.")
        else:
            with st.spinner(f"Анализируем «{company_name}»... это может занять 20-40 секунд"):
                result = run_analysis(company_name.strip(), website.strip(), location.strip())

            score_summary = result["score_summary"]
            final_score = score_summary["final_score"]
            risk_level = score_summary["risk_level"]

            risk_emoji = {"LOW_RISK": "🟢", "MEDIUM_RISK": "🟡", "HIGH_RISK": "🔴"}
            st.divider()
            st.subheader(f"{risk_emoji.get(risk_level, '⚪')} {result['company_name']} — Score: {final_score}/100 ({risk_level})")

            if score_summary.get("confidence_warning"):
                st.warning(score_summary["confidence_warning"])

            metric_cols = st.columns(3)
            breakdown = score_summary["breakdown"]
            metric_cols[0].metric("📊 Отзывы", f"{breakdown['reviews']['score']}/100")
            metric_cols[1].metric("📈 Видимость", f"{breakdown['visibility']['score']}/100")
            metric_cols[2].metric("🛡️ Защита от негатива", f"{breakdown['negative']['score']}/100")

            st.markdown("**Детали по категориям:**")
            st.write(f"- Отзывы: {breakdown['reviews']['detail']}")
            st.write(f"- Видимость: {breakdown['visibility']['detail']}")

            findings = breakdown["negative"]["findings"]
            st.markdown(f"**🔍 Найденные риски ({len(findings)}):**")
            if not findings:
                st.write("Негативных упоминаний не найдено.")
            else:
                for f in findings:
                    st.write(f"- [{f['title']}]({f['url']}) — {f['days_ago']} дней назад (штраф: {f['penalty_applied']})")

            st.markdown("**💡 AI-рекомендации:**")
            st.info(result["ai_summary"].get("summary", ""))
            for rec in result["ai_summary"].get("recommendations", []):
                st.write(f"- {rec}")

            if result["warnings"]:
                with st.expander("⚠️ Технические предупреждения"):
                    for w in result["warnings"]:
                        st.write(f"- {w}")

            st.divider()
            st.markdown("**📥 Скачать отчёт:**")
            dl_col1, dl_col2 = st.columns(2)

            excel_bytes = report_generator.generate_excel_report(
                result["company_name"], result["website"], score_summary, result["ai_summary"]
            )
            pdf_bytes = report_generator.generate_pdf_report(
                result["company_name"], result["website"], score_summary, result["ai_summary"]
            )

            safe_name = "".join(c for c in result["company_name"] if c.isalnum() or c in " -_").strip()

            with dl_col1:
                st.download_button(
                    "📥 Скачать отчёт Excel (.xlsx)",
                    data=excel_bytes,
                    file_name=f"{safe_name}_reputation_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            with dl_col2:
                st.download_button(
                    "📥 Скачать отчёт PDF (.pdf)",
                    data=pdf_bytes,
                    file_name=f"{safe_name}_reputation_report.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )


with tab_bulk:
    st.markdown("Загрузите Excel или CSV файл со списком компаний для массового анализа.")
    st.caption("Ожидаемые колонки: **company_name** (обязательно), **website**, **location** (опционально)")

    uploaded_file = st.file_uploader("Выберите файл", type=["xlsx", "csv"])

    if uploaded_file is not None:
        if uploaded_file.name.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)

        if "company_name" not in df.columns:
            st.error("В файле должна быть колонка 'company_name'.")
        else:
            st.write(f"Найдено {len(df)} компаний в файле.")
            st.dataframe(df.head(10))

            estimated_time_min = round(len(df) * 30 / 60, 1)
            st.warning(
                f"⚠️ Массовый анализ {len(df)} компаний займёт примерно {estimated_time_min} минут "
                f"и потратит существенную часть лимитов API. Подтвердите запуск."
            )

            if st.button("🔍 Запустить массовый анализ", type="primary"):
                progress_bar = st.progress(0)
                status_text = st.empty()
                results_list = []

                for idx, row in df.iterrows():
                    company = str(row.get("company_name", "")).strip()
                    site = str(row.get("website", "")) if pd.notna(row.get("website", "")) else ""
                    loc = str(row.get("location", "")) if pd.notna(row.get("location", "")) else ""

                    status_text.text(f"Анализируем {idx + 1}/{len(df)}: {company}")
                    result = run_analysis(company, site, loc)

                    results_list.append({
                        "company_name": company,
                        "score": result["score_summary"]["final_score"],
                        "risk_level": result["score_summary"]["risk_level"],
                        "negative_findings": result["score_summary"]["breakdown"]["negative"]["total_findings"],
                    })
                    progress_bar.progress((idx + 1) / len(df))

                results_df = pd.DataFrame(results_list)
                st.success("✅ Массовый анализ завершён.")
                st.dataframe(results_df)

                buffer = io.BytesIO()
                results_df.to_excel(buffer, index=False, engine="openpyxl")
                buffer.seek(0)

                st.download_button(
                    "📥 Скачать сводный отчёт (.xlsx)",
                    data=buffer.getvalue(),
                    file_name="bulk_reputation_report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
