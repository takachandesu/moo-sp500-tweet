# moo-sp500-tweet

S&P500 の本日のベスト4/ワースト4 銘柄を、毎日自動でブログ記事化・ツイートする仕組み。

## 概要

- **対象**: S&P500 521銘柄
- **データ源**: Stooq (CSV API)
- **スケジュール**: 火〜土 朝8:00 JST (米国市場終了の約2〜3時間後)
- **配信先**: WordPress (記事) + X (ツイート)

## 構成

```
moo-sp500-tweet/
├── scripts/
│   ├── sp500_tickers.py    # S&P500 521銘柄リスト
│   └── post_sp500.py        # メイン処理
├── .github/workflows/
│   └── post.yml             # 自動実行スケジュール
└── README.md
```

## 必要なSecrets

| 名前 | 用途 |
|---|---|
| `X_API_KEY` | X (Twitter) API キー |
| `X_API_SECRET` | X (Twitter) API シークレット |
| `X_ACCESS_TOKEN` | X (Twitter) アクセストークン |
| `X_ACCESS_SECRET` | X (Twitter) アクセストークン シークレット |
| `WP_BASE_URL` | WordPressのURL (例: `https://moo-stock-blog.com`) |
| `WP_USER` | WordPressユーザー名 |
| `WP_APP_PASSWORD` | WordPressアプリケーションパスワード |
| `ANTHROPIC_API_KEY` | Claude API キー (概況文生成用) |
| `WP_CATEGORY_ID` | (任意) WordPressカテゴリーID |

## 投稿例

### ツイート

```
📊 米国市場 本日の値動き (5/30)
S&P500 +0.42% / NASDAQ +0.81%

📈 ベスト
1. AAPL +5.23%
2. NVDA +4.81%
3. TSLA +3.95%
4. META +3.42%

📉 ワースト
1. WMT -4.55%
2. JNJ -3.91%
3. PFE -3.42%
4. KO -2.88%

#米国株 #SP500
```

### ブログ記事

タイトル: `5月30日 米国市場概況 - S&P500/NASDAQ 値上がり値下がり銘柄`

本文: Claudeで生成 (200〜350字程度)

## 教訓 (グロース250引継ぎメモより)

1. **正確に取れない数字は載せない** - 終値・前日比などの絶対値は載せない。変化率のみ
2. **割合(%)はLLMに計算させない** - 生の銘柄数のみ提示、概数語で表現
3. **ウェブ検索は使わない** - Stooqの自前データのみ使用
4. **WordPressの`.htaccess`** - `SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1` が必要 (既に設定済み)

## 手動実行

GitHub → Actions → 「S&P500 Daily Post」→ 「Run workflow」

## ローカルテスト

```bash
cd scripts
export ANTHROPIC_API_KEY=...
export WP_BASE_URL=...
# ...他のSecretsも
python post_sp500.py
```
