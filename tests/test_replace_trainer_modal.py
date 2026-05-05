from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "web" / "template.html"
INDEX = ROOT / "web" / "index.html"


def _replace_trainer_source() -> str:
    src = TEMPLATE.read_text()
    start = src.index("async function openReplaceTrainerModal")
    end = src.index("// ============================================================\n// SHOW SIMILAR MODAL", start)
    return src[start:end]


def test_replace_trainer_conflicts_use_every_location_not_active_filters():
    source = _replace_trainer_source()

    assert "getAllLocSlots()" in source
    assert "getActiveLocations().flatMap" not in source


def test_replace_trainer_conflicts_compare_normalized_trainer_names():
    source = _replace_trainer_source()

    assert "sameTrainerName(ls.trainer_1,t.trainer)" in source
    assert "ls.trainer_1!==t.trainer" not in source


def test_replace_trainer_uses_profiles_when_scheduled_pool_is_too_small():
    source = _replace_trainer_source()

    assert "(_settTrainerProfiles||[]).forEach(profile=>" in source


def test_replace_trainer_legacy_empty_qualifications_can_use_history():
    source = _replace_trainer_source()

    assert "hasExplicitQualifications(profile)" in source
    assert "hasImplicitClassHistory(name,cn)" in source


def test_replace_trainer_suggestions_have_one_click_apply_button():
    source = _replace_trainer_source()

    assert 'data-replace-payload' in source
    assert 'applyTrainerReplacement(JSON.parse' in source
    assert 'fetch("/api/replace-trainer"' in TEMPLATE.read_text()


def test_replace_trainer_endpoint_updates_schedule_data(tmp_path, monkeypatch):
    import app

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(
        """
{
  "locations": {
    "Studio A": [
      {
        "location": "Studio A",
        "date": "2026-05-04",
        "day_of_week": "Monday",
        "time": "09:00",
        "class_name": "Studio Mat 57",
        "room": "Studio 1",
        "trainer_1": "Old Trainer",
        "recommendation": "INCLUDE"
      }
    ]
  },
  "iterations": {}
}
""".strip()
    )
    monkeypatch.setattr(app, "WEB_DIR", web_dir)

    updated = app._replace_trainer_in_schedule(
        {
            "iteration": "Main",
            "slot": {
                "location": "Studio A",
                "date": "2026-05-04",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Mat 57",
                "room": "Studio 1",
                "trainer_1": "Old Trainer",
            },
            "new_trainer": "New Trainer",
        }
    )

    assert updated == 1
    assert '"trainer_1": "New Trainer"' in schedule_path.read_text()
    assert '"recommendation": "MANUAL"' in schedule_path.read_text()


def test_calendar_empty_slots_open_manual_add_modal():
    source = TEMPLATE.read_text()

    assert "openAddClassModal({location:_loc" in source
    assert 'fetch("/api/add-class"' in source
    assert "Historic options for this exact studio/day/time" in source
    assert 'id="manual-custom-class"' in source
    assert "customClass||cls" in source
    assert "historicSlotIntel(_loc,d,t)" in source
    assert "sg-empty-intel" in source


def test_add_class_endpoint_appends_manual_slot(tmp_path, monkeypatch):
    import app

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text('{"locations":{"Studio A":[]},"iterations":{}}')
    monkeypatch.setattr(app, "WEB_DIR", web_dir)
    monkeypatch.setattr(app, "_save_schedule_to_supabase", lambda data: False)
    monkeypatch.setattr(app, "_validate_manual_slot", lambda *args, **kwargs: None)

    result = app._add_class_to_schedule(
        {
            "iteration": "Main",
            "slot": {
                "location": "Studio A",
                "date": "2026-05-04",
                "day_of_week": "Monday",
                "time": "10:00",
                "class_name": "Studio Mat 57",
                "trainer_1": "Trainer A",
            },
        }
    )

    assert result["added"] == 1
    text = schedule_path.read_text()
    assert '"class_name": "Studio Mat 57"' in text
    assert '"recommendation": "MANUAL"' in text
    assert '"manual_added": true' in text


def test_calendar_classes_can_be_removed_and_dragged():
    source = TEMPLATE.read_text()

    assert "div.draggable=true" in source
    assert 'data-action="remove"' in source
    assert 'fetch("/api/remove-class"' in source
    assert 'fetch("/api/move-class"' in source


def test_move_class_endpoint_marks_manual_move(tmp_path, monkeypatch):
    import app

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(
        '{"locations":{"Studio A":[{"location":"Studio A","date":"2026-05-04","day_of_week":"Monday","time":"09:00","class_name":"Studio Mat 57","room":"Studio 1","trainer_1":"Trainer A","historical_avg_checkin":7.5,"historical_session_count":12}]},"iterations":{}}'
    )
    monkeypatch.setattr(app, "WEB_DIR", web_dir)
    monkeypatch.setattr(app, "_save_schedule_to_supabase", lambda data: False)
    monkeypatch.setattr(app, "_validate_manual_slot", lambda *args, **kwargs: None)

    result = app._move_class_in_schedule(
        {
            "iteration": "Main",
            "slot": {
                "location": "Studio A",
                "date": "2026-05-04",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Mat 57",
                "room": "Studio 1",
                "trainer_1": "Trainer A",
            },
            "target": {
                "location": "Studio A",
                "date": "2026-05-05",
                "day_of_week": "Tuesday",
                "time": "10:00",
            },
        }
    )

    assert result["moved"] == 1
    assert result["slot"]["historical_avg_checkin"] == 7.5
    text = schedule_path.read_text()
    assert '"day_of_week": "Tuesday"' in text
    assert '"time": "10:00"' in text
    assert '"manual_moved": true' in text


def test_manual_add_validates_trainer_availability(tmp_path, monkeypatch):
    import app

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text('{"locations":{"Studio A":[]},"iterations":{}}')
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(
        '[{"name":"Trainer A","active":true,"locations":{"Studio A":{"available_days":["Tuesday"],"time_window":{"start":"07:00","end":"12:00"},"max_classes_per_day":3}}}]'
    )
    monkeypatch.setattr(app, "WEB_DIR", web_dir)
    monkeypatch.setattr(app, "TRAINER_PROFILES_PATH", profiles_path)

    with pytest.raises(ValueError, match="not available"):
        app._add_class_to_schedule(
            {
                "iteration": "Main",
                "slot": {
                    "location": "Studio A",
                    "date": "2026-05-04",
                    "day_of_week": "Monday",
                    "time": "10:00",
                    "class_name": "Studio Mat 57",
                    "trainer_1": "Trainer A",
                },
            }
        )


def test_remove_class_endpoint_deletes_slot(tmp_path, monkeypatch):
    import app

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    schedule_path = web_dir / "schedule_data.json"
    schedule_path.write_text(
        '{"locations":{"Studio A":[{"location":"Studio A","date":"2026-05-04","day_of_week":"Monday","time":"09:00","class_name":"Studio Mat 57","room":"Studio 1","trainer_1":"Trainer A","historical_avg_checkin":7.5,"historical_session_count":12}]},"iterations":{}}'
    )
    monkeypatch.setattr(app, "WEB_DIR", web_dir)
    monkeypatch.setattr(app, "_save_schedule_to_supabase", lambda data: False)

    result = app._remove_class_from_schedule(
        {
            "iteration": "Main",
            "slot": {
                "location": "Studio A",
                "date": "2026-05-04",
                "day_of_week": "Monday",
                "time": "09:00",
                "class_name": "Studio Mat 57",
                "room": "Studio 1",
                "trainer_1": "Trainer A",
            },
        }
    )

    assert result["removed"] == 1
    assert "Studio Mat 57" not in schedule_path.read_text()


def test_settings_frontend_does_not_expose_supabase_tab():
    source = TEMPLATE.read_text()

    assert "stab-supabase" not in source
    assert "ssec-supabase" not in source
    assert "Supabase Integration" not in source


def test_settings_exposes_custom_rules_and_manual_pins():
    source = TEMPLATE.read_text()

    assert 'id="stab-customrules"' in source
    assert 'id="ssec-customrules"' in source
    assert "settRenderCustomRules" in source
    assert "settAddCustomRule" in source
    assert "settAddManualPin" in source
    assert "manual_protected" in source
    assert "custom_rules" in source


def test_generated_index_has_no_unreplaced_template_tokens():
    source = INDEX.read_text()

    assert "/*INJECT_" not in source


def test_generated_index_inline_script_is_valid_javascript():
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const fs=require('fs'),vm=require('vm');
const html=fs.readFileSync(process.argv[1],'utf8');
const scripts=[...html.matchAll(/<script[^>]*>([\\s\\S]*?)<\\/script>/gi)].map(m=>m[1]);
scripts.forEach((script,i)=>new vm.Script(script,{filename:`${process.argv[1]}#script${i}`}));
""",
            str(INDEX),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_multi_location_columns_scale_to_selected_locations():
    source = TEMPLATE.read_text()

    assert "const mlLocColWidth=132" in source
    assert "const mlDayMinWidth=Math.max(mlLocColWidth,locs.length*mlLocColWidth)" in source
    assert 'min-width:${mlDayMinWidth}px' in source


def test_karanvir_thumbnail_mapping_exists():
    source = TEMPLATE.read_text()

    assert '"karanvir":"/images/Karanvir.jpg"' in source
    assert (ROOT / "web" / "images" / "Karanvir.jpg").exists()


def test_drop_recommendation_is_not_labeled_manual():
    source = TEMPLATE.read_text()

    assert 'DROP:"Review"' in source
    assert 'if(r==="DROP"||r==="MANUAL")return"MANUAL"' not in source


def test_pipeline_status_polls_immediately_and_keeps_top_bar_visible():
    source = TEMPLATE.read_text()

    assert 'if(bar)bar.className="visible";' in source
    assert "tick();" in source
    assert "_pipelinePoller=setInterval(tick,2000)" in source


def test_generated_index_keeps_sleek_card_styles():
    source = INDEX.read_text()

    assert ".cc-hover-tools" in source
    assert ".cc-mini{min-height:78px" in source
    assert "cc-avatar-btn" in source


def test_analytics_class_mix_uses_canonical_format_counts():
    source = TEMPLATE.read_text()

    assert "function canonicalMixClass" in source
    assert "canonicalMixClass(s.class_name||\"Unknown\")" in source
    assert "slice(0,10)" not in source[source.index("function renderAnalytics"):source.index("// MODAL", source.index("function renderAnalytics"))]
