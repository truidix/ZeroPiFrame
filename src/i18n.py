#!/usr/bin/env python3
"""
Photoframe i18n (internationalization) helper.

Translations are plain flat JSON files under translations/, one file per
language (e.g. translations/en.json, translations/de.json). The set of
available languages is discovered dynamically by scanning that folder -
dropping in a new translations/<code>.json file is enough to make a new
language selectable in the web UI, no code changes required.

Each JSON file maps a dotted string key (e.g. "status.sync_label") to the
translated string for that language. English (en.json) is treated as the
canonical/reference language and is always used as the fallback for keys
missing in another language, so a partially-translated language file never
results in a blank label - it just falls back to English (or, if even
English is missing the key, to the raw key itself, which makes a missing
translation obvious/debuggable rather than silently blank).
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

TRANSLATIONS_DIR = Path(__file__).parent / 'translations'
DEFAULT_LANGUAGE = 'en'

# Simple in-memory cache: {lang_code: {key: value}}. Translation files are
# tiny (a few KB) and rarely change at runtime, so this is refreshed lazily
# based on file mtime rather than reloading on every single request.
_cache: dict[str, dict] = {}
_cache_mtime: dict[str, float] = {}


def available_languages() -> list[str]:
    """Returns the list of language codes for which a translations/<code>.json
    file exists, sorted alphabetically. Falls back to just the default
    language if the translations folder is missing or empty, so the app
    never ends up with zero selectable languages."""
    if not TRANSLATIONS_DIR.is_dir():
        return [DEFAULT_LANGUAGE]
    langs = sorted(p.stem for p in TRANSLATIONS_DIR.glob('*.json') if p.stem)
    return langs or [DEFAULT_LANGUAGE]


def _load(lang: str) -> dict:
    path = TRANSLATIONS_DIR / f'{lang}.json'
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if lang in _cache and _cache_mtime.get(lang) == mtime:
        return _cache[lang]
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f'Could not read translation file {path}: {e}')
        data = {}
    _cache[lang] = data
    _cache_mtime[lang] = mtime
    return data


def normalize_language(lang: str | None) -> str:
    """Falls back to the default language if the requested one has no
    translation file available (e.g. a stale/typo'd value in config.yaml)."""
    if lang and lang in available_languages():
        return lang
    return DEFAULT_LANGUAGE


def translate(lang: str, key: str, **kwargs) -> str:
    """Looks up `key` in the given language, falling back to English, and
    finally to the raw key if it's missing everywhere (so a missing
    translation shows up as a visibly odd string in the UI instead of
    silently rendering blank - much easier to spot and file a bug for).
    Supports simple str.format()-style placeholders via kwargs.
    """
    value = _load(lang).get(key)
    if value is None and lang != DEFAULT_LANGUAGE:
        value = _load(DEFAULT_LANGUAGE).get(key)
    if value is None:
        value = key
    if kwargs:
        try:
            value = value.format(**kwargs)
        except Exception:
            pass
    return value


def make_translator(lang: str):
    """Returns a `t(key, **kwargs)` function bound to a specific language,
    for use as a Jinja global in templates (see webui.py's context
    processor) or directly in Python route code."""
    def t(key: str, **kwargs) -> str:
        return translate(lang, key, **kwargs)
    return t
