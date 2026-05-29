import anthropic
import requests
import smtplib
import os
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
 
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.strftime("%Y년 %m월 %d일")
WEEKDAY = ["월", "화", "수", "목", "금", "토", "일"][NOW.weekday()]
IS_THURSDAY = NOW.weekday() == 3
 
# ── 소스 정의 (NewsAPI.ai 카테고리 기반) ────────────────
SOURCES = [
    {
        "section": "📈 경제",
        "location": "http://en.wikipedia.org/wiki/South_Korea",
        "lang": "kor",
        "category": "business",
        "max": 20,
    },
    {
        "section": "🌍 정치 & 외교",
        "location": None,
        "lang": "kor,eng",
        "category": "politics",
        "max": 20,
    },
]
 
# 목요일 전용 부동산 지표 섹션
THURSDAY_SOURCES = [
    {
        "section": "🏘️ 이번 주 부동산 지표",
        "keywords": ["아파트 매매가격지수", "전세가율", "미분양", "서울 아파트 거래량"],
        "location": "http://en.wikipedia.org/wiki/South_Korea",
        "lang": "kor",
        "category": "business",
        "max": 5,
        "summary_prompt": "부동산 시장 지표 분석",
    },
]
 
NEWSAPI_URL = "https://eventregistry.org/api/v1/article/getArticles"
 
 
def fetch_section(location: str, lang: str, category: str, max_articles: int) -> list:
    api_key = os.environ["NEWSAPI_KEY"]
    articles = []
    seen = set()
    lang_list = lang.split(",") if "," in lang else [lang]
 
    payload = {
        "action": "getArticles",
        "lang": lang_list,
        "dateStart": (NOW - timedelta(hours=36)).strftime("%Y-%m-%d"),
        "dateEnd": NOW.strftime("%Y-%m-%d"),
        "articlesSortBy": "date",
        "articlesSortByAsc": False,
        "articlesCount": max_articles * 2,
        "resultType": "articles",
        "apiKey": api_key,
    }
 
    if location:
        payload["sourceLocationUri"] = location
    if category == "business":
        payload["categoryUri"] = "dmoz/Business"
    elif category == "politics":
        payload["categoryUri"] = "dmoz/Society/Politics"
 
    try:
        res = requests.post(NEWSAPI_URL, json=payload, timeout=30)
        data = res.json()
        total = data.get("articles", {}).get("totalResults", 0)
        print(f"  API 응답: 총 {total}건")
        for art in data.get("articles", {}).get("results", []):
            title = art.get("title", "").strip()
            link = art.get("url", "").strip()
            source = art.get("source", {}).get("title", "")
            if not title or not link or title in seen:
                continue
            seen.add(title)
            articles.append({"title": title, "link": link, "source": source})
            if len(articles) >= max_articles:
                break
    except Exception as e:
        print(f"  NewsAPI 오류: {e}")
    return articles
 
 
def deduplicate(articles: list, seen_titles: set) -> list:
    result = []
    for a in articles:
        title = a["title"]
        words = set(title.replace(" ", "")[:20])
        is_dup = False
        for seen in seen_titles:
            seen_words = set(seen.replace(" ", "")[:20])
            overlap = len(words & seen_words) / max(len(words), 1)
            if overlap > 0.6:
                is_dup = True
                break
        if not is_dup:
            seen_titles.add(title)
            result.append(a)
    return result
 
 
def summarize_batch(articles: list, is_indicator: bool = False) -> list:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    items_text = "\n".join(f"{i+1}. {a['title']}" for i, a in enumerate(articles))
 
    if is_indicator:
        extra = "수치와 전주 대비 변동을 반드시 포함해. 없으면 기사에서 유추 가능한 방향성(상승/하락/보합)을 명시해."
    else:
        extra = ""
 
    prompt = f"""다음 뉴스 제목들을 각각 요약해줘. {extra}
반드시 아래 형식을 정확히 지켜. 번호 순서대로, 다른 말 없이 JSON 배열만 출력해.
 
형식:
[
  {{
    "what": "무슨 일이 일어났는지 (1줄, 핵심 사실만)",
    "why": "왜 중요한지 (1줄, 시장/경제/외교 영향)",
    "num": "핵심 수치 또는 방향성 (없으면 '-')",
    "score": 중요도 1-5 정수 (5=매우 중요. 기준: 정책발표/수치발표/정상회담=5, 분석칼럼/전망=2)
  }}
]
 
뉴스 목록:
{items_text}"""
 
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        start, end = raw.find("["), raw.rfind("]") + 1
        summaries = json.loads(raw[start:end])
        for i, a in enumerate(articles):
            a["summary"] = summaries[i] if i < len(summaries) else {"what": a["title"][:40], "why": "-", "num": "-", "score": 3}
        articles.sort(key=lambda x: x.get("summary", {}).get("score", 3), reverse=True)
    except Exception as e:
        print(f"  요약 오류: {e}")
        for a in articles:
            a["summary"] = {"what": a["title"][:40], "why": "-", "num": "-", "score": 3}
    return articles
 
 
def extract_keywords(all_articles: list) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    titles = " / ".join(a["title"] for a in all_articles[:15])
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content":
                f"다음 뉴스들에서 오늘의 핵심 키워드 3개만 추출해. "
                f"'키워드1 · 키워드2 · 키워드3' 형식으로만 출력. 다른 말 없이.\n{titles}"}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return "경제 · 금융 · 시장"
 
 
def generate_briefing(all_articles: list) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    titles = " / ".join(a["title"] for a in all_articles[:20])
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content":
                f"다음 뉴스들을 보고 오늘의 핵심 흐름을 한 문장으로 요약해줘. "
                f"경제·외교·지정학 흐름을 연결해서, 30자 이내로. 다른 말 없이 문장만.\n{titles}"}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return "오늘의 주요 경제·외교 동향을 확인하세요."
 
 
# ── HTML 생성 ─────────────────────────────────────────
def card(article: dict, is_indicator: bool = False) -> str:
    s = article.get("summary", {})
    what, why, num = s.get("what", "-"), s.get("why", "-"), s.get("num", "-")
    source = f'<span style="color:#888;font-size:11px">{article["source"]}</span>' if article["source"] else ""
    num_color = "#155724" if not is_indicator else "#0c3547"
    num_bg = "#D4EDDA" if not is_indicator else "#cce8f4"
 
    return f"""
    <div style="background:#1e1e1e;border:1px solid #2e2e2e;border-radius:8px;
                padding:16px 18px;margin-bottom:10px;">
      <div style="margin-bottom:8px;display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
        <a href="{article['link']}" style="color:#e8e8e8;font-size:14px;font-weight:600;
           text-decoration:none;line-height:1.4;flex:1;">{article['title']}</a>
        {source}
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px;">
        <tr>
          <td style="padding:3px 0;width:60px;vertical-align:top;">
            <span style="background:#3a3000;color:#f0b429;padding:2px 6px;
                  border-radius:3px;font-size:11px;font-weight:600;">무슨 일</span>
          </td>
          <td style="color:#c8c8c8;padding:3px 0 3px 8px;line-height:1.5;">{what}</td>
        </tr>
        <tr>
          <td style="padding:3px 0;vertical-align:top;">
            <span style="background:#002a33;color:#5bc8d8;padding:2px 6px;
                  border-radius:3px;font-size:11px;font-weight:600;">왜 중요</span>
          </td>
          <td style="color:#c8c8c8;padding:3px 0 3px 8px;line-height:1.5;">{why}</td>
        </tr>
        <tr>
          <td style="padding:3px 0;vertical-align:top;">
            <span style="background:#002200;color:#4ade80;padding:2px 6px;
                  border-radius:3px;font-size:11px;font-weight:600;">수치</span>
          </td>
          <td style="color:#e8e8e8;padding:3px 0 3px 8px;font-weight:600;">{num}</td>
        </tr>
      </table>
    </div>"""
 
 
def build_thursday_banner() -> str:
    return """
    <div style="background:linear-gradient(135deg,#1a3a2a,#0f2a1a);border-radius:10px;
                padding:16px 20px;margin-bottom:20px;display:flex;align-items:center;gap:12px;">
      <div style="font-size:24px;">🏘️</div>
      <div>
        <div style="color:#4ade80;font-size:11px;font-weight:700;letter-spacing:1px;">THURSDAY SPECIAL</div>
        <div style="color:#fff;font-size:14px;font-weight:600;margin-top:2px;">이번 주 부동산 지표 포함</div>
      </div>
    </div>"""
 
 
def build_html(sections_data: list, keywords: str, briefing: str = "") -> str:
    sections_html = ""
    for sec in sections_data:
        is_indicator = "지표" in sec["section"]
        is_marketpulse = sec["section"] in ["📈 경제", "🏘️ 이번 주 부동산 지표"]
        icon_map = {
            "📈 경제": "📈",
            "🌍 정치 & 외교": "🌍",
            "🏘️ 이번 주 부동산 지표": "🏘️",
        }
        icon = icon_map.get(sec["section"], "📰")
        label = sec["section"].replace("🏘️ ", "")
        border_color = "#4ade80" if is_indicator else ("#f0b429" if is_marketpulse else "#7c6fe0")
        cards_html = "".join(card(a, is_indicator) for a in sec["articles"])
 
        # 지정학 섹션 앞에 구분선 추가
        divider = ""
        if sec["section"] == "🌍 정치 & 외교":
            divider = '<div style="border-top:1px solid #2e2e2e;margin:24px 0 28px;"></div>'
 
        sections_html += f"""
        {divider}
        <div style="margin-bottom:28px;">
          <h2 style="font-size:15px;font-weight:700;color:#e8e8e8;
                     border-bottom:2px solid {border_color};padding-bottom:6px;margin-bottom:14px;">
            {icon} {label}
          </h2>
          {cards_html}
        </div>"""
 
    thursday_banner = build_thursday_banner() if IS_THURSDAY else ""
    weekday_badge = f'<span style="background:#f0b429;color:#0d0d0d;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:700;">{WEEKDAY}요일</span>'
 
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:wght@400;600;700&family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#0d0d0d;font-family:'IBM Plex Sans KR','Apple SD Gothic Neo',sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:20px 16px;">
 
    <!-- 메인 헤더 -->
    <div style="background:#141414;border:1px solid #2a2a2a;border-radius:12px;padding:28px 24px;margin-bottom:16px;text-align:center;">
      <div style="font-size:30px;font-weight:700;color:#f0b429;letter-spacing:-1px;font-family:'IBM Plex Serif',serif;">잡학다식</div>
      <div style="color:#555;font-size:11px;margin-top:4px;letter-spacing:2px;">DAILY INTELLIGENCE BRIEFING</div>
      <div style="margin-top:16px;padding-top:16px;border-top:1px solid #2a2a2a;">
        <div style="color:#888;font-size:11px;letter-spacing:1px;margin-bottom:4px;">📈 MarketPulse</div>
        <div style="color:#555;font-size:10px;letter-spacing:1px;">ECONOMIC & GEOPOLITICAL BRIEFING</div>
      </div>
      <div style="color:#999;font-size:13px;margin-top:12px;">
        {TODAY} &nbsp;{weekday_badge}
      </div>
    </div>
 
    {thursday_banner}
 
    <!-- 한줄 브리핑 -->
    <div style="background:#141414;border:1px solid #2a2a2a;border-radius:8px;
                padding:14px 18px;margin-bottom:12px;text-align:center;">
      <div style="font-size:10px;color:#888;font-weight:700;letter-spacing:2px;margin-bottom:6px;">
        TODAY'S BRIEFING
      </div>
      <div style="font-size:14px;color:#e0e0e0;line-height:1.6;">{briefing}</div>
    </div>
 
    <!-- 키워드 -->
    <div style="background:#1a1500;border:1px solid #3a2e00;border-radius:8px;
                padding:14px 18px;margin-bottom:24px;text-align:center;">
      <div style="font-size:10px;color:#f0b429;font-weight:700;letter-spacing:2px;margin-bottom:6px;">
        TODAY'S KEYWORDS
      </div>
      <div style="font-size:16px;font-weight:700;color:#f0f0f0;">{keywords}</div>
    </div>
 
    {sections_html}
 
    <div style="text-align:center;padding:16px 0;color:#444;font-size:11px;">
      잡학다식 · Powered by Google News + Claude Haiku<br>
      자동 발송 · 매일 오전 6시 KST
    </div>
  </div>
</body>
</html>"""
 
 
def send_email(html: str, keywords: str):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    receiver_raw = os.environ.get("RECEIVER_EMAIL", sender)
    receivers = [r.strip() for r in receiver_raw.split(",")]
 
    thursday_tag = " 🏘️" if IS_THURSDAY else ""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[JHDS] 📰 {TODAY} ({WEEKDAY}){thursday_tag} — {keywords}"
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg.attach(MIMEText(html, "html", "utf-8"))
 
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, receivers, msg.as_string())
 
    print(f"✅ 이메일 발송 완료 → {', '.join(receivers)}")
 
 
# ── 메인 ─────────────────────────────────────────────
if __name__ == "__main__":
    print(f"=== MarketPulse {TODAY} ({WEEKDAY}요일) ===")
    if IS_THURSDAY:
        print("📅 목요일 — 부동산 지표 섹션 포함")
 
    active_sources = SOURCES + (THURSDAY_SOURCES if IS_THURSDAY else [])
    sections_data = []
    all_articles = []
    seen_titles = set()  # 전체 중복 제거용
 
    for src in active_sources:
        print(f"\n[{src['section']}] 수집 중...")
        articles = fetch_section(src.get("location"), src.get("lang", "kor"), src.get("category", ""), src["max"])
        articles = deduplicate(articles, seen_titles)  # 중복 제거
        print(f"  {len(articles)}건 수집 (중복 제거 후)")
        if articles:
            is_ind = "지표" in src["section"]
            articles = summarize_batch(articles, is_indicator=is_ind)
            sections_data.append({"section": src["section"], "articles": articles})
            all_articles.extend(articles)
 
    print(f"\n총 {len(all_articles)}건 수집")
    print("\n키워드 추출 중...")
    keywords = extract_keywords(all_articles)
    print(f"키워드: {keywords}")
 
    print("\n한줄 브리핑 생성 중...")
    briefing = generate_briefing(all_articles)
    print(f"브리핑: {briefing}")
 
    html = build_html(sections_data, keywords, briefing)
    print("\n이메일 발송 중...")
    send_email(html, keywords)
    print("\n✅ 완료")
