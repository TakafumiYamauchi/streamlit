# Streamlitアプリの定期訪問

GitHub ActionsのChromiumで、登録したStreamlitアプリを1件ずつ開きます。
休止画面なら復帰ボタンを押し、アプリ本体が表示できることまで確認します。

- 対象一覧：[playwright_wake/urls.jsonc](playwright_wake/urls.jsonc)
- 操作方法と制約：[playwright_wake/README.md](playwright_wake/README.md)
- 実行履歴：[Wake Streamlit apps](https://github.com/TakafumiYamauchi/streamlit/actions/workflows/wake-streamlit-apps.yml)

2026-11-01に、[TakafumiYamauchi側](https://github.com/TakafumiYamauchi/streamlit/blob/main/streamlit_random_url_opener/playwright_wake/urls.jsonc)と[montainternoadorihistorion側](https://github.com/montainternoadorihistorion/streamlit/blob/main/streamlit_random_url_opener/playwright_wake/urls.jsonc)の対象を和集合に統一しました。名称・URL・順序と対象外の11件も同一です。今後、対象を変更する際は両方の一覧を同時に更新してください。

現在は39件を有効にしています。修正版の変換アプリ（日中韓）と逆変換アプリ（日中）を追加しました。
日本語・中国語・韓国語・英語・エスペラント語以外の11件は、URLを消さず `//` コメントとして残しています。
コメント行へのアクセスは行いません。Cloud上のアプリ自体を削除する操作でもありません。

各リポジトリは4時間ごとに実行し、2つのリポジトリの開始時刻は2時間ずらしています。
変換・版切替・キャッシュ削除は行わず、表示を確認したらブラウザの接続を閉じます。

このリポジトリにGoogle Apps Scriptの実装は同梱されていません。以前の説明にあった未収録のCode.gsへのリンクは使用しません。
休止予防は稼働の保証ではありません。Cloudの資源不足・障害・起動タイムアウトは別途調査が必要です。
