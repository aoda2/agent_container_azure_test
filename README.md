# Agent Skill × Docker × Azure Container Apps Job

Claude の **Agent Skill** を Docker イメージに同梱し、**Azure Container Apps Job**
としてサーバーレス実行するサンプルです。

タスクは 3 つ入っていて、環境変数 `TASK` で切り替えます。

| `TASK` | 内容 | 1回のコスト目安 |
|---|---|---|
| **`env-report`**（既定） | 導通確認用。スクリプトを1回実行してコンテナの health check レポートを出す | **$0.01〜0.05** |
| `code-review` | 実用サンプル。対象リポジトリを clone して差分をレビューする | $0.2〜0.6（大きい差分で $1〜2） |
| `explain-code` | 対象リポジトリを clone して差分の内容を解説する（レビューではなく説明） | $0.2〜0.6（大きい差分で $1〜2） |

まず `env-report` で「Skill が読み込まれ → エージェントループが回り → レポートが
stdout に出る」までを数セントで確認し、動いてから `code-review` / `explain-code` に
切り替える流れです。

```
┌─ Docker image ──────────────────────────────┐
│  /home/agent/.claude/skills/                │  ← Agent Skills
│      env-report/SKILL.md                    │
│      code-review/SKILL.md                   │
│      explain-code/SKILL.md                  │
│  /app/bin/collect-env.sh                    │  ← env-report が叩く唯一のコマンド
│  /app/app/run_agent.py                      │  ← Claude Agent SDK (Python)
│  claude-agent-sdk (Claude Code バイナリ同梱)  │
└─────────────────────────────────────────────┘
                    │ az acr build
                    ▼
            Azure Container Registry
                    │
                    ▼
   Azure Container Apps Job (Manual trigger)
     env: TASK / MODEL / RUN_ID / ...
     secret: ANTHROPIC_API_KEY / STORAGE_CONNECTION_STRING
                    │
                    ├─ stdout に Markdown レポート → az containerapp job logs show
                    ▼
        Blob Storage ({RUN_ID}/{MODEL}/{TASK}/report-*.md)
```

## コストについて

### Anthropic API

上の表は概算です。**実際の金額は毎回ジョブのログに出ます** — `run_agent.py` が
SDK の `ResultMessage.total_cost_usd` を拾って出力します:

```
[agent] finished: success (3 turns)
[agent] cost: $0.0213
```

`env-report` を安くしている仕組み:

- モデルは **haiku**（$1 / $5 per MTok）。`code-review` / `explain-code` は sonnet
- **ツール呼び出しは1回だけ**。Skill 側で「`collect-env.sh` を1回実行、他は何もするな」と明示し、
  許可ツールもそのパス限定にしてある
- `max_turns=6` で上限を固定。暴走しても数セントで止まる
- レポートは表 1 つ＋1 行の判定のみ

エージェントは毎ターン会話履歴を再送するため、**コストはターン数に対して
おおむね二次で効きます**。高くつくときは、まず `MAX_TURNS` を下げてください。

### Azure

- **Container Apps Job** — 従量課金。月 180,000 vCPU 秒の無料枠があり、
  このサンプルの実行はまず無料枠に収まります
- **Container Registry (Basic)** — 約 $5/月の**固定費**。ここが放置すると効いてくるので、
  試し終わったら `./infra/cleanup.sh` を実行してください
- **Log Analytics** — Container Apps Environment に付随。取り込み量ぶんだけ
- **Storage Account (Standard_LRS)** — レポートのアップロード先。従量課金で、
  このサンプルの使い方なら数円/月に収まります

## リポジトリ構成

| パス | 役割 |
|------|------|
| `.claude/skills/env-report/SKILL.md` | 導通確認用 Skill |
| `.claude/skills/code-review/SKILL.md` | コードレビュー Skill（レビュー手順・観点・出力形式） |
| `.claude/skills/explain-code/SKILL.md` | コード解説 Skill（解説手順・観点・出力形式） |
| `bin/collect-env.sh` | `env-report` が実行する唯一のスクリプト |
| `app/run_agent.py` | エントリポイント。タスク定義 → Agent SDK 実行 → レポート出力・Blob アップロード |
| `Dockerfile` | Skill を `~/.claude/skills/` に配置した実行イメージ |
| `infra/env.sh` | 各スクリプト共通の設定（`.env` があれば読み込む） |
| `infra/deploy.sh` | RG / ACR / Storage Account / Container Apps Env / Job を一括作成 |
| `infra/run-job.sh` | ジョブを 1 回実行して完了まで待つ |
| `infra/run-comparison.sh` | `explain-code` を複数モデルで並列実行し、結果を同じ RUN_ID で Blob に集約 |
| `infra/logs.sh` | 実行ログ（＝レポート）を取得 |
| `infra/cleanup.sh` | 作成したリソースをまとめて削除 |
| `scripts/run-local.sh` | Azure に出す前にローカル Docker で同じイメージを検証 |

## 前提

- Anthropic API キー（`ANTHROPIC_API_KEY`）
- ローカル検証用に Docker
- デプロイ用に [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) と `az login` 済みのサブスクリプション

設定はリポジトリルートの `.env` に置けます（`.gitignore` 済み）:

```sh
ANTHROPIC_API_KEY=sk-ant-...
LOCATION=japaneast
# TASK=code-review
# TARGET_REPO_URL=https://github.com/your-org/your-repo
# BASE_REF=main~3
```

## 1. ローカルで動かす

```sh
./scripts/run-local.sh
```

イメージをビルドして `env-report` を実行し、レポートを標準出力と `out/report.md` に
書き出します。出力はこんな形です:

```markdown
# Agent Container Health Check

| Check | Value |
|-------|-------|
| OS / arch | Linux x86_64 |
| Python | Python 3.12.x |
| ...
```

重いほうを試すとき:

```sh
TASK=code-review \
TARGET_REPO_URL=https://github.com/your-org/your-repo \
BASE_REF=main~3 HEAD_REF=main \
./scripts/run-local.sh
```

## 2. Azure にデプロイする

```sh
./infra/deploy.sh              # TASK=env-report で作られる
TASK=code-review ./infra/deploy.sh
```

冪等なので、コード変更後に再実行すればイメージの作り直し＋ジョブ再作成まで通しで行われます。
やっていること:

1. リソースグループを作成
2. Azure Container Registry を作成（`--admin-enabled`）
3. **`az acr build` でクラウド側ビルド** — ローカル Docker 不要、かつ確実に `linux/amd64` で焼かれる
4. Storage Account と Blob コンテナを作成（レポートのアップロード先）
5. Container Apps Environment を作成
6. Manual トリガーのジョブを作成。`ANTHROPIC_API_KEY` と Storage の接続文字列は
   Container Apps の secret として保存し、環境変数は `secretref:` 参照のみを持つ

## 3. 実行してレポートを取り出す

```sh
./infra/run-job.sh     # 起動して完了まで待つ
./infra/logs.sh        # 直近の実行ログ（＝レポート＋実コスト）を表示
```

ジョブ定義を変えずに 1 回だけ設定を変えたい場合は `OVERRIDE_ENV` を使います:

```sh
OVERRIDE_ENV="TASK=code-review TARGET_REPO_URL=https://github.com/org/repo BASE_REF=v1.0.0" \
./infra/run-job.sh
```

### モデル比較で実行する

同じ対象リポジトリ・差分範囲を `haiku` / `sonnet` / `opus` の3モデルで並列実行し、
結果をまとめて Blob Storage に保存します:

```sh
TARGET_REPO_URL=https://github.com/org/repo BASE_REF=v1.0.0 \
./infra/run-comparison.sh
```

比較するモデルは `MODELS="haiku sonnet ..."` で変更できます。実行が終わると
共有の `RUN_ID`（UTC タイムスタンプ）が表示され、各モデルのレポートは
`{RUN_ID}/{MODEL}/explain-code/report-*.md` として同じコンテナ配下に並ぶので、
`az storage blob list --prefix "${RUN_ID}/"` で一覧して見比べられます。

## 4. 後片付け

```sh
./infra/cleanup.sh
```

ACR Basic の固定費があるので、試し終わったら実行してください。

## 環境変数リファレンス

`app/run_agent.py` が読むもの:

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | （必須） | Anthropic API キー |
| `TASK` | `env-report` | `env-report` / `code-review` / `explain-code` |
| `MODEL` | タスク依存 | `env-report`→`haiku`、`code-review`/`explain-code`→`sonnet` |
| `MAX_TURNS` | タスク依存 | `env-report`→`6`、`code-review`/`explain-code`→`40` |
| `OUTPUT_DIR` | – | 指定するとレポートを `report.md` としても書き出す |
| `SETTING_SOURCES` | `user` | Skill の探索元（`user` / `project` / `local` のカンマ区切り） |
| `RUN_ID` | 実行時刻 | 複数実行の結果を Blob 上でグループ化する識別子（モデル比較で使用） |
| `STORAGE_CONNECTION_STRING` | – | Azure Storage 接続文字列。`BLOB_CONTAINER` と併せて設定するとレポートをアップロード |
| `BLOB_CONTAINER` | – | アップロード先の Blob コンテナ名 |
| `OTEL_CAPTURE` | – | `1` で Claude Code の OpenTelemetry ログ/メトリクスを取得し、`{RUN_ID}/{MODEL}/{TASK}/otel-*.jsonl` に OTLP JSON（1 行 1 リクエスト）としてアップロード。Collector 不要（プロセス内の受信機を使用） |
| `OTEL_LOG_USER_PROMPTS` | – | `1` でイベントにプロンプト本文も含める（機密情報が Blob に残る点に注意） |

`code-review` / `explain-code` のみ:

| 変数 | 既定値 | 説明 |
|------|--------|------|
| `TARGET_REPO_URL` | – | clone 対象。未設定なら `WORKSPACE_DIR` を既存リポジトリとして扱う |
| `GIT_TOKEN` | – | プライベートリポジトリ用。https URL に注入される |
| `BASE_REF` | `origin/HEAD` | 差分の基点。解決できなければ `origin/HEAD` → `HEAD~1` の順にフォールバック |
| `HEAD_REF` | `HEAD` | 差分の先端 |
| `CLONE_DEPTH` | `50` | shallow clone の深さ。`0` で全履歴 |
| `REVIEW_PROMPT` | – | プロンプトに追記する追加指示 |
| `WORKSPACE_DIR` | `/workspace/repo` | リポジトリの配置先 |

## 設計上のポイント

**Skill はユーザースコープに置く。** SDK が Skill を見つけるのは `setting_sources`
が指すファイルシステム上の場所です。`project` スコープ（`<cwd>/.claude/skills/`）は
`code-review` では cwd＝clone したリポジトリになるため、対象リポジトリに `.claude/` が
あると**その中身がエージェントへの指示として読み込まれてしまいます**。そのため
Dockerfile では Skill を `/home/agent/.claude/skills/` に配置し、既定の
`SETTING_SOURCES` を `user` のみにしています。

**権限はプリアプルーブ方式。** TTY のないコンテナで許可プロンプトが出るとジョブが
タイムアウトまでハングします。`permission_mode="dontAsk"` にして、許可リストに
無いツールはプロンプトではなく拒否させています。許可範囲はタスクごとに最小化:

- `env-report` — `Skill` と `Bash(bash /app/bin/collect-env.sh)`（このパスの完全一致のみ）
- `code-review` / `explain-code` — `Skill` / `Read` / `Grep` / `Glob` と `Bash(git:*)`（bash は git サブコマンドのみ）

**標準出力＝レポート、標準エラー＝進捗。** 進捗ログを stderr に分けているので、
ローカルでは `docker run ... > report.md` がそのまま使えます。

**イメージは大きい（約 700MB）。** `claude-agent-sdk` の wheel が
プラットフォーム固有の Claude Code バイナリ（約 290MB）を同梱するためです。
アーキテクチャ違いの wheel はビルド時ではなく**実行時**に失敗するので、
`--platform linux/amd64` の指定を外さないでください。

## 拡張の方向性

- **Skill を増やす** — `.claude/skills/<name>/SKILL.md` を追加し、
  `run_agent.py` の `TASKS` に 1 エントリ足す（skill 名・許可ツール・モデル・ターン上限）
- **モデル比較結果の評価** — `run-comparison.sh` は Blob にレポートを集約するところまで。
  次の一手は、`RUN_ID` 配下のレポートをまとめて読み、LLM-as-judge やルーブリックで
  スコアリングして1枚の比較レポートにする集計スクリプト
- **定期実行** — `--trigger-type Schedule --cron-expression "0 9 * * 1"` でジョブを作る
- **イベント駆動** — `--trigger-type Event` で Storage Queue / Service Bus をトリガーにし、
  GitHub Webhook から PR ごとにレビューを起こす
- **認証の本番化** — ACR の admin ユーザーをやめてマネージド ID＋AcrPull ロールにし、
  API キーは Key Vault + `keyvaultref:` で参照する

## 参考

- [Claude Agent SDK — Python reference](https://code.claude.com/docs/en/agent-sdk/python)
- [Agent Skills in the SDK](https://code.claude.com/docs/en/agent-sdk/skills)
- [Azure Container Apps Jobs](https://learn.microsoft.com/azure/container-apps/jobs)
- [Anthropic API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
