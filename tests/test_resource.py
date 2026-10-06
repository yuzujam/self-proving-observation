# self-proving-observation/
# └── tests/
#     └── test_resource.py

import sys
from unittest.mock import MagicMock, patch  # noqa: F401

from src.monitor.resource import (  # noqa: E501
    collect_docker_stats,
    collect_docker_stats_ssh,
    collect_system_stats,
)


class TestCollectDockerStats:
    def test_returns_empty_when_docker_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert collect_docker_stats("/fake/compose.yml") == []

    def test_returns_empty_when_no_containers(self):
        ps_result = MagicMock(returncode=0, stdout="")
        with patch("subprocess.run", return_value=ps_result):
            assert collect_docker_stats("/fake/compose.yml") == []

    def test_parses_stats_correctly(self):
        ps_result = MagicMock(returncode=0, stdout="abc123\n")
        stats_result = MagicMock(
            returncode=0,
            stdout="my-container\t1.5%\t100MiB / 2GiB\t5.00%\t0B / 0B\n",
        )
        with patch("subprocess.run", side_effect=[ps_result, stats_result]):
            records = collect_docker_stats("/fake/compose.yml")
        assert len(records) == 1
        assert records[0]["container"] == "my-container"
        assert records[0]["cpu_percent"] == "1.5"
        assert records[0]["mem_percent"] == "5.00"

    def test_returns_empty_when_stats_fails(self):
        ps_result = MagicMock(returncode=0, stdout="abc123\n")
        stats_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", side_effect=[ps_result, stats_result]):
            assert collect_docker_stats("/fake/compose.yml") == []


class TestCollectDockerStatsSsh:
    def test_returns_empty_when_ssh_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert collect_docker_stats_ssh("1.2.3.4", "root") == []

    def test_returns_empty_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=15)):
            assert collect_docker_stats_ssh("1.2.3.4", "root") == []

    def test_returns_empty_when_ssh_fails(self):
        result = MagicMock(returncode=255, stdout="")
        with patch("subprocess.run", return_value=result):
            assert collect_docker_stats_ssh("1.2.3.4", "root") == []

    def test_parses_pipe_delimited_output(self):
        ssh_output = "redis_1|0.5%|50MiB / 2GiB|2.50%|1kB / 500B\n"
        result = MagicMock(returncode=0, stdout=ssh_output)
        with patch("subprocess.run", return_value=result):
            records = collect_docker_stats_ssh("1.2.3.4", "root")
        assert len(records) == 1
        assert records[0]["container"] == "redis_1"
        assert records[0]["cpu_percent"] == "0.5"
        assert records[0]["mem_usage"] == "50MiB / 2GiB"
        assert records[0]["mem_percent"] == "2.50"
        assert records[0]["block_io"] == "1kB / 500B"

    def test_parses_multiple_containers(self):
        ssh_output = (
            "redis_1|0.5%|50MiB / 2GiB|2.50%|0B / 0B\n"
            "clickhouse_1|3.2%|800MiB / 2GiB|40.00%|100MB / 10MB\n"
            "receiver_1|0.1%|30MiB / 2GiB|1.50%|0B / 0B\n"
        )
        result = MagicMock(returncode=0, stdout=ssh_output)
        with patch("subprocess.run", return_value=result):
            records = collect_docker_stats_ssh("1.2.3.4", "root")
        assert len(records) == 3
        assert records[1]["container"] == "clickhouse_1"
        assert records[1]["cpu_percent"] == "3.2"

    def test_ssh_command_uses_correct_host_and_user(self):
        result = MagicMock(returncode=0, stdout="")
        with patch("subprocess.run", return_value=result) as mock_run:
            collect_docker_stats_ssh("192.168.1.10", "ubuntu")
        call_args = mock_run.call_args[0][0]
        assert "ubuntu@192.168.1.10" in call_args

    def test_skips_malformed_lines(self):
        ssh_output = "bad_line_without_pipes\nredis_1|0.5%|50MiB / 2GiB|2.50%|0B\n"
        result = MagicMock(returncode=0, stdout=ssh_output)
        with patch("subprocess.run", return_value=result):
            records = collect_docker_stats_ssh("1.2.3.4", "root")
        assert len(records) == 1
        assert records[0]["container"] == "redis_1"


class TestCollectSystemStats:
    def _make_mock_psutil(self, cpu=42.0, mem_used_mb=512, mem_total_mb=2048, mem_pct=25.0,
                          disk_read_mb=100, disk_write_mb=50, disk=True):
        mock = MagicMock()
        mock.cpu_percent.return_value = cpu
        mock.virtual_memory.return_value = MagicMock(
            used=mem_used_mb * 1024 ** 2, total=mem_total_mb * 1024 ** 2, percent=mem_pct,
        )
        if disk:
            mock.disk_io_counters.return_value = MagicMock(
                read_bytes=disk_read_mb * 1024 ** 2, write_bytes=disk_write_mb * 1024 ** 2,
            )
        else:
            mock.disk_io_counters.return_value = None
        return mock

    def test_returns_system_record_when_psutil_available(self):
        mock_psutil = self._make_mock_psutil()
        with patch("src.monitor.resource._PSUTIL_AVAILABLE", True), \
             patch.dict(sys.modules, {"psutil": mock_psutil}):
            records = collect_system_stats()
        assert len(records) == 1
        assert records[0]["container"] == "system"
        assert records[0]["cpu_percent"] == "42.0"
        assert records[0]["mem_percent"] == "25.0"

    def test_returns_empty_when_psutil_unavailable(self):
        with patch("src.monitor.resource._PSUTIL_AVAILABLE", False):
            assert collect_system_stats() == []

    def test_handles_no_disk_io(self):
        mock_psutil = self._make_mock_psutil(disk=False)
        with patch("src.monitor.resource._PSUTIL_AVAILABLE", True), \
             patch.dict(sys.modules, {"psutil": mock_psutil}):
            records = collect_system_stats()
        assert records[0]["block_io"] == "N/A"


class TestSshHostKeyChecking:
    """ssh経由のdocker stats取得は、既知ホストの鍵変更を無警告で受け入れない
    （StrictHostKeyChecking=no は中間者攻撃を検知できない）。"""

    def test_uses_accept_new_not_no(self):
        result = MagicMock(returncode=0, stdout="")
        with patch("subprocess.run", return_value=result) as run:
            collect_docker_stats_ssh("1.2.3.4", "root")

        cmd = run.call_args.args[0]
        options = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-o"]
        assert "StrictHostKeyChecking=accept-new" in options
        assert "StrictHostKeyChecking=no" not in options
        assert "BatchMode=yes" in options
