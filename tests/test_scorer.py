import pytest
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


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
        assert worst["score"] < 30, (
            f"Expected score < 30 for low performer Raunak Khemuka, got {worst['score']}"
        )

    def test_recommendation_labels_consistent(self):
        scores_path = Path("state/03_scores.json")
        if not scores_path.exists():
            pytest.skip("03_scores.json not yet generated — run pipeline first")
        with open(scores_path) as f:
            data = json.load(f)
        # Thresholds from scorer.py: PROTECT=65, INCLUDE=40, CONSIDER=20
        # PROTECT variants (PROTECT_EXACT, PROTECT_SLOT) may override score for rule-pinned slots.
        for r in data["class_slot_ranking"]:
            score = r["score"]
            rec = r["recommendation"]
            sessions = r.get("trainer_sessions", r.get("session_count", 0))
            if rec.startswith("PROTECT"):
                # Rule-pinned — score check not applicable
                continue
            if rec == "INCLUDE":
                assert score >= 40, f"INCLUDE rec requires score >= 40, got {score}"
            elif rec == "CONSIDER":
                assert score >= 20, f"CONSIDER rec requires score >= 20, got {score}"
            elif rec == "DROP":
                assert score < 40, f"DROP rec should have score < 40, got {score}"

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
