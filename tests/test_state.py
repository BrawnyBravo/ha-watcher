import json

from ha_watcher.state import empty_state, load_state, save_state


def test_round_trip(tmp_path):
    p = tmp_path / "s.json"
    st = empty_state()
    st["suppressed"] = 3
    save_state(p, st)
    assert load_state(p) == st


def test_missing_sections_are_filled_from_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 1, "checks": {}, "power": {"cycles": []}}), encoding="utf-8")
    st = load_state(p)
    assert st["phone"]["calls"] == [] and st["power"]["busy_seen"] is None
    assert st["alert_times"] == []


def test_wrong_types_are_reset_not_fatal(tmp_path):
    # A hand-edited or half-migrated file must not crash every loop forever.
    p = tmp_path / "s.json"
    bad = {
        "version": 1,
        "checks": [],
        "alert_times": None,
        "suppressed": "lots",
        "power": {"cycles": None},
        "recovery": "nope",
        "last_run": {"api": 5},
    }
    p.write_text(json.dumps(bad), encoding="utf-8")
    st = load_state(p)
    assert st["checks"] == {} and st["alert_times"] == [] and st["suppressed"] == 0
    assert st["power"]["cycles"] == [] and st["recovery"]["runs"] == []
    assert st["last_run"] == {}


def test_bad_timestamps_and_malformed_check_entries_are_dropped(tmp_path):
    p = tmp_path / "s.json"
    good = {
        "check": "api",
        "ok": False,
        "failures": 4,
        "alerting": True,
        "first_failure": "2026-01-15T11:00:00+00:00",
        "last_alert": "2026-01-15T11:02:00+00:00",
        "message": "API unreachable",
    }
    st = empty_state()
    st["checks"] = {"api": good, "core": {"check": "core"}, "x": "y"}
    st["alert_times"] = ["2026-01-15T11:02:00+00:00", "yesterday", "2026-01-15T11:02:00"]
    st["power"]["cycles"] = ["not a date"]
    st["power"]["busy_seen"] = "2026-01-15T11:00:00"  # naive: would break every comparison
    p.write_text(json.dumps(st), encoding="utf-8")
    out = load_state(p)
    assert out["checks"] == {"api": good}
    assert out["alert_times"] == ["2026-01-15T11:02:00+00:00"]
    assert out["power"]["cycles"] == [] and out["power"]["busy_seen"] is None


def test_future_version_starts_fresh(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 99, "checks": {"a": 1}}), encoding="utf-8")
    assert load_state(p) == empty_state()
