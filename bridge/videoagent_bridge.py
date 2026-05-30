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

# 持久化存储
_bridge_store_dir = _bridge_dir.parent / "store"
if str(_bridge_store_dir) not in sys.path:
    sys.path.insert(0, str(_bridge_store_dir))
from store.storage import AccountStorage

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


def _assess_data_sufficiency(videos: list[dict]) -> dict:
    """评估抓取数据的覆盖充分性

    从视频列表的时间跨度判断是否可能遗漏了早期内容。
    返回: { video_count, date_range, time_span_days, gap_count, flag }
    """
    if not videos:
        return {"flag": "empty", "video_count": 0, "date_range": "", "time_span_days": 0, "gap_count": 0}

    dates = sorted(
        v["create_time"] for v in videos
        if v.get("create_time") and len(str(v.get("create_time", ""))) >= 10
    )
    if len(dates) < 2:
        return {"flag": "insufficient_date_info", "video_count": len(videos)}

    earliest = dates[0]
    latest = dates[-1]
    span = (datetime.strptime(latest, "%Y-%m-%d") - datetime.strptime(earliest, "%Y-%m-%d")).days
    span = max(span, 1)

    months_in_range = (span // 30) + 1
    months_with_videos = len(set(d[:7] for d in dates))
    gap_count = months_in_range - months_with_videos
    avg_per_month = round(len(videos) / months_in_range, 1)

    # 判定旗标
    if span < 30 and len(videos) < 30:
        flag = "partial_very_short_span"
    elif gap_count > months_in_range * 0.5:
        flag = "partial_large_gaps"
    elif months_in_range > 12 and avg_per_month < 3:
        flag = "partial_low_density"
    else:
        flag = "ok"

    return {
        "flag": flag,
        "video_count": len(videos),
        "date_range": f"{earliest} ~ {latest}",
        "time_span_days": span,
        "avg_per_month": avg_per_month,
        "gap_count": gap_count,
    }


def _build_result(
    videos: list[dict], nickname: str,
    follower_count: str, profile_desc: str, uid: str,
) -> dict:
    """构造统一格式的输出，含数据充分性评估"""
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
        "data_sufficiency": _assess_data_sufficiency(videos),
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
    自动将数据持久化到 store/accounts/{nickname}/
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

    result = _build_result(videos, mc_nickname, follower_count, profile_desc, creator_id)

    # 持久化存储
    if mc_nickname:
        try:
            storage = AccountStorage(mc_nickname)
            storage.save_raw_data({
                "creator_id": creator_id,
                "nickname": mc_nickname,
                "follower_count": follower_count,
                "desc": profile_desc,
                "mc_results_count": len(mc_results),
                "videos_deduped": len(videos),
                "data_sufficiency": result.get("data_sufficiency", {}),
            })
            storage.save_videos(videos)
            result["_stored_at"] = str(storage.root)
        except Exception as e:
            result["_storage_error"] = str(e)

    return result


def cmd_analyze_video(url: str, platform: str, account_name: str = "") -> dict:
    """深度分析单个视频（下载 -> 转写 -> 多模态分析）

    如果指定 account_name，自动将结果持久化到 store/accounts/{account_name}/analysis/
    """
    try:
        from bridge.video_analyzer import VideoAnalyzer

        analyzer = VideoAnalyzer()
        result = analyzer.analyze(url=url, note_id="bridge_" + url.split("/")[-1][:20], platform=platform)
        data = result.model_dump()

        # 持久化存储
        if account_name:
            try:
                storage = AccountStorage(account_name)
                video_id = url.split("/")[-1][:30]
                if data.get("transcript"):
                    storage.save_transcript(video_id, data["transcript"])
                storage.save_video_analysis(video_id, data)
                data["_stored_at"] = str(storage.root / "analysis")
            except Exception as e:
                data["_storage_error"] = str(e)

        return data
    except Exception as e:
        return {"error": str(e)}


def cmd_extract_audio(url: str, platform: str, account_name: str = "") -> dict:
    """轻量音频提取+转写（下载视频→ffmpeg提音频→whisper转写，不做多模态分析）

    如果指定 account_name，自动将转写结果持久化到 store/accounts/{account_name}/analysis/

    用法:
        python3 bridge/videoagent_bridge.py extract-audio <url> <platform> [account_name]
    """
    from video_analyzer import VideoDownloader, AudioVisualProcessor, SpeechTranscriber
    from pathlib import Path

    def _persist(result: dict) -> dict:
        if account_name and result.get("status") == "ok" and result.get("transcript"):
            try:
                storage = AccountStorage(account_name)
                video_id = url.split("/")[-1][:30]
                storage.save_transcript(video_id, result["transcript"])
                storage.save_video_analysis(video_id, result)
                result["_stored_at"] = str(storage.root / "analysis")
            except Exception as e:
                result["_storage_error"] = str(e)
        return result

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
            return _persist({"url": url, "platform": platform, "duration_seconds": 0, "transcript": transcript, "status": "ok"})
        except Exception as e:
            return _persist({"url": url, "status": "error", "error": f"转写失败: {e}"})
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
        return _persist({"url": url, "platform": platform, "duration_seconds": duration, "transcript": transcript, "status": "ok"})
    except Exception as e:
        return _persist({"url": url, "status": "error", "error": str(e)})
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
