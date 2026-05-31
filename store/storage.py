"""账号分析数据持久化存储

每个账号的数据存储在 store/accounts/{account_name}/ 下：

  raw_data/
    creator_{date}.json        # fetch-creator 原始返回（含 profile）
    videos_{date}.json         # 标准化视频列表（去重后）

  analysis/
    transcripts/               # 音频转写结果（每条视频一个 .txt）
    video_analysis/            # 视频多模态分析结果（每条视频一个 .json）

  reports/
    diagnosis_{date}.html      # 最终诊断报告

用法:
    from store.storage import AccountStorage
    storage = AccountStorage("君月呀")
    storage.save_raw_data(result_dict)
    storage.save_videos(video_list)
    storage.save_transcript("video_123", "转写文本...")
    storage.save_report("<html>...</html>")
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


_STORE_ROOT = Path(__file__).parent


def _sanitize_name(name: str) -> str:
    """清理账号名，去掉特殊字符，用作目录名"""
    name = re.sub(r"[^一-龥a-zA-Z0-9_\-]", "", name.strip())
    return name or "unknown"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class AccountStorage:
    """账号数据持久化存储"""

    def __init__(self, account_name: str):
        safe = _sanitize_name(account_name)
        self.root = _STORE_ROOT / "accounts" / safe
        self._ensure_dirs()

    def _ensure_dirs(self):
        for sub in ("raw_data", "analysis/transcripts", "analysis/video_analysis", "reports"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ── raw_data ─────────────────────────────────

    def save_raw_data(self, data: dict[str, Any]) -> Path:
        """保存 fetch-creator 原始返回数据"""
        path = self.root / "raw_data" / f"creator_{_today()}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_videos(self, videos: list[dict]) -> Path:
        """保存标准化视频列表（去重后）"""
        path = self.root / "raw_data" / f"videos_{_today()}.json"
        path.write_text(json.dumps(videos, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ── analysis ─────────────────────────────────

    def save_transcript(self, video_id: str, transcript: str) -> Path:
        """保存单条视频的音频转写文本"""
        path = self.root / "analysis" / "transcripts" / f"{video_id}.txt"
        path.write_text(transcript, encoding="utf-8")
        return path

    def save_video_analysis(self, video_id: str, analysis: dict[str, Any]) -> Path:
        """保存单条视频的多模态分析结果"""
        path = self.root / "analysis" / "video_analysis" / f"{video_id}.json"
        path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ── reports ─────────────────────────────────

    def save_report(self, html_content: str, filename: str = "") -> Path:
        """保存 HTML 诊断报告"""
        fn = filename or f"diagnosis_{_today()}.html"
        path = self.root / "reports" / fn
        path.write_text(html_content, encoding="utf-8")
        return path

    # ── listing ─────────────────────────────────

    def list_stored_data(self) -> dict[str, list[str]]:
        """列出该账号已持久化的数据清单"""
        result = {}
        for subdir, glob_pat in [
            ("raw_data", "*.json"),
            ("analysis/transcripts", "*.txt"),
            ("analysis/video_analysis", "*.json"),
            ("reports", "*.html"),
        ]:
            files = sorted((self.root / subdir).glob(glob_pat))
            result[subdir] = [f.name for f in files]
        return result
