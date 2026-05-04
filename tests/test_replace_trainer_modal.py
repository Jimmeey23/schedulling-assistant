from pathlib import Path
import subprocess


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
