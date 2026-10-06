# self-proving-observation/
# └── src/
#     └── logging_config.py  — 常駐プロセス（worker/monitor/generator/ml）向け共通ロガー

import logging
import sys


def ensure_utf8_stdio() -> None:
    """stdout/stderr を明示的に UTF-8 に固定する。

    Windows の既定コンソールコードページ（cp932 等）では、コード中の
    日本語コメント記号（em dash "—" 等）を含む文字列を print()/logging
    で出力した際に UnicodeEncodeError で落ちる。source encoding とは
    無関係に発生するため、エントリーポイントの先頭で毎回呼び出すこと。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


class _MaxLevelFilter(logging.Filter):
    """指定レベル以下のレコードのみ通す（print(..., file=sys.stdout) 相当）。"""

    def __init__(self, max_level: int):
        super().__init__()
        self._max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self._max_level


def get_logger(name: str) -> logging.Logger:
    """print()/print(file=sys.stderr) 互換の出力先分離ロガーを返す。

    INFO 以下は stdout、WARNING 以上は stderr に出す（従来の print 呼び分けと同じ経路）。
    フォーマットはメッセージそのまま（メッセージ内の [INFO]/[WARN]/[ERROR] 等の
    手書きプレフィックスは維持する）。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        ensure_utf8_stdio()
        formatter = logging.Formatter("%(message)s")

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(_MaxLevelFilter(logging.INFO))

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.setLevel(logging.WARNING)

        logger.addHandler(stdout_handler)
        logger.addHandler(stderr_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
