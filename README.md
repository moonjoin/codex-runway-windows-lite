# Codex Runway Lite for Windows

非官方 Windows 轻量版原型，参考 [Licoy/codex-runway](https://github.com/Licoy/codex-runway) 的工作思路，把本机 Codex 配额、reset credits 和会话用量整理成一个中文桌面小面板。

## 功能

- 读取 `%USERPROFILE%\.codex\auth.json`，只读，不写回凭证。
- 查询 ChatGPT/Codex 的 quota 和 reset credits 接口。
- 扫描本机 `%USERPROFILE%\.codex\sessions` 和 `archived_sessions`。
- 展示 5 小时、每周和额外模型窗口的剩余额度。
- 基于现有数据反推消耗速度、预计耗尽时间、reset credits 到期风险。
- 按近 7 天本机会话估算 API 等价成本、tokens、轮数和模型分布。
- 最近会话默认折叠，展开后显示完整标题、项目路径、tokens 和估算成本。
- 支持导出脱敏快照到 `%USERPROFILE%\.codex-runway\status-lite.json`。

## 不做什么

- 不修改 `.codex/auth.json`。
- 不上传本机会话内容。
- 不等同于真实账单，界面里的成本是“API 等价成本估算”。
- 目前还没有托盘、安装包、自动更新。

<img width="518" height="1278" alt="screenshot-20260703-162836" src="https://github.com/user-attachments/assets/985db451-e089-4c31-bc64-57e562810056" />


## 运行

需要 Windows 和 Python 3.11+。运行界面：

```powershell
python run.py
```

或者双击：

```text
start-windows-lite.bat
```

自检：

```powershell
python run.py --self-check
```

## 测试

```powershell
python -m pytest tests -q
```

## 打包 EXE

```powershell
.\build-exe.ps1
```

打包产物：

```text
dist\CodexRunwayLite.exe
```

## 许可证

本项目按 AGPL-3.0 发布。原始项目许可证同为 AGPL-3.0。
