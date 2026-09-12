import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "omlx/admin/static/css/dashboard.css").read_text(encoding="utf-8")
CHAT = (ROOT / "omlx/admin/templates/chat.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "omlx/admin/templates/login.html").read_text(encoding="utf-8")


def _dark_theme_declarations(stylesheet: str) -> dict[str, str]:
    match = re.search(r'\[data-theme="dark"\]\s*\{([^}]*)\}', stylesheet, re.DOTALL)
    assert match is not None
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", match.group(1)))


def test_dashboard_and_chat_use_high_contrast_dark_palette():
    expected = {
        "--bg-primary": "#000000",
        "--bg-secondary": "#0a0a0a",
        "--bg-tertiary": "#171717",
        "--text-primary": "#ffffff",
        "--text-secondary": "#f5f5f5",
        "--text-tertiary": "#e5e5e5",
        "--text-muted": "#d4d4d4",
        "--border-faint": "#404040",
        "--border-normal": "#737373",
        "--code-bg": "#0a0a0a",
    }

    for stylesheet in (DASHBOARD, CHAT):
        declarations = _dark_theme_declarations(stylesheet)
        assert declarations.items() >= expected.items()


def test_login_uses_true_black_and_white_in_dark_mode():
    assert (
        '[data-theme="dark"] body { background-color: #000000 !important; '
        "color: #ffffff !important; }"
    ) in LOGIN
    assert (
        '[data-theme="dark"] input { background-color: #0a0a0a !important; '
        "color: #ffffff !important; border-color: #404040 !important; }"
    ) in LOGIN
