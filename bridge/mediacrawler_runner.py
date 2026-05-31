"""MediaCrawler CLI 直接调用器

不依赖 videoagent 的 mediacrawler_bridge，直接 subprocess 调用 MediaCrawler CLI，
自己读取 JSONL 输出并按 creator_id 过滤，避免缓存返回错误账号的数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_PLATFORM_DIR: dict[str, str] = {"dy": "douyin"}


def _parse_count(val: str | int | None) -> int:
    """解析数字值，支持 "10万+" "6.8万" 等中文格式"""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return 0
    s = s.replace("+", "").strip()
    if "万" in s:
        try:
            return int(float(s.replace("万", "")) * 10000)
        except (ValueError, TypeError):
            return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


class MediaCrawlerRunner:
    """MediaCrawler CLI 调用器

    用法:
        runner = MediaCrawlerRunner()
        videos = runner.search_creator("dy", "MS4wLjABAAAA...")
        results = runner.search_keyword("dy", "关键词", limit=50)
    """

    def __init__(self, repo_path: str = ""):
        self.repo_path = repo_path or os.getenv("MEDIACRAWLER_PATH", "")
        if not self.repo_path:
            # 默认位置：与 diagnosis 同级目录
            self.repo_path = str(Path(__file__).parent.parent.parent / "MediaCrawler")

    def _python(self) -> str:
        """MediaCrawler venv 中的 python 路径"""
        venv = Path(self.repo_path) / "venv" / "bin" / "python"
        return str(venv) if venv.exists() else "python3"

    def search_creator(self, platform: str, creator_id: str) -> list[dict]:
        """获取创作者全部视频

        关键改进：自己读取 JSONL 输出并按 creator_id 过滤，
        不依赖 mediacrawler_bridge._read_latest_output（那个不做 creator_id 过滤）。

        先检查当日缓存是否有效（文件存在 + creator_id 匹配），
        匹配时跳过 CLI 直接读缓存，避免不必要的新鲜抓取。

        Args:
            platform: "dy"
            creator_id: 抖音 sec_uid

        Returns:
            [{aweme_id, desc, liked_count, comment_count, collected_count, share_count, ...}]
        """
        plat_flag = {"dy": "dy", "douyin": "dy"}.get(platform, platform)
        plat_dir_name = _PLATFORM_DIR.get(plat_flag, plat_flag)

        data_dir = Path(self.repo_path) / "data" / plat_dir_name / "jsonl"
        data_dir.mkdir(parents=True, exist_ok=True)

        # 1. 检查当日缓存是否有效（文件存在 + creator_id 匹配）
        today = datetime.now().strftime("%Y-%m-%d")
        cache_files = sorted(data_dir.glob(f"creator_contents_{today}.jsonl"),
                             key=lambda f: f.stat().st_mtime, reverse=True)
        use_cache = False
        if cache_files and cache_files[0].stat().st_size > 100:
            first_line = cache_files[0].open(encoding="utf-8").readline().strip()
            if first_line:
                try:
                    cached = json.loads(first_line)
                    cached_sec = str(cached.get("sec_uid", "") or "")
                    cached_uid = str(cached.get("user_id", "") or "")
                    if (cached_sec and (cached_sec == creator_id or creator_id in cached_sec or cached_sec in creator_id)) or \
                       (cached_uid and (cached_uid == creator_id or creator_id in cached_uid or cached_uid in creator_id)):
                        use_cache = True
                except json.JSONDecodeError:
                    pass

        if use_cache:
            items = self._read_jsonl_with_filter(cache_files[0], creator_id)
            if items:
                return self._enrich_items(items, creator_id, plat_flag)

        # 2. 缓存不匹配或不存在 → 运行 CLI（60 秒超时，失败就跳过）
        cmd = [
            self._python(), "main.py",
            "--platform", plat_flag,
            "--lt", "qrcode",
            "--type", "creator",
            "--creator_id", creator_id,
            "--max_comments_count_singlenotes", "0",
            "--save_data_option", "jsonl",
        ]
        try:
            result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                print(f"  [MediaCrawlerRunner] CLI 返回错误码 {result.returncode}: {result.stderr.strip()[:200]}", file=sys.stderr)
                # CLI 失败时不再读旧缓存，返回空结果
                return []
        except subprocess.TimeoutExpired:
            # CLI 超时（通常是因为需要扫码登录），不阻塞，尝试读已有缓存
            print(f"  [MediaCrawlerRunner] CLI 超时(600s)，尝试读已有缓存", file=sys.stderr)
        except Exception as e:
            print(f"  [MediaCrawlerRunner] CLI 异常: {e}", file=sys.stderr)

        # 3. 读取 CLI 运行后的输出（新文件或已有缓存）
        new_files = sorted(data_dir.glob("creator_contents_*.jsonl"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
        if not new_files:
            return []

        items = self._read_jsonl_with_filter(new_files[0], creator_id)
        return self._enrich_items(items, creator_id, plat_flag)

    def _read_jsonl_with_filter(self, file_path: Path, creator_id: str) -> list[dict]:
        """从 JSONL 文件读取并按 creator_id 过滤"""
        items: list[dict] = []
        for line in file_path.open(encoding="utf-8").readlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                # 优先用 sec_uid 匹配（因为我们通常用 sec_uid 搜索）
                item_sec = str(item.get("sec_uid", "") or "")
                item_uid = str(item.get("user_id", "") or "")
                if (item_sec and (item_sec == creator_id or creator_id in item_sec or item_sec in creator_id)) or \
                   (item_uid and (item_uid == creator_id or creator_id in item_uid or item_uid in creator_id)):
                    items.append(item)
            except json.JSONDecodeError:
                continue
        return items

    def _enrich_items(self, items: list[dict], creator_id: str, platform: str) -> list[dict]:
        """标准化数字字段并附加创作者粉丝数"""
        for item in items:
            for field in ("liked_count", "comment_count", "collected_count", "share_count", "video_play_count"):
                if field in item:
                    item[field] = _parse_count(item[field])

        profile = self._read_creator_profile(platform, creator_id)
        if profile and items:
            items[0]["_profile"] = profile

        return items

    def _read_creator_profile(self, platform: str, creator_id: str) -> dict[str, Any] | None:
        """按 creator_id 匹配读取创作者粉丝数、简介"""
        plat_dir_name = _PLATFORM_DIR.get(platform, platform)
        data_dir = Path(self.repo_path) / "data" / plat_dir_name / "jsonl"
        files = sorted(data_dir.glob("creator_creators_*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return None
        for line in files[0].open(encoding="utf-8").readlines():
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
                p_sec = str(p.get("sec_uid", "") or "")
                p_uid = str(p.get("user_id", "") or "")
                if (p_sec and (p_sec == creator_id or creator_id in p_sec or p_sec in creator_id)) or \
                   (p_uid and (p_uid == creator_id or creator_id in p_uid or p_uid in creator_id)):
                    return {
                        "follower_count": str(p.get("fans", 0) or 0),
                        "desc": p.get("desc", "") or "",
                        "nickname": p.get("nickname", "") or "",
                    }
            except json.JSONDecodeError:
                continue
        return None

    def search_keyword(self, platform: str, keyword: str, limit: int = 20) -> list[dict]:
        """关键词搜索

        Args:
            platform: "dy"
            keyword: 搜索关键词
            limit: 最大返回条数

        Returns:
            搜索结果列表
        """
        plat_flag = {"dy": "dy", "douyin": "dy"}.get(platform, platform)
        plat_dir_name = _PLATFORM_DIR.get(plat_flag, plat_flag)

        cmd = [
            self._python(), "main.py",
            "--platform", plat_flag,
            "--lt", "qrcode",
            "--type", "search",
            "--keywords", keyword,
            "--max_comments_count_singlenotes", "0",
            "--save_data_option", "jsonl",
        ]
        result = subprocess.run(cmd, cwd=self.repo_path, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"  [MediaCrawlerRunner] search CLI 返回错误码 {result.returncode}: {result.stderr.strip()[:200]}", file=sys.stderr)
            return []

        data_dir = Path(self.repo_path) / "data" / plat_dir_name / "jsonl"
        files = sorted(data_dir.glob("search_contents_*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return []

        items: list[dict] = []
        for line in files[0].open(encoding="utf-8").readlines()[-100:]:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        for item in items:
            for field in ("liked_count", "comment_count", "collected_count", "share_count", "video_play_count"):
                if field in item:
                    item[field] = _parse_count(item[field])
        return items[:limit]
