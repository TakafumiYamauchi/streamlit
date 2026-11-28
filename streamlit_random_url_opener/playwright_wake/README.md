# Streamlitの表示確認と休止からの復帰

## 対象の変更

`urls.jsonc` を編集します。通常のJSON配列に加え、行頭の空白に続く `//` から始まるコメント行に対応します。
行末コメント、ブロックコメント、末尾カンマは使用できません。URL内の `https://` はそのまま保持します。
独自の厳密JSONも `--urls-file path/to/urls.json` で指定できます。

2026-10-31の更新で追加した5件：

- [日本語・修正版変換](https://esperanto-cjk-revision-ja-20261017.streamlit.app/)
- [中国語・修正版変換](https://esperanto-cjk-revision-zh-20261017.streamlit.app/)
- [韓国語・修正版変換](https://esperanto-cjk-revision-ko-20261017.streamlit.app/)
- [日本語・修正版逆変換](https://esperanto-reverter-revision-ja-20261018.streamlit.app/)
- [中国語・修正版逆変換](https://esperanto-reverter-revision-zh-20261018.streamlit.app/)

日本語・中国語・韓国語・英語・エスペラント語の既存URLは維持しました。
アラビア語・フランス語・ドイツ語・ヒンディー語・インドネシア語・イタリア語・ポーランド語・ロシア語（通常／beta）・スペイン語・ベトナム語の11件はコメントアウトしています。

## 動作

1. 1アプリずつ通常URLを開きます。
2. 休止画面がある場合だけ、公式の復帰ボタンを押します。
3. 外枠だけでなく、メイン画面または埋め込まれたアプリ内のStreamlit表示を確認します。
4. 通常URLの外枠が進まない場合は、同じアプリの本体URL `/~/+/` も確認します。本体側の表示・エラー・タイムアウトを記録し、残り時間で通常URLをもう一度開きます。通常URLが404や認証エラーを直接返した場合は、そこで確認を終えます。通常URLでも表示できたときに成功を記録します。
5. HTTPエラー、読み込み中、復帰処理中だけの画面は成功扱いにしません。
6. 接続を閉じ、失敗したアプリがあっても後続へ進みます。個別結果の `checks` には、試行ごとのURL・状態・エラー内容を保存します。本体だけ表示できた場合の `body_only` は巡回失敗に数えます。
7. Actionsで全件を回り終えた後、先頭の修正版5件で時間切れ、本体だけの表示、HTTP 5xx・408・429が起きた場合に限り、該当する通常URLを各1回、最大30秒再確認します。再確認したアプリの初回結果は `first_visit` に、最終結果は従来の欄に保存します。実行概要には成功数と、再確認で復旧したアプリ・未復旧のアプリを表示します。

初回の巡回では、アプリごとの画面移動・復帰ボタン・再試行を合わせて最大180秒待機します。全件巡回後の再確認には、対象アプリごとに追加で最大30秒を使います。
既定の180秒では、通常URLの初回と本体の確認に各60秒までを使い、最後の通常URL確認には残り時間を使います。別アプリを同時に大量起動しません。
復帰ボタン以外の操作、本文変換、版切替、キャッシュ削除、アプリの管理設定変更は行いません。

## 定期実行と手動確認

このリポジトリのUTCスケジュールは `17 */4 * * *` です。
TakafumiYamauchi側は0・4・8・12・16・20時17分、montainternoadorihistorion側は2・6・10・14・18・22時29分。
各リポジトリの設定間隔は4時間で、実行を交互に予定しています。時刻はGitHubの混雑で遅れることがあります。

[Actions](https://github.com/TakafumiYamauchi/streamlit/actions/workflows/wake-streamlit-apps.yml) から `Run workflow` を押すと手動実行できます。
`wake-results` の成果物に成功・失敗と未処理対象が残ります。失敗があれば実行全体も失敗となるので、その対象を調べてください。

リポジトリ直下からのローカル確認：

```bash
python -m unittest discover -s tests -v
python streamlit_random_url_opener/playwright_wake/wake_streamlit_apps.py --dry-run
python -m pip install -r streamlit_random_url_opener/playwright_wake/requirements.txt
python -m playwright install chromium
python streamlit_random_url_opener/playwright_wake/wake_streamlit_apps.py --report-file wake-results.json
```

登録済み1アプリだけ確認する場合は `--only-url https://example.streamlit.app/` を指定します。
複数ならこの引数を繰り返します。既存の `--randomize-order` と `--limit` も利用できますが、定期実行は追加した修正版5件を最初に確認します。

## 保証できないこと

Streamlit Community Cloudは無アクセス12時間で休止します。定期訪問で休止を減らし、休止していた場合に復帰を試みます。
単純なHTTP GETだけが活動として数えられるという保証には依存しません。
資源不足・アプリ内の例外・Cloudの障害による停止は、この巡回だけでは修復できません。
[Streamlit公式の管理・休止・資源制限の説明](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)

GitHub Actionsの定期実行は既定ブランチの内容で動作し、混雑時には遅延・取りこぼしがあり得ます。
公開リポジトリは60日間活動がないと定期実行が自動停止するため、Actionsの状態も確認してください。
無意味な空コミットで常時更新する仕組みは追加していません。
[GitHub公式のschedule仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

手動実行の limit は 0（全件）または 5（先頭の修正版5件）を選べます。定期実行は常に全件です。

## フォーク側のActionsが止まっている場合

Actions画面に `Workflows on this fork have been disabled` と表示されたら、
GitHubがフォーク元から継承した実行を停止しています。設定APIで enabled=true、個別workflowで active と返っても、この状態が残ることがあります。
必要な対象・頻度であることを確認し、画面の `I understand my workflows, go ahead and re-enable them` で再有効化します。
今回のmontainternoadorihistorion側は2026-10-31にこの画面から再有効化しました。

## 更新後の実確認（2026-10-31）

新しい5アプリをGitHub Actions上のブラウザで確認し、すべて通常URLから表示できました（約5.6～6.4秒）。単体17件・ブラウザ画面判定9件も成功しました。
[実行結果](https://github.com/TakafumiYamauchi/streamlit/actions/runs/41210707920) / [保存した個別結果](../../verification/20261031/wake-results.json)
この実行は先頭5件を対象としており、残る34件を実接続済みとする記録ではありません。

独自の画面部品は、Streamlit本文内の正規の部品iframeに操作可能な画面が現れた場合だけ成功と判定します。部品自体の読み込みエラー、空のiframe、読み込み中表示は成功に数えません。

最終版の独自部品判定を追加した後も、単体17件・ブラウザ判定15件と新しい5アプリの実接続が成功しました。
[最終実行](https://github.com/TakafumiYamauchi/streamlit/actions/runs/41211932843) / [最終個別結果](../../verification/20261031/final/wake-results.json)
