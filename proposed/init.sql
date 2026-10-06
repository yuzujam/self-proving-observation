CREATE TABLE IF NOT EXISTS threat_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    sensor_id String,
    event_type String,
    severity UInt8,
    count UInt64 DEFAULT 1,
    inject_id String DEFAULT '',
    INDEX inject_id_bloom (inject_id) TYPE bloom_filter(0.01) GRANULARITY 1
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, sensor_id)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- 対照実験（src/generator/spike.py）の合成注入トラフィック専用テーブル。
-- inject_id突合によるloss_rate計測にのみ使う使い捨てデータのため、
-- 90日観測データ本体（threat_events）とは分離し、短いTTLで自動的に
-- 消える設計にする。
-- カラム構成はthreat_eventsと同一。
CREATE TABLE IF NOT EXISTS threat_events_experiment (
    timestamp DateTime64(3) DEFAULT now64(3),
    sensor_id String,
    event_type String,
    severity UInt8,
    count UInt64 DEFAULT 1,
    inject_id String DEFAULT '',
    INDEX inject_id_bloom (inject_id) TYPE bloom_filter(0.01) GRANULARITY 1
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, sensor_id)
TTL toDateTime(timestamp) + INTERVAL 3 DAY
SETTINGS index_granularity = 8192;

-- threat_eventsの生データをそのまま集計に使うと、既知の汚染源（対照実験の
-- ネットワーク層混入・両ノード自身のIP・src_ip不明パケット）が混入する。
-- 分析のたびに手動で3条件除外クエリを
-- 書く運用は書き忘れのリスクがあるため、確立済みの除外条件をVIEWとして
-- 固定する。生テーブル自体は一切変更せず（削除・上書きなし）、あくまで
-- 閲覧用のフィルタを一箇所に集約するのが目的。
-- 対象: (1)対照実験の合成注入トラフィック(inject_id) (2)両ノード自身のIP
-- 宛/発の混入 (3)src_ipが空文字または'unknown'（2026-08-13のvector.toml
-- 修正前は空文字、修正後はunknownとして記録される）
CREATE VIEW IF NOT EXISTS threat_events_clean AS
SELECT *
FROM threat_events
WHERE inject_id = ''
  AND sensor_id NOT IN ('203.0.113.10', '203.0.113.11')
  AND sensor_id NOT IN ('', 'unknown');

CREATE TABLE IF NOT EXISTS pipeline_health (
    checked_at DateTime DEFAULT now(),
    events_last_hour UInt64,
    unique_sensors UInt32,
    experiment_table_rows UInt64 DEFAULT 0,
    experiment_table_bytes UInt64 DEFAULT 0
) ENGINE = MergeTree()
ORDER BY checked_at
TTL checked_at + INTERVAL 180 DAY;

-- 既存本番環境向けマイグレーション（新規デプロイではCREATE TABLE時点で
-- 列が存在するため IF NOT EXISTS により何もしない）。threat_events_experiment
-- の肥大化がTTL失効前にClickHouse書き込み速度を低下させた事故を受け、
-- 毎時のpipeline_health記録に
-- 当該テーブルの行数・バイト数も含めて推移を追えるようにする
-- （scripts/record_pipeline_health.py参照。即時アラートを担っていた
-- check_experiment_table_growth.shは通知系全廃で削除済み）。
ALTER TABLE pipeline_health ADD COLUMN IF NOT EXISTS experiment_table_rows UInt64 DEFAULT 0;
ALTER TABLE pipeline_health ADD COLUMN IF NOT EXISTS experiment_table_bytes UInt64 DEFAULT 0;

-- 合成トラフィックと実観測データの経路分離（vector.tomlのtag_synthetic transform）で
-- 付与するタグ用の列。既存行・既存クエリ（SELECT *以外）への影響はない。
-- 主経路（parse_log→aggregator）はこの列を一切送信しないため、既存の書き込みには
-- 影響しない（DEFAULT空文字が入る）。
ALTER TABLE threat_events ADD COLUMN IF NOT EXISTS traffic_class LowCardinality(String) DEFAULT '';

-- 各拠点からの生存証明（1分単位）。通信断・クラッシュによる観測欠損期間を
-- 自動検知するための自己証明レイヤー。send_heartbeat.sh は生存時に status='ok'
-- のみを送信し、失敗時は何も送信せず終了するため、欠損は status != 'ok' の
-- 行としてではなく「本来1分ごとにあるはずの行がテーブルに存在しない」という
-- 行の不在として現れる（集計は the coverage query 参照）。
CREATE TABLE IF NOT EXISTS heartbeats (
    timestamp DateTime DEFAULT now(),
    node_id String,
    status LowCardinality(String) DEFAULT 'ok'  -- 現状 'ok' 固定。将来の拡張用に列として保持
) ENGINE = MergeTree()
ORDER BY (timestamp, node_id)
TTL timestamp + INTERVAL 180 DAY;

-- バックアップ成否を記録し、論文の自己証明型完全性保証に組み込む
CREATE TABLE IF NOT EXISTS backup_log (
    backup_date Date,
    node_id String,
    table_name String,
    status LowCardinality(String),  -- 'success' | 'export_failed' | 'upload_failed'
    row_count UInt64 DEFAULT 0,
    error_message String DEFAULT '',
    created_at DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (backup_date, node_id, table_name)
TTL backup_date + INTERVAL 180 DAY;

-- 既知ノードのIP・役割をデータとして
-- 持つ。拠点追加は
-- 1行INSERTで完結させる設計。本テーブル定義のみで、行の投入（実IPの登録）は
-- 運用時の手動INSERTとして別途行う。
CREATE TABLE IF NOT EXISTS known_nodes (
    ip String,
    node_id String,
    role LowCardinality(String),  -- 'proposed' | 'baseline' 等
    added_at DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY ip;

-- 検証・通知機構自体がサイレントに
-- 壊れる構造的パターンへの対処。Heartbeatの
-- 「行の不在」検知パターン（Proposition 1〜3）をbackup_log・pipeline_healthにも
-- 一般化しClickHouse上で完結させる。cron通知は伴わず、対話セッションでの手動確認
-- （the audit-trail check）からの参照を想定する。
--
-- results_syncは両ノードのrotate_and_backup.shが4時間おき（cron: 0 */4 * * *）に
-- 必ず1回ずつ記録する行。gap_minutes > 480（2周期分、1回分の
-- 遅延は許容）を欠損区間の目安とする（/coverageの欠損区間クエリと同じ考え方）。
CREATE VIEW IF NOT EXISTS backup_log_gaps AS
SELECT
    node_id,
    created_at AS gap_start,
    next_ts AS gap_end,
    dateDiff('minute', created_at, next_ts) AS gap_minutes
FROM
(
    SELECT
        node_id,
        created_at,
        leadInFrame(created_at) OVER (PARTITION BY node_id ORDER BY created_at ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS next_ts
    FROM backup_log
    WHERE table_name = 'results_sync'
)
WHERE next_ts != toDateTime(0) AND dateDiff('minute', created_at, next_ts) > 480;

-- pipeline_healthはproposed-nodeのみが毎時（cron: 0 * * * *）記録するため
-- node_idでの区分はしない。gap_minutes > 120（2周期分）を欠損区間の目安とする。
CREATE VIEW IF NOT EXISTS pipeline_health_gaps AS
SELECT
    checked_at AS gap_start,
    next_ts AS gap_end,
    dateDiff('minute', checked_at, next_ts) AS gap_minutes
FROM
(
    SELECT
        checked_at,
        leadInFrame(checked_at) OVER (ORDER BY checked_at ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS next_ts
    FROM pipeline_health
)
WHERE next_ts != toDateTime(0) AND dateDiff('minute', checked_at, next_ts) > 120;
