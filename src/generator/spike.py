# self-proving-observation/
# └── src/
#     └── generator/
#         └── spike.py  — Suricata eve.json 形式のバースト攻撃ジェネレーター

import argparse
import csv
import json
import math
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# `python3 src/generator/spike.py` のように直接パス実行された場合、
# プロジェクトルートが sys.path に乗らず `from src...` の import が失敗する。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.logging_config import get_logger

logger = get_logger(__name__)

# GeoIP解決可能なグローバルIPレンジ（プライベート・予約済みを除外）
_PUBLIC_IP_RANGES = [
    (0x01000000, 0x02FFFFFF),  # 1.0.0.0 - 2.255.255.255
    (0x05000000, 0x09FFFFFF),  # 5.0.0.0 - 9.255.255.255
    (0x0B000000, 0x0CFFFFFF),  # 11.0.0.0 - 12.255.255.255
    (0x0E000000, 0x63FFFFFF),  # 14.0.0.0 - 99.255.255.255
    (0x65000000, 0x7EFFFFFF),  # 101.0.0.0 - 126.255.255.255
    (0x80000000, 0x9FFFFFFF),  # 128.0.0.0 - 159.255.255.255
    (0xA2000000, 0xA9FEFFFF),  # 162.0.0.0 - 169.254.255.255
    (0xAB000000, 0xABFFFFFF),  # 171.0.0.0 - 171.255.255.255
    (0xB0000000, 0xBFFFFFFF),  # 176.0.0.0 - 191.255.255.255
    (0xC1000000, 0xC5FFFFFF),  # 193.0.0.0 - 197.255.255.255
    (0xC8000000, 0xDFFFFFFF),  # 200.0.0.0 - 223.255.255.255
]
_TOTAL_PUBLIC = sum(hi - lo + 1 for lo, hi in _PUBLIC_IP_RANGES)


def _random_public_ip(seed: int) -> str:
    """シード値から再現性のあるグローバルIPを生成する。"""
    n = seed % _TOTAL_PUBLIC
    for lo, hi in _PUBLIC_IP_RANGES:
        span = hi - lo + 1
        if n < span:
            ip_int = lo + n
            a = (ip_int >> 24) & 0xFF
            b = (ip_int >> 16) & 0xFF
            c = (ip_int >> 8) & 0xFF
            d = ip_int & 0xFF
            return f"{a}.{b}.{c}.{d}"
        n -= span
    return "1.1.1.1"

EVENT_TYPES = [
    "alert", "dns", "http", "tls", "flow",
    "fileinfo", "ssh", "smtp", "anomaly",
]

ALERT_SIGNATURES = [
    "ET MALWARE Win32/Emotet Activity",
    "ET EXPLOIT Possible CVE-2021-44228 Log4j RCE",
    "ET SCAN Nmap SYN Scan",
    "ET TROJAN Cobalt Strike Beacon",
    "ET POLICY PE EXE Download",
    "ET EXPLOIT Apache Struts RCE",
    "ET MALWARE Trickbot CnC Beacon",
    "ET SCAN Aggressive SSH Brute Force",
]


def generate_event(sequence_num: int, sensor_pool_size: int = 0) -> tuple[dict[str, Any], str]:
    """Suricata eve.json 形式の1イベントを生成する。

    sensor_pool_size: 0（既定）なら従来通りsequence_numごとにほぼユニークな
    送信元IPを生成する（対照実験の欠損率計測はinject_id単位の突合のため、
    送信元IPの多様性自体は結果に影響しない）。0より大きい場合は送信元IPを
    その個数の小さなプールに限定し、同一(sensor_id, event_type)の組が
    短時間に繰り返し出現するようにする（doc/pipeline-spec.md「補強実験:
    集約ウィンドウのアブレーション実験」で、Vectorのreduce transformが
    実際に複数イベントを1レコードへ集約する場面を検証するために必要）。
    """
    inject_id = str(uuid.uuid4())
    event_type = EVENT_TYPES[sequence_num % len(EVENT_TYPES)]
    ip_seed = sequence_num % sensor_pool_size if sensor_pool_size > 0 else sequence_num

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "src_ip": _random_public_ip(ip_seed),
        "src_port": 1024 + (sequence_num % 64000),
        "dest_ip": "192.168.1.1",
        "dest_port": [80, 443, 22, 25, 53][sequence_num % 5],
        "proto": "TCP",
        "inject_id": inject_id,
    }

    if event_type == "alert":
        event["alert"] = {
            "signature": ALERT_SIGNATURES[sequence_num % len(ALERT_SIGNATURES)],
            "severity": (sequence_num % 4) + 1,
            "category": "Malware",
        }
    else:
        event["severity"] = (sequence_num % 4) + 1

    return event, inject_id


def compute_rps_schedule(base_rps: int, duration: int, pattern: str) -> list[int]:
    """各秒の目標 RPS を計算する。"""
    schedule = []
    for t in range(duration):
        if pattern == "flat":
            schedule.append(base_rps)
        elif pattern == "spike":
            if duration // 3 <= t < 2 * duration // 3:
                schedule.append(base_rps * 10)
            else:
                schedule.append(base_rps)
        elif pattern == "wave":
            multiplier = 1 + 4 * abs(math.sin(2 * math.pi * t / max(duration // 3, 1)))
            schedule.append(int(base_rps * multiplier))
        elif pattern == "ramp":
            multiplier = 1 + (9 * t / max(duration - 1, 1))
            schedule.append(int(base_rps * multiplier))
        else:
            schedule.append(base_rps)
    return schedule


def send_batch(target: str, events: list[dict[str, Any]]) -> bool:
    """イベントバッチを HTTP POST で送信する。"""
    body = json.dumps(events).encode("utf-8")
    req = Request(
        target,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=5) as resp:  # nosec B310
            resp.read()
        return True
    except (URLError, OSError, HTTPException):
        return False


def run_generator(
    targets: list[str],
    base_rps: int,
    duration: int,
    pattern: str,
    output_path: str,
) -> None:
    """ジェネレーターのメインループ。"""
    schedule = compute_rps_schedule(base_rps, duration, pattern)

    logger.info(f"[GEN] Targets: {targets}")
    logger.info(f"[GEN] Pattern: {pattern}  Base RPS: {base_rps}  Duration: {duration}s")
    logger.info(f"[GEN] Total planned events: {sum(schedule)}")

    total_sent = 0
    total_failed = 0
    seq = 0

    batch_size = 100
    with open(output_path, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["inject_id", "timestamp", "event_type", "target", "sent_ok"])

        with ThreadPoolExecutor(max_workers=len(targets) * 2) as executor:
            for second, rps in enumerate(schedule):
                t_start = time.monotonic()
                events_this_second = []
                ids_this_second = []

                for _ in range(rps):
                    event, inject_id = generate_event(seq)
                    events_this_second.append(event)
                    ids_this_second.append((inject_id, event["timestamp"], event["event_type"]))
                    seq += 1

                n_batches = (
                    math.ceil(len(events_this_second) / batch_size) if events_this_second else 0
                )

                futures = []
                for target in targets:
                    for i in range(0, len(events_this_second), batch_size):
                        batch = events_this_second[i : i + batch_size]
                        futures.append(
                            executor.submit(send_batch, target, batch)
                        )

                results = [f.result() for f in futures]
                ok_count = sum(1 for r in results if r)

                # ターゲット×バッチの成否をイベント単位に展開
                target_event_ok: list[list[bool]] = []
                for t_idx in range(len(targets)):
                    batch_results = results[t_idx * n_batches : (t_idx + 1) * n_batches]
                    event_ok: list[bool] = []
                    for b_idx, ok in enumerate(batch_results):
                        start = b_idx * batch_size
                        end = min(start + batch_size, len(events_this_second))
                        event_ok.extend([ok] * (end - start))
                    target_event_ok.append(event_ok)

                for e_idx, (inject_id, ts, etype) in enumerate(ids_this_second):
                    for t_idx, target in enumerate(targets):
                        sent = "1" if target_event_ok[t_idx][e_idx] else "0"
                        writer.writerow([inject_id, ts, etype, urlparse(target).netloc, sent])

                total_sent += rps * len(targets)
                total_failed += (len(results) - ok_count)

                elapsed = time.monotonic() - t_start
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)

                if (second + 1) % 10 == 0 or second == 0:
                    logger.info(
                        f"[GEN] t={second + 1:>4d}s  rps={rps:>6d}  "
                        f"sent={total_sent:>8d}  failed_batches={total_failed}"
                    )

    logger.info(f"[GEN] Done. Total events sent: {total_sent}  Log: {output_path}")


def run_generator_file(
    base_rps: int,
    duration: int,
    pattern: str,
    log_path: str,
    output_path: str,
    sensor_pool_size: int = 0,
) -> None:
    """生成イベントをHTTP送信せず、Suricata eve.json互換のNDJSONとしてファイルへ
    追記する。doc/pipeline-spec.md「補強実験: 集約ウィンドウのアブレーション実験」向けに
    Vectorのfileソースへ直接投入するための経路で、既存のHTTP送信経路（run_generator）
    とは独立しており、そちらのロジック・副作用には一切触れない。

    sensor_pool_size: generate_event()と同じ意味（0=従来通りほぼユニーク）。
    """
    schedule = compute_rps_schedule(base_rps, duration, pattern)

    logger.info(f"[GEN-FILE] Pattern: {pattern}  Base RPS: {base_rps}  Duration: {duration}s")
    logger.info(f"[GEN-FILE] Total planned events: {sum(schedule)}")

    total_written = 0
    seq = 0
    with open(log_path, "a") as log_file, open(output_path, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["inject_id", "timestamp", "event_type"])

        for second, rps in enumerate(schedule):
            t_start = time.monotonic()
            for _ in range(rps):
                event, inject_id = generate_event(seq, sensor_pool_size)
                log_file.write(json.dumps(event) + "\n")
                writer.writerow([inject_id, event["timestamp"], event["event_type"]])
                seq += 1
            log_file.flush()

            total_written += rps
            elapsed = time.monotonic() - t_start
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            if (second + 1) % 10 == 0 or second == 0:
                logger.info(
                    f"[GEN-FILE] t={second + 1:>4d}s  rps={rps:>6d}  written={total_written:>8d}"
                )

    logger.info(f"[GEN-FILE] Done. Total events written: {total_written}  Log: {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="攻撃スパイクジェネレーター",
    )
    parser.add_argument(
        "--targets", nargs="+", required=False, default=[],
        help="送信先 URL（例: http://host1:5080 http://host2:8000/events）。--file-output指定時は不要",
    )
    parser.add_argument("--rps", type=int, default=100, help="ベース秒間リクエスト数")
    parser.add_argument("--duration", type=int, default=60, help="持続秒数")
    parser.add_argument(
        "--pattern", choices=["flat", "spike", "wave", "ramp"], default="spike",
        help="負荷パターン",
    )
    parser.add_argument("--output", default="results/inject_log.csv", help="送信ログ出力先")
    parser.add_argument(
        "--file-output", default="",
        help="指定時はHTTP送信せず、Suricata eve.json互換NDJSONとしてこのパスへ追記する"
        "（doc/pipeline-spec.md「補強実験: 集約ウィンドウのアブレーション実験」向け）",
    )
    parser.add_argument(
        "--sensor-pool-size", type=int, default=0,
        help="--file-output指定時のみ有効。0（既定）ならほぼユニークな送信元IP、"
        "N>0なら送信元IPをN個のプールに限定し同一(sensor_id,event_type)の"
        "繰り返し出現を作る（Vectorのreduce集約を実際に検証するために必要）",
    )

    args = parser.parse_args()
    if args.file_output:
        run_generator_file(
            args.rps, args.duration, args.pattern, args.file_output, args.output,
            args.sensor_pool_size,
        )
    else:
        if not args.targets:
            parser.error("--targets は --file-output を指定しない場合は必須です")
        run_generator(args.targets, args.rps, args.duration, args.pattern, args.output)


if __name__ == "__main__":
    main()
