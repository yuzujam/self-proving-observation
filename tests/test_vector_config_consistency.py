# self-proving-observation/
# └── tests/
#     └── test_vector_config_consistency.py
#
# edge/vector.toml と proposed/vector.toml は Suricata ログの取り込み・
# パース・1秒集約ロジック（sources / transforms）が完全に同一であるべきで、
# ノードごとに異なるのは送信先・エラーログ出力先（sinks）のみ。
# 2ファイルを手動で同期しているため、片方だけ編集して集約ロジックが
# 意図せず乖離することを防ぐガードレールとして比較する。

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(relative_path: str) -> dict:
    with open(ROOT / relative_path, "rb") as f:
        return tomllib.load(f)


def test_sources_and_transforms_match_between_edge_and_proposed():
    edge = _load("edge/vector.toml")
    proposed = _load("proposed/vector.toml")

    assert edge["sources"] == proposed["sources"]
    assert edge["transforms"] == proposed["transforms"]


def test_sinks_intentionally_differ_between_edge_and_proposed():
    """edge は環境変数 ${PROPOSED_URL} 宛て・エラーは console、
    proposed は receiver コンテナ宛て・エラーはファイル出力。
    この差分自体は意図的なので固定化はせず、キー構成のみ確認する。
    """
    edge = _load("edge/vector.toml")
    proposed = _load("proposed/vector.toml")

    assert set(edge["sinks"].keys()) == {"receiver", "error_log"}
    assert set(proposed["sinks"].keys()) == {"receiver", "error_log"}
    assert edge["sinks"]["receiver"]["uri"] == "${PROPOSED_URL}"
    assert proposed["sinks"]["receiver"]["uri"] == "http://receiver:8000/events"
