# self-proving-observation/
# └── tests/
#     └── test_verification_failed_consistency.py
#
# stats.pyがverification_failedの除外ロジックを
# 実装し忘れ、ES 429による検証失敗が「100%欠損」として統計検定に混入した。
# aggregate.py/batch_report.py/report.pyには既に除外ロジックがあったが、
# 2026-07-15の全コード監査はこの1箇所を横展開で洗い出せず見落とした。
#
# loss_rate_percentを読むモジュールが増えるたびに人間の記憶やレビューだけに
# 頼って除外ロジックの有無を確認するのではなく、「loss_rate_percentを
# 参照するsrc/配下の全ファイルはverification_failedにも言及している」ことを
# 機械的に強制し、同種の見落としが3回目に起きるのを防ぐ。

import pathlib

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src"


def test_every_module_reading_loss_rate_percent_also_handles_verification_failed():
    offenders = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "loss_rate_percent" in text and "verification_failed" not in text:
            offenders.append(str(path.relative_to(SRC_ROOT.parent)))

    assert not offenders, (
        "loss_rate_percentを参照しているがverification_failedへの言及が"
        f"見つからないファイル: {offenders}"
    )
