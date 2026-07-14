#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO Slack Bot
- 지정한 RSS 피드를 확인해서 '새 글'만 Slack 채널에 올립니다.
- 외부 라이브러리 없이 파이썬 기본 기능(stdlib)만 사용합니다.
- 이미 올린 글은 seen.json에 기록해 중복을 막습니다.

바꾸고 싶으면 아래 [설정] 부분만 손대면 됩니다.
"""

import os
import re
import json
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# ============================================================
# [설정] ─ 여기만 고치면 됩니다
# ============================================================

# 1) 받아볼 피드 목록 (이름, 주소, 필터여부)
#    filter=False 인 피드는 키워드 필터를 안 걸고 '전부' 올립니다.
#    (구글 공식은 글이 드물고 하나하나 중요해서 전부 받도록 예외 처리)
FEEDS = [
    # ── 공식 소스: 필터 없이 전부 받기 (글이 드물고 하나하나 중요) ──
    {"name": "구글 검색 공식", "url": "https://developers.google.com/search/blog/feed.xml", "filter": False},
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "filter": False},
    {"name": "Bing Blogs", "url": "https://blogs.bing.com/feed", "filter": False},
    # ── 업계·집계 소스: GEO/모델 키워드로 필터 ──
    {"name": "SEJ · Generative AI", "url": "https://www.searchenginejournal.com/category/generative-ai/feed/", "filter": True},
    {"name": "GeekNews", "url": "https://news.hada.io/rss/news", "filter": True},
    # ── 참고: Anthropic·Perplexity는 공식 RSS가 없습니다 ──
    # 아래는 openrss.org(비공식) 경유라 불안정합니다. 원하면 앞의 #만 지워서 켜세요.
    # 안 되면 봇이 알아서 건너뜁니다. (이들 소식은 SEJ·GeekNews + 키워드로도 잡힙니다)
    # {"name": "Anthropic(비공식)", "url": "https://openrss.org/www.anthropic.com/news", "filter": False},
    # {"name": "Perplexity(비공식)", "url": "https://openrss.org/www.perplexity.ai/hub/blog", "filter": False},
]

# 2) 키워드 필터. 제목/요약에 이 중 하나라도 들어간 글만 올립니다. (대소문자 무시)
#    'AI'가 email 같은 단어에 잘못 걸리지 않도록 '단어 단위'로 매칭합니다.
#    비워두면(=[]) 필터 없이 전부 올립니다.
KEYWORDS = [
    # ── GEO 개념 ──
    "GEO", "AEO", "SEO",
    "AI Overview", "AI Mode", "AI search", "ChatGPT", "Perplexity", "zero-click"
    # ── 모델 출시·업데이트 뉴스 ──
    "Gemini", "Claude", "Copilot", "Anthropic", "GPT", "Grok", 
     "Google AI", "AI model", "language model"
]

# 3) 한 번 실행에 최대 몇 개까지 올릴지 (폭탄 방지)
MAX_POST_PER_RUN = 10

# 4) 제목·요약을 한국어로 번역해서 함께 올릴지 (True/False). 무료, API 키 불필요.
#    이미 한국어인 글(GeekNews 등)은 자동으로 번역을 건너뜁니다.
TRANSLATE_TO_KOREAN = True

# ============================================================
# 여기부터는 손대지 않아도 됩니다
# ============================================================

CACHE_FILE = "seen.json"
MAX_CACHE = 800  # 기록 파일이 무한정 커지지 않게 최근 것만 유지
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip() != ""  # 테스트용: 실제 전송 대신 출력

USER_AGENT = "Mozilla/5.0 (compatible; GEO-Slack-Bot/1.0)"


def fetch(url):
    """URL 또는 로컬 파일에서 내용을 읽어온다."""
    if url.startswith("http"):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    # 로컬 파일 (테스트용)
    with open(url, "r", encoding="utf-8") as f:
        return f.read()


def strip_tags(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(xml_text, feed_name):
    """RSS(2.0)와 Atom을 모두 최소한으로 지원. 아이템 리스트를 반환."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0: channel/item
    for item in root.iter("item"):
        title = item.findtext("title") or "(제목 없음)"
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or link
        date = item.findtext("pubDate") or ""
        desc = strip_tags(item.findtext("description") or "")
        items.append({
            "id": guid.strip(),
            "title": strip_tags(title),
            "link": link.strip(),
            "date": date.strip(),
            "desc": desc,
            "feed": feed_name,
        })

    # Atom: entry (RSS로 이미 잡혔으면 건너뜀)
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = entry.findtext("a:title", default="(제목 없음)", namespaces=ns)
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            eid = entry.findtext("a:id", default=link, namespaces=ns)
            date = entry.findtext("a:updated", default="", namespaces=ns)
            summary = entry.findtext("a:summary", default="", namespaces=ns)
            items.append({
                "id": (eid or link).strip(),
                "title": strip_tags(title),
                "link": (link or "").strip(),
                "date": (date or "").strip(),
                "desc": strip_tags(summary),
                "feed": feed_name,
            })
    return items


# 영어 키워드는 '단어 단위'로 찾습니다 (앞뒤가 글자면 매칭 안 됨 → 'AI'가 'email'에 안 걸림).
# 끝에 s? 를 붙여 복수형(LLMs, AI Overviews, agents 등)도 함께 잡습니다.
# 한국어 키워드는 단어 경계 규칙이 잘 안 맞아 '부분 일치'로 찾습니다.
def _compile(k):
    if k.isascii():
        return re.compile(r'(?<!\w)' + re.escape(k) + r's?(?!\w)', re.IGNORECASE)
    return re.compile(re.escape(k), re.IGNORECASE)


_PATTERNS = [_compile(k) for k in KEYWORDS]


def passes_keyword(item):
    if not KEYWORDS or not item.get("_filter", True):
        return True
    haystack = item["title"] + " " + item["desc"]
    return any(p.search(haystack) for p in _PATTERNS)


def load_seen():
    if not os.path.exists(CACHE_FILE):
        return None  # None = 첫 실행
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_seen(seen_list):
    trimmed = seen_list[-MAX_CACHE:]
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=0)


def _has_hangul(s):
    return any("가" <= ch <= "힣" for ch in (s or ""))


def translate_ko(text):
    """영어(등) 텍스트를 한국어로 번역. 무료 엔드포인트 사용, 키 불필요.
    이미 한국어면 그대로 두고, 실패하면 원문을 그대로 반환(안전장치)."""
    text = (text or "").strip()
    if not text or not TRANSLATE_TO_KOREAN or _has_hangul(text):
        return text
    # 1순위: Google 무료 번역 엔드포인트
    try:
        q = urllib.parse.quote(text[:900])
        url = ("https://translate.googleapis.com/translate_a/single"
               f"?client=gtx&sl=auto&tl=ko&dt=t&q={q}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        if out.strip():
            return out.strip()
    except Exception:  # noqa
        pass
    # 2순위: MyMemory 무료 API
    try:
        q = urllib.parse.quote(text[:450])
        url = f"https://api.mymemory.translated.net/get?q={q}&langpair=en|ko"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if out:
            return out
    except Exception:  # noqa
        pass
    return text  # 둘 다 실패하면 원문 유지


def post_to_slack(item):
    ko_title = translate_ko(item["title"])
    link = item["link"]
    meta = f"{item['feed']}"
    if item["date"]:
        meta += f" · {item['date']}"
    lines = [f"*<{link}|{ko_title}>*" if link else f"*{ko_title}*"]
    if item.get("desc"):
        ko_desc = translate_ko(item["desc"][:300])
        if len(ko_desc) > 150:
            ko_desc = ko_desc[:150].rstrip() + "…"
        lines.append(ko_desc)
    lines.append(f"_{meta}_")
    text = "\n".join(lines)
    payload = json.dumps({"text": text}).encode("utf-8")

    if DRY_RUN or not SLACK_WEBHOOK:
        print("[DRY_RUN] would post:\n" + text + "\n")
        return True
    req = urllib.request.Request(
        SLACK_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:  # noqa
        print(f"[ERROR] Slack 전송 실패: {e}")
        return False


def post_hello():
    text = ("✅ GEO 소식 봇이 연결되었습니다. 이제 새 글이 올라오면 자동으로 알려드릴게요.\n"
            f"_감시 중인 피드: {', '.join(f['name'] for f in FEEDS)}_")
    if DRY_RUN or not SLACK_WEBHOOK:
        print("[DRY_RUN] hello:\n" + text + "\n")
        return
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:  # noqa
        print(f"[ERROR] Slack 전송 실패: {e}")


def main():
    # 1) 모든 피드에서 현재 글 수집
    all_items = []
    for feed in FEEDS:
        try:
            xml_text = fetch(feed["url"])
            items = parse_feed(xml_text, feed["name"])
            for it in items:
                it["_filter"] = feed.get("filter", True)  # 이 글에 필터 적용할지
            print(f"[INFO] {feed['name']}: {len(items)}개 글 확인")
            all_items.extend(items)
        except Exception as e:  # noqa
            print(f"[ERROR] {feed['name']} 가져오기 실패: {e}")

    seen = load_seen()

    # 2) 첫 실행: 과거 글 폭탄 방지 — 전부 '읽음' 처리하고 인사만
    if seen is None:
        ids = [it["id"] for it in all_items if it["id"]]
        save_seen(ids)
        post_hello()
        print(f"[INFO] 첫 실행: 기존 {len(ids)}개 글을 읽음 처리했습니다.")
        return

    seen_set = set(seen)

    # 3) 새 글만 골라 (오래된 것부터) 전송
    new_items = [it for it in all_items
                 if it["id"] and it["id"] not in seen_set and passes_keyword(it)]
    new_items = list(reversed(new_items))  # 피드는 최신순 → 오래된 순으로 전송

    posted = 0
    for it in new_items:
        if posted >= MAX_POST_PER_RUN:
            print(f"[INFO] 한 번 제한({MAX_POST_PER_RUN}) 도달. 나머지는 다음 실행에.")
            break
        if post_to_slack(it):
            seen.append(it["id"])
            seen_set.add(it["id"])
            posted += 1

    save_seen(seen)
    print(f"[INFO] 새 글 {posted}개 전송 완료.")


if __name__ == "__main__":
    main()
