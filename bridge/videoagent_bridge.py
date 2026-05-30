"""诊断桥接脚本 — 采集抖音账号数据

自包含实现，不依赖 videoagent 的任何内部代码。
依赖：MediaCrawler（外部安装）、yt-dlp、faster-whisper

用法：
  python bridge/videoagent_bridge.py search <platform> <keyword> [--min-likes 500]
  python bridge/videoagent_bridge.py fetch-creator <url> <platform>
  python bridge/videoagent_bridge.py analyze-video <url> <platform>
  python bridge/videoagent_bridge.py analyze-article <url> <platform>

输出：JSON 到 stdout

fetch-creator 支持的 URL 格式：
  抖音:  https://www.douyin.com/user/{sec_uid}
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 确保 bridge 目录在路径中
_bridge_dir = Path(__file__).parent
if str(_bridge_dir) not in sys.path:
    sys.path.insert(0, str(_bridge_dir))

from mediacrawler_runner import MediaCrawlerRunner

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


def _to_date(val) -> str:
    """将 Unix 时间戳或日期字符串转为 YYYY-MM-DD"""
    if not val:
        return ""
    if isinstance(val, (int, float)) and val > 1000000000:
        return datetime.fromtimestamp(val).strftime("%Y-%m-%d")
    s = str(val).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s
    return ""


def _standardize_videos(raw_list: list[dict]) -> list[dict]:
    """将 MediaCrawler 原始数据转成统一格式（按 aweme_id 去重）"""
    seen: set[str] = set()
    videos = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        aweme_id = item.get("aweme_id", "") or ""
        if not aweme_id or aweme_id in seen:
            continue
        seen.add(aweme_id)

        # 兼容 MediaCrawler 和 Playwright Crawler 的字段名
        likes = int(item.get("liked_count", 0) or 0)
        comments = int(item.get("comment_count", 0) or 0)
        favorites = int(item.get("collected_count", 0) or 0)
        shares = int(item.get("share_count", 0) or 0)
        desc = item.get("desc", "") or item.get("title", "") or ""
        create_time = _to_date(item.get("create_time", "") or "")
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

    调用 MediaCrawler CLI 采集完整主页作品列表。
    优先读缓存，缓存不命中时自动触发 CLI 采集。
    """
    if platform not in ("dy", "douyin"):
        return {"status": "error", "error": f"暂不支持的平台: {platform}"}

    try:
        creator_id = _extract_creator_id(url, platform)
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    try:
        runner = MediaCrawlerRunner()
        mc_results = runner.search_creator(platform, creator_id)
    except Exception as e:
        return {"status": "error", "error": f"MediaCrawler 采集失败: {e}"}

    if not mc_results:
        return {"status": "error", "error": "MediaCrawler 未返回任何作品数据"}

    videos = _standardize_videos(mc_results)
    mc_nickname = str(mc_results[0].get("nickname", "") or "")
    profile = mc_results[0].get("_profile", {}) or {}
    follower_count = profile.get("follower_count", "0") or "0"
    profile_desc = profile.get("desc", "") or ""
    return _build_result(videos, mc_nickname, follower_count, profile_desc, creator_id)


def cmd_analyze_video(url: str, platform: str) -> dict:
    """深度分析单个视频（下载 -> 转写 -> 多模态分析）"""
    try:
        from bridge.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        result = analyzer.analyze(url=url, note_id="bridge_" + url.split("/")[-1][:20], platform=platform)
        return result.model_dump()
    except Exception as e:
        return {"error": str(e)}


def cmd_extract_audio(url: str, platform: str) -> dict:
    """轻量音频提取+转写（下载视频→ffmpeg提音频→whisper转写，不做多模态分析）

    用法:
        python3 bridge/videoagent_bridge.py extract-audio <url> <platform>
    """
    from video_analyzer import VideoDownloader, AudioVisualProcessor, SpeechTranscriber
    from pathlib import Path

    work_dir = "output/audio_extract"
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    work_dir_path = Path(work_dir)
    note_id = "audio_" + url.split("/")[-1][:20]

    plat_map = {"dy": "douyin"}
    plat = plat_map.get(platform, platform)

    # 先尝试 yt-dlp 直接下载音频
    audio_file = work_dir_path / f"{note_id}.mp3"
    try:
        subprocess.run([
            "yt-dlp", "-f", "bestaudio", "-x", "--audio-format", "mp3",
            "-o", str(audio_file), "--max-filesize", "100M",
            "--cookies-from-browser", "chrome",
            "--no-playlist", "--no-warnings", "--force-ipv4", url,
        ], capture_output=True, text=True, timeout=120)
    except Exception:
        audio_file = None

    if audio_file and audio_file.exists() and audio_file.stat().st_size > 1000:
        try:
            transcriber = SpeechTranscriber(model="small")
            transcript = transcriber.transcribe(audio_file)
            duration = 0
            return {"url": url, "platform": platform, "duration_seconds": duration, "transcript": transcript, "status": "ok"}
        except Exception as e:
            return {"url": url, "status": "error", "error": f"转写失败: {e}"}
        finally:
            try:
                if audio_file and audio_file.exists():
                    audio_file.unlink()
            except OSError:
                pass

    # 降级：下载完整视频 → ffmpeg 提音频 → whisper 转写
    downloader = VideoDownloader(work_dir=work_dir, timeout=120, max_seconds=600)
    av = AudioVisualProcessor(work_dir=work_dir)
    transcriber = SpeechTranscriber(model="small")
    video_path = None
    audio_path = None
    try:
        video_path = downloader.download(url, note_id, platform=plat)
        audio_path = work_dir_path / f"{note_id}_audio.wav"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_path),
        ], capture_output=True, timeout=300)
        duration = int(av.get_duration(video_path))
        transcript = ""
        if audio_path and audio_path.exists() and audio_path.stat().st_size > 1000:
            transcript = transcriber.transcribe(audio_path)
        return {"url": url, "platform": platform, "duration_seconds": duration, "transcript": transcript, "status": "ok"}
    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}
    finally:
        if video_path:
            downloader.cleanup(video_path)
        if audio_path and audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass


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
        print(f"  {sys.argv[0]} extract-audio <url> <platform>", file=sys.stderr)
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

    elif command == "extract-audio":
        url = sys.argv[2]
        platform = sys.argv[3]
        result = cmd_extract_audio(url, platform)
        print(json.dumps(result, ensure_ascii=False))

    else:
        print(f"未知命令: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
