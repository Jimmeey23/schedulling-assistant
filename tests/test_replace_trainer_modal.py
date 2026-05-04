from pathlib import Path


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


def test_settings_frontend_does_not_expose_supabase_tab():
    source = TEMPLATE.read_text()

    assert "stab-supabase" not in source
    assert "ssec-supabase" not in source
    assert "Supabase Integration" not in source


def test_generated_index_has_no_unreplaced_template_tokens():
    source = INDEX.read_text()

    assert "/*INJECT_" not in source


def test_multi_location_columns_scale_to_selected_locations():
    source = TEMPLATE.read_text()

    assert "const mlLocColWidth=132" in source
    assert "const mlDayMinWidth=Math.max(mlLocColWidth,locs.length*mlLocColWidth)" in source
    assert 'min-width:${mlDayMinWidth}px' in source
