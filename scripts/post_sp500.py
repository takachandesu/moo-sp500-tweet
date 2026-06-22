"""
S&P500 概況 自動投稿スクリプト
- yfinance(Yahoo Finance)から S&P500 521銘柄 + ^GSPC + ^IXIC を取得
- ベスト/ワースト + 騰落数を集計
- Claudeで概況を生成
- WordPressに記事投稿
- X (Twitter)にツイート

データ取得は Stooq ではなく yfinance を使う。
理由: GitHub Actions(米国IP)からStooqを直接叩くと404/429でブロックされ、
ロリポップのプロキシ経由は共有IPがStooqにレート制限されるため不安定。
yfinance は moo-growth250-tracker で実績があり、GitHub Actionsからでも安定して取得できる。

【依存パッケージ】requirements.txt に `yfinance` を追加すること。
実行: python scripts/post_sp500.py
"""

import os
import sys
import datetime
import requests
import tweepy
import anthropic
import yfinance as yf
from decimal import Decimal, ROUND_HALF_UP
from sp500_tickers import SP500_TICKERS

# ============================================================
# 設定
# ============================================================
HISTORY_PERIOD = "5d"   # 直近の終値2本(当日・前日)を取るため余裕をもって5営業日分
TOP_N = 4               # ベスト/ワースト件数 (最大)
MIN_N = 3               # 文字数が収まらない時に減らす最小件数

HEATMAP_URL = "https://moo-stock-blog.com/heatmap/"  # ツイート末尾に付けるURL
TWEET_LIMIT = 280       # Xの文字数上限 (重み付き)
URL_WEIGHT = 23         # XはURLを t.co 短縮で一律23文字として数える

# yfinanceの指数シンボル → 出力キー(既存コードに合わせる)
INDEX_SYMBOLS = {'^GSPC': '^SPX', '^IXIC': '^NDQ'}


# ============================================================
# 1. yfinance からデータ取得
# ============================================================
def _last_two_closes(df, symbol):
    """download結果(group_by='ticker')から、指定シンボルの (前日終値, 当日終値) を返す。
    取得できなければ None。"""
    try:
        sub = df[symbol] if symbol in df.columns.get_level_values(0) else None
        if sub is None:
            return None
        closes = sub['Close'].dropna()
        if len(closes) < 2:
            return None
        prev = float(closes.iloc[-2])
        close = float(closes.iloc[-1])
        if prev <= 0:
            return None
        return prev, close
    except Exception:
        return None


def _download(symbols):
    """yfinanceで複数シンボルをまとめて取得。group_by='ticker'のDataFrameを返す。"""
    return yf.download(
        symbols,
        period=HISTORY_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )


def fetch_all_sp500():
    """SP500の全銘柄を yfinance で取得"""
    name_map = {t: n for t, n in SP500_TICKERS}
    tickers = [t for t, _ in SP500_TICKERS]
    # yfinanceは BRK.B → BRK-B 形式(ピリオドはハイフン)
    yf_symbols = [t.replace('.', '-') for t in tickers]
    sym_to_ticker = {s: t for s, t in zip(yf_symbols, tickers)}

    print(f"yfinanceで取得開始 (合計 {len(yf_symbols)} 銘柄)")
    try:
        df = _download(yf_symbols)
    except Exception as e:
        print(f"  ⚠ yfinance一括取得エラー: {e}", file=sys.stderr)
        return []

    rows = []
    missing = []
    for s in yf_symbols:
        res = _last_two_closes(df, s)
        if res is None:
            missing.append(s)
            continue
        prev, close = res
        t = sym_to_ticker[s]
        rows.append({
            'ticker': t,
            'name': name_map.get(t, t),
            'close': close,
            'prev': prev,
            'chg': (close - prev) / prev * 100,
        })

    print(f"  取得成功: {len(rows)}銘柄 / 取得不可: {len(missing)}銘柄")
    if missing:
        print(f"  取得できなかった例: {missing[:10]}", file=sys.stderr)
    return rows


def fetch_indices():
    """^GSPC (S&P500) と ^IXIC (NASDAQ総合) の変化率を取得"""
    result = {}
    try:
        df = _download(list(INDEX_SYMBOLS.keys()))
    except Exception as e:
        print(f"  ⚠ 指数取得エラー: {e}", file=sys.stderr)
        return result
    for yf_sym, out_key in INDEX_SYMBOLS.items():
        res = _last_two_closes(df, yf_sym)
        if res is None:
            print(f"  ⚠ 指数 {yf_sym} を取得できませんでした", file=sys.stderr)
            continue
        prev, close = res
        result[out_key] = {
            'symbol': out_key, 'close': close, 'prev': prev,
            'chg': (close - prev) / prev * 100,
        }
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
    text = text.replace(HEATMAP_URL, 'x' * URL_WEIGHT)
    weight = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x1100 <= cp <= 0x115F or
            0x2E80 <= cp <= 0x303E or
            0x3041 <= cp <= 0x33FF or
            0x3400 <= cp <= 0x4DBF or
            0x4E00 <= cp <= 0x9FFF or
            0xA000 <= cp <= 0xA4CF or
            0xAC00 <= cp <= 0xD7A3 or
            0xF900 <= cp <= 0xFAFF or
            0xFE30 <= cp <= 0xFE4F or
            0xFF00 <= cp <= 0xFF60 or
            0xFFE0 <= cp <= 0xFFE6 or
            cp >= 0x1F000
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
    lines.append("ニュース詳細はプロフィールのリンクと固定ポストに。")  # 固定ポスト/リンクへ誘導(URLなし=通常ポスト課金)

    return '\n'.join(lines)


def build_tweet_fit(summary, indices, date_str):
    """まず TOP_N 件で組み立て、文字数上限を超える場合は MIN_N 件まで減らす。
    戻り値: (ツイート文, 採用した件数)"""
    for n in range(TOP_N, MIN_N - 1, -1):
        text = build_tweet(summary, indices, date_str, n)
        if tweet_weighted_len(text) <= TWEET_LIMIT:
            return text, n
    return build_tweet(summary, indices, date_str, MIN_N), MIN_N


# ============================================================
# 4. ブログ記事の生成 (Claude使用)
# ============================================================
def build_blog_article(summary, indices, date_str):
    """Claudeでブログ記事を生成"""
    spx = indices.get('^SPX')
    ndq = indices.get('^NDQ')

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
        r = requests.post(url, json=payload, auth=(user, password), timeout=(30, 60))
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

    # 1. データ取得 (yfinance)
    print("\n[1/5] yfinance(Yahoo Finance)からデータ取得中...")
    stocks = fetch_all_sp500()
    print(f"  取得銘柄数: {len(stocks)} / {len(SP500_TICKERS)}")

    if len(stocks) < 100:
        print("✗ 取得銘柄が少なすぎます。中断します。", file=sys.stderr)
        print("  → yfinanceのバージョンが古いと取得できないことがあります。", file=sys.stderr)
        print("  → requirements.txt の yfinance を最新に更新してみてください。", file=sys.stderr)
        sys.exit(1)

    # 2. 指数取得
    print("\n[2/5] 指数取得中 (^GSPC, ^IXIC)...")
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
    post_to_wordpress(title, article_html)
    post_to_twitter(tweet_text)

    print("\n" + "=" * 60)
    print(f"完了: {datetime.datetime.now()}")
    print("=" * 60)


if __name__ == '__main__':
    main()
