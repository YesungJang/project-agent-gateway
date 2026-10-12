# Agent Gateway v0

## Read-only Dashboard MVP

The independent observer and lightweight browser UI run with Python standard library
only, on `http://127.0.0.1:8765`:

```sh
python3 dashboard.py --state /absolute/path/gateway-state --port 8765
```

It never starts the worker, calls GitHub, exposes raw prompts/logs/configuration, or
offers approval/retry/merge/shell controls. Existing state without event history is
displayed with unknown stage/time. New history is recorded by the instrumented
single-shot worker under its existing lock; viewing the UI does not activate it.

See [local startup and operations](docs/dashboard-operations.md),
[event history](docs/event-history.md), [worker observation](docs/worker-observation.md),
[read-only API](docs/read-only-api.md), [browser UI and synthetic demo](docs/dashboard-ui.md),
and [verification and remaining acceptance work](docs/dashboard-verification.md).

停止した run（`approval-required` / `failed`）は観測メトリクスより上に、最後の診断コードと停止ステージ付きで一覧する。停止がなければ非表示。

## CI

`.github/workflows/ci.yml` が push と PR で Python スイート、Dashboard の DOM 境界テスト、`gateway.py` の Ruff を実行する。`required_checks` には `check-run:CI/test` と `check-run:CI/lint` を指定できる。安全 regression は pinned commit と比較するため、checkout は `fetch-depth: 0` が必要。
The implementation PRs remain drafts pending independent Codex CLI and real-browser
verification. macOS-specific behavior has not been verified in the cloud session.

Python 3.12 の標準ライブラリのみ。GitHub Issue → Claude の計画 → Codex の Plan Gate → タスクごとの編集案・テスト・Codex Gate・修復 → 全体検証 → PR → 決定論的 Merge Gate。

## 実行

`config.example.json` をリポジトリ外の信頼済み設定にコピーし、対象・許可パス・既存の検証コマンド・必須 CI 名を設定する。設定と state はエージェント編集範囲外に置く。依存準備が必要なプロジェクトでは、管理者が確認した `bootstrap` を設定する。省略時は依存インストール・生成を行わない。

```sh
mise exec -- python3 -m unittest discover -s tests -v
mise exec -- python3 gateway.py --config /absolute/path/config.json --state /absolute/path/gateway-state
```

通常の実行は GitHub を読み取り、ローカル worktree と PR 本文まで作成する。Claude/Codex API への送信を伴うため、対象の Issue・許可ソース・差分を両サービスに送ってよいことを事前に承認する。外部送信の承認後のみ実行する。
GitHub 書き込みも承認済みの場合に限り `--publish` を追加する。`--publish` はラベル変更、push、PR 作成、安全なマージ、Issue close、停止理由の Issue コメントを明示的に許可する。初回に以下のラベルを GitHub で作成する:

- `gateway:queued`
- `gateway:running`
- `gateway:approval-required`
- `gateway:failed`
- `gateway:pr-ready`
- `gateway:done`

queued ラベルを付けた Issue を古い番号から1件処理して終了する。v0 は常駐サービスを導入しない。手動または信頼済みスケジューラで同じコマンドを繰り返す。全ワーカーは同じ state を使用する。Mac のファイルロックで1件ずつ実行する。

## Worktree bootstrap

Git worktree には Git 管理下のファイルだけが入り、元 checkout の `node_modules` や生成済みパッケージは引き継がれない。必要な準備を信頼済み Gateway 設定の任意の `bootstrap` に argv 配列のリストで指定する。例（実際のコマンドは対象プロジェクトに合わせて管理者が確認する）:

```json
"bootstrap": [
  ["npm", "--prefix", "web", "ci"],
  ["npm", "--prefix", "web", "run", "build:generated"]
]
```

- 省略または `[]` は何もしない。文字列コマンド、空の argv、文字列以外・空文字・NUL を含む引数は設定エラー。
- origin の確認と新規 worktree 作成後、その Issue worktree を cwd として順番に一度だけ実行する。計画生成・Claude 実装・タスク検証・Codex TASK 検証・全体検証より前に完了する。タスク追加や修復では再実行しない。
- 実行元は信頼済み設定だけ。Issue、計画、Claude/Codex 出力からコマンドを追加・選択しない。シェルを介さず実行するため、シェル展開・パイプ・リダイレクトは使えない。
- 各コマンドに既存の `timeout_seconds` を適用する。非ゼロ終了・起動失敗・タイムアウトは `BootstrapFailure`（環境障害）として直ちに `approval-required` で停止する。失敗理由には `bootstrap` のゼロ始まりの添字と実行ファイル名を記録し、実行ログは従来の private state に保持する。後続コマンド・Claude 修復は実行せず、実装修復回数も消費しない。`Task validation/disagreement unresolved` には分類しない。
- bootstrap は検証コマンドと同様にローカルの信頼済みコードを実行する。管理者は依存の install scripts を含む実行内容を確認し、秘密の読み取り・外部送信を許可するコマンドを無断で設定しない。生成物は対象リポジトリの既存の ignore 設定に従う。Gateway が ignore や許可パスを自動追加することはなく、追跡ファイルや ignore されない生成物への変更には既存の編集・差分・merge gates が適用される。
- 失敗時も worktree とログを保持する。既存どおり自動再開はせず、管理者が設定・環境と残った worktree を調査する。

## 検証結果の記録

結果は AI の申告ではなく Gateway が観測した事実として記録する。

- 新規 worktree の bootstrap 直後、実装前に `full_validation` を1回実行する（baseline）。失敗は `BootstrapFailure`（環境障害）で即 `approval-required`。計画・実装・修復に入らず、修復回数も消費しない。対象リポジトリが元から赤い状態を、エージェントの失敗と取り違えないため。
- baseline が作業ツリー・index・HEAD を変更した場合も `BootstrapFailure` で停止する。
- baseline は新しいステージを作らず、既存の `bootstrap` ステージとして観測する。失敗は `BOOTSTRAP_FAILED` として Dashboard の診断に出る。
- 各検証コマンドについて `status`（実測）、`seconds`、`output_tail`（末尾15行・2000文字）を記録する。TASK verifier の `test_results` にはこの実測値を渡す。固定の `PASS` は渡さない。
- 検証失敗時は出力末尾を例外メッセージに含める。修復時の Claude へのフィードバックと state の `task_feedback` に残り、`(see private logs)` だけで終わらない。
- PR 本文に baseline と全体検証の実測結果（name / status / seconds）、タスクごとの検証結果、変更されたテストファイル一覧を載せる。`output_tail` は PR に載せず、private log と verifier のみに渡す。本文サイズと公開範囲を抑えるため。
- baseline・タスク検証・全体検証の実測（name / status / seconds）は event の `validation` detail としても記録し、Dashboard の該当ステージに表示する。`output_tail` は event にも HTTP にも出さない。
- `tests_changed` はパスに `test` / `spec` を含むかどうかのヒューリスティックで、カバレッジの指標ではない。テスト変更がなくても停止はせず、事実として表示する。
- `output_tail` は検証コマンドの出力であり、verifier（外部サービス）と PR 本文経由ではない private log に渡る。出力に秘密を含むコマンドを `validation` に設定しない。

## 安全と状態

- Issue は JSON のデータとして stdin に渡す。シェルは使わず、検証コマンドは設定の argv 配列だけから選択する。ただし検証コマンド自体がコードを実行するため、対象リポジトリとそのテストは事前に信頼・確認が必要。
- Claude は safe/restricted mode、tools 無効、MCP 無効。許可されたテキストソースだけを渡し、構造化編集案を Gateway が適用する。セッションはタスク間で resume。直接 Bash/Edit は与えない。
- Codex は毎回 fresh な read-only sandbox で検証する。`--approve-for-me` は workspace-write を許可するため検証器には使用しない。`--sandbox` との併用なし。危険な権限 bypass なし。
- Codex には秘密ファイル・外部送信・変更を禁止する指示を渡す。CLI read-only sandbox は機密ファイルに対する完全な読み取り隔離ではない。機密を持つリポジトリは別の OS ユーザー/隔離環境で運用する。
- `.env` / `.env.*` / `.git` / `.ssh` / `.aws`、パス逸脱、シンボリックリンク、許可範囲外の編集は拒否する。
- 依存/lock、migration/SQL、CI/隠しパス、infra/Terraform/AWS、auth/permission/secret/security/config/gateway、指示書、シェルスクリプトは承認へ昇格する。意味上の認証・権限変更を完全に判定することはできないため、リポジトリ管理者が protected_paths を追加する。
- Git hooks は Gateway の Git 操作で無効。GitHub は設定の origin と一致する必要がある。各タスクを1コミットにする。
- タスクのテスト失敗/FAIL は最大 repair_attempts 回修復する。計画修復の上限超過、修復不能、危険変更、タイムアウト、不明な CI は approval-required で停止。自動承認の機構は設けない。
- 全体検証後に dirty または HEAD 変化があれば停止。PR の head/base、許可パス、必須 CI 成功、MERGEABLE/CLEAN、既定では strict branch protection、管理者にも適用される checks を確認。マージは `--match-head-commit` を使用。管理者 bypass、自動マージ予約なし。
- 必須 CI が pending なら ci_wait_seconds（既定300秒）まで待つ。失敗、スキップ、待ち時間超過は PR を残して承認待ちで停止する。停止後の自動再開は行わず、人が GitHub 上で状態を確認する。
- 停止時は `--publish` のときだけ Issue にコメントする。内容は停止ステージ、固定の診断コードと定型文、固定語彙のゲート判定と試行回数のみ。verifier の文言・検証コマンドの出力・private パスは含めない。コメント送信の失敗は元の停止を隠さず、stderr に出して停止理由はそのまま伝播する。
- PR は計画・検証コマンド・head/base・実測の検証結果の監査記録。Issue を自動 close する参照形式は使わず、マージ確認後のみ close。
- state は mode 0700 推奨。ログは umask 077 で作成し、エージェント出力を含む。PR に載る情報を確認し、機密 Issue を投入しない。
- crash や停止後は state に残る Issue を自動再実行しない。人が worktree、PR、state を調査する。worktree/ログは削除せず保持する。

NORMAL がデフォルト。FAST は独立検証を省略し PR まで（自動マージなし）。DEBATE は計画前に独立した要件批評を追加する。

## Branch protection がない private リポジトリ

信頼済み設定に `"require_branch_protection": false` を指定すると、branch protection なしでも自動マージできる。省略時と `true` は従来どおり保護を必須とする。値は JSON の boolean のみ受け付ける。

`false` は branch-protection API の呼び出しと保護設定の必須判定だけを省略する。この API にアクセスできず 403 となるリポジトリでも利用できる。`true` では 403 を含む API エラーで停止し、他の GitHub API エラーは設定にかかわらず停止する。API エラーを一括で無視する処理はない。

両設定とも、全タスクの検証・全体検証、allowed_paths / protected_paths / 危険変更の判定、検証後の HEAD と作業ツリー、PR head/base の一致、configured required_checks の全成功、MERGEABLE/CLEAN が必要。必須 CI の失敗・スキップ・neutral・キャンセル・タイムアウト、および不明・不正な CI データではマージしない。pending・不足 CI・未確定の mergeability は既存の待機期限後に approval-required となる。FAST モードでは引き続き自動マージしない。

## Required checks の識別子と状態判定

`required_checks` は PR の `gh pr view <PR> --json statusCheckRollup` に実在するフィールドから指定する。表示ラベルやイベントの接尾辞を推測しない。形式は以下のとおり（大文字・小文字を区別）:

- CheckRun: `check-run:<workflowName>/<name>`。workflowName が空または存在しない場合だけ `check-run:/<name>`。workflowName が存在する run を名前だけで照合する fallback はない。
- StatusContext: `status-context:<context>`。
- 各要素は UTF-8 の percent-encoding（Python `urllib.parse.quote(value, safe="")`）。英数字と `-._~` はそのまま、空白は `%20`、`/` は `%2F`、`%` は `%25`。区切りの `/` と各要素内の `/` を区別する。例: workflowName=`CI/main`, name=`unit tests` → `check-run:CI%2Fmain/unit%20tests`。
- 旧形式の単独 job 名や `CI/core (pull_request)` は設定読み込み時に拒否する。設定例の CI/test・CI/lint も対象 PR の実際の値に置き換える。

PR #11 で報告されたデータに対する移行後の設定:

```json
"required_checks": [
  "check-run:CI/core",
  "check-run:ci-medium/node-tests",
  "check-run:ci-fast/rust-tests",
  "check-run:CI/web"
]
```

`~/.config/agent-gateway/laplogos.json` は上記の新形式へ移行済み（2026-10-12 に `validate_config` で確認）。Gateway は設定の自動書き換えを行わない。

| 必須チェックの観測値 | 判定 |
| --- | --- |
| 未出現（rollup が null/空も含む） | GatePending |
| CheckRun: QUEUED / IN_PROGRESS / PENDING / REQUESTED / WAITING、conclusion が欠落・null・空文字 | GatePending |
| CheckRun: COMPLETED / SUCCESS | 成功 |
| CheckRun: COMPLETED / FAILURE / CANCELLED / TIMED_OUT / ACTION_REQUIRED / STARTUP_FAILURE / SKIPPED / NEUTRAL / STALE | ApprovalRequired（conclusion が SUCCESS 以外） |
| StatusContext: SUCCESS | 成功 |
| StatusContext: PENDING / EXPECTED | GatePending |
| StatusContext: FAILURE / ERROR | ApprovalRequired |
| 未知の型・状態、壊れた識別子、COMPLETED の conclusion 欠落、未完了なのに conclusion が確定 | ApprovalRequired |

同じ識別子の重複は rollup 内の全件を評価する。一件でも失敗なら停止し、一件でも未完了なら待機する。失敗は未完了・不足より優先する。時刻順の選別や一件の成功による上書きはしない。イベントはこの JSON から識別できないため、push/pull_request の実行を推測して除外しない。

設定で必須ではない、既知の正しい状態のチェックは CI 判定を妨げない（SKIPPED/NEUTRAL を含む）。不正・未知のデータは必須かどうかにかかわらず停止する。GitHub の MERGEABLE/CLEAN と既存の保護判定は引き続き必要。保護 API の contexts/checks には独自識別子ではなく、一致した実データの CheckRun.name または StatusContext.context が含まれることを要求し、strict・enforce_admins の検査も維持する。

GatePending は `ci_wait_seconds` まで最大5秒間隔で再取得し、期限切れだけ ApprovalRequired へ昇格する。失敗・待機理由には識別子、rollup の添字、status/conclusion/state を含め、期限切れにも最後の理由を残す。不足時は不足識別子と観測済み識別子を表示する。

根拠: [GitHub CheckRun の状態定義](https://docs.github.com/en/enterprise-cloud%40latest/graphql/reference/checks)、[StatusContext の状態定義](https://docs.github.com/en/graphql/reference/commits)、[gh の statusCheckRollup JSON 出力実装](https://github.com/cli/cli/blob/trunk/api/export_pr.go)。CheckRun の status と conclusion は別のフィールドであり、gh は workflowName/name を返すが `(pull_request)` 付きの識別子は返さない。

## 現時点の制約

GitHub 認証、Claude/Codex 認証、必要なラベル・CI、既定では branch protection の事前設定が必要。認証設定や権限を変更しない。GitHub の base 更新と API 呼び出しの間には競合の余地があるため、既定では strict protection による GitHub 側の最終判定も必須。`require_branch_protection=false` でも既存の base SHA 一致と CLEAN 判定を維持するが、判定後の base 更新を GitHub 側の strict protection で防ぐ保証はなくなる。ローカルの失敗注入 E2E を tests に用意。実サービス E2E は明示的な外部送信承認を得てから行う。

## 読み取り文脈と Plan Gate

verifier payload の `verification_phase` は必須で、`PLAN` と `TASK` を明示する。

- `PLAN`（DEBATE の事前要件批評も含む）: 信頼済み文脈に照らして計画の実行可能性、Issue 要件、パス・コマンド、依存順序、書き込み範囲、安全性だけを検証する。変更後 README、実装 diff、テスト実行結果、変更済み worktree、git diff/status、実装後成果物を要求しない。不正なコマンド・パス・順序・範囲は具体的な修正理由付き `FAIL`。本当に必要な信頼済み文脈の欠落や検証基盤障害は `ERROR`。
- root `Cargo.toml` の明示的な不存在は有効な知識。その存在を前提とする root の cargo コマンドは `FAIL` とし、実際の構成に基づく修正を Claude に返す。既存の回数上限内で再計画し、毎回 fresh/read-only の Codex で再検証する。
- `PLAN` には信頼済み設定の `bootstrap` を渡す。Gateway が計画前に実行済みであることを前提とし、依存インストールや生成物の作成を計画に含めることを禁じる（`allowed_paths` 外への書き込みであり、かつ既に済んでいるため）。計画がそれらを提案したら `FAIL`。
- `PLAN` では計画が CI の調査・変更に依存するか、Issue が CI 知識を要求する場合以外、`.github/workflows` の本文は要求しない。`required_checks` と検証コマンドは信頼済み Gateway 設定として渡す。読み取り文脈の選択範囲は広げない。
- `TASK`: 編集後のソース・diff・タスク固有の検証証拠で達成条件を検証する。 `allowed_paths` と `protected_paths` も渡す（契約が範囲検査を要求するため）。Gateway が実際に成功を確認したコマンドを `test_results` に渡す。必要な証拠不足は `ERROR`、証拠に基づく未達成は `FAIL`。引き続き fresh/read-only とする。
- `TASK` は実装結果を Issue と信頼済みリポジトリ文脈に照らして判定する。「編集前に package.json を読む」「構成や既存設定を調べる」は実装指示であり、操作順序・探索コマンド・内部推論の履歴証明は要求しない。README のコマンドを実際の package.json / Cargo.toml / repository_manifest と比較し、正しければ `PASS`、観測可能な不一致は `FAIL`、判断に本当に必要な文脈不足は `ERROR`。テスト成功だけで不一致を免除しない。
- 元の Issue を TASK にも渡す。Issue が監査・ログ・プロセス証拠を受入成果物として明示した場合のみ、その成果物の存在と内容を検証する。要求成果物の不存在が確認できれば `FAIL`、判断材料が未提供なら `ERROR`。PLAN も隠れた推論・復元不能な過去の履歴証明を要求せず、将来の成果物の実現可能性を確認する。
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
