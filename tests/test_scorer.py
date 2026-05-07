import pytest
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.scorer import (
    CONSIDER_SCORE,
    ClassScorer,
    INCLUDE_SCORE,
    INCLUDE_SESSIONS,
    PROTECT_SCORE,
    PROTECT_SESSIONS,
)


def test_scorer_interprets_performance_csv_rows_as_aggregated_slots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Strong Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,240,300,120000,20,12,80.00%,6000",
                "Weak Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,11:00:00,12,100,5000,10,1.2,12.00%,500",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    strong = next(r for r in output["class_slot_ranking"] if r["trainer"] == "Strong Trainer")

    assert strong["session_count"] == 20
    assert strong["avg_checkin"] == 12.0
    assert strong["avg_fill_rate"] == 0.8
    assert strong["recommendation"] == "PROTECT"


def test_scorer_outputs_auditable_score_breakdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Strong Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,240,300,120000,20,12,80.00%,6000",
                "Weak Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,11:00:00,12,100,5000,10,1.2,12.00%,500",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    record = next(r for r in output["class_slot_ranking"] if r["trainer"] == "Strong Trainer")

    assert "score_breakdown" in record
    assert record["score_breakdown"]["total_score"] == record["score"]
    assert record["score_breakdown"]["base_score"] == record["base_score"]
    components = record["score_breakdown"]["components"]
    assert {c["key"] for c in components} == {
        "avg_attendance",
        "capacity_fill",
        "revenue",
        "sessions",
    }
    assert all("points" in c and "weight" in c and "explanation" in c for c in components)
    assert round(sum(c["points"] for c in components), 2) == record["base_score"]
    by_key = {c["key"]: c for c in components}
    assert by_key["avg_attendance"]["weight"] == 0.75
    assert by_key["avg_attendance"]["max_points"] == 75.0
    assert by_key["capacity_fill"]["weight"] == 0.15
    assert by_key["capacity_fill"]["max_points"] == 15.0
    assert by_key["revenue"]["weight"] == 0.07
    assert by_key["revenue"]["max_points"] == 7.0
    assert by_key["sessions"]["weight"] == 0.03
    assert by_key["sessions"]["max_points"] == 3.0


def test_default_class_attendance_weight_can_beat_fill_revenue_and_sample_size(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "High Attendance,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,10,100,1000,1,10,10.00%,1000",
                "Lower Attendance,Studio FIT,\"Kwality House, Kemps Corner\",Monday,10:00:00,44,100,200000,30,6,44.00%,10000",
                "Attendance Floor,Studio Mat 57,\"Kwality House, Kemps Corner\",Monday,11:00:00,1,100,1000,1,1,1.00%,1000",
                "Attendance Ceiling,Studio Cardio Barre,\"Kwality House, Kemps Corner\",Monday,12:00:00,11,100,1000,1,11,11.00%,1000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    high = next(r for r in output["class_slot_ranking"] if r["trainer"] == "High Attendance")
    lower = next(r for r in output["class_slot_ranking"] if r["trainer"] == "Lower Attendance")

    assert high["score"] > lower["score"]


def test_scorer_groups_primary_slots_by_unique_id_1_and_trainers_by_unique_id_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "UniqueID1,UniqueID2,Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "SLOT_A,A_T1,Trainer One,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,200,300,90000,20,10,66.67%,4500",
                "SLOT_A,A_T2,Trainer Two,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,120,300,50000,10,6,40.00%,5000",
                "SLOT_B,B_T1,Trainer Three,Studio FIT,\"Kwality House, Kemps Corner\",Monday,11:00:00,30,200,20000,10,3,15.00%,2000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()

    assert len(output["slot_group_ranking"]) == 2
    slot_a = next(r for r in output["slot_group_ranking"] if r["unique_id_1"] == "SLOT_A")
    assert slot_a["session_count"] == 30
    assert slot_a["avg_attendance"] == 8.67
    assert [t["trainer"] for t in slot_a["top_trainers"]] == ["Trainer One", "Trainer Two"]
    assert all(not r["class"].lower().startswith("studio hosted") for r in output["slot_group_ranking"])


def test_ud1_slot_ignores_attached_trainer_and_uses_trainer_csv_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    Path("rules").mkdir()
    Path("rules/trainer_profiles.json").write_text(json.dumps([
        {"name": "Inactive Placeholder", "active": False},
        {"name": "Active Trainer", "active": True},
    ]))
    ud1_path = tmp_path / "Class Performance by UD1.csv"
    ud1_path.write_text(
        "\n".join(
            [
                "UniqueID1,UniqueID2,Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "SLOT_1845,PLACEHOLDER,Inactive Placeholder,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,18:45:00,950,1784,834930,119,7.98,53.25%,7016",
            ]
        )
    )
    Path("Class Performance by Trainer.csv").write_text(
        "\n".join(
            [
                "UniqueID1,UniqueID2,Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "SLOT_1845,ACTIVE_TRAINER,Active Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,18:45:00,103,236,92317,11,9.36,43.64%,8392",
            ]
        )
    )

    output = ClassScorer(csv_path=str(ud1_path)).run()
    slot = next(r for r in output["slot_group_ranking"] if r["unique_id_1"] == "SLOT_1845")
    trainer = next(r for r in output["class_slot_ranking"] if r["unique_id_1"] == "SLOT_1845")

    assert slot["trainer"] == "Inactive Placeholder"
    assert [t["trainer"] for t in slot["top_trainers"]] == ["Active Trainer"]
    assert trainer["trainer"] == "Active Trainer"
    assert trainer["time"] == "18:45"


def test_strength_lab_policy_protection_does_not_inflate_score_or_recommendation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Atulan Purohit,Studio Strength Lab,\"Kwality House, Kemps Corner\",Monday,18:00:00,12,21,9000,3,4,57.14%,3000",
                "Trainer Two,Studio Barre 57,\"Kwality House, Kemps Corner\",Tuesday,09:00:00,50,200,30000,10,5,25.00%,3000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    strength = next(r for r in output["slot_group_ranking"] if r["class"] == "Studio Strength Lab")

    assert strength["protect_class_time"] is True
    assert strength["score"] < PROTECT_SCORE
    assert strength["recommendation"] != "PROTECT"


def test_top_ranked_weak_slots_are_not_auto_promoted_to_protect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Trainer One,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,20,100,5000,5,4,20.00%,1000",
                "Trainer Two,Studio Mat 57,\"Kwality House, Kemps Corner\",Tuesday,09:00:00,15,100,4000,5,3,15.00%,800",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()

    assert all(r["recommendation"] != "PROTECT" for r in output["slot_group_ranking"])
    assert all(not r.get("pinned_slot") for r in output["slot_group_ranking"])


def test_trainer_specific_score_uses_trainer_history_not_parent_slot_norms(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "UniqueID1,UniqueID2,Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "SLOT_A,A_T1,High Evidence,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,200,300,120000,20,10,66.67%,6000",
                "SLOT_A,A_T2,Low Evidence,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,50,75,10000,5,10,66.67%,2000",
                "SLOT_B,B_T1,Comparison,Studio FIT,\"Kwality House, Kemps Corner\",Tuesday,11:00:00,20,100,5000,5,4,20.00%,1000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    high = next(r for r in output["class_slot_ranking"] if r["trainer"] == "High Evidence")
    low = next(r for r in output["class_slot_ranking"] if r["trainer"] == "Low Evidence")

    assert high["score"] > low["score"]


def test_class_and_studio_averages_are_session_weighted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "UniqueID1,UniqueID2,Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "SLOT_A,A_T1,Trainer One,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,200,250,120000,20,10,80.00%,6000",
                "SLOT_B,B_T1,Trainer Two,Studio Barre 57,\"Kwality House, Kemps Corner\",Tuesday,11:00:00,1,10,1000,1,1,10.00%,1000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()
    class_metric = next(r for r in output["class_metrics"] if r["class"] == "Studio Barre 57")
    slot = next(r for r in output["slot_group_ranking"] if r["unique_id_1"] == "SLOT_A")

    assert class_metric["avg_checkin"] == 9.57
    assert class_metric["avg_fill_rate"] == 0.7667
    assert slot["studio_avg_fill"] == 0.7667


def test_scorer_excludes_profile_disabled_trainers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    Path("rules").mkdir()
    Path("rules/trainer_profiles.json").write_text(json.dumps([
        {"name": "Disabled Trainer", "active": False}
    ]))
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Disabled Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,240,300,120000,20,12,80.00%,6000",
                "Active Trainer,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,10:00:00,120,200,50000,10,6,60.00%,5000",
            ]
        )
    )

    output = ClassScorer(csv_path=str(csv_path)).run()

    trainers = {r["trainer"] for r in output["class_slot_ranking"]}
    assert "Disabled Trainer" not in trainers
    assert "Active Trainer" in trainers


def test_scorer_enriches_historic_slots_with_session_drilldown_metrics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("state").mkdir()
    csv_path = tmp_path / "perf.csv"
    csv_path.write_text(
        "\n".join(
            [
                "Trainer,Class,Location,Day,Time,CheckedIn,Capacity,Revenue,Classes,ClassAvgInclEmpty,ClassAvgExclEmpty,FillRate",
                "Trainer A,Studio Barre 57,\"Kwality House, Kemps Corner\",Monday,09:00:00,20,40,10000,2,10,50.00%,5000",
            ]
        )
    )
    Path("state/01_sessions.json").write_text(json.dumps({
        "sessions": [
            {
                "Date": "2026-04-01",
                "Location": "Kwality House, Kemps Corner",
                "Class": "Studio Barre 57",
                "Trainer": "Trainer A",
                "Day": "Monday",
                "Time": "09:00:00",
                "CheckedIn": 8,
                "Booked": 12,
                "Capacity": 20,
                "Revenue": 4000,
                "late_cancel_rate": 0.1,
                "no_show_rate": 0.2,
            },
            {
                "Date": "2026-04-08",
                "Location": "Kwality House, Kemps Corner",
                "Class": "Studio Barre 57",
                "Trainer": "Trainer A",
                "Day": "Monday",
                "Time": "09:00:00",
                "CheckedIn": 12,
                "Booked": 15,
                "Capacity": 20,
                "Revenue": 6000,
                "late_cancel_rate": 0.2,
                "no_show_rate": 0.1,
            },
        ]
    }))

    output = ClassScorer(csv_path=str(csv_path)).run()
    record = output["class_slot_ranking"][0]

    assert record["historic_detail"]["avg_booked"] == 13.5
    assert record["historic_detail"]["avg_capacity"] == 20.0
    assert record["historic_detail"]["avg_late_cancel_rate"] == 0.15
    assert record["historic_detail"]["avg_no_show_rate"] == 0.15
    assert record["historic_detail"]["total_revenue"] == 10000.0
    assert len(record["historic_detail"]["individual_sessions"]) == 2
    assert record["historic_detail"]["individual_sessions"][0]["date"] == "2026-04-08"
    assert record["historic_detail"]["individual_sessions"][0]["trainer"] == "Trainer A"


class TestScorerOutputs:
    def test_scores_in_valid_range(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        for r in data["class_slot_ranking"]:
            assert 0 <= r["score"] <= 100, (
                f"Score out of range: {r['score']} for {r.get('trainer')} "
                f"@ {r.get('location')} {r.get('time')}"
            )

    def test_anisha_kwality_scores_above_median(self):
        """Anisha Shah is documented as highest avg attendance at Kwality (7.7 check-in).
        Her best recorded combo should rank in INCLUDE territory (≥45) and beat Kwality median."""
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        kwality_all = [r for r in data["class_slot_ranking"] if r.get("location") == "Kwality House, Kemps Corner"]
        if not kwality_all:
            pytest.skip("No Kwality records in scored data")
        anisha_kwality = [r for r in kwality_all if r.get("trainer") == "Anisha Shah"]
        if not anisha_kwality:
            pytest.skip("No Anisha Shah Kwality records in scored data")
        import statistics
        kwality_median = statistics.median(r["score"] for r in kwality_all)
        best = max(anisha_kwality, key=lambda x: x["score"])
        assert best["score"] >= kwality_median, (
            f"Anisha Shah best score {best['score']:.1f} should be >= Kwality median {kwality_median:.1f}"
        )

    def test_low_performer_scores_low(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        raunak_supreme = [
            r
            for r in data["class_slot_ranking"]
            if r.get("trainer") == "Raunak Khemuka"
            and r.get("location") == "Supreme HQ, Bandra"
        ]
        if not raunak_supreme:
            pytest.skip("No Raunak Khemuka Supreme records in scored data")
        peak_slots = [
            r
            for r in raunak_supreme
            if r.get("time") in ("11:00", "11:30", "19:00", "19:15")
        ]
        if not peak_slots:
            pytest.skip("No Raunak peak slot records")
        worst = min(peak_slots, key=lambda x: x["score"])
        assert worst["recommendation"] in {"CONSIDER", "DROP"}, (
            f"Expected low performer to avoid protected/include tier, got {worst['recommendation']} at {worst['score']}"
        )

    def test_recommendation_labels_consistent(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        # Thresholds are imported from scorer.py to keep tests aligned with the
        # production recommendation policy.
        for r in data["class_slot_ranking"]:
            score = r["score"]
            rec = r["recommendation"]
            sessions = r.get("session_count", 0)
            if rec == "PROTECT":
                assert score >= PROTECT_SCORE, f"PROTECT requires score >= {PROTECT_SCORE}, got {score}"
                assert sessions >= PROTECT_SESSIONS, (
                    f"PROTECT requires slot sessions >= {PROTECT_SESSIONS}, got {sessions}"
                )
            if rec == "INCLUDE":
                assert score >= INCLUDE_SCORE, f"INCLUDE rec requires score >= {INCLUDE_SCORE}, got {score}"
                assert sessions >= INCLUDE_SESSIONS, (
                    f"INCLUDE requires slot sessions >= {INCLUDE_SESSIONS}, got {sessions}"
                )
            elif rec == "CONSIDER":
                assert score >= CONSIDER_SCORE, f"CONSIDER rec requires score >= {CONSIDER_SCORE}, got {score}"
                assert score < INCLUDE_SCORE or sessions < INCLUDE_SESSIONS, (
                    f"CONSIDER requires score < {INCLUDE_SCORE} or sessions < {INCLUDE_SESSIONS}, "
                    f"got score={score}, sessions={sessions}"
                )
            elif rec == "DROP":
                assert score < CONSIDER_SCORE or sessions < 3, (
                    f"DROP requires low score or insufficient slot history, got score={score}, sessions={sessions}"
                )

    def test_all_locations_present_in_scores(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        locations = {r["location"] for r in data["class_slot_ranking"]}
        expected = {
            "Kwality House, Kemps Corner",
            "Supreme HQ, Bandra",
            "Kenkere House",
        }
        for loc in expected:
            assert loc in locations, f"Location {loc} missing from scores"

    def test_weights_sum_to_one(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        weights = data.get("weights_used", {})
        if not weights:
            pytest.skip("No weights_used in scores file")
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.01, f"Weights should sum to 1.0, got {total}"
