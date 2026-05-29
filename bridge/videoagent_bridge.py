"""诊断桥接脚本 — 采集抖音/小红书账号数据

自包含实现，不依赖 videoagent 的任何内部代码。
依赖：MediaCrawler（外部安装）、playwright、yt-dlp、faster-whisper

用法：
  python bridge/videoagent_bridge.py search <platform> <keyword> [--min-likes 500]
  python bridge/videoagent_bridge.py fetch-creator <url> <platform>
  python bridge/videoagent_bridge.py analyze-video <url> <platform>
  python bridge/videoagent_bridge.py analyze-article <url> <platform>

输出：JSON 到 stdout

fetch-creator 支持的 URL 格式：
  抖音:  https://www.douyin.com/user/{sec_uid}
  小红书: https://www.xiaohongshu.com/user/profile/{user_id}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 确保 bridge 目录在路径中
_bridge_dir = Path(__file__).parent
if str(_bridge_dir) not in sys.path:
    sys.path.insert(0, str(_bridge_dir))

from mediacrawler_runner import MediaCrawlerRunner
from playwright_crawler import PlaywrightCrawler

# ── 浏览器配置目录发现（用于 Playwright 爬虫的登录态）──

def _find_douyin_profile() -> str | None:
    """查找抖音浏览器配置目录（含登录 cookies）

    优先级：环境变量 > videoagent 默认位置
    """
    env_path = os.getenv("DOUYIN_PROFILE_DIR")
    if env_path and Path(env_path).exists():
        return env_path
    va_path = Path.home() / "Desktop/videoagent/browser_profiles/dy"
    if va_path.exists():
        return str(va_path)
    return None

# ── URL 解析工具 ─────────────────────────────

DOUYIN_PROFILE_PREFIX = "https://www.douyin.com/user/"


def _extract_creator_id(url: str, platform: str) -> str:
    """从主页 URL 中提取创作者 ID，支持短链接自动解析"""
    url = url.split("?")[0]
    if platform in ("dy", "douyin"):
        if DOUYIN_PROFILE_PREFIX in url:
            return url.split(DOUYIN_PROFILE_PREFIX)[1].split("/")[0]
        if "v.douyin.com" in url:
            return _resolve_douyin_shortlink(url)
    elif platform in ("xhs", "xiaohongshu"):
        prefix = "https://www.xiaohongshu.com/user/profile/"
        if prefix in url:
            return url.split(prefix)[1].split("/")[0].split("?")[0]
    raise ValueError(f"无法从 URL 解析创作者 ID: {url}")


def _resolve_douyin_shortlink(url: str) -> str:
    """解析 v.douyin.com 短链接，从重定向 URL 中提取 sec_uid"""
    try:
        result = subprocess.run(
            ["curl", "-sI", "-o", "/dev/null", "-w", "%{redirect_url}", url],
            capture_output=True, text=True, timeout=10,
        )
        redirect_url = result.stdout.strip()
        if redirect_url:
            parsed = urlparse(redirect_url)
            params = parse_qs(parsed.query)
            sec_uid = params.get("sec_uid", [None])[0]
            if sec_uid:
                return sec_uid
            m = re.search(r"/share/user/([^/?]+)", redirect_url)
            if m:
                return m.group(1)
    except Exception:
        pass
    raise ValueError(f"无法解析抖音短链接: {url}")


# ── 数据标准化 ─────────────────────────────


def _standardize_videos(raw_list: list[dict]) -> list[dict]:
    """将 MediaCrawler/Playwright 原始数据转成统一格式"""
    videos = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        aweme_id = item.get("aweme_id", "") or ""
        if not aweme_id:
            continue

        # 兼容 MediaCrawler 和 Playwright Crawler 的字段名
        likes = int(item.get("liked_count", 0) or 0)
        comments = int(item.get("comment_count", 0) or 0)
        favorites = int(item.get("collected_count", 0) or 0)
        shares = int(item.get("share_count", 0) or 0)
        desc = item.get("desc", "") or item.get("title", "") or ""
        create_time = item.get("create_time", "") or ""
        aweme_type = str(item.get("aweme_type", "0") or "0")

        videos.append({
            "id": aweme_id,
            "title": desc[:200],
            "likes": likes,
            "comments": comments,
            "favorites": favorites,
            "shares": shares,
            "is_video": aweme_type != "2",
            "create_time": create_time,
        })
    return videos


def _build_result(
    videos: list[dict], nickname: str,
    follower_count: str, profile_desc: str, uid: str,
) -> dict:
    """构造统一格式的输出"""
    return {
        "status": "ok",
        "creator": {
            "nickname": (nickname or "")[:50],
            "follower_count": follower_count,
            "desc": profile_desc,
            "user_id": uid,
        },
        "videos": videos,
        "total_clean": len(videos),
    }


# ═══════════════════════════════════════════════════════
# 命令实现
# ═══════════════════════════════════════════════════════


def cmd_search(platform: str, keyword: str, min_likes: int = 500) -> list[dict]:
    """在指定平台搜索关键词，过滤 >=min_likes 的结果"""
    try:
        runner = MediaCrawlerRunner()
        raw = runner.search_keyword(platform, keyword, limit=50)
    except Exception as e:
        print(f"[bridge] MediaCrawler 搜索失败: {e}", file=sys.stderr)
        return []

    results = []
    for item in raw:
        likes = int(item.get("liked_count", 0) or 0)
        if likes < min_likes:
            continue

        item_id = item.get("aweme_id", "") or item.get("note_id", "") or ""
        is_video = True
        if platform in ("dy", "douyin"):
            aweme_type = item.get("aweme_type", 0)
            is_video = aweme_type != 2
            item_url = f"https://www.douyin.com/video/{item_id}"
        else:
            item_type = item.get("type", "video")
            is_video = item_type == "video"
            item_url = item.get("note_url", "") or f"https://www.xiaohongshu.com/explore/{item_id}"

        results.append({
            "id": item_id,
            "title": item.get("title", "") or item.get("desc", "") or "",
            "desc": item.get("desc", "") or "",
            "url": item_url,
            "author_name": item.get("nickname", ""),
            "author_id": item.get("user_id", ""),
            "likes": likes,
            "comments": int(item.get("comment_count", 0) or 0),
            "favorites": int(item.get("collected_count", 0) or 0),
            "shares": int(item.get("share_count", 0) or 0),
            "plays": int(item.get("play_count", 0) or 0),
            "is_video": is_video,
            "create_time": str(item.get("create_time", "")),
        })
    return results


def cmd_fetch_creator(url: str, platform: str) -> dict:
    """通过创作者主页 URL 获取全部作品数据

    双策略：
    1. MediaCrawler CLI（优先）— 支持完整分页
    2. Playwright 爬虫（兜底）— 约 32 条，但确保数据属于正确创作者
    """
    if platform not in ("dy", "douyin"):
        return {"status": "error", "error": f"暂不支持的平台: {platform}"}

    try:
        creator_id = _extract_creator_id(url, platform)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    # ── 策略 1: MediaCrawler CLI ──
    try:
        runner = MediaCrawlerRunner()
        mc_results = runner.search_creator(platform, creator_id)
    except Exception as e:
        print(f"[bridge] MediaCrawler CLI 异常: {e}", file=sys.stderr)
        mc_results = []

    # 校验数据是否匹配请求的 creator_id
    mc_valid = False
    mc_nickname = ""
    if mc_results and len(mc_results) > 0:
        mc_sec_uid = str(mc_results[0].get("sec_uid", "") or "")
        mc_nickname = str(mc_results[0].get("nickname", "") or "")
        if mc_sec_uid and (mc_sec_uid == creator_id or creator_id in mc_sec_uid or mc_sec_uid in creator_id):
            mc_valid = True

    if mc_valid and mc_results:
        videos = _standardize_videos(mc_results)
        profile = mc_results[0].get("_profile", {}) or {}
        follower_count = profile.get("follower_count", "0") or "0"
        profile_desc = profile.get("desc", "") or ""
        return _build_result(videos, mc_nickname, follower_count, profile_desc, creator_id)

    # ── 策略 2: Playwright 爬虫兜底（约 32 条但数据正确）──
    try:
        dy_profile = _find_douyin_profile()
        if dy_profile:
            print(f"[bridge] Playwright: 使用已有浏览器配置（{dy_profile}）", file=sys.stderr)
        else:
            print(f"[bridge] Playwright: 无登录态（配置 DOUYIN_PROFILE_DIR 环境变量可传入）", file=sys.stderr)
        crawler = PlaywrightCrawler(persistent_profile_dir=dy_profile)
        cb_result = crawler.fetch_creator_data(creator_id)
    except Exception as e:
        print(f"[bridge] Playwright 爬虫异常: {e}", file=sys.stderr)
        cb_result = {"status": "error", "error": str(e)}

    if cb_result.get("status") == "ok" and cb_result.get("videos"):
        raw_videos = cb_result["videos"]
        videos = _standardize_videos(raw_videos)
        profile = cb_result.get("profile", {})
        nickname = profile.get("nickname", "") or ""
        follower_count = profile.get("follower_count", "0") or "0"
        profile_desc = profile.get("desc", "") or ""
        return _build_result(videos, nickname, follower_count, profile_desc, creator_id)

    # ── 全部失败 ──
    error_msg = cb_result.get("error", "") or "MediaCrawler + Playwright 均失败"
    return {"status": "error", "error": error_msg}


def cmd_analyze_video(url: str, platform: str) -> dict:
    """深度分析单个视频（下载 -> 转写 -> 多模态分析）"""
    try:
        from bridge.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        result = analyzer.analyze(url=url, note_id="bridge_" + url.split("/")[-1][:20], platform=platform)
        return result.model_dump()
    except Exception as e:
        return {"error": str(e)}


def cmd_analyze_article(url: str, platform: str) -> dict:
    """分析图文内容（目前返回该平台的搜索结果）"""
    try:
        runner = MediaCrawlerRunner()
        raw = runner.search_keyword(platform, "", limit=5)
        return {"status": "ok", "data": raw[:3] if raw else []}
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════


def main():
    if len(sys.argv) < 3:
        print("用法:", file=sys.stderr)
        print(f"  {sys.argv[0]} search <platform> <keyword> [--min-likes N]", file=sys.stderr)
        print(f"  {sys.argv[0]} fetch-creator <url> <platform>", file=sys.stderr)
        print(f"  {sys.argv[0]} analyze-video <url> <platform>", file=sys.stderr)
        print(f"  {sys.argv[0]} analyze-article <url> <platform>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "search":
        platform = sys.argv[2]
        keyword = sys.argv[3]
        min_likes = 500
        if "--min-likes" in sys.argv:
            idx = sys.argv.index("--min-likes")
            if idx + 1 < len(sys.argv):
                min_likes = int(sys.argv[idx + 1])
        results = cmd_search(platform, keyword, min_likes)
        print(json.dumps(results, ensure_ascii=False))

    elif command == "fetch-creator":
        url = sys.argv[2]
        platform = sys.argv[3]
        result = cmd_fetch_creator(url, platform)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "analyze-video":
        url = sys.argv[2]
        platform = sys.argv[3]
        result = cmd_analyze_video(url, platform)
        print(json.dumps(result, ensure_ascii=False))

    elif command == "analyze-article":
        url = sys.argv[2]
        platform = sys.argv[3]
        result = cmd_analyze_article(url, platform)
        print(json.dumps(result, ensure_ascii=False))

    else:
        print(f"未知命令: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
