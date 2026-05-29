from html import escape, unescape
from re import sub

from ...core.config_manager import Config

UI_THEME_WZML = "WZML"
UI_THEME_JINMUPS = "JINMUPS"
VALID_TELEGRAM_UI_THEMES = (UI_THEME_WZML, UI_THEME_JINMUPS)


def get_telegram_ui_theme():
    theme = getattr(Config, "TELEGRAM_UI_THEME", UI_THEME_WZML)
    return theme if theme in VALID_TELEGRAM_UI_THEMES else UI_THEME_WZML


def use_jinmups_ui():
    return get_telegram_ui_theme() == UI_THEME_JINMUPS


def html_escape(value):
    return escape(str(value), quote=False)


def plain_text(value):
    return unescape(sub(r"<[^>]+>", "", str(value)))


def blockquote(text):
    return f"<blockquote>{html_escape(plain_text(text))}</blockquote>"


def format_jinmups_progress(progress):
    pct = float(str(progress).strip("%") or 0)
    pct = min(max(pct, 0), 100)
    filled = int(pct // 8)
    return f"☾{'✦' * filled}{'✧' * (12 - filled)}☽"


def format_jinmups_command(commands, description):
    if isinstance(commands, list):
        cmd_text = " or ".join(f"/{cmd}" for cmd in commands)
    else:
        cmd_text = f"/{commands}"
    return f"➭ {cmd_text}: {description}"
