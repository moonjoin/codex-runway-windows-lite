from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from codex_runway_lite.core import (
    RateWindow,
    QuotaClient,
    ResetCredit,
    ResetCreditsSnapshot,
    SessionScanner,
    SessionSummary,
    estimate_window_exhaustion,
    parse_quota_snapshot,
    parse_reset_credits,
    redacted_auth_summary,
    reset_risk_summary,
)


def test_parse_quota_snapshot_extracts_primary_weekly_and_extra_windows() -> None:
    now = datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)
    snapshot = parse_quota_snapshot(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 29,
                    "reset_at": 1783069200,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 36,
                    "reset_at": 1783472400,
                    "limit_window_seconds": 604800,
                },
            },
            "additional_rate_limits": [
                {
                    "limit_name": "Codex extra limit",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 12,
                            "reset_at": 1783155600,
                            "limit_window_seconds": 86400,
                        }
                    },
                }
            ],
            "credits": {"balance": "3.5"},
        },
        now=now,
    )

    assert snapshot.plan == "pro"
    assert snapshot.primary.used_percent == 29
    assert snapshot.primary.window_minutes == 300
    assert snapshot.secondary is not None
    assert snapshot.secondary.used_percent == 36
    assert snapshot.additional_windows[0].name == "Codex extra limit"
    assert snapshot.credits_balance == 3.5


def test_parse_reset_credits_accepts_object_and_array_shapes() -> None:
    now = datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)

    object_snapshot = parse_reset_credits(
        {
            "available_count": 1,
            "credits": [
                {
                    "id": "credit-1",
                    "status": "available",
                    "created_at": "2026-07-01T00:00:00Z",
                    "expires_at": "2026-07-05T08:00:00Z",
                },
                {"id": "credit-2", "status": "used"},
            ],
        },
        now=now,
    )
    array_snapshot = parse_reset_credits([{"id": "credit-3", "status": "available"}], now=now)

    assert object_snapshot.available_count == 1
    assert object_snapshot.total_count == 2
    assert object_snapshot.credits[0].remaining_seconds == 172800
    assert array_snapshot.available_count == 1
    assert array_snapshot.total_count == 1


def test_session_scanner_reads_recent_sessions_and_token_usage(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    session_dir = codex_home / "sessions" / "2026" / "07" / "03"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "rollout-abc.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-03T08:00:00Z",
                        "payload": {"id": "abc", "cwd": r"F:\codex\demo"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-03T08:01:00Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"text": "帮我看看这个项目"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-03T08:02:00Z",
                        "turn_context": {"model": "gpt-5"},
                        "payload": {
                            "model": "gpt-5",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 70,
                                    "reasoning_output_tokens": 5,
                                }
                            },
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "abc",
                "thread_name": "索引里的标题",
                "updated_at": "2026-07-03T08:02:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = SessionScanner(codex_home).scan_recent(limit=5)

    assert len(summary) == 1
    assert summary[0].id == "abc"
    assert summary[0].title == "索引里的标题"
    assert summary[0].project_name == "demo"
    assert summary[0].total_tokens == 175
    assert summary[0].estimated_usd > 0


def test_redacted_auth_summary_never_returns_tokens_or_full_account_id() -> None:
    summary = redacted_auth_summary(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "secret-id-token",
                "access_token": "secret-access-token",
                "refresh_token": "secret-refresh-token",
                "account_id": "account-1234567890",
            },
        }
    )

    assert summary["auth_mode"] == "chatgpt"
    assert summary["account_id"] == "accou...7890"
    assert "secret" not in json.dumps(summary)


def test_quota_client_retries_once_after_timeout(monkeypatch) -> None:
    calls = {"count": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 10,
                            "reset_at": 1783069200,
                            "limit_window_seconds": 18000,
                        }
                    }
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("timed out")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = QuotaClient(timeout_seconds=1, retries=1)
    snapshot = client.fetch_quota({"tokens": {"access_token": "token"}})

    assert calls["count"] == 2
    assert snapshot.primary.used_percent == 10


def test_scan_recent_limits_candidate_files_before_parsing(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / ".codex"
    session_dir = codex_home / "sessions"
    session_dir.mkdir(parents=True)
    for index in range(40):
        file = session_dir / f"rollout-{index:02d}.jsonl"
        file.write_text("{}", encoding="utf-8")
        stamp = 1_800_000_000 + index
        os.utime(file, (stamp, stamp))

    parsed: list[str] = []

    def fake_parse(self, file, index_titles):
        parsed.append(file.name)
        return SessionSummary(
            id=file.stem,
            title=file.stem,
            project_name="demo",
            cwd=None,
            updated_at=datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc),
            state="recent",
            usage_by_model={},
            estimated_usd=0,
        )

    monkeypatch.setattr(SessionScanner, "_parse_session", fake_parse)

    result = SessionScanner(codex_home).scan_recent(limit=5)

    assert len(result) == 5
    assert len(parsed) <= 30


def test_estimate_window_exhaustion_uses_rate_window_progress() -> None:
    now = datetime(2026, 7, 3, 2, 30, tzinfo=timezone.utc)
    window = RateWindow(
        used_percent=75,
        resets_at=datetime(2026, 7, 3, 5, 0, tzinfo=timezone.utc),
        window_minutes=300,
    )

    exhaustion = estimate_window_exhaustion(window, now)

    assert exhaustion == datetime(2026, 7, 3, 3, 20, tzinfo=timezone.utc)


def test_reset_risk_summary_counts_available_expiring_and_unavailable() -> None:
    now = datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)
    snapshot = ResetCreditsSnapshot(
        available_count=2,
        total_count=3,
        updated_at=now,
        credits=[
            ResetCredit("a", "available", None, datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc), 86400),
            ResetCredit("b", "available", None, datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc), 17 * 86400),
            ResetCredit("c", "used", None, None, 0),
        ],
    )

    summary = reset_risk_summary(snapshot, now=now)

    assert summary.available == 2
    assert summary.expiring_soon == 1
    assert summary.unavailable == 1
    assert summary.nearest_expiry == datetime(2026, 7, 4, 8, 0, tzinfo=timezone.utc)
    assert summary.furthest_expiry == datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    assert summary.risk_level == "warning"


def test_scan_usage_returns_daily_breakdown(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    session_dir = codex_home / "sessions"
    session_dir.mkdir(parents=True)
    file = session_dir / "rollout-usage.jsonl"
    file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-07-01T08:00:00Z",
                        "payload": {"id": "daily-a"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-01T08:01:00Z",
                        "payload": {
                            "model": "gpt-5",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 50,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-02T09:01:00Z",
                        "payload": {
                            "model": "gpt-5",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 200,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 70,
                                }
                            },
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    stamp = datetime(2026, 7, 2, 9, 1, tzinfo=timezone.utc).timestamp()
    os.utime(file, (stamp, stamp))

    summary = SessionScanner(codex_home).scan_usage(days=30, now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert [row.date for row in summary.daily_rows] == ["2026-07-02", "2026-07-01"]
    assert [row.total_tokens for row in summary.daily_rows] == [270, 150]
    assert [row.turns for row in summary.daily_rows] == [1, 1]
