import streamlit as st
from modules.search_engine import SearchEngine
from modules.scoring import calculate_score_and_sentiment
from modules.ai_analyzer import AIAnalyzer

st.set_page_config(page_title="Reputation Qualifier", page_icon="🎯", layout="wide")

st.title("🎯 B2B Reputation Qualifier & Outreach Tool")

# Извлечение API ключей из Streamlit Secrets
GROQ_KEY = st.secrets.get("GROQ_API_KEY", "")
NEWS_KEY = st.secrets.get("NEWS_API_KEY", "")

@st.cache_data(ttl=86400, show_spinner=False)
def analyze_company(company_name: str):
    engine = SearchEngine(news_api_key=NEWS_KEY)
    ai = AIAnalyzer(groq_key=GROQ_KEY)

    # 1. Сбор данных
    raw_data = engine.collect_all(company_name)
    findings = raw_data["findings"]
    
    # 2. Скоринг
    metrics = calculate_score_and_sentiment(findings)
    
    # 3. AI-генерация угла для аутрича
    ai_res = ai.generate_outreach(company_name, findings, metrics["score"])

    return {"metrics": metrics, "ai": ai_res, "findings": findings, "osm": raw_data["osm"]}

with st.form("search"):
    company = st.text_input("Введите название компании:", value="Tesla")
    btn = st.form_submit_button("🔍 Проверить репутацию")

if btn and company:
    with st.spinner("Собираем открытые данные и рассчитываем оффер..."):
        data = analyze_company(company)
        
        m = data["metrics"]
        ai = data["ai"]

        st.divider()

        c1, c2, c3 = st.columns(3)
        c1.metric("Reputation Score", f"{m['score']} / 100")
        c2.metric("Уверенность данных", f"{m['confidence']}%")
        c3.metric("Уровень риска", ai.get("risk_level", "N/A"))

        st.subheader("💡 Угол для аутрича (Outreach Angle):")
        st.info(ai.get("outreach_angle"))

        st.subheader("📊 Резюме:")
        st.write(ai.get("summary"))
        st.caption(f"📍 {data['osm']}")

        with st.expander("🔗 Посмотреть найденные источники"):
            for f in data["findings"]:
                st.write(f"• **[{f['source'].upper()}]** [{f['title']}]({f['url']})")
