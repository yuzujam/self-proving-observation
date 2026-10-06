# self-proving-observation/
# └── src/
#     └── monitor/
#         └── resource.py  — リソースモニタリング（CPU/メモリ/ディスクI/O）

import argparse
import csv
import importlib.util
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

# `python3 src/monitor/resource.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.logging_config import get_logger
from src.measure._common import ensure_parent_dir

logger = get_logger(__name__)

_PSUTIL_AVAILABLE = importlib.util.find_spec("psutil") is not None

_shutdown = False


def _handle_signal(_sig: int, _frame: FrameType | None) -> None:
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def collect_system_stats() -> list[dict[str, str]]:
    """psutil でシステム全体のリソースを収集するフォールバック。

    Docker が使えない環境や、compose コンテナが未起動の場合に代替記録する。
    """
    if not _PSUTIL_AVAILABLE:
        return []
    import psutil  # noqa: PLC0415
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_io_counters()
    block_io = (
        f"{disk.read_bytes // (1024 ** 2)}MB / {disk.write_bytes // (1024 ** 2)}MB"
        if disk else "N/A"
    )
    return [{
        "container": "system",
        "cpu_percent": f"{cpu:.1f}",
        "mem_usage": f"{mem.used // (1024 ** 2)}MiB / {mem.total // (1024 ** 2)}MiB",
        "mem_percent": f"{mem.percent:.1f}",
        "block_io": block_io,
    }]


def collect_docker_stats(compose_file: str, project_name: str = "") -> list[dict[str, str]]:
    """docker-compose のコンテナリソース使用状況をローカルで取得する。

    docker コマンドが見つからない場合（リモートサーバー構成など）は
    空リストを返してモニタリングをスキップする。

    project_name: 省略時はdocker composeの既定挙動（compose fileのディレクトリ名を
    プロジェクト名とみなす）に従う。複数のcompose fileが同一ディレクトリに置かれる
    構成（例: proposed/docker-compose.yml と proposed/docker-compose.ablation.yml）では
    既定挙動だと同じプロジェクト名を共有してしまい、`ps -q`が無関係な他方の
    コンテナ群まで返してしまう（実機確認済み）。呼び出し元が独立したスタックを
    対象にする場合は明示的なproject_nameの指定を強く推奨する。
    """
    cmd = ["docker", "compose"]
    if project_name:
        cmd += ["-p", project_name]
    cmd += ["-f", compose_file, "ps", "-q"]
    try:
        id_result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    container_ids = [c for c in id_result.stdout.strip().split("\n") if c]
    if not container_ids:
        return []

    try:
        result = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.BlockIO}}",
            ] + container_ids,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    return _parse_stats_lines(result.stdout, "\t")


def collect_docker_stats_ssh(
    ssh_host: str, ssh_user: str, ssh_port: int = 22,
) -> list[dict[str, str]]:
    """SSH経由でリモートサーバーのDockerコンテナリソース使用状況を取得する。

    パイプ区切りで出力させることでコンテナ名・メモリ値内のスペースと衝突しない。
    baseline-nodeはポート22がT-Potのデコイ（本物の管理シェルではない）のため、
    呼び出し元でssh_portに実際の管理用ポートを指定する必要がある（内部指針 5.2）。
    """
    remote_cmd = (
        "docker stats --no-stream "
        "--format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.BlockIO}}'"
    )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                # 初回接続のホスト鍵は自動で受け入れるが、既知ホストの鍵が変わった場合は拒否する
                # （`no`は鍵変更も無警告で受け入れ、中間者攻撃を検知できない）。BatchModeでも
                # 非対話で動く。
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(ssh_port),
                f"{ssh_user}@{ssh_host}",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        logger.warning(
            f"[MON] SSH経由のdocker stats取得に失敗 ({ssh_user}@{ssh_host}:{ssh_port}, "
            f"exit={result.returncode}): {result.stderr.strip()}"
        )
        return []

    return _parse_stats_lines(result.stdout, "|")


def _parse_stats_lines(output: str, delimiter: str) -> list[dict[str, str]]:
    """`docker stats` の区切り文字付き出力行をレコードへ変換する。"""
    records: list[dict[str, str]] = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split(delimiter)
        if len(parts) < 5:
            continue
        records.append({
            "container": parts[0],
            "cpu_percent": parts[1].rstrip("%"),
            "mem_usage": parts[2],
            "mem_percent": parts[3].rstrip("%"),
            "block_io": parts[4],
        })
    return records


def run_monitor(
    compose_file: str,
    output_path: str,
    interval: float = 1.0,
    duration: int = 0,
    ssh_host: str = "",
    ssh_user: str = "root",
    ssh_port: int = 22,
    project_name: str = "",
) -> None:
    """リソース使用状況を定期的に CSV に記録する。"""
    ensure_parent_dir(output_path)

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "container", "cpu_percent",
            "mem_usage", "mem_percent", "block_io",
        ])

        elapsed: float = 0
        mode = f"ssh://{ssh_user}@{ssh_host}:{ssh_port}" if ssh_host else f"local:{compose_file}"
        logger.info(f"[MON] Recording to {output_path} (interval={interval}s, mode={mode})")

        while not _shutdown:
            if duration > 0 and elapsed >= duration:
                break

            if ssh_host:
                records = collect_docker_stats_ssh(ssh_host, ssh_user, ssh_port)
            else:
                records = collect_docker_stats(compose_file, project_name)
                if not records:
                    records = collect_system_stats()

            now = datetime.now(UTC).isoformat()

            for rec in records:
                writer.writerow([
                    now,
                    rec["container"],
                    rec["cpu_percent"],
                    rec["mem_usage"],
                    rec["mem_percent"],
                    rec["block_io"],
                ])
            f.flush()

            time.sleep(interval)
            elapsed += interval

    logger.info(f"[MON] Stopped. Output: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="リソースモニター",
    )
    parser.add_argument("--compose-file", required=True, help="docker-compose.yml のパス")
    parser.add_argument("--output", default="results/resource.csv", help="出力 CSV パス")
    parser.add_argument("--interval", type=float, default=1.0, help="記録間隔（秒）")
    parser.add_argument("--duration", type=int, default=0, help="記録時間（秒、0=無制限）")
    parser.add_argument("--ssh-host", default="", help="SSH接続先ホスト（省略時はローカルDocker）")
    parser.add_argument("--ssh-user", default="root", help="SSHユーザー名（デフォルト: root）")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSHポート（デフォルト: 22）")
    parser.add_argument(
        "--project-name", default="",
        help="docker composeのプロジェクト名を明示指定（省略時は既定のディレクトリ名ベース挙動）。"
        "同一ディレクトリに複数のcompose fileがある構成では明示指定を推奨",
    )

    args = parser.parse_args()
    run_monitor(
        args.compose_file,
        args.output,
        args.interval,
        args.duration,
        args.ssh_host,
        args.ssh_user,
        args.ssh_port,
        args.project_name,
    )


if __name__ == "__main__":
    main()
