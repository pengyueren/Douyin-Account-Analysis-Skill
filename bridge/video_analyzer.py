"""视频内容分析模块——下载 → 提取关键帧+音频 → 转写 → 多模态 LLM 分析

自包含实现，不依赖 videoagent 的 VideoAnalyzer。
依赖外部工具: yt-dlp, ffmpeg, faster-whisper, openai
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════


@dataclass
class VideoAnalysisResult:
    url: str = ""
    note_id: str = ""
    platform: str = ""
    duration_seconds: int = 0
    transcript: str = ""
    hook_text: str = ""
    hook_type: str = ""
    storytelling: str = ""
    cta_type: str = ""
    speaking_speed: str = ""
    scene_setting: str = ""
    camera_shot: str = ""
    onscreen_talent: str = ""
    visual_elements: str = ""
    text_overlays: str = ""
    visual_tone: str = ""
    products_shown: str = ""
    content_style: str = ""
    key_topics: list[str] = field(default_factory=list)
    replicability: str = ""
    summary: str = ""

    def model_dump(self) -> dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════
# VideoDownloader
# ═══════════════════════════════════════════════════════


class VideoDownloadError(Exception):
    pass


class VideoDownloader:
    """使用 yt-dlp 下载视频"""

    def __init__(self, work_dir: str, timeout: int = 30, max_seconds: int = 90):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_seconds = max_seconds

    def download(self, url: str, note_id: str, platform: str = "douyin") -> Path:
        output_template = str(self.work_dir / f"{note_id}_%(id)s.%(ext)s")
        cmd = [
            "yt-dlp", "--output", output_template,
            "--max-filesize", "50M", "--limit-rate", "5M",
            "--no-playlist", "--no-warnings",
            "--print", "filename", "--force-ipv4",
        ]
        if self.max_seconds > 0:
            cmd.extend(["--download-sections", f"*0-{self.max_seconds}"])
        cmd.append(url)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self.timeout, env={**os.environ, "LC_ALL": "C"})
        except subprocess.TimeoutExpired:
            raise VideoDownloadError(f"下载超时({self.timeout}s): {url}")
        except FileNotFoundError:
            raise VideoDownloadError("yt-dlp 未安装")

        if result.returncode != 0:
            raise VideoDownloadError(f"下载失败: {result.stderr.strip()[:200]}")

        out = result.stdout.strip()
        if out and Path(out).exists():
            return Path(out)
        for f in self.work_dir.glob(f"{note_id}_*"):
            if f.is_file() and f.stat().st_size > 1024:
                return f
        raise VideoDownloadError(f"下载完成但未找到视频文件: {url}")

    def cleanup(self, video_path: Path):
        try:
            if video_path.exists():
                video_path.unlink()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════
# AudioVisualProcessor
# ═══════════════════════════════════════════════════════


class AudioVisualProcessor:
    """使用 ffmpeg 提取音频和关键帧"""

    def __init__(self, work_dir: str, frame_interval: int = 10, max_frames: int = 50):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.frame_interval = frame_interval
        self.max_frames = max_frames

    def get_duration(self, video_path: Path) -> float:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            info = json.loads(result.stdout)
            return float(info.get("format", {}).get("duration", 0))
        except Exception:
            return 0.0

    def extract_audio(self, video_path: Path, note_id: str) -> Path | None:
        output = self.work_dir / f"{note_id}_audio.wav"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1",
            "-t", "90", str(output),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
            if output.exists() and output.stat().st_size > 1000:
                return output
        except Exception:
            pass
        return None

    def extract_frames(self, video_path: Path, note_id: str) -> list[Path]:
        frames: list[Path] = []
        duration = self.get_duration(video_path)
        if duration <= 0:
            duration = 60
        interval = max(1, self.frame_interval)
        timestamps = list(range(0, min(int(duration), 90), interval))
        timestamps = timestamps[:self.max_frames]

        for i, ts in enumerate(timestamps):
            output = self.work_dir / f"{note_id}_frame_{i:02d}.jpg"
            cmd = [
                "ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                "-vframes", "1", "-vf", "scale=1280:-1", "-q:v", "5", str(output),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30)
                if output.exists() and output.stat().st_size > 1000:
                    frames.append(output)
            except Exception:
                continue
        return frames

    def cleanup_frames(self, note_id: str):
        for f in self.work_dir.glob(f"{note_id}_frame_*.jpg"):
            try:
                f.unlink()
            except OSError:
                pass

    def cleanup_audio(self, note_id: str):
        audio = self.work_dir / f"{note_id}_audio.wav"
        try:
            if audio.exists():
                audio.unlink()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════
# SpeechTranscriber
# ═══════════════════════════════════════════════════════


class TranscriptionError(Exception):
    pass


class SpeechTranscriber:
    """使用 faster-whisper 将音频转为文字"""

    def __init__(self, model: str = "small", device: str = "auto"):
        self.model_name = model
        self.device = device
        self._model = None

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            compute_type = "int8"
            device = self.device
            if device == "auto":
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
                compute_type = "float16" if device == "cuda" else "int8"
            self._model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
        return self._model

    def transcribe(self, audio_path: Path) -> str:
        if not audio_path or not audio_path.exists():
            raise TranscriptionError("音频文件不存在")
        try:
            model = self._get_model()
            segments, _ = model.transcribe(str(audio_path), language="zh", beam_size=5, vad_filter=True)
            return "".join(seg.text.strip() for seg in segments)
        except Exception as e:
            raise TranscriptionError(f"语音转写失败: {e}")


# ═══════════════════════════════════════════════════════
# ContentAnalyzer — Seed 2.0 多模态
# ═══════════════════════════════════════════════════════


class AnalysisError(Exception):
    pass


class ContentAnalyzer:
    """使用字节跳动 Seed 2.0 多模态模型分析视频"""

    def __init__(self, api_key: str, base_url: str, model: str):
        if not api_key:
            raise ValueError("Seed 2.0 API key 未配置")
        if not model:
            raise ValueError("Seed 2.0 model 名称未配置")
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def analyze(
        self,
        transcript: str,
        frames: list[Path],
        title: str = "",
        description: str = "",
        author: str = "",
        likes: int = 0,
    ) -> dict[str, Any]:
        from openai import OpenAI

        system_prompt = """你是一位专业的短视频内容分析师。分析视频的帧画面（视觉）和口播文案（听觉）。

## 输出格式
严格按照以下JSON格式输出，不要加额外内容：
{
  "hook_text": "...",
  "hook_type": "痛点提问|反常识断言|场景代入|数据冲击|悬念设问",
  "storytelling": "...",
  "cta_type": "关注|评论|收藏|转发|下期预告|无明确引导",
  "speaking_speed": "快|中|慢",
  "scene_setting": "...",
  "camera_shot": "...",
  "onscreen_talent": "...",
  "visual_elements": "...",
  "text_overlays": "...",
  "visual_tone": "...",
  "products_shown": "...",
  "content_style": "口播干货|Vlog日常|教程演示|剧情演绎|开箱测评",
  "key_topics": ["..."],
  "replicability": "高-模板化结构易模仿|中-需要特定场景|低-依赖个人特色",
  "summary": "..."
}"""

        user_content: list[dict] = []
        text_block = f"## 视频信息\n标题: {title}\n描述: {description}\n作者: {author}\n点赞: {likes}\n\n"
        text_block += f"## 口播文案\n{transcript}\n\n" if transcript else "## 口播文案\n(无语音)\n\n"
        text_block += "## 视频帧画面(按时间顺序)"
        user_content.append({"type": "input_text", "text": text_block})

        for fp in frames:
            if fp.exists() and fp.stat().st_size > 0:
                with open(fp, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                user_content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})

        try:
            response = self.client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": system_prompt}, *user_content]}],
                temperature=0.3,
                max_output_tokens=4096,
            )
            content = getattr(response, "output_text", None)
            if not content:
                raise AnalysisError("API 返回空内容")
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise AnalysisError(f"API 返回非 JSON 格式: {e}")
        except Exception as e:
            raise AnalysisError(f"Seed 2.0 API 调用失败: {e}")


# ═══════════════════════════════════════════════════════
# VideoAnalyzer — 主控
# ═══════════════════════════════════════════════════════


DEFAULT_CONFIG = {
    "work_dir": "output/videos_analysis",
    "download_timeout": 30,
    "max_download_seconds": 90,
    "frame_interval": 10,
    "max_frames": 50,
    "whisper_model": "small",
    "whisper_device": "auto",
    "seed2_api_key": "",
    "seed2_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "seed2_model": "",
}


class VideoAnalyzer:
    """视频内容分析主控

    用法:
        analyzer = VideoAnalyzer({"seed2_api_key": "...", "seed2_model": "..."})
        result = analyzer.analyze(url="https://www.douyin.com/video/xxx", note_id="xxx", platform="dy")
    """

    def __init__(self, config: dict | None = None):
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.downloader = VideoDownloader(
            work_dir=cfg["work_dir"], timeout=cfg["download_timeout"], max_seconds=cfg["max_download_seconds"],
        )
        self.av_processor = AudioVisualProcessor(
            work_dir=cfg["work_dir"], frame_interval=cfg["frame_interval"], max_frames=cfg["max_frames"],
        )
        self.transcriber = SpeechTranscriber(model=cfg["whisper_model"], device=cfg["whisper_device"])
        self.use_multimodal = bool(cfg["seed2_api_key"] and cfg["seed2_model"])
        if self.use_multimodal:
            self.content_analyzer = ContentAnalyzer(
                api_key=cfg["seed2_api_key"], base_url=cfg["seed2_base_url"], model=cfg["seed2_model"],
            )

    def analyze(
        self,
        url: str,
        note_id: str,
        platform: str = "douyin",
        title: str = "",
        description: str = "",
        author: str = "",
        likes: int = 0,
    ) -> VideoAnalysisResult:
        plat_map = {"dy": "douyin"}
        plat = plat_map.get(platform, platform)

        video_path = None
        frames: list[Path] = []
        duration = 0

        # Step 1: 下载
        try:
            video_path = self.downloader.download(url, note_id, platform=plat)
        except VideoDownloadError as e:
            print(f"  [下载失败] {e}")
            return VideoAnalysisResult(url=url, note_id=note_id, platform=plat, summary=f"下载失败: {e}")

        # Step 2: 提取音频+关键帧
        audio_path = None
        transcript = ""

        if video_path:
            audio_path = self.av_processor.extract_audio(video_path, note_id)
            frames = self.av_processor.extract_frames(video_path, note_id)
            duration = int(self.av_processor.get_duration(video_path))

            if audio_path:
                try:
                    transcript = self.transcriber.transcribe(audio_path)
                except TranscriptionError as e:
                    print(f"  [转写失败] {e}")

        # Step 3: 多模态分析
        analysis_data: dict[str, Any] = {
            "hook_text": "", "hook_type": "", "storytelling": "",
            "cta_type": "", "speaking_speed": "",
            "scene_setting": "", "camera_shot": "", "onscreen_talent": "",
            "visual_elements": "", "text_overlays": "", "visual_tone": "",
            "products_shown": "", "content_style": "", "key_topics": [],
            "replicability": "", "summary": "",
        }

        if self.use_multimodal and frames:
            try:
                result = self.content_analyzer.analyze(
                    transcript=transcript, frames=frames,
                    title=title, description=description, author=author, likes=likes,
                )
                analysis_data.update(result)
            except AnalysisError as e:
                print(f"  [多模态分析失败] {e}")
                if transcript:
                    analysis_data["summary"] = "语音转录完成，视觉分析不可用"
        else:
            if not self.use_multimodal:
                analysis_data["summary"] = "未配置 Seed 2.0 API"
            elif not frames:
                analysis_data["summary"] = "无视频帧可用"

        # 清理
        if video_path:
            self.downloader.cleanup(video_path)
        self.av_processor.cleanup_frames(note_id)
        self.av_processor.cleanup_audio(note_id)

        return VideoAnalysisResult(
            url=url, note_id=note_id, platform=plat,
            duration_seconds=duration, transcript=transcript,
            hook_text=analysis_data.get("hook_text", ""),
            hook_type=analysis_data.get("hook_type", ""),
            storytelling=analysis_data.get("storytelling", ""),
            cta_type=analysis_data.get("cta_type", ""),
            speaking_speed=analysis_data.get("speaking_speed", ""),
            scene_setting=analysis_data.get("scene_setting", ""),
            camera_shot=analysis_data.get("camera_shot", ""),
            onscreen_talent=analysis_data.get("onscreen_talent", ""),
            visual_elements=analysis_data.get("visual_elements", ""),
            text_overlays=analysis_data.get("text_overlays", ""),
            visual_tone=analysis_data.get("visual_tone", ""),
            products_shown=analysis_data.get("products_shown", ""),
            content_style=analysis_data.get("content_style", ""),
            key_topics=analysis_data.get("key_topics", []),
            replicability=analysis_data.get("replicability", ""),
            summary=analysis_data.get("summary", ""),
        )
