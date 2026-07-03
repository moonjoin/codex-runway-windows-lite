from __future__ import annotations

from datetime import datetime, timezone
import json

from codex_runway_lite import app as app_module
from codex_runway_lite.core import QuotaSnapshot, RateWindow, ResetCreditsSnapshot, UsageSummary


def fake_snapshot(recent_limit: int = 20):
    return {
        "generated_at": "2026-07-03T08:00:00Z",
        "account": {"auth_mode": "chatgpt", "account_id": "accou...7890"},
        "quota": QuotaSnapshot(
            plan="prolite",
            primary=RateWindow(used_percent=41, resets_at=None, window_minutes=300),
            secondary=RateWindow(used_percent=38, resets_at=None, window_minutes=10080),
            additional_windows=[],
            credits_balance=None,
            updated_at=datetime.now(timezone.utc),
        ),
        "reset_credits": ResetCreditsSnapshot(
            available_count=0,
            total_count=0,
            credits=[],
            updated_at=datetime.now(timezone.utc),
        ),
        "recent_sessions": [],
        "usage_7d": UsageSummary(sessions=0, turns=0, total_tokens=0, estimated_usd=0, by_model={}),
    }


def test_app_constructs_with_scrollbar_settings_and_tray_close(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_module, "build_status_snapshot", fake_snapshot)
    settings_path = tmp_path / "settings.json"

    instance = app_module.CodexRunwayLiteApp(
        auto_refresh=False,
        tray_enabled=False,
        settings_path=settings_path,
    )
    instance.root.update()

    assert hasattr(instance, "content_canvas")
    assert hasattr(instance, "main_scrollbar")
    assert hasattr(instance, "scroll_column")
    assert instance.main_scrollbar.cget("style") == "Runway.Vertical.TScrollbar"
    assert instance.main_scrollbar.winfo_ismapped()

    instance.auto_refresh_var.set("30分钟")
    instance._on_auto_refresh_changed(None)

    assert app_module.load_app_settings(settings_path).auto_refresh_minutes == 30
    assert instance.auto_refresh_after_id is not None
    assert "30分钟" in instance.status_var.get()

    error_dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda title, body: error_dialogs.append((title, body)))
    instance._show_error(RuntimeError("network failed"), show_dialog=False)

    assert error_dialogs == []

    instance.tray_running = True
    instance._on_close()
    instance.root.update()

    assert instance.root.state() == "withdrawn"

    instance.quit_app()


def test_app_settings_default_and_persist_auto_refresh_interval(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"

    settings = app_module.load_app_settings(settings_path)

    assert settings.auto_refresh_minutes == 15

    app_module.save_app_settings(settings_path, app_module.AppSettings(auto_refresh_minutes=30))

    assert app_module.load_app_settings(settings_path).auto_refresh_minutes == 30

    settings_path.write_text(json.dumps({"auto_refresh_minutes": -1}), encoding="utf-8")

    assert app_module.load_app_settings(settings_path).auto_refresh_minutes == 15
