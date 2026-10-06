# self-proving-observation/
# └── tests/
#     └── test_spike.py

import uuid

from src.generator.spike import compute_rps_schedule, generate_event


class TestGenerateEvent:
    def test_returns_event_and_inject_id(self):
        event, inject_id = generate_event(0)
        assert isinstance(event, dict)
        assert isinstance(inject_id, str)

    def test_inject_id_is_uuid(self):
        _, inject_id = generate_event(0)
        uuid.UUID(inject_id)  # raises ValueError if not valid UUID

    def test_inject_id_matches_event(self):
        event, inject_id = generate_event(0)
        assert event["inject_id"] == inject_id

    def test_inject_id_unique_per_call(self):
        _, id1 = generate_event(0)
        _, id2 = generate_event(0)
        assert id1 != id2

    def test_event_has_required_fields(self):
        event, _ = generate_event(0)
        for field in ["timestamp", "event_type", "src_ip", "dest_ip", "inject_id"]:
            assert field in event

    def test_event_type_cycles(self):
        types = set()
        for i in range(20):
            event, _ = generate_event(i)
            types.add(event["event_type"])
        assert len(types) > 1


class TestComputeRpsSchedule:
    def test_flat_returns_constant(self):
        schedule = compute_rps_schedule(100, 10, "flat")
        assert len(schedule) == 10
        assert all(v == 100 for v in schedule)

    def test_spike_middle_higher(self):
        schedule = compute_rps_schedule(100, 60, "spike")
        mid = schedule[30]
        edge = schedule[0]
        assert mid > edge

    def test_spike_peak_is_10x(self):
        schedule = compute_rps_schedule(100, 60, "spike")
        assert max(schedule) == 100 * 10

    def test_ramp_monotonically_increases(self):
        schedule = compute_rps_schedule(100, 10, "ramp")
        for i in range(1, len(schedule)):
            assert schedule[i] >= schedule[i - 1]

    def test_wave_has_variation(self):
        schedule = compute_rps_schedule(100, 30, "wave")
        assert max(schedule) > min(schedule)

    def test_schedule_length_matches_duration(self):
        for duration in [10, 60, 120]:
            schedule = compute_rps_schedule(100, duration, "flat")
            assert len(schedule) == duration

    def test_unknown_pattern_defaults_to_base_rps(self):
        schedule = compute_rps_schedule(200, 5, "unknown_pattern")
        assert all(v == 200 for v in schedule)
