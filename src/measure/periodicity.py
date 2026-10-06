# self-proving-observation/
# └── src/
#     └── measure/
#         └── periodicity.py  — 攻撃周期パターン（6.3章）向け、threat_eventsから
#                                対照実験の汚染を除去して集計するヘルパー

"""
対照実験（generator/spike.py）の稼働期間中、threat_eventsは2つの独立した
汚染機構の影響を受けた（doc/known-limitations.md #7）。

  1. app層混入（2026-07-07〜07-12限定）: worker.pyのinject_idベース書き込み
     分離（threat_events / threat_events_experiment）が本番反映される前、
     spike.pyが直接POSTするinject_id付きイベントが本体に混入し続けていた。
  2. network層混入（対照実験稼働期間全体）: vector.tomlのVRLフォールバックの
     盲点（`??`はエラー時のみ発火し、src_ipが空文字の場合は"unknown"へ
     正規化されない）により、Suricataがsrc_ip未解決のまま記録したalert
     イベント（sensor_id=""）が、対照実験の高RPS試行と重なる日に爆発的に
     増加していた（該当日の汚染の63.8%、既知2ノードIPの寄与を上回る
     単独最大の汚染源）。

vector.toml側の修正（2026-08-13）により機構2は将来のデータでは発生しなくなるが、
既存データ（本番デプロイ前に書き込まれた分）はこの恩恵を受けないため、
本モジュールで事後クリーニングする。
"""

import sys
from datetime import date
from pathlib import Path
from typing import Any

# `python3 src/measure/periodicity.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.clickhouse import CLICKHOUSE_ERRORS, query_json

WEEKDAY_LABELS = ("月", "火", "水", "木", "金", "土", "日")

# network層混入の送信元として確認済みのIP（doc/known-limitations.md #7）。
# 対照実験の生成器（generator/spike.py）はproposed-node上で実行され、
# baseline-node（エッジノード、実Suricata稼働）宛てのHTTP POSTトラフィックを
# 生成する。このトラフィック自体がSuricataに正規のネットワークイベントとして
# 検知・記録される。
KNOWN_CONTAMINATED_SENSOR_IDS = ("203.0.113.10", "203.0.113.11")

# src_ip未解決のイベントを表すsensor_id。vector.tomlの修正（2026-08-13）前は空文字、
# 修正後は"unknown"として記録される（proposed/init.sqlのthreat_events_clean VIEWと同じ
# 除外対象。doc/known-limitations.md #7・#UU）。同一の母集団が期間により表記を変えただけ
# なので、観測期間全体で一貫して除外する。
UNRESOLVED_SENSOR_IDS = ("", "unknown")

# app層混入（inject_id付きイベントの直接書き込み）が集中していた期間。
# 同時にディスク逼迫・Elasticsearch障害（known-limitations.md #J・#K・#L）も
# 発生しており実データ自体の劣化も疑われるため、この期間はクリーニングを
# 試みず攻撃周期パターン分析から丸ごと除外する（開始日を含み、終了日は含まない）。
UNSALVAGEABLE_START = "2026-07-07"
UNSALVAGEABLE_END = "2026-07-13"


def fetch_known_node_ids(
    clickhouse_url: str,
    *,
    timeout: int = 10,
) -> tuple[str, ...]:
    """known_nodesテーブルから既知ノードIPを取得する（doc/pipeline-spec.md Phase 7）。

    known-limitations.md #7で発見されたネットワーク層混入の除外対象を、コードへの
    ハードコード（KNOWN_CONTAMINATED_SENSOR_IDS）ではなくデータとして持つための
    オプトイン用ヘルパー。テーブル未作成・クエリ失敗時は、本番未反映の過渡期でも
    呼び出し元が壊れないよう KNOWN_CONTAMINATED_SENSOR_IDS へフォールバックする。
    """
    try:
        # query_json()が末尾に" FORMAT JSON"を付与するため、ここでは付けない
        # （二重に付けるとClickHouseが構文エラーを返し、下のCLICKHOUSE_ERRORSに
        # 握りつぶされて常にフォールバックしていた、doc/known-limitations.md #UU）。
        rows = query_json(
            clickhouse_url, "SELECT ip FROM known_nodes", {}, timeout=timeout,
        )
    except CLICKHOUSE_ERRORS:
        return KNOWN_CONTAMINATED_SENSOR_IDS
    ips = tuple(r["ip"] for r in rows if r.get("ip"))
    return ips if ips else KNOWN_CONTAMINATED_SENSOR_IDS


def fetch_clean_daily_counts(
    clickhouse_url: str,
    ts_from: str,
    ts_to: str,
    *,
    exclude_unsalvageable: bool = True,
    timeout: int = 120,
    contaminated_sensor_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """threat_eventsから対照実験の汚染を除去した日次件数を取得する。

    2026-08-13時点で検証済みの3条件除外（inject_id・既知ノードIP・src_ip未解決の
    sensor_id〔空文字および"unknown"、UNRESOLVED_SENSOR_IDS〕）に加え、既定でapp層混入が集中していた期間（UNSALVAGEABLE_START〜END）を
    丸ごと除外する（doc/known-limitations.md #7・doc/decisions.md参照）。

    contaminated_sensor_ids: 未指定時は従来通り KNOWN_CONTAMINATED_SENSOR_IDS
    （固定2拠点）を使う（既存呼び出しは無変更で動作、内部指針 3.4）。
    fetch_known_node_ids() の戻り値を渡すと、known_nodesテーブルに登録された
    拠点数に応じて除外対象を拡張できる（doc/pipeline-spec.md Phase 7）。

    ts_from/ts_to は ClickHouse の DateTime64(3) がパース可能な文字列
    （例: "2026-07-01 00:00:00"）。exclude_unsalvageable=False にすると
    期間除外なしの3条件除外のみになる（検証・比較用）。

    Returns: [{"day": "2026-07-13", "total_count": 468703}, ...]
    通信エラー時は空リストを返す（呼び出し元でログ出力等は行わない、
    既存の query_json 系ヘルパーと同じ責務分離）。
    """
    sensor_ids = list(contaminated_sensor_ids or KNOWN_CONTAMINATED_SENSOR_IDS)
    where_clauses = [
        "timestamp >= {ts_from:DateTime64(3)}",
        "timestamp < {ts_to:DateTime64(3)}",
        "inject_id = ''",
        "sensor_id NOT IN ({known_sensors:Array(String)})",
        "sensor_id NOT IN ({unresolved_sensors:Array(String)})",
    ]
    params = {
        "ts_from": ts_from,
        "ts_to": ts_to,
        "known_sensors": sensor_ids,
        "unresolved_sensors": list(UNRESOLVED_SENSOR_IDS),
    }
    if exclude_unsalvageable:
        where_clauses.append(
            "(toDate(timestamp) < {unsalvageable_start:Date} "
            "OR toDate(timestamp) >= {unsalvageable_end:Date})"
        )
        params["unsalvageable_start"] = UNSALVAGEABLE_START
        params["unsalvageable_end"] = UNSALVAGEABLE_END

    where = " AND ".join(where_clauses)
    sql = (
        "SELECT toDate(timestamp) AS day, sum(count) AS total_count "  # noqa: S608  # nosec
        f"FROM threat_events WHERE {where} GROUP BY day ORDER BY day"
    )

    try:
        rows = query_json(clickhouse_url, sql, params, timeout=timeout)
    except CLICKHOUSE_ERRORS:
        return []

    return [{"day": r["day"], "total_count": int(r["total_count"])} for r in rows]


def group_by_weekday(daily_counts: list[dict[str, Any]]) -> dict[str, list[float]]:
    """清浄日次件数を曜日別（月〜日）にグルーピングする（5.3節・6.3章の曜日別集計用）。

    段差（doc/paper-data-snapshot.md「07-20段差」）等のレジーム変化点が含まれる
    期間をそのまま渡すと、特定のレジームが特定の曜日カテゴリを支配し誤った周期性を
    示唆しうる（doc/known-limitations.md #7）。レジーム区分が必要な場合は、
    呼び出し側で daily_counts を区間ごとに分割してから本関数へ渡すこと
    （本関数自身はレジーム境界を決め打ちしない）。
    """
    groups: dict[str, list[float]] = {label: [] for label in WEEKDAY_LABELS}
    for row in daily_counts:
        weekday = date.fromisoformat(row["day"]).weekday()
        groups[WEEKDAY_LABELS[weekday]].append(float(row["total_count"]))
    return groups


def group_by_cycle_phase(
    daily_counts: list[dict[str, Any]],
    period_days: int = 24,
) -> dict[str, list[float]]:
    """清浄日次件数を、先頭日からの経過日数 mod period_days でグルーピングする
    （5.3節の任意周期候補・24日周期のビン集計用）。

    group_by_weekday と同様、レジーム変化点を跨ぐ期間を渡す場合は呼び出し側で
    区間分割してから渡すこと。daily_counts の並び順に依存せず、"day" 文字列の
    昇順で先頭日（位相0）を決定する。
    """
    if not daily_counts:
        return {}

    start = min(date.fromisoformat(row["day"]) for row in daily_counts)
    groups: dict[str, list[float]] = {str(i): [] for i in range(period_days)}
    for row in daily_counts:
        phase = (date.fromisoformat(row["day"]) - start).days % period_days
        groups[str(phase)].append(float(row["total_count"]))
    return groups
