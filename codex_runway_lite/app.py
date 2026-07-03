from __future__ import annotations

import json
import threading
import tkinter as tk
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # noqa: BLE001 - tray support is optional for source runs.
    pystray = None
    Image = None
    ImageDraw = None

from .core import (
    NamedRateWindow,
    QuotaSnapshot,
    RateWindow,
    ResetCreditsSnapshot,
    SessionSummary,
    UsageSummary,
    build_status_snapshot,
    default_codex_home,
    estimate_window_exhaustion,
    reset_risk_summary,
)


COLORS = {
    "page": "#1f3044",
    "panel": "#2b3f56",
    "panel_soft": "#344a63",
    "panel_strong": "#3c536d",
    "line": "#405873",
    "text": "#e8eef6",
    "muted": "#aab7c7",
    "dim": "#7f8fa2",
    "green": "#7fd75a",
    "blue": "#5b83ff",
    "yellow": "#f0d64f",
    "orange": "#f2a33b",
    "danger": "#ff6b6b",
    "button": "#405873",
    "button_hover": "#4a6481",
}


DEFAULT_AUTO_REFRESH_MINUTES = 15
AUTO_REFRESH_CHOICES = (0, 5, 10, 15, 30, 60)
AUTO_REFRESH_LABELS = {
    0: "关闭",
    5: "5分钟",
    10: "10分钟",
    15: "15分钟",
    30: "30分钟",
    60: "60分钟",
}
AUTO_REFRESH_BY_LABEL = {label: minutes for minutes, label in AUTO_REFRESH_LABELS.items()}


@dataclass(frozen=True)
class AppSettings:
    auto_refresh_minutes: int = DEFAULT_AUTO_REFRESH_MINUTES
    close_to_tray: bool = True


def default_settings_path() -> Path:
    return Path.home() / ".codex-runway" / "settings-lite.json"


def load_app_settings(path: Path | None = None) -> AppSettings:
    settings_path = path or default_settings_path()
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    return AppSettings(
        auto_refresh_minutes=_normalize_auto_refresh_minutes(data.get("auto_refresh_minutes")),
        close_to_tray=bool(data.get("close_to_tray", True)),
    )


def save_app_settings(path: Path | None, settings: AppSettings) -> None:
    settings_path = path or default_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_auto_refresh_minutes(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_REFRESH_MINUTES
    if minutes not in AUTO_REFRESH_CHOICES:
        return DEFAULT_AUTO_REFRESH_MINUTES
    return minutes


class CodexRunwayLiteApp:
    def __init__(
        self,
        auto_refresh: bool = True,
        tray_enabled: bool = True,
        settings_path: Path | None = None,
    ) -> None:
        self.root = tk.Tk()
        self.root.title("Codex Runway")
        self.root.geometry("540x720")
        self.root.minsize(480, 520)
        self.root.configure(bg=COLORS["page"])
        self.root.option_add("*Font", "{Segoe UI} 9")
        self.settings_path = settings_path or default_settings_path()
        self.settings = load_app_settings(self.settings_path)
        self.tray_enabled = tray_enabled
        self.tray_icon: Any | None = None
        self.tray_running = False
        self.tray_thread: threading.Thread | None = None
        self.is_quitting = False
        self.refresh_in_progress = False
        self.auto_refresh_after_id: str | None = None
        self.hide_notice_shown = False
        self.snapshot: dict[str, Any] | None = None
        self.recent_expanded = False
        self.refresh_button: tk.Button | None = None
        self.status_var = tk.StringVar(value="正在启动...")
        self.auto_refresh_var = tk.StringVar(value=self._auto_refresh_label(self.settings.auto_refresh_minutes))
        self.tray_status_var = tk.StringVar(value="托盘：准备中")
        self._set_icon()
        self._configure_theme()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_tray()
        if auto_refresh:
            self.refresh()

    def run(self) -> None:
        self.root.mainloop()

    def _set_icon(self) -> None:
        icon = Path(__file__).resolve().parents[2] / "Resources" / "AppIcon.png"
        if icon.exists():
            try:
                self.icon_image = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self.icon_image)
            except tk.TclError:
                self.icon_image = None

    def _configure_theme(self) -> None:
        self.ui_style = ttk.Style(self.root)
        try:
            self.ui_style.theme_use("clam")
        except tk.TclError:
            pass
        try:
            self.ui_style.layout(
                "Runway.Vertical.TScrollbar",
                [
                    (
                        "Vertical.Scrollbar.trough",
                        {
                            "sticky": "ns",
                            "children": [
                                (
                                    "Vertical.Scrollbar.thumb",
                                    {"expand": "1", "sticky": "nswe"},
                                )
                            ],
                        },
                    )
                ],
            )
        except tk.TclError:
            pass
        self.ui_style.configure(
            "Runway.Vertical.TScrollbar",
            background=COLORS["line"],
            troughcolor=COLORS["page"],
            bordercolor=COLORS["page"],
            darkcolor=COLORS["line"],
            lightcolor=COLORS["line"],
            arrowcolor=COLORS["muted"],
            relief="flat",
            borderwidth=0,
            gripcount=0,
            arrowsize=0,
            width=10,
        )
        self.ui_style.map(
            "Runway.Vertical.TScrollbar",
            background=[
                ("pressed", COLORS["button_hover"]),
                ("active", COLORS["panel_strong"]),
                ("!active", COLORS["line"]),
            ],
            troughcolor=[
                ("disabled", COLORS["page"]),
                ("!disabled", COLORS["page"]),
            ],
        )
        self.ui_style.configure(
            "Runway.TCombobox",
            fieldbackground=COLORS["panel_soft"],
            background=COLORS["button"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            relief="flat",
        )
        self.ui_style.map(
            "Runway.TCombobox",
            fieldbackground=[("readonly", COLORS["panel_soft"])],
            foreground=[("readonly", COLORS["text"])],
        )

    def _build(self) -> None:
        self.shell = tk.Frame(self.root, bg=COLORS["page"], padx=16, pady=14)
        self.shell.pack(fill="both", expand=True)
        self._build_header()

        self._build_scroll_area()

        self.quota_body = self._section(self.content, "配额", "看 5 小时、每周和额外限制还剩多少")
        self.reset_body = self._section(self.content, "重置次数", "可用 reset credits 和到期风险")
        self.cost_body = self._section(self.content, "API 等价成本", "按本机会话 token 估算，不是实际账单")
        self.sessions_body = self._section(self.content, "最近会话", "默认折叠，展开后显示完整明细")
        self.settings_body = self._section(self.content, "设置", "托盘常驻和自动刷新")
        self._render_settings()

        self.status = tk.Label(
            self.shell,
            textvariable=self.status_var,
            bg=COLORS["page"],
            fg=COLORS["muted"],
            anchor="w",
        )
        self.status.pack(fill="x", pady=(10, 0))

    def _build_scroll_area(self) -> None:
        scroll_wrap = tk.Frame(self.shell, bg=COLORS["page"])
        scroll_wrap.pack(fill="both", expand=True, pady=(12, 0))
        self.content_canvas = tk.Canvas(scroll_wrap, bg=COLORS["page"], highlightthickness=0)
        self.scroll_column = tk.Frame(scroll_wrap, bg=COLORS["page"], width=10)
        self.scroll_column.pack_propagate(False)
        self.main_scrollbar = ttk.Scrollbar(
            self.scroll_column,
            orient="vertical",
            command=self.content_canvas.yview,
            style="Runway.Vertical.TScrollbar",
        )
        self.content_canvas.configure(yscrollcommand=self.main_scrollbar.set)
        self.content_canvas.pack(side="left", fill="both", expand=True)
        self.scroll_column.pack(side="right", fill="y", padx=(8, 0))
        self.main_scrollbar.pack(fill="y", expand=True)

        self.content = tk.Frame(self.content_canvas, bg=COLORS["page"])
        self.content_window = self.content_canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._update_scroll_region)
        self.content_canvas.bind("<Configure>", self._sync_content_width)
        self.content_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.content_canvas.configure(scrollregion=self.content_canvas.bbox("all"))

    def _sync_content_width(self, event: tk.Event) -> None:
        self.content_canvas.itemconfigure(self.content_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.root.focus_displayof() is None:
            return
        self.content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _build_header(self) -> None:
        header = tk.Frame(self.shell, bg=COLORS["page"])
        header.pack(fill="x")

        left = tk.Frame(header, bg=COLORS["page"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(
            left,
            text="Codex Runway",
            bg=COLORS["page"],
            fg=COLORS["text"],
            font=("Segoe UI", 16, "bold"),
            anchor="w",
        ).pack(anchor="w")
        self.account_var = tk.StringVar(value="账号：读取中")
        tk.Label(
            left,
            textvariable=self.account_var,
            bg=COLORS["page"],
            fg=COLORS["muted"],
            anchor="w",
        ).pack(anchor="w", pady=(3, 0))

        actions = tk.Frame(header, bg=COLORS["page"])
        actions.pack(side="right", anchor="n")
        self.refresh_button = self._button(actions, "刷新", self.refresh)
        self.refresh_button.pack(side="left", padx=(0, 6))
        self._button(actions, "导出", self.export_status).pack(side="left", padx=(0, 6))
        self._button(actions, "打开 .codex", self.open_codex_folder).pack(side="left")

    def _section(self, parent: tk.Frame, title: str, subtitle: str) -> tk.Frame:
        section = tk.Frame(parent, bg=COLORS["panel"], padx=14, pady=12)
        section.pack(fill="x", pady=(0, 10))
        head = tk.Frame(section, bg=COLORS["panel"])
        head.pack(fill="x")
        tk.Label(
            head,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            section,
            text=subtitle,
            bg=COLORS["panel"],
            fg=COLORS["dim"],
            anchor="w",
        ).pack(fill="x", pady=(3, 8))
        body = tk.Frame(section, bg=COLORS["panel"])
        body.pack(fill="x")
        return body

    def refresh(self, show_errors: bool = True) -> None:
        if self.refresh_in_progress:
            return
        if self.snapshot is None:
            self._show_loading_placeholders()
        self._set_busy(True, "正在刷新配额和本机会话...")
        threading.Thread(target=self._refresh_worker, args=(show_errors,), daemon=True).start()

    def _refresh_worker(self, show_errors: bool) -> None:
        try:
            snapshot = build_status_snapshot(recent_limit=20)
        except Exception as exc:  # noqa: BLE001 - UI should show user-facing failures.
            self.root.after(0, lambda: self._show_error(exc, show_dialog=show_errors))
            return
        self.root.after(0, lambda: self._render(snapshot))

    def _render(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.account_var.set(self._account_text(snapshot["account"], snapshot["quota"]))
        self._render_quota(snapshot["quota"])
        self._render_resets(snapshot["reset_credits"])
        self._render_cost(snapshot["usage_7d"])
        self._render_sessions(snapshot["recent_sessions"])
        self._update_tray_title()
        self._set_busy(False, f"已刷新：{datetime.now().strftime('%H:%M:%S')}")
        self._schedule_auto_refresh()
        self.root.after_idle(lambda: self.content_canvas.yview_moveto(0))

    def _show_loading_placeholders(self) -> None:
        for body in (self.quota_body, self.reset_body, self.cost_body, self.sessions_body):
            self._clear(body)
            self._loading_row(body)

    def _render_quota(self, quota: QuotaSnapshot) -> None:
        self._clear(self.quota_body)
        self._meter_block(self.quota_body, "5小时", quota.primary, COLORS["green"])
        if quota.secondary:
            self._meter_block(self.quota_body, "每周", quota.secondary, COLORS["green"])
        for item in quota.additional_windows:
            self._meter_block(self.quota_body, self._window_name(item), item.window, COLORS["green"])
        if quota.credits_balance is not None:
            self._kv(self.quota_body, "Credits 余额", f"{quota.credits_balance:.2f}")

    def _render_resets(self, resets: ResetCreditsSnapshot) -> None:
        self._clear(self.reset_body)
        risk = reset_risk_summary(resets)
        row1 = tk.Frame(self.reset_body, bg=COLORS["panel"])
        row1.pack(fill="x")
        self._stat_tile(row1, "可用", str(risk.available), COLORS["green"]).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._stat_tile(row1, "即将过期", str(risk.expiring_soon), COLORS["yellow"]).pack(side="left", fill="x", expand=True, padx=(6, 0))
        row2 = tk.Frame(self.reset_body, bg=COLORS["panel"])
        row2.pack(fill="x", pady=(8, 0))
        self._stat_tile(row2, "总剩余", self._relative_future(risk.furthest_expiry), COLORS["blue"]).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._stat_tile(row2, "最近到期", self._relative_future(risk.nearest_expiry), COLORS["orange"]).pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._risk_bar(self.reset_body, risk.available, risk.expiring_soon, risk.unavailable)
        if not resets.credits:
            self._muted(self.reset_body, "接口没有返回重置次数。")
            return
        self._muted(self.reset_body, "重置次数详情")
        for index, credit in enumerate(resets.credits[:6], start=1):
            expiry_text = self._absolute_date(credit.expires_at)
            left_text = self._relative_future(credit.expires_at)
            text = f"{self._status_text(credit.status)}，{expiry_text}，剩余 {left_text}"
            self._kv(self.reset_body, f"次数 {index}", text)

    def _render_cost(self, usage: UsageSummary) -> None:
        self._clear(self.cost_body)
        top = tk.Frame(self.cost_body, bg=COLORS["panel"])
        top.pack(fill="x")
        self._stat_tile(top, "估算 API 成本", f"${usage.estimated_usd:.2f}", COLORS["green"]).pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 6),
        )
        self._stat_tile(top, "Tokens", self._compact_number(usage.total_tokens), COLORS["blue"]).pack(
            side="left",
            fill="x",
            expand=True,
            padx=(6, 0),
        )
        self._kv(self.cost_body, "轮数", str(usage.turns))
        self._kv(self.cost_body, "会话", str(usage.sessions))
        self._kv(self.cost_body, "价格表", usage.pricing_version)
        if usage.daily_rows:
            self._muted(self.cost_body, "近 7 日明细")
            self._daily_usage_table(self.cost_body, usage)
        for model, tokens in sorted(usage.by_model.items(), key=lambda item: item[1].total_tokens, reverse=True)[:4]:
            self._kv(self.cost_body, model, self._compact_number(tokens.total_tokens))

    def _render_sessions(self, sessions: list[SessionSummary]) -> None:
        self._clear(self.sessions_body)
        toolbar = tk.Frame(self.sessions_body, bg=COLORS["panel"])
        toolbar.pack(fill="x")
        count_text = f"{len(sessions)} 条最近会话" if sessions else "没有最近会话"
        tk.Label(toolbar, text=count_text, bg=COLORS["panel"], fg=COLORS["muted"], anchor="w").pack(side="left")
        label = "收起" if self.recent_expanded else "展开查看完整"
        self._button(toolbar, label, self.toggle_recent_sessions).pack(side="right")

        if not sessions:
            self._muted(self.sessions_body, "本机没有可展示的 Codex 会话。")
            return
        if not self.recent_expanded:
            self._folded_hint(sessions)
            return

        for session in sessions:
            self._session_row(self.sessions_body, session)

    def _render_settings(self) -> None:
        self._clear(self.settings_body)
        row = tk.Frame(self.settings_body, bg=COLORS["panel"])
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="自动刷新", bg=COLORS["panel"], fg=COLORS["muted"], anchor="w").pack(side="left")
        combo = ttk.Combobox(
            row,
            textvariable=self.auto_refresh_var,
            values=[AUTO_REFRESH_LABELS[minutes] for minutes in AUTO_REFRESH_CHOICES],
            state="readonly",
            width=10,
            style="Runway.TCombobox",
        )
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", self._on_auto_refresh_changed)
        tray_text = "关闭窗口后继续在托盘运行" if self.tray_enabled else "托盘已关闭"
        self._muted(self.settings_body, tray_text)
        tk.Label(
            self.settings_body,
            textvariable=self.tray_status_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(2, 0))

    def _on_auto_refresh_changed(self, _event: tk.Event | None) -> None:
        minutes = AUTO_REFRESH_BY_LABEL.get(self.auto_refresh_var.get(), DEFAULT_AUTO_REFRESH_MINUTES)
        self.settings = AppSettings(auto_refresh_minutes=minutes, close_to_tray=self.settings.close_to_tray)
        save_app_settings(self.settings_path, self.settings)
        self._schedule_auto_refresh()
        label = self._auto_refresh_label(minutes)
        self.status_var.set(f"自动刷新已设置为：{label}")

    def _schedule_auto_refresh(self) -> None:
        self._cancel_auto_refresh()
        if self.is_quitting:
            return
        minutes = self.settings.auto_refresh_minutes
        if minutes <= 0:
            return
        self.auto_refresh_after_id = self.root.after(minutes * 60 * 1000, self._auto_refresh_tick)

    def _cancel_auto_refresh(self) -> None:
        if not self.auto_refresh_after_id:
            return
        try:
            self.root.after_cancel(self.auto_refresh_after_id)
        except tk.TclError:
            pass
        self.auto_refresh_after_id = None

    def _auto_refresh_tick(self) -> None:
        self.auto_refresh_after_id = None
        if self.is_quitting:
            return
        self.refresh(show_errors=False)

    def toggle_recent_sessions(self) -> None:
        self.recent_expanded = not self.recent_expanded
        if self.snapshot:
            self._render_sessions(self.snapshot["recent_sessions"])

    def _folded_hint(self, sessions: list[SessionSummary]) -> None:
        box = tk.Frame(self.sessions_body, bg=COLORS["panel_soft"], padx=10, pady=9)
        box.pack(fill="x", pady=(8, 0))
        latest = sessions[0]
        tk.Label(
            box,
            text=f"已折叠，最新：{latest.project_name}",
            bg=COLORS["panel_soft"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            box,
            text="展开后显示完整标题、项目路径、Token 和估算成本。",
            bg=COLORS["panel_soft"],
            fg=COLORS["muted"],
            anchor="w",
            wraplength=420,
            justify="left",
        ).pack(fill="x", pady=(3, 0))

    def _session_row(self, parent: tk.Frame, session: SessionSummary) -> None:
        row = tk.Frame(parent, bg=COLORS["panel_soft"], padx=10, pady=8)
        row.pack(fill="x", pady=(0, 8))
        title = tk.Label(
            row,
            text=session.title,
            bg=COLORS["panel_soft"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=420,
        )
        title.pack(fill="x")
        meta = [
            session.project_name,
            self._state_text(session.state),
            f"{self._compact_number(session.total_tokens)} Tokens",
            f"${session.estimated_usd:.2f}",
        ]
        tk.Label(
            row,
            text=" · ".join(meta),
            bg=COLORS["panel_soft"],
            fg=COLORS["muted"],
            anchor="w",
            justify="left",
            wraplength=420,
        ).pack(fill="x", pady=(3, 0))
        if session.cwd:
            tk.Label(
                row,
                text=session.cwd,
                bg=COLORS["panel_soft"],
                fg=COLORS["dim"],
                anchor="w",
                justify="left",
                wraplength=420,
            ).pack(fill="x", pady=(3, 0))

    def _start_tray(self) -> None:
        if not self.tray_enabled:
            self.tray_status_var.set("托盘未启用")
            return
        if pystray is None or Image is None or ImageDraw is None:
            self.tray_status_var.set("托盘不可用：缺少 pystray / Pillow")
            return
        try:
            self.tray_icon = pystray.Icon(
                "CodexRunwayLite",
                self._create_tray_image(),
                self._tray_title(),
                pystray.Menu(
                    pystray.MenuItem("显示面板", self._tray_show, default=True),
                    pystray.MenuItem("立即刷新", self._tray_refresh),
                    pystray.MenuItem("打开 .codex", self._tray_open_codex),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出", self._tray_quit),
                ),
            )
            self.tray_thread = threading.Thread(target=self._run_tray_icon, daemon=True)
            self.tray_thread.start()
            self.tray_running = True
            self.tray_status_var.set("托盘已启用：关闭窗口后仍会后台刷新")
        except Exception as exc:  # noqa: BLE001 - tray should not block the main UI.
            self.tray_running = False
            self.tray_status_var.set(f"托盘启动失败：{exc}")

    def _run_tray_icon(self) -> None:
        if self.tray_icon is None:
            return
        try:
            self.tray_icon.run()
        except Exception:
            self.tray_running = False

    def _create_tray_image(self) -> Any:
        image = Image.new("RGBA", (64, 64), (*self._hex_to_rgb(COLORS["page"]), 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=COLORS["panel"], outline=COLORS["line"], width=2)
        draw.rectangle((18, 20, 46, 26), fill=COLORS["green"])
        draw.rectangle((18, 32, 38, 38), fill=COLORS["blue"])
        draw.rectangle((18, 44, 30, 50), fill=COLORS["orange"])
        return image

    def _tray_title(self) -> str:
        if not self.snapshot:
            return "Codex Runway\n正在加载"
        quota: QuotaSnapshot = self.snapshot["quota"]
        resets: ResetCreditsSnapshot = self.snapshot["reset_credits"]
        remaining = max(0, 100 - quota.primary.used_percent)
        return f"Codex Runway\n5小时剩余 {remaining}%\n重置次数 {resets.available_count}/{resets.total_count}"

    def _update_tray_title(self) -> None:
        if not self.tray_icon:
            return
        try:
            self.tray_icon.title = self._tray_title()
        except Exception:
            pass

    def _tray_show(self, _icon: Any = None, _item: Any = None) -> None:
        self.root.after(0, self.show_window)

    def _tray_refresh(self, _icon: Any = None, _item: Any = None) -> None:
        self.root.after(0, self.refresh)

    def _tray_open_codex(self, _icon: Any = None, _item: Any = None) -> None:
        self.root.after(0, self.open_codex_folder)

    def _tray_quit(self, _icon: Any = None, _item: Any = None) -> None:
        self.root.after(0, self.quit_app)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()
        if self.tray_icon and not self.hide_notice_shown:
            try:
                self.tray_icon.notify("Codex Runway 正在托盘继续运行", "Codex Runway")
            except Exception:
                pass
            self.hide_notice_shown = True

    def _on_close(self) -> None:
        if self.settings.close_to_tray and self.tray_running and not self.is_quitting:
            self.hide_window()
            return
        self.quit_app()

    def quit_app(self) -> None:
        self.is_quitting = True
        self._cancel_auto_refresh()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def export_status(self) -> None:
        if not self.snapshot:
            messagebox.showinfo("Codex Runway", "先刷新一次，再导出。")
            return
        output = Path.home() / ".codex-runway" / "status-lite.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.snapshot, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        self.status_var.set(f"已导出：{output}")

    def open_codex_folder(self) -> None:
        folder = default_codex_home()
        folder.mkdir(exist_ok=True)
        try:
            import os

            os.startfile(folder)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开 .codex 失败", str(exc))

    def _show_error(self, exc: Exception, show_dialog: bool = True) -> None:
        self._set_busy(False, f"刷新失败：{exc}")
        self._schedule_auto_refresh()
        if show_dialog:
            messagebox.showerror("刷新失败", str(exc))

    def _set_busy(self, busy: bool, text: str) -> None:
        self.refresh_in_progress = busy
        self.status_var.set(text)
        self.root.config(cursor="watch" if busy else "")
        if self.refresh_button:
            self.refresh_button.configure(state="disabled" if busy else "normal")

    def _meter_block(self, parent: tk.Frame, title: str, window: RateWindow, color: str) -> None:
        tk.Label(
            parent,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(4, 2))
        canvas = tk.Canvas(parent, height=14, bg=COLORS["panel"], highlightthickness=0)
        canvas.pack(fill="x")
        canvas.bind("<Configure>", lambda event, pct=window.used_percent, bar_color=color: self._draw_meter(event.widget, pct, bar_color))
        self._kv(parent, "剩余", f"{max(0, 100 - window.used_percent)}%，下次重置于 {self._relative_future(window.resets_at)}")
        self._muted(parent, self._consumption_text(window))

    def _draw_meter(self, canvas: tk.Canvas, used_percent: int, color: str) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = 10
        y = 2
        canvas.create_rectangle(0, y, width, y + height, fill=COLORS["panel_strong"], outline="", width=0)
        remaining_width = int(width * max(0, min(100, 100 - used_percent)) / 100)
        canvas.create_rectangle(0, y, remaining_width, y + height, fill=color, outline="", width=0)

    def _stat_tile(self, parent: tk.Frame, label: str, value: str, color: str) -> tk.Frame:
        tile = tk.Frame(parent, bg=COLORS["panel_soft"], padx=10, pady=9)
        tk.Label(tile, text=label, bg=COLORS["panel_soft"], fg=COLORS["muted"], anchor="w").pack(fill="x")
        tk.Label(
            tile,
            text=value,
            bg=COLORS["panel_soft"],
            fg=color,
            font=("Segoe UI", 13, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))
        return tile

    def _risk_bar(self, parent: tk.Frame, available: int, expiring: int, unavailable: int) -> None:
        total = max(available + unavailable, 1)
        tk.Label(
            parent,
            text="到期风险",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(10, 2))
        canvas = tk.Canvas(parent, height=14, bg=COLORS["panel"], highlightthickness=0)
        canvas.pack(fill="x")
        canvas.bind("<Configure>", lambda event: self._draw_risk_bar(event.widget, available, expiring, unavailable, total))
        self._muted(parent, f"可用 {available}，即将过期 {expiring}，不可用次数 {unavailable}")

    def _draw_risk_bar(self, canvas: tk.Canvas, available: int, expiring: int, unavailable: int, total: int) -> None:
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = 10
        y = 2
        green_width = int(width * max(0, available - expiring) / total)
        yellow_width = int(width * expiring / total)
        red_width = width - green_width - yellow_width
        x = 0
        for segment_width, color in (
            (green_width, COLORS["green"]),
            (yellow_width, COLORS["yellow"]),
            (red_width, COLORS["danger"] if unavailable else COLORS["panel_strong"]),
        ):
            if segment_width <= 0:
                continue
            canvas.create_rectangle(x, y, x + segment_width, y + height, fill=color, outline="", width=0)
            x += segment_width

    def _daily_usage_table(self, parent: tk.Frame, usage: UsageSummary) -> None:
        table = tk.Frame(parent, bg=COLORS["panel_soft"], padx=8, pady=6)
        table.pack(fill="x", pady=(4, 8))
        self._table_row(table, ("日期", "Tokens", "估算成本", "轮"), is_header=True)
        for row in usage.daily_rows[:7]:
            self._table_row(
                table,
                (
                    row.date,
                    self._compact_number(row.total_tokens),
                    f"${row.estimated_usd:.2f}",
                    str(row.turns),
                ),
            )

    def _table_row(self, parent: tk.Frame, values: tuple[str, str, str, str], is_header: bool = False) -> None:
        row = tk.Frame(parent, bg=COLORS["panel_soft"])
        row.pack(fill="x", pady=(0, 3))
        color = COLORS["muted"] if is_header else COLORS["text"]
        weight = "bold" if is_header else "normal"
        widths = (14, 10, 12, 5)
        for value, width in zip(values, widths, strict=True):
            tk.Label(
                row,
                text=value,
                bg=COLORS["panel_soft"],
                fg=color,
                font=("Segoe UI", 8, weight),
                width=width,
                anchor="w",
            ).pack(side="left")

    def _kv(self, parent: tk.Frame, label: str, value: str) -> None:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=COLORS["panel"], fg=COLORS["muted"], anchor="w").pack(side="left")
        tk.Label(row, text=value, bg=COLORS["panel"], fg=COLORS["text"], anchor="e").pack(side="right")

    def _button(self, parent: tk.Frame, text: str, command: Any) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=COLORS["button"],
            fg=COLORS["text"],
            activebackground=COLORS["button_hover"],
            activeforeground=COLORS["text"],
            relief="flat",
            bd=0,
            padx=10,
            pady=5,
            cursor="hand2",
        )
        return button

    def _muted(self, parent: tk.Frame, text: str) -> None:
        tk.Label(parent, text=text, bg=COLORS["panel"], fg=COLORS["muted"], anchor="w", justify="left").pack(fill="x")

    def _loading_row(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=COLORS["panel_soft"], padx=10, pady=10)
        row.pack(fill="x")
        tk.Label(row, text="正在加载...", bg=COLORS["panel_soft"], fg=COLORS["muted"], anchor="w").pack(fill="x")

    def _clear(self, parent: tk.Frame) -> None:
        for child in parent.winfo_children():
            child.destroy()

    def _account_text(self, account: dict[str, Any], quota: QuotaSnapshot) -> str:
        plan = self._plan_text(quota.plan)
        account_id = account.get("account_id") or "未知账号"
        return f"{plan} · {account_id}"

    def _plan_text(self, plan: str | None) -> str:
        if not plan:
            return "未知套餐"
        aliases = {
            "prolite": "Pro 5X",
            "promax": "Pro 20X",
            "plus": "Plus",
            "free": "Free",
        }
        return aliases.get(plan.lower(), plan)

    def _window_name(self, item: NamedRateWindow) -> str:
        key = item.name.lower()
        if "spark" in key:
            return "GPT-5.3-Codex-Spark"
        return item.name

    def _status_text(self, value: str) -> str:
        if value == "available":
            return "可用"
        if value == "used":
            return "已使用"
        return "未知"

    def _state_text(self, value: str) -> str:
        if value == "failed":
            return "失败"
        if value == "needs_attention":
            return "待处理"
        return "最近活跃"

    def _consumption_text(self, window: RateWindow) -> str:
        exhaustion = estimate_window_exhaustion(window)
        if window.used_percent <= 0:
            return "消耗速度：暂无明显消耗"
        if exhaustion is None:
            return "消耗速度：数据不足，暂不能估算"
        if window.resets_at and exhaustion >= window.resets_at:
            return "消耗速度：按当前速度不会提前耗尽"
        return f"消耗速度：预计耗尽于 {self._relative_future(exhaustion)}"

    def _absolute_date(self, value: datetime | None) -> str:
        if value is None:
            return "无到期时间"
        return value.astimezone().strftime("%Y/%m/%d %H:%M")

    def _relative_future(self, value: datetime | None) -> str:
        if value is None:
            return "未知"
        seconds = int((value - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            return "现在"
        minutes = seconds // 60
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        if days:
            return f"{days}天{hours}小时后"
        if hours:
            return f"{hours}小时{minutes}分钟后"
        return f"{minutes}分钟后"

    def _compact_number(self, value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.2f}K"
        return str(value)

    def _auto_refresh_label(self, minutes: int) -> str:
        return AUTO_REFRESH_LABELS.get(minutes, AUTO_REFRESH_LABELS[DEFAULT_AUTO_REFRESH_MINUTES])

    def _hex_to_rgb(self, color: str) -> tuple[int, int, int]:
        value = color.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
