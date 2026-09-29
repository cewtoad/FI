"""UI i18n + feature-help completeness (web panel + config page)."""

from __future__ import annotations

import re

import config_schema
import config_ui
import webui

# ------------------------------------------------------------- schema bilingual

def test_every_setting_has_bilingual_label_and_help():
    for s in config_schema.SCHEMA:
        assert s.label, f"{s.key}: missing zh label"
        assert s.label_en, f"{s.key}: missing en label"
        assert s.help, f"{s.key}: missing zh help"
        assert s.help_en, f"{s.key}: missing en help"


def test_group_labels_bilingual_and_cover_schema():
    for s in config_schema.SCHEMA:
        assert s.group in config_schema.GROUP_LABELS, f"{s.key}: group {s.group} unlabelled"
    for g, (zh, en) in config_schema.GROUP_LABELS.items():
        assert zh and en, g


def test_settings_payload_exposes_bilingual_fields():
    payload = config_ui._settings_payload()
    for item in payload:
        assert "label_en" in item and "help_en" in item and "help" in item


# ------------------------------------------------------------- web i18n dict

def _i18n_block(lang: str) -> str:
    """Slice the zh/en object out of the PAGE's I18N dict."""
    text = webui.PAGE
    start = text.index(f"{lang}: {{")
    ends = [i for i in (text.find("\n  },", start), text.find("\n};", start))
            if i != -1]
    return text[start:min(ends)]


def _keys(block: str) -> set:
    return set(re.findall(r'"([a-zA-Z][a-zA-Z0-9_.]*)"\s*:', block))


def test_i18n_zh_and_en_have_same_keys():
    zh = _keys(_i18n_block("zh"))
    en = _keys(_i18n_block("en"))
    assert zh, "no zh keys parsed"
    assert zh == en, f"only in zh: {sorted(zh-en)}; only in en: {sorted(en-zh)}"


def test_all_data_i18n_attributes_are_defined():
    zh = _keys(_i18n_block("zh"))
    used = set()
    for attr in ("data-i18n", "data-i18n-ph", "data-i18n-html"):
        used |= set(re.findall(attr + r'="([^"]+)"', webui.PAGE))
    missing = used - zh
    assert not missing, f"used in HTML but not defined: {sorted(missing)}"


def test_web_has_language_toggle_and_apply():
    p = webui.PAGE
    assert 'id="langZh"' in p and 'id="langEn"' in p
    assert "function setLang" in p and "function applyI18n" in p
    assert "f1tr_lang" in p          # persisted choice


# ------------------------------------------------------------- audio devices

def test_audio_panel_has_selects_and_refresh():
    p = webui.PAGE
    assert 'id="setMic"' in p and 'id="setSpk"' in p
    assert 'id="audRefresh"' in p
    assert 'id="audNow"' in p
    assert "/api/audio" in p


# ------------------------------------------------------------- config page

def test_config_page_has_lang_toggle_and_groups():
    page = config_ui._page()
    assert 'id="zh"' in page and 'id="en"' in page
    assert "groups" in page          # group labels come from the payload
    assert "help" in page            # concrete help is rendered
