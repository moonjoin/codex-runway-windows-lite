from __future__ import annotations

from datetime import datetime, timezone

from codex_runway_lite import app as app_module
from codex_runway_lite.core import QuotaSnapshot, RateWindow, ResetCreditsSnapshot, UsageSummary


def test_app_constructs_without_tk_font_error(monkeypatch) -> None:
    def fake_snapshot(recent_limit: int = 20):
        return {
            "generated_at": "2026-07-03T08:00:00Z",
            "account": {"auth_mode": "chatgpt", "account_id": "accou...7890"},
            "quota": QuotaSnapshot(
                plan="prolite",
                primary=RateWindow(used_percent=41, resets_at=None, window_minutes=300),
                secondary=None,
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

    monkeypatch.setattr(app_module, "build_status_snapshot", fake_snapshot)

    instance = app_module.CodexRunwayLiteApp(auto_refresh=False)
    instance.root.update()
    instance.root.destroy()


def test_app_main_content_has_visible_scrollbar(monkeypatch) -> None:
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

    monkeypatch.setattr(app_module, "build_status_snapshot", fake_snapshot)

    instance = app_module.CodexRunwayLiteApp(auto_refresh=False)
    instance.root.update()

    assert hasattr(instance, "content_canvas")
    assert hasattr(instance, "main_scrollbar")
    assert hasattr(instance, "scroll_column")
    assert instance.main_scrollbar.cget("style") == "Runway.Vertical.TScrollbar"
    assert instance.main_scrollbar.winfo_ismapped()

    instance.root.destroy()
