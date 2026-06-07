# -*- coding: utf-8 -*-
"""
クラウド本文取得テスト
----------------------
目的: クラウド(GitHub Actions等)のサーバーから、
      Googleニュースの記事本文がちゃんと取れるかだけを確認する。
      Geminiも画像生成も使わない。本文取得の成否判定だけ。

元の news_topic.py の本文取得関数をそのまま流用している。
（ロジックは1ミリも変えていない）
"""

import os
import re
import html
import json
import time
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote
from bs4 import BeautifulSoup

# テストで拾う記事数と、本文取得を試す件数
NUM_QUERIES_TO_TRY = 5      # 検索キーワード数
MAX_ARTICLES_TO_TEST = 8    # 本文取得を試す記事の上限
BODY_MIN_CHARS = 400        # これ以上なら「本文取得成功」とみなす（元コードと同基準）

SAUNA_WORDS = [
    "サウナ", "ロウリュ", "アウフグース", "水風呂", "外気浴", "ととのう",
    "熱波", "サウナー", "銭湯", "岩盤浴", "温浴",
]

NEWS_QUERIES = [
    "サウナ 健康", "サウナ 研究", "サウナ 効果",
    "フィンランド サウナ", "サ飯", "サウナ トレンド",
    "銭湯 文化", "サウナハット",
]


# ========== 以下、news_topic.py からそのまま流用した本文取得関数 ==========

def clean_title(title):
    title = html.unescape(title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def extract_source_domain(title):
    match = re.search(r' - ([a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,})\s*$', title)
    if match:
        return match.group(1)
    return ""


def resolve_via_rss_search(title, domain):
    keyword = re.sub(r' - [^-]+$', '', title).strip()
    keyword = keyword[:50]
    query = f"{keyword} site:{domain}"
    encoded = quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        response = requests.get(rss_url, timeout=10)
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        for item in items[:3]:
            link = item.findtext("link", "")
            if link and "news.google.com" in link:
                resolved = resolve_google_news_url(link)
                if resolved and domain in resolved:
                    return resolved
    except Exception as e:
        print(f"  RSS再検索エラー: {e}")
    return ""


def resolve_google_news_url(url):
    if "news.google.com" in url:
        try:
            from googlenewsdecoder import gnewsdecoder
            result = gnewsdecoder(url, interval=1)
            if result.get("status") and result.get("decoded_url"):
                decoded = result["decoded_url"]
                if "news.google.com" not in decoded:
                    return decoded
        except ImportError:
            print("  googlenewsdecoder未インストール")
        except Exception as e:
            print(f"  デコード失敗: {e}")
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, timeout=10, allow_redirects=True, headers=headers)
        return response.url
    except Exception as e:
        print(f"  URL解決エラー: {e}")
        return url


def extract_jsonld_article_body(soup):
    def find_article_body(data):
        if isinstance(data, dict):
            if data.get("articleBody"):
                return data["articleBody"]
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    found = find_article_body(item)
                    if found:
                        return found
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = find_article_body(value)
                    if found:
                        return found
        elif isinstance(data, list):
            for item in data:
                found = find_article_body(item)
                if found:
                    return found
        return ""

    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            body = find_article_body(data)
            if body:
                body = re.sub(r"\s+", " ", body).strip()
                return body
        except Exception:
            continue
    return ""


def fetch_article_body(url, title=""):
    print("  [本文取得]")
    print(f"  元URL: {url[:80]}")

    real_url = resolve_google_news_url(url)
    print(f"  解決後URL: {real_url[:80]}")

    if not real_url and title:
        domain = extract_source_domain(title)
        if domain:
            print(f"  RSS再検索を試みます（ドメイン: {domain}）")
            real_url = resolve_via_rss_search(title, domain)

    # 1. trafilatura
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(real_url)
        if downloaded:
            text = trafilatura.extract(downloaded)
            chars = len(text) if text else 0
            print(f"  trafilatura: {chars}文字")
            if text and chars > 100:
                return text[:3000], "trafilatura"
    except Exception as e:
        print(f"  trafilatura: エラー ({e})")

    # 2. newspaper3k
    try:
        from newspaper import Article
        article = Article(real_url, language='ja')
        article.download()
        article.parse()
        chars = len(article.text) if article.text else 0
        print(f"  newspaper3k: {chars}文字")
        if article.text and chars > 100:
            return article.text[:3000], "newspaper3k"
    except Exception as e:
        print(f"  newspaper3k: エラー ({e})")

    # 3. JSON-LD → pタグ → meta description
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(real_url, timeout=10, headers=headers)
        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        jsonld_body = extract_jsonld_article_body(soup)
        chars = len(jsonld_body)
        print(f"  JSON-LD articleBody: {chars}文字")
        if chars > 100:
            return jsonld_body[:3000], "jsonld"

        paragraphs = soup.find_all("p")
        text = " ".join(p.get_text() for p in paragraphs[:10])
        chars = len(text)
        print(f"  BeautifulSoup pタグ: {chars}文字")
        if chars > 100:
            return text[:3000], "beautifulsoup"

        meta = (
            soup.find("meta", attrs={"property": "og:description"}) or
            soup.find("meta", attrs={"name": "description"}) or
            soup.find("meta", attrs={"name": "twitter:description"})
        )
        if meta and meta.get("content"):
            text = meta["content"]
            chars = len(text)
            print(f"  meta description: {chars}文字")
            if chars >= 50:
                return text[:500], "meta"
    except Exception as e:
        print(f"  BeautifulSoup/meta: エラー ({e})")

    return "", "failed"


# ========== ここからテスト本体 ==========

def collect_articles():
    """Googleニュースからサウナ記事を拾う"""
    articles = []
    seen = set()
    for query in NEWS_QUERIES[:NUM_QUERIES_TO_TRY]:
        encoded = quote(query)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        try:
            response = requests.get(url, timeout=15)
            root = ET.fromstring(response.content)
            items = root.findall(".//item")
            print(f"検索『{query}』→ {len(items)}件")
            for item in items[:3]:
                title = clean_title(item.findtext("title", ""))
                link = item.findtext("link", "")
                if not title or not link:
                    continue
                if title in seen:
                    continue
                if not any(w in title for w in SAUNA_WORDS):
                    continue
                seen.add(title)
                articles.append({"title": title, "link": link})
            time.sleep(0.5)
        except Exception as e:
            print(f"検索失敗『{query}』: {e}")
    return articles


def main():
    print("=" * 50)
    print("クラウド本文取得テスト 開始")
    print("=" * 50)
    print()

    articles = collect_articles()
    print(f"\n拾えた記事: {len(articles)}件\n")

    if not articles:
        print("記事が拾えませんでした（RSS取得の段階で失敗）")
        return

    results = []
    for i, art in enumerate(articles[:MAX_ARTICLES_TO_TEST], 1):
        print(f"\n--- [{i}] {art['title'][:50]} ---")
        body, method = fetch_article_body(art["link"], title=art["title"])
        chars = len(body)
        success = chars >= BODY_MIN_CHARS and method not in ("meta", "failed")
        results.append({
            "title": art["title"][:50],
            "chars": chars,
            "method": method,
            "success": success,
        })
        print(f"  → {chars}文字 / {method} / {'成功' if success else '不足'}")

    # ========== 集計 ==========
    print("\n" + "=" * 50)
    print("テスト結果サマリー")
    print("=" * 50)
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    meta_count = sum(1 for r in results if r["method"] == "meta")
    failed_count = sum(1 for r in results if r["method"] == "failed")

    for r in results:
        mark = "○" if r["success"] else ("△" if r["method"] == "meta" else "×")
        print(f"  {mark} {r['chars']:>5}文字 [{r['method']:>13}] {r['title']}")

    print()
    print(f"  本文取得成功: {success_count}/{total} 件")
    print(f"  meta止まり  : {meta_count}/{total} 件")
    print(f"  完全失敗    : {failed_count}/{total} 件")
    print()

    rate = (success_count / total * 100) if total else 0
    if rate >= 60:
        print(f"  判定: ◎ クラウドでも本文取得は十分使える（成功率{rate:.0f}%）")
    elif rate >= 30:
        print(f"  判定: △ 半々。改善の余地あり（成功率{rate:.0f}%）")
    else:
        print(f"  判定: × クラウドだと本文取得が苦しい（成功率{rate:.0f}%）別作戦を検討")
    print("=" * 50)


if __name__ == "__main__":
    main()
