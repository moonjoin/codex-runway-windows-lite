from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from codex_runway_lite.app import CodexRunwayLiteApp
from codex_runway_lite.core import QuotaSnapshot, ResetCreditsSnapshot, UsageSummary, build_status_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex Runway Lite for Windows")
    parser.add_argument("--self-check", action="store_true", help="Refresh once and print a redacted summary.")
    args = parser.parse_args()
    if args.self_check:
        snapshot = build_status_snapshot()
        print_summary(snapshot)
        return
    CodexRunwayLiteApp().run()


def print_summary(snapshot: dict[str, Any]) -> None:
    quota: QuotaSnapshot = snapshot["quota"]
    resets: ResetCreditsSnapshot = snapshot["reset_credits"]
    usage: UsageSummary = snapshot["usage_7d"]
    print(f"generated_at: {snapshot['generated_at']}")
    print(f"account: {json.dumps(snapshot['account'], ensure_ascii=False)}")
    print(f"plan: {quota.plan or 'unknown'}")
    print(f"five_hour_used: {quota.primary.used_percent}%")
    if quota.secondary:
        print(f"weekly_used: {quota.secondary.used_percent}%")
    print(f"reset_credits: {resets.available_count}/{resets.total_count}")
    print(f"usage_7d_tokens: {usage.total_tokens}")
    print(f"usage_7d_estimated_usd: {usage.estimated_usd:.4f}")
    print(f"recent_sessions: {len(snapshot['recent_sessions'])}")


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    main()

