# Agent Gateway v0

Python 3.12 の標準ライブラリのみ。GitHub Issue → Claude の計画 → Codex の Plan Gate → タスクごとの編集案・テスト・Codex Gate・修復 → 全体検証 → PR → 決定論的 Merge Gate。

## 実行

`config.example.json` をリポジトリ外の信頼済み設定にコピーし、対象・許可パス・既存の検証コマンド・必須 CI 名を設定する。設定と state はエージェント編集範囲外に置く。依存インストール・更新は行わない。

```sh
mise exec -- python3 -m unittest discover -s tests -v
mise exec -- python3 gateway.py --config /absolute/path/config.json --state /absolute/path/gateway-state
```

通常の実行は GitHub を読み取り、ローカル worktree と PR 本文まで作成する。Claude/Codex API への送信を伴うため、対象の Issue・許可ソース・差分を両サービスに送ってよいことを事前に承認する。外部送信の承認後のみ実行する。
GitHub 書き込みも承認済みの場合に限り `--publish` を追加する。`--publish` はラベル変更、push、PR 作成、安全なマージ、Issue close を明示的に許可する。初回に以下のラベルを GitHub で作成する:

- `gateway:queued`
- `gateway:running`
- `gateway:approval-required`
- `gateway:failed`
- `gateway:pr-ready`
- `gateway:done`

queued ラベルを付けた Issue を古い番号から1件処理して終了する。v0 は常駐サービスを導入しない。手動または信頼済みスケジューラで同じコマンドを繰り返す。全ワーカーは同じ state を使用する。Mac のファイルロックで1件ずつ実行する。

## 安全と状態

- Issue は JSON のデータとして stdin に渡す。シェルは使わず、検証コマンドは設定の argv 配列だけから選択する。ただし検証コマンド自体がコードを実行するため、対象リポジトリとそのテストは事前に信頼・確認が必要。
- Claude は safe/restricted mode、tools 無効、MCP 無効。許可されたテキストソースだけを渡し、構造化編集案を Gateway が適用する。セッションはタスク間で resume。直接 Bash/Edit は与えない。
- Codex は毎回 fresh な read-only sandbox で検証する。`--approve-for-me` は workspace-write を許可するため検証器には使用しない。`--sandbox` との併用なし。危険な権限 bypass なし。
- Codex には秘密ファイル・外部送信・変更を禁止する指示を渡す。CLI read-only sandbox は機密ファイルに対する完全な読み取り隔離ではない。機密を持つリポジトリは別の OS ユーザー/隔離環境で運用する。
- `.env` / `.env.*` / `.git` / `.ssh` / `.aws`、パス逸脱、シンボリックリンク、許可範囲外の編集は拒否する。
- 依存/lock、migration/SQL、CI/隠しパス、infra/Terraform/AWS、auth/permission/secret/security/config/gateway、指示書、シェルスクリプトは承認へ昇格する。意味上の認証・権限変更を完全に判定することはできないため、リポジトリ管理者が protected_paths を追加する。
- Git hooks は Gateway の Git 操作で無効。GitHub は設定の origin と一致する必要がある。各タスクを1コミットにする。
- タスクのテスト失敗/FAIL は最大 repair_attempts 回修復する。計画修復の上限超過、修復不能、危険変更、タイムアウト、不明な CI は approval-required で停止。自動承認の機構は設けない。
- 全体検証後に dirty または HEAD 変化があれば停止。PR の head/base、許可パス、全 CI 成功、必須 CI、MERGEABLE/CLEAN、既定では strict branch protection、管理者にも適用される checks を確認。マージは `--match-head-commit` を使用。管理者 bypass、自動マージ予約なし。
- CI が pending なら ci_wait_seconds（既定300秒）まで待つ。失敗、スキップ、待ち時間超過は PR を残して承認待ちで停止する。停止後の自動再開は行わず、人が GitHub 上で状態を確認する。
- PR は計画・検証コマンド・head/base・実行結果の監査記録。Issue を自動 close する参照形式は使わず、マージ確認後のみ close。
- state は mode 0700 推奨。ログは umask 077 で作成し、エージェント出力を含む。PR に載る情報を確認し、機密 Issue を投入しない。
- crash や停止後は state に残る Issue を自動再実行しない。人が worktree、PR、state を調査する。worktree/ログは削除せず保持する。

NORMAL がデフォルト。FAST は独立検証を省略し PR まで（自動マージなし）。DEBATE は計画前に独立した要件批評を追加する。

## Branch protection がない private リポジトリ

信頼済み設定に `"require_branch_protection": false` を指定すると、branch protection なしでも自動マージできる。省略時と `true` は従来どおり保護を必須とする。値は JSON の boolean のみ受け付ける。

`false` は branch-protection API の呼び出しと保護設定の必須判定だけを省略する。この API にアクセスできず 403 となるリポジトリでも利用できる。`true` では 403 を含む API エラーで停止し、他の GitHub API エラーは設定にかかわらず停止する。API エラーを一括で無視する処理はない。

両設定とも、全タスクの検証・全体検証、allowed_paths / protected_paths / 危険変更の判定、検証後の HEAD と作業ツリー、PR head/base の一致、全 CI 成功、configured required_checks、MERGEABLE/CLEAN が必要。失敗・スキップ・キャンセル・タイムアウト・不明な CI はマージしない。pending・不足 CI・未確定の mergeability は既存の待機期限後に approval-required となる。FAST モードでは引き続き自動マージしない。

## 現時点の制約

GitHub 認証、Claude/Codex 認証、必要なラベル・CI、既定では branch protection の事前設定が必要。認証設定や権限を変更しない。GitHub の base 更新と API 呼び出しの間には競合の余地があるため、既定では strict protection による GitHub 側の最終判定も必須。`require_branch_protection=false` でも既存の base SHA 一致と CLEAN 判定を維持するが、判定後の base 更新を GitHub 側の strict protection で防ぐ保証はなくなる。ローカルの失敗注入 E2E を tests に用意。実サービス E2E は明示的な外部送信承認を得てから行う。

## 読み取り文脈と Plan Gate

verifier payload の `verification_phase` は必須で、`PLAN` と `TASK` を明示する。

- `PLAN`（DEBATE の事前要件批評も含む）: 信頼済み文脈に照らして計画の実行可能性、Issue 要件、パス・コマンド、依存順序、書き込み範囲、安全性だけを検証する。変更後 README、実装 diff、テスト実行結果、変更済み worktree、git diff/status、実装後成果物を要求しない。不正なコマンド・パス・順序・範囲は具体的な修正理由付き `FAIL`。本当に必要な信頼済み文脈の欠落や検証基盤障害は `ERROR`。
- root `Cargo.toml` の明示的な不存在は有効な知識。その存在を前提とする root の cargo コマンドは `FAIL` とし、実際の構成に基づく修正を Claude に返す。既存の回数上限内で再計画し、毎回 fresh/read-only の Codex で再検証する。
- `PLAN` では計画が CI の調査・変更に依存するか、Issue が CI 知識を要求する場合以外、`.github/workflows` の本文は要求しない。`required_checks` と検証コマンドは信頼済み Gateway 設定として渡す。読み取り文脈の選択範囲は広げない。
- `TASK`: 編集後のソース・diff・タスク固有の検証証拠で達成条件を検証する。Gateway が実際に成功を確認したコマンドを `test_results` に渡す。必要な証拠不足は `ERROR`、証拠に基づく未達成は `FAIL`。引き続き fresh/read-only とする。
- FINAL/MERGE はこの verifier を共有せず、既存の決定論的な全体検証・CI・マージゲートを維持する。

計画生成は、軽微な文書・設定のみの Issue は1タスク、小規模な局所変更は1〜3タスクを推奨する。さらに分割するのは依存関係が実際に必要な場合だけとし、その理由を達成条件に記す。調査・編集・検証をチェック項目ごとの別タスクにせず、1つの成果にまとめる。既存の最大12タスクと再計画回数上限は維持する。

`allowed_paths` は編集許可だけを表す。`read_context_paths` は信頼済み管理者が指定する読み取り用パターンで、編集許可を追加しない。Git 管理下から読み取りパターンで選択したファイルの完全な本文を Claude に渡し、Codex の一時 workspace に同じ相対パスでコピーする。コピーは mode 0444、Codex 自体も `--sandbox read-only` で毎回新規起動する。元リポジトリへの書き込み権限は与えない。

既定の読み取り対象は `README.md`, `package.json`, `Cargo.toml`, `*config.*`, `scripts/*`, `prepare-wasm.mjs`, `Makefile`, `justfile`, `pyproject.toml`, `mise.toml`。これらを各ディレクトリ階層で照合するため、nested `package.json` / `Cargo.toml` も収集する。`read_context_paths` は既定対象への追加であり、`allowed_paths` は収集対象の選択に使わない。追加のスクリプトや設定が必要なら信頼済み設定にパスを追加する。Issue や計画に書かれたパスによる自動拡張はしない。例えば `allowed_paths=["README.md"]` でも manifest とビルド設定は読めるが、適用・差分・PR の各編集ゲートでは README.md 以外を許可しない。

`repository_manifest` を計画生成・Plan Gate・実装後検証に渡す。安全な Git 管理パス (`tracked`)、本文提供済みパス (`copied`)、リテラルの読み取り候補について実ファイルの不存在を確認したパス (`absent`) を区別する。たとえば root `Cargo.toml` が存在しなければ `absent` に入り、基盤エラーにはしない。未提供・除外パスを不存在とは推測しない。選択済みの Git 管理ファイルが欠落した場合は引き続き基盤エラーにする。

`.env*`, `.git`, `.ssh`, `.aws`、既知の認証設定、secret/credential/password/token/private-key 名、鍵ファイル、`node_modules` / `target` / `build` / `dist` / cache / generated / vendor / 仮想環境などの依存・生成ディレクトリ、既知のバイナリ拡張子は manifest とコピー対象から除外する。NUL を含む内容もコピーしない。シンボリックリンク、選択済みファイルの欠落・読み取り不能・サイズ超過は文脈エラーで停止する（1ファイル100KB、総量2MB）。文脈は黙って切り詰めない。ファイル名による除外は任意のソース内に埋め込まれた秘密の検出ではないため、読み取り対象は管理者が確認する。CLI sandbox 自体の読み取り隔離に関する上記の制約は変わらない。

Plan Gate の状態遷移:

- Claude 計画生成 → 構造・テスト名検査 → 新規 Codex 検証。
- `PASS` → タスク実装へ。
- `FAIL`（十分な文脈に基づく具体的な計画不一致）または不正な計画構造 → 前の計画と具体的フィードバックを Claude に返す → 再計画 → 新規 Codex 検証。
- 修復は `plan_repair_attempts` 回まで（既定2、整数0〜5）。初回を含め最大3計画。上限到達 → `approval-required`。
- `ERROR`（文脈不足・workspace/起動/タイムアウト/出力解析などの検証基盤エラー）→ 診断付き `approval-required`。計画修復には入らない。危険変更の検出も即時停止。

Plan Gate の結果・試行回数・フィードバックを state に保存する。FAST は従来どおり独立検証を省略する。ローカル回帰テストはサービス境界をモックし、実際の文脈収集・一時 workspace 構築・編集拒否・修復遷移を検証する。
