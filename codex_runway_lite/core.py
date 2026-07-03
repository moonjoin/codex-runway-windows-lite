from __future__ import annotations

import base64
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any


CHATGPT_BACKEND = "https://chatgpt.com/backend-api"
TOKEN_URL = "https://auth.openai.com/oauth/token"
PRICING_VERSION = "2026-06-29"


@dataclass(frozen=True)
class RateWindow:
    used_percent: int
    resets_at: datetime | None
    window_minutes: int | None


@dataclass(frozen=True)
class NamedRateWindow:
    name: str
    window: RateWindow


@dataclass(frozen=True)
class QuotaSnapshot:
    plan: str | None
    primary: RateWindow
    secondary: RateWindow | None
    additional_windows: list[NamedRateWindow]
    credits_balance: float | None
    updated_at: datetime


@dataclass(frozen=True)
class ResetCredit:
    id: str | None
    status: str
    created_at: datetime | None
    expires_at: datetime | None
    remaining_seconds: int


@dataclass(frozen=True)
class ResetCreditsSnapshot:
    available_count: int
    total_count: int
    credits: list[ResetCredit]
    updated_at: datetime


@dataclass(frozen=True)
class ResetRiskSummary:
    available: int
    expiring_soon: int
    unavailable: int
    nearest_expiry: datetime | None
    furthest_expiry: datetime | None
    risk_level: str


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class DailyUsageRow:
    date: str
    total_tokens: int
    estimated_usd: float
    turns: int


@dataclass(frozen=True)
class SessionSummary:
    id: str
    title: str
    project_name: str
    cwd: str | None
    updated_at: datetime
    state: str
    usage_by_model: dict[str, TokenUsage] = field(default_factory=dict)
    estimated_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return sum(usage.total_tokens for usage in self.usage_by_model.values())


@dataclass(frozen=True)
class UsageSummary:
    sessions: int
    turns: int
    total_tokens: int
    estimated_usd: float
    by_model: dict[str, TokenUsage]
    daily_rows: list[DailyUsageRow] = field(default_factory=list)
    pricing_version: str = PRICING_VERSION


def estimate_window_exhaustion(window: RateWindow, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    if window.used_percent <= 0 or not window.resets_at or not window.window_minutes:
        return None
    window_start = window.resets_at - timedelta(minutes=window.window_minutes)
    elapsed = (now - window_start).total_seconds()
    if elapsed <= 0:
        return None
    used_ratio = max(0.01, window.used_percent / 100)
    total_seconds_at_full = elapsed / used_ratio
    return window_start + timedelta(seconds=round(total_seconds_at_full))


def reset_risk_summary(snapshot: ResetCreditsSnapshot, now: datetime | None = None) -> ResetRiskSummary:
    now = now or snapshot.updated_at
    available_credits = [credit for credit in snapshot.credits if credit.status == "available"]
    unavailable = max(0, snapshot.total_count - len(available_credits))
    expiries = sorted(credit.expires_at for credit in available_credits if credit.expires_at)
    expiring_soon = sum(1 for expiry in expiries if expiry - now <= timedelta(days=7))
    if len(available_credits) == 0 and snapshot.total_count > 0:
        risk = "danger"
    elif expiring_soon > 0:
        risk = "warning"
    else:
        risk = "ok"
    return ResetRiskSummary(
        available=len(available_credits),
        expiring_soon=expiring_soon,
        unavailable=unavailable,
        nearest_expiry=expiries[0] if expiries else None,
        furthest_expiry=expiries[-1] if expiries else None,
        risk_level=risk,
    )


def parse_quota_snapshot(payload: dict[str, Any], now: datetime | None = None) -> QuotaSnapshot:
    now = now or datetime.now(timezone.utc)
    rate_limit = payload.get("rate_limit") or {}
    primary_payload = rate_limit.get("primary_window") or {}
    secondary_payload = rate_limit.get("secondary_window")
    additional = []
    for item in payload.get("additional_rate_limits") or []:
        item_rate_limit = item.get("rate_limit") or {}
        window_payload = item_rate_limit.get("primary_window") or item_rate_limit.get("secondary_window")
        if not window_payload:
            continue
        additional.append(
            NamedRateWindow(
                name=_first_non_empty(item.get("limit_name"), item.get("metered_feature")) or "Codex extra limit",
                window=_parse_rate_window(window_payload),
            )
        )

    credits_balance = None
    credits = payload.get("credits")
    if isinstance(credits, dict) and credits.get("balance") is not None:
        try:
            credits_balance = float(credits["balance"])
        except (TypeError, ValueError):
            credits_balance = None

    return QuotaSnapshot(
        plan=_first_non_empty(payload.get("plan_type")),
        primary=_parse_rate_window(primary_payload),
        secondary=_parse_rate_window(secondary_payload) if isinstance(secondary_payload, dict) else None,
        additional_windows=additional,
        credits_balance=credits_balance,
        updated_at=now,
    )


def parse_reset_credits(payload: Any, now: datetime | None = None) -> ResetCreditsSnapshot:
    now = now or datetime.now(timezone.utc)
    if isinstance(payload, list):
        raw_credits = payload
        available_count = None
    elif isinstance(payload, dict):
        raw_credits = payload.get("credits") or []
        available_count = payload.get("available_count")
    else:
        raw_credits = []
        available_count = None

    credits = []
    for item in raw_credits:
        if not isinstance(item, dict):
            continue
        expires_at = _parse_date(item.get("expires_at"))
        remaining = max(0, int((expires_at - now).total_seconds())) if expires_at else 0
        credits.append(
            ResetCredit(
                id=item.get("id"),
                status=item.get("status") or "unknown",
                created_at=_parse_date(item.get("created_at")),
                expires_at=expires_at,
                remaining_seconds=remaining,
            )
        )
    if available_count is None:
        available_count = sum(1 for credit in credits if credit.status == "available")
    return ResetCreditsSnapshot(
        available_count=int(available_count),
        total_count=len(credits),
        credits=credits,
        updated_at=now,
    )


def redacted_auth_summary(auth: dict[str, Any]) -> dict[str, str | None]:
    tokens = auth.get("tokens") or {}
    account_id = _first_non_empty(tokens.get("account_id"))
    return {
        "auth_mode": _first_non_empty(auth.get("auth_mode")),
        "account_id": _shorten(account_id),
    }


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def load_auth(auth_path: Path | None = None) -> dict[str, Any]:
    path = auth_path or default_codex_home() / "auth.json"
    return json.loads(path.read_text(encoding="utf-8"))


def token_is_expired(access_token: str, skew_seconds: int = 60) -> bool:
    try:
        claims = _decode_jwt_payload(access_token)
        exp = float(claims.get("exp"))
    except (ValueError, TypeError, KeyError):
        return True
    return exp - datetime.now(timezone.utc).timestamp() <= skew_seconds


def refresh_auth_in_memory(auth: dict[str, Any]) -> dict[str, Any]:
    tokens = auth.get("tokens") or {}
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("refresh_token missing")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    updated = json.loads(json.dumps(auth))
    updated_tokens = updated.setdefault("tokens", {})
    updated_tokens["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        updated_tokens["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        updated_tokens["id_token"] = data["id_token"]
    return updated


class QuotaClient:
    def __init__(self, base_url: str = CHATGPT_BACKEND, timeout_seconds: int = 30, retries: int = 1) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)

    def fetch_quota(self, auth: dict[str, Any]) -> QuotaSnapshot:
        return parse_quota_snapshot(self._get_json("wham/usage", auth))

    def fetch_reset_credits(self, auth: dict[str, Any]) -> ResetCreditsSnapshot:
        return parse_reset_credits(self._get_json("wham/rate-limit-reset-credits", auth))

    def _get_json(self, path: str, auth: dict[str, Any]) -> Any:
        tokens = auth.get("tokens") or {}
        access_token = tokens.get("access_token")
        if not access_token:
            raise RuntimeError("access_token missing")
        request = urllib.request.Request(
            f"{self.base_url}/{path}",
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/json",
                **({"ChatGPT-Account-Id": tokens["account_id"]} if tokens.get("account_id") else {}),
            },
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"HTTP {exc.code} while fetching {path}") from exc
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                if attempt < self.retries:
                    continue
                raise RuntimeError(f"Network error while fetching {path}: {exc}") from exc
        raise RuntimeError(f"Network error while fetching {path}")


class SessionScanner:
    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = Path(codex_home) if codex_home else default_codex_home()

    def scan_recent(self, limit: int = 5) -> list[SessionSummary]:
        titles = self._read_index_titles()
        summaries = []
        candidate_limit = max(limit * 6, 30)
        files = sorted(self._jsonl_files(), key=self._file_sort_time, reverse=True)[:candidate_limit]
        for file in files:
            summary = self._parse_session(file, titles)
            if summary:
                summaries.append(summary)
        summaries.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        by_id: dict[str, SessionSummary] = {}
        for item in summaries:
            by_id.setdefault(item.id, item)
        return list(by_id.values())[: max(0, limit)]

    def scan_usage(self, days: int = 7, now: datetime | None = None) -> UsageSummary:
        now = now or datetime.now(timezone.utc)
        cutoff = now.timestamp() - days * 86400
        by_model: dict[str, TokenUsage] = {}
        daily_usage: dict[str, TokenUsage] = {}
        daily_by_model: dict[str, dict[str, TokenUsage]] = {}
        daily_turns: dict[str, int] = {}
        turns = 0
        session_ids = set()
        for file in self._jsonl_files():
            try:
                if file.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            current_model = "unknown-model"
            session_id = None
            for record in _read_jsonl(file):
                timestamp = _parse_date(record.get("timestamp"))
                if timestamp and timestamp.timestamp() < cutoff:
                    continue
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                if record.get("type") == "session_meta":
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    if session_id:
                        session_ids.add(session_id)
                if record.get("type") == "turn_context" and payload.get("model"):
                    current_model = payload["model"]
                model = (record.get("turn_context") or {}).get("model") or payload.get("model") or current_model
                usage = _token_usage(payload.get("info"), "last_token_usage")
                if not usage:
                    continue
                turns += 1
                by_model[model] = by_model.get(model, TokenUsage()).add(usage)
                day = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d") if timestamp else "unknown"
                daily_usage[day] = daily_usage.get(day, TokenUsage()).add(usage)
                daily_by_model.setdefault(day, {})
                daily_by_model[day][model] = daily_by_model[day].get(model, TokenUsage()).add(usage)
                daily_turns[day] = daily_turns.get(day, 0) + 1
        daily_rows = [
            DailyUsageRow(
                date=day,
                total_tokens=usage.total_tokens,
                estimated_usd=sum(_estimate_cost(model, model_usage) for model, model_usage in daily_by_model.get(day, {}).items()),
                turns=daily_turns.get(day, 0),
            )
            for day, usage in sorted(daily_usage.items(), reverse=True)
        ]
        return UsageSummary(
            sessions=len(session_ids),
            turns=turns,
            total_tokens=sum(usage.total_tokens for usage in by_model.values()),
            estimated_usd=sum(_estimate_cost(model, usage) for model, usage in by_model.items()),
            by_model=by_model,
            daily_rows=daily_rows,
        )

    def _parse_session(self, file: Path, index_titles: dict[str, str]) -> SessionSummary | None:
        state: dict[str, Any] = {
            "id": None,
            "cwd": None,
            "title": None,
            "updated_at": None,
            "state": "recent",
            "current_model": "unknown-model",
            "usage_by_model": {},
        }
        for record in _read_jsonl(file):
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            timestamp = _parse_date(record.get("timestamp"))
            if timestamp and (state["updated_at"] is None or timestamp > state["updated_at"]):
                state["updated_at"] = timestamp
            if record.get("type") == "session_meta":
                state["id"] = state["id"] or payload.get("id") or payload.get("session_id")
                state["cwd"] = state["cwd"] or payload.get("cwd")
            if not state["title"] and payload.get("type") == "message" and payload.get("role") == "user":
                state["title"] = _clean_title(_message_text(payload.get("content")))
            state["state"] = _stronger_state(state["state"], _record_state(record, payload))
            if record.get("type") == "turn_context" and payload.get("model"):
                state["current_model"] = payload["model"]
            model = (record.get("turn_context") or {}).get("model") or payload.get("model") or state["current_model"]
            usage = _token_usage(payload.get("info"), "total_token_usage")
            if usage:
                state["usage_by_model"][model] = usage
                continue
            usage = _token_usage(payload.get("info"), "last_token_usage")
            if usage:
                previous = state["usage_by_model"].get(model, TokenUsage())
                state["usage_by_model"][model] = previous.add(usage)

        session_id = state["id"]
        updated_at = state["updated_at"]
        if not session_id or not updated_at:
            return None
        title = _clean_title(index_titles.get(session_id)) or state["title"] or "Untitled"
        usage_by_model = state["usage_by_model"]
        return SessionSummary(
            id=session_id,
            title=title,
            project_name=_project_name(state["cwd"]),
            cwd=state["cwd"],
            updated_at=updated_at,
            state=state["state"],
            usage_by_model=usage_by_model,
            estimated_usd=sum(_estimate_cost(model, usage) for model, usage in usage_by_model.items()),
        )

    def _read_index_titles(self) -> dict[str, str]:
        index_path = self.codex_home / "session_index.jsonl"
        titles = {}
        if not index_path.exists():
            return titles
        for record in _read_jsonl(index_path):
            session_id = record.get("id")
            title = _clean_title(record.get("thread_name"))
            if session_id and title:
                titles[session_id] = title
        return titles

    def _jsonl_files(self) -> list[Path]:
        files = []
        for folder in ("sessions", "archived_sessions"):
            root = self.codex_home / folder
            if root.exists():
                files.extend(root.rglob("*.jsonl"))
        return files

    def _file_sort_time(self, file: Path) -> float:
        try:
            return file.stat().st_mtime
        except OSError:
            return 0


def build_status_snapshot(recent_limit: int = 5) -> dict[str, Any]:
    auth = load_auth()
    if token_is_expired(auth.get("tokens", {}).get("access_token", "")):
        auth = refresh_auth_in_memory(auth)
    client = QuotaClient()
    scanner = SessionScanner()
    quota = client.fetch_quota(auth)
    resets = client.fetch_reset_credits(auth)
    sessions = scanner.scan_recent(limit=recent_limit)
    usage = scanner.scan_usage(days=7)
    return {
        "generated_at": _format_date(datetime.now(timezone.utc)),
        "account": redacted_auth_summary(auth),
        "quota": quota,
        "reset_credits": resets,
        "recent_sessions": sessions,
        "usage_7d": usage,
    }


def _parse_rate_window(payload: dict[str, Any]) -> RateWindow:
    seconds = _int_value(payload.get("limit_window_seconds"))
    reset_at = _parse_date(payload.get("reset_at"))
    return RateWindow(
        used_percent=_int_value(payload.get("used_percent")),
        resets_at=reset_at,
        window_minutes=seconds // 60 if seconds > 0 else None,
    )


def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _shorten(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 9:
        return value
    return f"{value[:5]}...{value[-4:]}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _message_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(parts)
    return None


def _clean_title(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:80] if text else None


def _project_name(cwd: str | None) -> str:
    if not cwd:
        return "Unknown project"
    text = cwd.strip()
    if "\\" in text or ":" in text:
        name = PureWindowsPath(text).name
    else:
        name = Path(text).name
    return name or text


def _record_state(record: dict[str, Any], payload: dict[str, Any]) -> str:
    text = " ".join(
        str(item).lower()
        for item in (record.get("type"), payload.get("type"), payload.get("status"))
        if item
    )
    if "error" in text or "failed" in text:
        return "failed"
    if "approval" in text or "permission" in text or "waiting" in text:
        return "needs_attention"
    return "recent"


def _stronger_state(left: str, right: str) -> str:
    order = {"recent": 0, "needs_attention": 1, "failed": 2}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _token_usage(info: Any, key: str) -> TokenUsage | None:
    if not isinstance(info, dict) or not isinstance(info.get(key), dict):
        return None
    usage = info[key]
    output_tokens = _int_value(usage.get("output_tokens")) + _int_value(usage.get("reasoning_output_tokens"))
    return TokenUsage(
        input_tokens=_int_value(usage.get("input_tokens")),
        cached_input_tokens=_int_value(usage.get("cached_input_tokens")),
        output_tokens=output_tokens,
    )


def _estimate_cost(model: str, usage: TokenUsage) -> float:
    price = _price_for_model(model)
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    return (
        uncached / 1_000_000 * price[0]
        + usage.cached_input_tokens / 1_000_000 * price[1]
        + usage.output_tokens / 1_000_000 * price[2]
    )


def _price_for_model(model: str) -> tuple[float, float, float]:
    key = model.lower()
    if "gpt-5.3-codex" in key or "gpt-5.2-codex" in key:
        return (1.75, 0.175, 14.0)
    if "gpt-5.5" in key:
        return (5.0, 0.5, 30.0)
    if "gpt-5" in key:
        return (1.25, 0.125, 10.0)
    return (5.0, 0.5, 30.0)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("invalid jwt")
    payload = parts[1].replace("-", "+").replace("_", "/")
    payload += "=" * ((4 - len(payload) % 4) % 4)
    return json.loads(base64.b64decode(payload).decode("utf-8"))
