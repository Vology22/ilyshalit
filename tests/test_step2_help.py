"""Проверка блока помощи на шаге 2 в боевом режиме."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TPL_DIR = Path(__file__).resolve().parent.parent / "chzkiz" / "web" / "templates"
env = Environment(loader=FileSystemLoader(str(TPL_DIR)))


def _render_step2(is_mock: bool) -> str:
    return env.get_template("step2.html").render(
        request={}, run_id="test-run-123", is_mock=is_mock
    )


def test_help_block_present_in_battle_mode():
    html = _render_step2(is_mock=False)
    assert 'id="help-block"' in html, "в боевом режиме должен быть блок помощи"
    assert 'id="retry-certs-btn"' in html, "в боевом режиме должна быть кнопка Проверить снова"
    assert "КриптоПро CSP 5" in html
    assert "КриптоПро ЭЦП Browser plug-in" in html
    assert "CryptoPro Extension for CAdES Browser Plug-in" in html
    assert "флешку с подписью" in html
    assert "Перезапустите браузер" in html


def test_help_block_absent_in_demo_mode():
    html = _render_step2(is_mock=True)
    assert 'id="help-block"' not in html, "в демо-режиме блока помощи быть не должно"
    assert 'id="retry-certs-btn"' not in html, "в демо-режиме кнопки Проверить снова быть не должно"


def test_login_button_present():
    html = _render_step2(is_mock=True)
    assert 'id="login-btn"' in html
    assert 'id="cert-list"' in html