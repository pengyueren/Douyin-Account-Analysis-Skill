"""批量音频提取+转写 — 研究复现脚本，非核心 CLI

从 batch_extract.json 读取视频列表，逐个下载 → 提取音频 → 转写。

用法:
    python3 distillation/batch_audio_extract.py [--max N]

依赖（需额外安装）:
    pip install requests browser-cookie3

输入: distillation/raw_data/douyin/batch_extract.json
输出: distillation/raw_data/douyin/transcripts/

注意: 此脚本是知识蒸馏阶段的研究工具，不属于核心 CLI 命令。
      核心 CLI 请使用 bridge/videoagent_bridge.py extract-audio。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from browser_cookie3 import chrome

BASE_DIR = Path(__file__).parent.parent
WORK_DIR = BASE_DIR / "output" / "audio_extract"
RESULT_DIR = BASE_DIR / "distillation" / "raw_data" / "douyin" / "transcripts"
WORK_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)


def _get_session():
    """获取带 douyin cookies 的 requests session"""
    cj = chrome(domain_name='douyin.com')
    session = requests.Session()
    for c in cj:
        session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://www.douyin.com/',
    })
    return session


def get_cdn_url(download_url: str, session: requests.Session) -> str | None:
    """从 aweme/v1/play/ URL 获取 CDN 跳转链接"""
    try:
        resp = session.get(download_url, timeout=15, allow_redirects=False)
        if resp.status_code in (301, 302):
            return resp.headers.get('Location', '')
    except Exception as e:
        print(f"    [CDN解析失败] {e}")
    return None


def download_and_transcribe(download_url: str, note_id: str, session: requests.Session) -> dict:
    """下载视频 → 提取音频 → 转写，返回结果"""
    cdn_url = get_cdn_url(download_url, session)
    if not cdn_url:
        return {"status": "error", "error": "cdn_url_failed"}

    # 下载视频到临时文件
    tmp_video = WORK_DIR / f"{note_id}_video.mp4"
    try:
        resp = session.get(cdn_url, stream=True, timeout=60)
        total = 0
        with open(tmp_video, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    except Exception as e:
        return {"status": "error", "error": f"download_failed: {e}"}

    # ffmpeg 提取音频
    audio_path = WORK_DIR / f"{note_id}_audio.wav"
    try:
        subprocess.run([
            'ffmpeg', '-y', '-i', str(tmp_video),
            '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
            str(audio_path),
        ], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {"status": "error", "error": f"ffmpeg_failed: {e}"}
    finally:
        try:
            tmp_video.unlink()
        except OSError:
            pass

    # whisper 转写
    if not audio_path.exists() or audio_path.stat().st_size < 1000:
        return {"status": "error", "error": "audio_too_small"}

    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), language="zh", beam_size=5, vad_filter=True)
        transcript = "".join(seg.text.strip() for seg in segments)
        return {"status": "ok", "transcript": transcript}
    except Exception as e:
        return {"status": "error", "error": f"whisper_failed: {e}"}
    finally:
        try:
            audio_path.unlink()
        except OSError:
            pass


def main():
    max_videos = 116
    for arg in sys.argv[1:]:
        if arg.startswith("--max="):
            max_videos = int(arg.split("=")[1])

    # 读取视频列表
    batch_file = BASE_DIR / "distillation" / "raw_data" / "douyin" / "batch_extract.json"
    all_videos = json.loads(batch_file.read_text())
    videos = all_videos[:max_videos]
    print(f"总共 {len(all_videos)} 个视频, 本次处理 {len(videos)} 个")

    session = _get_session()

    ok_count = 0
    err_count = 0
    skip_count = 0

    for i, video in enumerate(videos):
        video_id = video.get('id', '')
        download_url = video.get('download_url', '')

        # 跳过已处理的
        result_file = RESULT_DIR / f"{video_id}.json"
        if result_file.exists():
            skip_count += 1
            if i % 10 == 0:
                print(f"[{i+1}/{len(videos)}] ⏭ {video.get('title', '')[:50]}")
            continue

        print(f"[{i+1}/{len(videos)}] {video.get('title', '')[:50]}", end="")
        sys.stdout.flush()

        if not download_url:
            err_count += 1
            print(f" ❌ no_download_url")
            continue

        result = download_and_transcribe(download_url, video_id, session)
        if result.get('status') == 'ok':
            transcript = result['transcript']
            result_file.write_text(json.dumps({
                "id": video_id,
                "title": video.get('title', ''),
                "author": video.get('author_name', ''),
                "likes": video.get('likes', 0),
                "total_interaction": video.get('total_interaction', 0),
                "transcript": transcript,
                "status": "ok",
            }, ensure_ascii=False), encoding='utf-8')
            ok_count += 1
            print(f" ✅ {len(transcript)}字")
        else:
            err_count += 1
            print(f" ❌ {result.get('error', 'unknown')}")

    print(f"\n完成: {ok_count} 成功, {err_count} 失败, {skip_count} 跳过")


if __name__ == "__main__":
    main()
