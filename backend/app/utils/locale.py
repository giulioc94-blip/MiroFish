import json
import os
import threading
from flask import request, has_request_context

_thread_local = threading.local()

_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

# Load language registry
with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
    _languages = json.load(f)

# Load translation files
_translations = {}
for filename in os.listdir(_locales_dir):
    if filename.endswith('.json') and filename != 'languages.json':
        locale_name = filename[:-5]
        with open(os.path.join(_locales_dir, filename), 'r', encoding='utf-8') as f:
            _translations[locale_name] = json.load(f)


def set_locale(locale: str):
    """Set locale for current thread. Call at the start of background threads."""
    _thread_local.locale = locale


def get_locale() -> str:
    if has_request_context():
        raw = request.headers.get('Accept-Language', 'zh')
        return raw if raw in _translations else 'zh'
    return getattr(_thread_local, 'locale', 'zh')


def t(key: str, **kwargs) -> str:
    locale = get_locale()
    messages = _translations.get(locale, _translations.get('zh', {}))

    value = messages
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        value = _translations.get('zh', {})
        for part in key.split('.'):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

    if value is None:
        return key

    if kwargs:
        for k, v in kwargs.items():
            value = value.replace(f'{{{k}}}', str(v))

    return value


def get_language_instruction() -> str:
    locale = get_locale()
    lang_config = _languages.get(locale, _languages.get('zh', {}))
    return lang_config.get('llmInstruction', '请使用中文回答。')


# World framing: optional text file (WORLD_BRIEF_PATH) describing a fictional
# world. When set, it is appended to the LLM system prompts so that every stage
# (ontology, profiles, config, report, interviews) treats the seed material as
# the world of a novel instead of a real-world news event.
_world_brief_cache = {}


def get_world_framing() -> str:
    path = os.environ.get('WORLD_BRIEF_PATH', '').strip()
    if not path:
        return ''
    if path not in _world_brief_cache:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                _world_brief_cache[path] = f.read().strip()
        except OSError:
            _world_brief_cache[path] = ''
    return _world_brief_cache[path]


def with_world_framing(system_prompt: str) -> str:
    """Append the fictional-world framing to a system prompt, if configured."""
    framing = get_world_framing()
    if not framing:
        return system_prompt
    return f"{system_prompt}\n\n{framing}"
