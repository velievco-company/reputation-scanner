import json
from groq import Groq

class AIAnalyzer:
    def __init__(self, groq_key: str = None):
        self.client = Groq(api_key=groq_key) if groq_key else None

    def generate_outreach(self, company: str, findings: list, score: int) -> dict:
        if not self.client:
            return {
                "risk_level": "MEDIUM",
                "summary": "Анализ выполнен базовыми алгоритмами (без Groq API).",
                "outreach_angle": f"Предложить аудит цифрового присутствия и репутации для {company}."
            }

        prompt = f"""
        Analyze reputation findings for company: "{company}". 
        Score: {score}/100.
        Findings: {json.dumps(findings[:5], ensure_ascii=False)}

        Return ONLY a JSON object with keys:
        - "risk_level": "LOW" or "MEDIUM" or "HIGH"
        - "summary": "Short summary of findings in Russian"
        - "outreach_angle": "Best sales pitch/offer for cold B2B outreach in Russian"
        """

        try:
            res = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "You output JSON only."},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.3-70b-versatile",
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            return json.loads(res.choices[0].message.content)
        except Exception:
            return {
                "risk_level": "MEDIUM",
                "summary": "Ошибка вызова Groq API.",
                "outreach_angle": "Предложить стандартный аудит репутации."
            }
