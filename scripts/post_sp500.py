"""
S&P500 概況 自動投稿スクリプト
- Stooqから S&P500 521銘柄 + ^SPX + ^NDQ を取得
- ベスト4/ワースト4 + 騰落数を集計
- Claudeで概況を生成
- WordPressに記事投稿
- X (Twitter)にツイート

実行: python scripts/post_sp500.py
"""

import os
import sys
import time
import datetime
import requests
import tweepy
import anthropic
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote
from sp500_tickers import SP500_TICKERS

# ============================================================
# 設定
# ============================================================
STOOQ_BASE = "https://stooq.com/q/l/?s={syms}&f=sd2t2ohlcvbp&h&e=csv"
BATCH_SIZE = 40       # Stooqに1リクエストで送る銘柄数
BATCH_DELAY = 0.5     # バッチ間の待機秒数
TOP_N = 4             # ベスト/ワースト件数 (最大)
MIN_N = 3             # 文字数が収まらない時に減らす最小件数

# Stooqへのアクセス方法
# ロリポップは「国外IPアクセス制限」で海外IPを遮断するため、GitHub Actions(米国IP)からは
# プロキシ(moo-stock-blog.com)に接続できずタイムアウトする。
# よってデフォルトは Stooq 直接取得。ただしブラウザ用の User-Agent を付けること。
# python-requests のデフォルトUAだと Stooq にブロックされて全件0件になりやすいため。
# (どうしてもプロキシ経由にしたい場合のみ環境変数 USE_PROXY=1 を設定する)
USE_PROXY = os.environ.get('USE_PROXY', '0') != '0'
PROXY_BASE = os.environ.get('PROXY_BASE', 'https://moo-stock-blog.com/stock-proxy.php')
HTTP_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept': 'text/csv, text/plain, */*',
}

HEATMAP_URL = "https://moo-stock-blog.com/heatmap/"  # ツイート末尾に付けるURL
TWEET_LIMIT = 280     # Xの文字数上限 (重み付き)
URL_WEIGHT = 23       # XはURLを t.co 短縮で一律23文字として数える


# ============================================================
# 1. Stooqからデータ取得
# ============================================================
def build_request_url(symbols):
    """取得先URLを組み立てる。USE_PROXY時はPHPプロキシ経由のURLにする。"""
    target = STOOQ_BASE.format(syms='+'.join(symbols))
    if USE_PROXY:
        return f"{PROXY_BASE}?url={quote(target, safe='')}"
    return target


def looks_like_rate_limit(body):
    """Stooqのレート制限/エラー応答かどうかを簡易判定 (CSVではない短い文言)"""
    head = body.strip().lower()[:120]
    return any(k in head for k in ('exceeded', 'limit', 'too many', 'forbidden', 'denied'))


def fetch_batch(symbols):
    """1バッチ(40銘柄程度)を取得しCSVをパース"""
    url = build_request_url(symbols)
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=25)
        if r.status_code != 200:
            print(f"  ⚠ HTTP {r.status_code} / 応答先頭: {r.text[:160]!r}", file=sys.stderr)
            return []
        body = r.text
        if looks_like_rate_limit(body):
            # Stooqがレート制限文言を返している(CSVではない)
            print(f"  ⚠ レート制限/エラー応答の可能性: {body.strip()[:160]!r}", file=sys.stderr)
            return []
        rows = parse_csv(body)
        if not rows:
            # 200だが0件 → 原因究明のため応答の先頭を出す
            print(f"  ⚠ パース0件。応答先頭: {body.strip()[:160]!r}", file=sys.stderr)
        return rows
    except Exception as e:
        print(f"  ⚠ バッチ取得エラー: {e}", file=sys.stderr)
        return []


def parse_csv(csv_text):
    """StooqのCSVをパース"""
    lines = [l for l in csv_text.replace('\r', '').split('\n') if l]
    if len(lines) < 2:
        return []
    headers = [h.strip() for h in lines[0].split(',')]
    idx = {h: i for i, h in enumerate(headers)}
    rows = []
    for line in lines[1:]:
        cols = line.split(',')
        sym = cols[idx.get('Symbol', 0)]
        if not sym or sym == 'N/D':
            continue
        try:
            close = float(cols[idx['Close']])
        except (ValueError, KeyError):
            continue
        prev_key = 'PrevClose' if 'PrevClose' in idx else ('Prev' if 'Prev' in idx else None)
        if not prev_key:
            continue
        try:
            prev = float(cols[idx[prev_key]])
        except ValueError:
            continue
        if prev <= 0:
            continue
        chg = (close - prev) / prev * 100
        rows.append({'symbol': sym, 'close': close, 'prev': prev, 'chg': chg})
    return rows


def fetch_all_sp500():
    """SP500の全銘柄をバッチで取得"""
    # ティッカーをStooq形式に変換 (AAPL → aapl.us, BRK.B → brk-b.us)
    syms = []
    sym_to_ticker = {}
    for ticker, _ in SP500_TICKERS:
        stooq_sym = ticker.lower().replace('.', '-') + '.us'
        syms.append(stooq_sym)
        sym_to_ticker[stooq_sym.upper()] = ticker
    
    all_rows = []
    n_batches = (len(syms) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"バッチ数: {n_batches} (合計 {len(syms)} 銘柄)")
    
    for i in range(0, len(syms), BATCH_SIZE):
        batch = syms[i:i+BATCH_SIZE]
        batch_no = i // BATCH_SIZE + 1
        rows = fetch_batch(batch)
        if not rows:
            print(f"  [{batch_no}/{n_batches}] 失敗, リトライ")
            time.sleep(1.0)
            rows = fetch_batch(batch)
        # シンボルからティッカーへの逆引きを付与
        for r in rows:
            r['ticker'] = sym_to_ticker.get(r['symbol'].upper(), r['symbol'])
        all_rows.extend(rows)
        print(f"  [{batch_no}/{n_batches}] {len(rows)}銘柄取得")
        time.sleep(BATCH_DELAY)
    
    # ティッカー → 企業名のマップ
    name_map = {t: n for t, n in SP500_TICKERS}
    for r in all_rows:
        r['name'] = name_map.get(r['ticker'], r['ticker'])
    
    return all_rows


def fetch_indices():
    """^SPX (S&P500) と ^NDQ (NASDAQ) の変化率を取得"""
    rows = fetch_batch(['^spx', '^ndq'])
    result = {}
    for r in rows:
        result[r['symbol'].upper()] = r
    return result


# ============================================================
# 2. 集計
# ============================================================
def summarize(stocks):
    """ベスト/ワースト4と騰落数を集計"""
    sorted_stocks = sorted(stocks, key=lambda s: s['chg'], reverse=True)
    best = sorted_stocks[:TOP_N]
    worst = sorted_stocks[-TOP_N:][::-1]
    
    up = sum(1 for s in stocks if s['chg'] > 0)
    down = sum(1 for s in stocks if s['chg'] < 0)
    flat = sum(1 for s in stocks if s['chg'] == 0)
    
    return {
        'best': best,
        'worst': worst,
        'up': up,
        'down': down,
        'flat': flat,
        'total': len(stocks)
    }


# ============================================================
# 3. ツイート文の生成 (LLM不要・テンプレート)
# ============================================================
def fmt_pct(chg):
    """変化率を小数第1位まで(第2位を四捨五入)で符号付きフォーマット。
    例: 5.234 → '+5.2%' / -3.95 → '-4.0%' / -0.04 → '+0.0%'"""
    v = Decimal(str(chg)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    if v == 0:
        v = Decimal('0.0')  # -0.0 を 0.0 に正規化
    sign = '+' if v >= 0 else ''
    return f"{sign}{v}%"


def tweet_weighted_len(text):
    """Xの重み付き文字数を概算する。
    - URL(HEATMAP_URL)は一律 URL_WEIGHT 文字
    - CJK・全角・絵文字は2文字
    - それ以外(半角英数記号・改行)は1文字"""
    # URLを23文字分のプレースホルダに置換してから数える
    text = text.replace(HEATMAP_URL, 'x' * URL_WEIGHT)
    weight = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x1100 <= cp <= 0x115F or   # ハングル字母
            0x2E80 <= cp <= 0x303E or   # CJK部首・記号
            0x3041 <= cp <= 0x33FF or   # かな・CJK記号
            0x3400 <= cp <= 0x4DBF or   # CJK拡張A
            0x4E00 <= cp <= 0x9FFF or   # CJK統合漢字
            0xA000 <= cp <= 0xA4CF or
            0xAC00 <= cp <= 0xD7A3 or   # ハングル音節
            0xF900 <= cp <= 0xFAFF or
            0xFE30 <= cp <= 0xFE4F or
            0xFF00 <= cp <= 0xFF60 or   # 全角英数記号
            0xFFE0 <= cp <= 0xFFE6 or
            cp >= 0x1F000               # 絵文字など補助面
        ):
            weight += 2
        else:
            weight += 1
    return weight


def build_tweet(summary, indices, date_str, n=TOP_N):
    """ツイート文を生成 (ベスト/ワースト n 件、URLを末尾に付与)"""
    spx = indices.get('^SPX')
    ndq = indices.get('^NDQ')
    
    lines = [
        f"📊 米国市場 本日の値動き ({date_str})",
    ]
    
    if spx and ndq:
        lines.append(f"S&P500 {fmt_pct(spx['chg'])} / NASDAQ {fmt_pct(ndq['chg'])}")
    
    lines.append("")
    lines.append("📈 ベスト")
    for i, s in enumerate(summary['best'][:n], 1):
        lines.append(f"{i}. {s['ticker']} {fmt_pct(s['chg'])}")
    
    lines.append("")
    lines.append("📉 ワースト")
    for i, s in enumerate(summary['worst'][:n], 1):
        lines.append(f"{i}. {s['ticker']} {fmt_pct(s['chg'])}")
    
    lines.append("")
    lines.append("#米国株 #SP500")
    lines.append(HEATMAP_URL)  # URLは最後
    
    return '\n'.join(lines)


def build_tweet_fit(summary, indices, date_str):
    """まず TOP_N 件で組み立て、文字数上限を超える場合は MIN_N 件まで減らす。
    戻り値: (ツイート文, 採用した件数)"""
    for n in range(TOP_N, MIN_N - 1, -1):
        text = build_tweet(summary, indices, date_str, n)
        if tweet_weighted_len(text) <= TWEET_LIMIT:
            return text, n
    # MIN_N でも超える場合はそのまま返す (最低件数)
    return build_tweet(summary, indices, date_str, MIN_N), MIN_N


# ============================================================
# 4. ブログ記事の生成 (Claude使用)
# ============================================================
def build_blog_article(summary, indices, date_str):
    """Claudeでブログ記事を生成"""
    spx = indices.get('^SPX')
    ndq = indices.get('^NDQ')
    
    # 生のデータをLLMに渡す (LLMに計算させない方針)
    spx_text = f"+{spx['chg']:.2f}%" if spx and spx['chg'] >= 0 else (f"{spx['chg']:.2f}%" if spx else "—")
    ndq_text = f"+{ndq['chg']:.2f}%" if ndq and ndq['chg'] >= 0 else (f"{ndq['chg']:.2f}%" if ndq else "—")
    
    best_list = "\n".join([
        f"- {s['name']} ({s['ticker']}): {('+' if s['chg']>=0 else '')}{s['chg']:.2f}%"
        for s in summary['best']
    ])
    worst_list = "\n".join([
        f"- {s['name']} ({s['ticker']}): {('+' if s['chg']>=0 else '')}{s['chg']:.2f}%"
        for s in summary['worst']
    ])
    
    # Claude に投げるプロンプト (グロース250の教訓を反映)
    prompt = f"""以下のデータを元に、米国市場(S&P500)の本日の概況をブログ記事として200〜350字程度で書いてください。

## 厳守ルール
1. 提供されたデータの数字以外を新たに「計算」しないこと。割合(%)は計算せず、「半数強」「やや多い」など概数語で書くこと。
2. データに無い情報(ニュース、要因分析の推測)は書かないこと。
3. 「終値○○ドル」「○○ポイント」のような絶対値は載せないこと(変化率のみ使う)。
4. HTMLは <p> と <strong> のみ使ってよい。見出しタグは使わない。

## データ
- 日付: {date_str}
- S&P500 指数: {spx_text}
- NASDAQ 指数: {ndq_text}
- 値上がり銘柄数: {summary['up']}
- 値下がり銘柄数: {summary['down']}
- 変わらず: {summary['flat']}
- 合計: {summary['total']}

### 値上がり上位
{best_list}

### 値下がり上位
{worst_list}

## 出力形式
HTMLで本文のみ。タイトル不要。"""
    
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("⚠ ANTHROPIC_API_KEY 未設定、フォールバック記事を使います", file=sys.stderr)
        return build_fallback_article(summary, indices, date_str)
    
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        article = response.content[0].text.strip()
        # 万一<html>とかが入ったら除去
        article = article.replace('```html', '').replace('```', '').strip()
        return article
    except Exception as e:
        print(f"⚠ Claude API エラー: {e}", file=sys.stderr)
        return build_fallback_article(summary, indices, date_str)


def build_fallback_article(summary, indices, date_str):
    """LLM不調時のフォールバック記事"""
    spx = indices.get('^SPX')
    ndq = indices.get('^NDQ')
    spx_text = f"{'+' if spx and spx['chg']>=0 else ''}{spx['chg']:.2f}%" if spx else "—"
    ndq_text = f"{'+' if ndq and ndq['chg']>=0 else ''}{ndq['chg']:.2f}%" if ndq else "—"
    
    parts = [
        f"<p>米国市場({date_str})の概況をお伝えします。本日のS&P500は<strong>{spx_text}</strong>、NASDAQ総合指数は<strong>{ndq_text}</strong>でした。</p>",
        f"<p>S&P500構成銘柄({summary['total']}銘柄)では、値上がり<strong>{summary['up']}銘柄</strong>、値下がり<strong>{summary['down']}銘柄</strong>、変わらず{summary['flat']}銘柄となりました。</p>",
        "<p><strong>値上がり上位</strong></p><p>",
        " / ".join([f"{s['name']} {('+' if s['chg']>=0 else '')}{s['chg']:.2f}%" for s in summary['best']]),
        "</p>",
        "<p><strong>値下がり上位</strong></p><p>",
        " / ".join([f"{s['name']} {('+' if s['chg']>=0 else '')}{s['chg']:.2f}%" for s in summary['worst']]),
        "</p>"
    ]
    return ''.join(parts)


# ============================================================
# 5. WordPress 投稿
# ============================================================
def post_to_wordpress(title, content):
    """WordPress REST API で記事を投稿"""
    base_url = os.environ.get('WP_BASE_URL', '').rstrip('/')
    user = os.environ.get('WP_USER')
    password = os.environ.get('WP_APP_PASSWORD')
    category_id = os.environ.get('WP_CATEGORY_ID')
    
    if not (base_url and user and password):
        print("⚠ WordPress認証情報未設定、スキップ", file=sys.stderr)
        return None
    
    url = f"{base_url}/wp-json/wp/v2/posts"
    payload = {
        'title': title,
        'content': content,
        'status': 'publish',
    }
    if category_id:
        try:
            payload['categories'] = [int(category_id)]
        except ValueError:
            pass
    
    try:
        r = requests.post(url, json=payload, auth=(user, password), timeout=30)
        r.raise_for_status()
        data = r.json()
        print(f"✓ WordPress投稿成功: {data.get('link', '')}")
        return data
    except Exception as e:
        print(f"✗ WordPress投稿失敗: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Response: {e.response.text[:300]}", file=sys.stderr)
        return None


# ============================================================
# 6. X (Twitter) 投稿
# ============================================================
def post_to_twitter(text):
    """X (Twitter) API v2 で投稿"""
    api_key = os.environ.get('X_API_KEY')
    api_secret = os.environ.get('X_API_SECRET')
    access_token = os.environ.get('X_ACCESS_TOKEN')
    access_secret = os.environ.get('X_ACCESS_SECRET')
    
    if not all([api_key, api_secret, access_token, access_secret]):
        print("⚠ X API認証情報未設定、スキップ", file=sys.stderr)
        return None
    
    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        response = client.create_tweet(text=text)
        print(f"✓ X投稿成功: tweet_id={response.data['id']}")
        return response
    except Exception as e:
        print(f"✗ X投稿失敗: {e}", file=sys.stderr)
        return None


# ============================================================
# メイン
# ============================================================
def main():
    print("=" * 60)
    print("S&P500 概況 自動投稿")
    print(f"開始: {datetime.datetime.now()}")
    print("=" * 60)
    
    # 1. データ取得
    print("\n[1/5] Stooqからデータ取得中...")
    print(f"  取得方法: {'PHPプロキシ経由 (' + PROXY_BASE + ')' if USE_PROXY else 'Stooq直接'}")
    stocks = fetch_all_sp500()
    print(f"  取得銘柄数: {len(stocks)} / {len(SP500_TICKERS)}")
    
    if len(stocks) < 100:
        print("✗ 取得銘柄が少なすぎます。中断します。", file=sys.stderr)
        print("  → 上の ⚠ ログで応答内容を確認してください。", file=sys.stderr)
        print("  → プロキシ経由でも失敗する場合は USE_PROXY=0 で直接取得も試せます。", file=sys.stderr)
        sys.exit(1)
    
    # 2. 指数取得
    print("\n[2/5] 指数取得中 (^SPX, ^NDQ)...")
    indices = fetch_indices()
    if '^SPX' in indices:
        print(f"  S&P500: {indices['^SPX']['chg']:+.2f}%")
    if '^NDQ' in indices:
        print(f"  NASDAQ: {indices['^NDQ']['chg']:+.2f}%")
    
    # 3. 集計
    print("\n[3/5] 集計中...")
    summary = summarize(stocks)
    print(f"  値上がり: {summary['up']} / 値下がり: {summary['down']} / 変わらず: {summary['flat']}")
    print(f"  ベスト1: {summary['best'][0]['name']} {summary['best'][0]['chg']:+.2f}%")
    print(f"  ワースト1: {summary['worst'][0]['name']} {summary['worst'][0]['chg']:+.2f}%")
    
    # 4. 投稿用テキスト生成
    print("\n[4/5] テキスト生成中...")
    # 日付: 米国市場の日付 (JSTの前日)
    jst_now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    market_date = jst_now - datetime.timedelta(days=1)
    date_str = market_date.strftime('%-m/%-d')
    
    tweet_text, used_n = build_tweet_fit(summary, indices, date_str)
    print(f"  ツイート文 (ベスト/ワースト各{used_n}件 / "
          f"{len(tweet_text)}文字 / 重み付き{tweet_weighted_len(tweet_text)}):")
    print("-" * 40)
    print(tweet_text)
    print("-" * 40)
    
    article_html = build_blog_article(summary, indices, date_str)
    
    title = f"{market_date.strftime('%-m月%-d日')} 米国市場概況 - S&P500/NASDAQ 値上がり値下がり銘柄"
    
    # 5. 投稿
    print("\n[5/5] 投稿中...")
    
    # WordPress
    post_to_wordpress(title, article_html)
    
    # X
    post_to_twitter(tweet_text)
    
    print("\n" + "=" * 60)
    print(f"完了: {datetime.datetime.now()}")
    print("=" * 60)


if __name__ == '__main__':
    main()
