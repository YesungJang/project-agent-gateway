# agent-gateway

AI の申告ではなく、機械が観測した事実で判断するリポジトリ。この Gateway が対象リポジトリに課すゲートを、この repo 自身にも課す。

## main へのマージ

main へのマージは事前承認済み。ただし**下の条件が全部揃ってから**。1つでも欠けたらブランチに残して報告し、main には入れない。揃っていない状態で「たぶん大丈夫」は根拠にしない。

1. 全テストが緑。`mise exec -- python3 -m unittest discover -s tests` を実行し、件数と結果を報告する。
2. 振る舞いを変えたなら、それを守る回帰テストがある。**追加したテストが変更前のコードで落ちることを確認済み**。落ちないテストは何も証明していない。
3. Ruff の新規指摘ゼロ。`uvx ruff check gateway.py dashboard.py events.py demo_dashboard.py` は常にゼロ（CI の lint ジョブと同じ）。`tests/` は既存の B023 / SIM117 を超えないことだけ確認する。`uv run --no-sync ruff` は使えない。
4. `git diff --check` 成功。
5. 挙動を変えたなら README.md も更新済み。README は仕様書として実装と一致させる契約。
6. `docs/WORK_LOG.md` に3行追記（日付 / 変更 / 検証 / 影響範囲）。コミット対象。clone した人に見えない `work/LOG.md` は使わない。
7. 作業ツリーがクリーンで、**マージ後にもう一度テストが緑**。マージコミット自体を未検証のまま残さない。
8. main から直接コミットせず、ブランチを切って 1コミット1意図、`--no-ff` でマージ。マージ後にブランチを削除する。

## 条件が揃っていても人に聞くもの

- **安全ゲートを緩める変更**: `risky` / `check_paths` / `check_context_path` / `validate_config` / `required_check_gate` / `merge_gate` / `POLICY` / `VERIFICATION_CONTRACTS`。緩める変更を自己承認でマージできるなら、このリポジトリの意味が消える。厳しくする変更は条件を満たせばマージしてよい。
- `config.example.json` の非互換変更、state ファイル形式の破壊的変更。既存の設定が黙って壊れる。
- 実サービス（Claude / Codex / GitHub）への送信を伴う E2E。外部送信は毎回承認を取る。
- **push は別承認**。マージ OK は push OK ではない。

## 設計の前提

- AI が賢くなることに期待しない。価値は AI の判断ではなく、AI が何を言おうと実行経路が決定論的なゲートを通ること。
- 検証結果は実測（status / 所要時間 / 出力末尾）で記録する。固定の `PASS` 文字列や「ログを見よ」で済ませない。
- 止めるときは理由を人が読める形で残す。自動再開も自動承認もしない。
