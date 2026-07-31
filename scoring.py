from textblob import TextBlob

def calculate_score_and_sentiment(findings: list) -> dict:
    if not findings:
        return {"score": 50, "confidence": 20, "avg_sentiment": 0.0}

    total_sentiment = 0.0
    for f in findings:
        text = f.get("text", "") or f.get("title", "")
        polarity = TextBlob(text).sentiment.polarity
        f["sentiment"] = polarity
        total_sentiment += polarity

    avg_sentiment = total_sentiment / len(findings)
    
    # Перевод шкалы тональности из [-1.0, 1.0] в балл от 0 до 100
    base_score = round((avg_sentiment + 1.0) * 50)
    confidence = min(100, len(findings) * 12)

    return {
        "score": base_score,
        "confidence": confidence,
        "avg_sentiment": round(avg_sentiment, 2)
    }
