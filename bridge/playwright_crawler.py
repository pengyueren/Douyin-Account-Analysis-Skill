"""独立 Playwright 爬虫——采集抖音创作者主页视频

不依赖 videoagent 的 cloakbrowser_crawler，完全自包含。
使用临时浏览器配置目录 + --disable-http-cache + URL 时间戳，
避免持久化 HTTP 缓存导致的跨账号数据污染。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

DOUYIN_PROFILE_URL = "https://www.douyin.com/user/{user_id}"

SCROLL_WAIT = 1.0
MAX_SCROLLS = 30


# ── 数值解析 ─────────────────────────────


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


def _extract_number(text: str) -> int:
    """从字符串中提取数字，支持 '1.2万', '3.5k', '1234'"""
    text = text.strip().replace(",", "").replace(" ", "")
    if not text:
        return 0
    if "万" in text:
        try:
            return int(float(text.replace("万", "")) * 10000)
        except ValueError:
            return 0
    if "k" in text.lower():
        try:
            return int(float(text.lower().replace("k", "")) * 1000)
        except ValueError:
            return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_relative_time(time_str: str) -> str:
    """将中文相对时间转为 YYYY-MM-DD 格式"""
    if not time_str or not time_str.strip():
        return ""
    s = time_str.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    now = datetime.now()
    if "刚刚" in s:
        return now.strftime("%Y-%m-%d")
    m = re.match(r"(\d+)分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"(\d+)小时前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"(\d+)天前", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"(\d+)[周星期]前", s)
    if m:
        return (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.match(r"(\d+)个月前", s)
    if m:
        return (now - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return ""


# ── 页面操作工具 ─────────────────────────


def _scrolldown(page, times: int = MAX_SCROLLS):
    """模拟人类滚动页面到底部"""
    for i in range(times):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_WAIT + random.uniform(0.2, 0.8))
        at_bottom = page.evaluate(
            "window.innerHeight + window.scrollY >= document.body.scrollHeight - 500"
        )
        if at_bottom and i > 2:
            break


# ── 抖音数据提取核心函数 ──────────────────


def _parse_dy_video(video: dict) -> dict:
    """将抖音内部数据格式转为标准 dict"""
    statistics = video.get("statistics", {})
    author = video.get("author", {})
    desc = video.get("desc", "")
    aweme_id = video.get("aweme_id", "")

    create_ts = video.get("create_time", 0) or 0
    time_str = ""
    if create_ts and isinstance(create_ts, (int, float)) and create_ts > 1000000000:
        time_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")

    def _s(*keys):
        for k in keys:
            v = statistics.get(k)
            if v is not None and v != "":
                return v
        return "0"

    return {
        "aweme_id": aweme_id,
        "desc": desc,
        "liked_count": _parse_count(_s("digg_count", "diggCount")),
        "comment_count": _parse_count(_s("comment_count", "commentCount")),
        "collected_count": _parse_count(_s("collect_count", "collectCount")),
        "share_count": _parse_count(_s("share_count", "shareCount")),
        "video_play_count": _parse_count(_s("play_count", "playCount")),
        "nickname": author.get("nickname", "") or video.get("nickname", ""),
        "user_id": author.get("uid", author.get("sec_uid", "")) or video.get("user_id", ""),
        "aweme_url": f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
        "create_time": time_str,
    }


def _deep_find_dy_videos(data: Any, depth: int = 0) -> list[dict] | None:
    """递归搜索抖音页面内嵌数据中的视频列表"""
    if depth > 8:
        return None
    if isinstance(data, dict):
        if "aweme_id" in data and "desc" in data:
            return [_parse_dy_video(data)]
        if "aweme_list" in data and isinstance(data["aweme_list"], list):
            return [_parse_dy_video(v) for v in data["aweme_list"] if isinstance(v, dict)]
        if "awemeList" in data and isinstance(data["awemeList"], list):
            return [_parse_dy_video(v) for v in data["awemeList"] if isinstance(v, dict)]
        if "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, dict) and "aweme_id" in item:
                    return [_parse_dy_video(v) for v in data["data"] if isinstance(v, dict)]
        if "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "aweme_list" in inner and isinstance(inner["aweme_list"], list):
                return [_parse_dy_video(v) for v in inner["aweme_list"] if isinstance(v, dict)]
            if "awemeList" in inner and isinstance(inner["awemeList"], list):
                return [_parse_dy_video(v) for v in inner["awemeList"] if isinstance(v, dict)]
        for v in data.values():
            result = _deep_find_dy_videos(v, depth + 1)
            if result:
                return result
    elif isinstance(data, list):
        if data and isinstance(data[0], dict):
            if "aweme_id" in data[0]:
                return [_parse_dy_video(v) for v in data if isinstance(v, dict)]
            result = _deep_find_dy_videos(data[0], depth + 1)
            if result:
                return result
    return None


def _extract_dy_profile(page) -> dict[str, Any]:
    """从抖音创作者主页渲染后的 DOM 提取个人信息"""
    try:
        return page.evaluate("""() => {
            const result = { fans: 0, nickname: '', desc: '', follows: 0, likes: 0 };
            const h1 = document.querySelector('h1');
            if (h1) result.nickname = h1.textContent.trim();
            const text = document.body.innerText;
            const nums = text.match(/(\\d+[\\d.]*[万千]?)/g) || [];
            const fanIdx = text.indexOf('粉丝');
            if (fanIdx >= 0) {
                const afterFans = text.substring(fanIdx, fanIdx + 30);
                const n = afterFans.match(/(\\d+[\\d.]*[万千]?)/);
                if (n) result.fans = n[1];
            }
            const followIdx = text.indexOf('关注');
            if (followIdx >= 0) {
                const afterFollow = text.substring(followIdx, followIdx + 30);
                const n = afterFollow.match(/(\\d+[\\d.]*[万千]?)/);
                if (n) result.follows = n[1];
            }
            const likeIdx = text.indexOf('获赞');
            if (likeIdx >= 0) {
                const afterLike = text.substring(likeIdx, likeIdx + 30);
                const n = afterLike.match(/(\\d+[\\d.]*[万千]?)/);
                if (n) result.likes = n[1];
            }
            return result;
        }""")
    except Exception:
        return {"fans": 0, "nickname": "", "desc": ""}


def _extract_dy_creator_videos(page, creator_id: str = "") -> list[dict]:
    """从创作者主页提取视频列表及互动数据（三层降级策略）"""
    results: list[dict] = []

    # 层1: 尝试内嵌数据
    try:
        data = page.evaluate("""() => {
            if (window._ROUTER_DATA) return window._ROUTER_DATA;
            if (window.__INITIAL_STATE__) return window.__INITIAL_STATE__;
            return null;
        }""")
        if data:
            raw = _deep_find_dy_videos(data)
            if raw:
                return raw
    except Exception:
        pass

    # 层2: 通过页面 fetch API 直接获取视频数据
    if creator_id:
        try:
            all_parsed = []
            cursor = 0
            max_items = 100
            max_pages = 5
            page_idx = 0
            while page_idx < max_pages and len(all_parsed) < max_items:
                data = page.evaluate("""async (params) => {
                    const secUid = params.secUid;
                    const cursorVal = params.cursorVal;
                    const urlParams = new URLSearchParams({
                        sec_user_id: secUid,
                        count: '30',
                        cursor: String(cursorVal),
                        aid: '1128',
                        version_code: '36.6.0',
                    });
                    const url = 'https://www.douyin.com/aweme/v1/web/aweme/post/?' + urlParams;
                    try {
                        const resp = await fetch(url, {
                            credentials: 'include',
                            headers: { 'Referer': window.location.href },
                        });
                        if (!resp.ok) return null;
                        const json = await resp.json();
                        return json;
                    } catch (e) {
                        return null;
                    }
                }""", {"secUid": creator_id, "cursorVal": cursor})
                if not data:
                    break
                aweme_list = data.get("aweme_list") or data.get("awemeList") or []
                if not aweme_list:
                    break
                parsed = [_parse_dy_video(v) for v in aweme_list if isinstance(v, dict)]
                all_parsed.extend(parsed)
                has_more = data.get("has_more") or data.get("hasMore")
                if not has_more:
                    break
                cursor = data.get("cursor", 0) or data.get("next_cursor", 0) or (cursor + 30)
                page_idx += 1
            if all_parsed:
                return all_parsed
        except Exception:
            pass

    # 层3: 从 DOM 提取视频链接
    try:
        results = page.evaluate("""() => {
            const items = [];
            const seen = new Set();
            const allLinks = document.querySelectorAll('a');
            for (const a of allLinks) {
                const href = a.href || '';
                if (!href.includes('/video/')) continue;
                const title = a.getAttribute('title') || a.textContent.trim();
                const awemeId = href.split('/video/').pop()?.split('?')[0] || '';
                if (href.includes('Baiduspider')) continue;
                if (!title && !awemeId) continue;
                if (awemeId && !seen.has(awemeId)) {
                    seen.add(awemeId);
                    let container = a;
                    for (let i = 0; i < 4; i++) {
                        if (container.parentElement) container = container.parentElement;
                        else break;
                    }
                    const ctext = container.textContent || '';
                    let likes = 0, plays = 0;
                    if (/赞/.test(ctext)) {
                        const m = ctext.match(/([\\d.]+[万千]?)\\s*赞/);
                        if (m) likes = m[1];
                    }
                    if (/播/.test(ctext)) {
                        const m = ctext.match(/([\\d.]+[万千]?)\\s*[播次]/);
                        if (m) plays = m[1];
                    }
                    items.push({
                        aweme_id: awemeId,
                        title: title || '',
                        likes: String(likes),
                        plays: String(plays),
                    });
                }
            }
            return items;
        }""")

        parsed = []
        for card in results:
            aweme_id = card.get("aweme_id", "")
            title = card.get("title", "")
            if not title and not aweme_id:
                continue
            parsed.append({
                "aweme_id": aweme_id,
                "desc": title,
                "liked_count": _extract_number(card.get("likes", "0")),
                "comment_count": 0,
                "collected_count": 0,
                "share_count": 0,
                "video_play_count": _extract_number(card.get("plays", "0")),
                "nickname": "",
                "user_id": "",
                "aweme_url": f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
                "create_time": "",
            })
        return parsed
    except Exception:
        pass

    return results


# ═══════════════════════════════════════════════════════
# PlaywrightCrawler — 主类
# ═══════════════════════════════════════════════════════


class PlaywrightCrawler:
    """独立 Playwright 爬虫——采集抖音创作者主页视频

    使用临时浏览器配置目录，避免跨账号 HTTP 缓存污染。
    可通过 persistent_profile_dir 复用已有登录态的浏览器配置。

    用法:
        crawler = PlaywrightCrawler()
        result = crawler.fetch_creator_data("MS4wLjABAAAA...")
        print(result["profile"]["nickname"])
        print(f'{len(result["videos"])} videos')
    """

    def __init__(self, persistent_profile_dir: str | None = None):
        self.persistent_profile_dir = persistent_profile_dir
        self._tmp_dir: str | None = None

    def _get_profile_dir(self) -> str:
        if self.persistent_profile_dir and Path(self.persistent_profile_dir).exists():
            return self.persistent_profile_dir
        self._tmp_dir = tempfile.mkdtemp(prefix="dy_crawler_")
        return self._tmp_dir

    def _cleanup(self):
        if self._tmp_dir:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

    def _extract_dy_dom_videos(self, page) -> list[dict]:
        """从 DOM 提取所有可见视频（长滚动后），生成完整的标准化 dict"""
        try:
            return page.evaluate("""() => {
                const items = [];
                const seen = new Set();
                const allLinks = document.querySelectorAll('a[href*="/video/"]');
                for (const a of allLinks) {
                    const href = a.href || '';
                    if (href.includes('Baiduspider')) continue;
                    const awemeId = href.split('/video/').pop()?.split('?')[0] || '';
                    if (!awemeId || seen.has(awemeId)) continue;
                    seen.add(awemeId);

                    const title = a.getAttribute('title') || a.textContent.trim();
                    let container = a;
                    for (let i = 0; i < 6; i++) {
                        if (container.parentElement) container = container.parentElement;
                        else break;
                    }
                    const ctext = container.textContent || '';

                    let likes = 0, plays = 0;
                    const mLike = ctext.match(/([\\d.]+[万千]?)\\s*赞/);
                    if (mLike) likes = mLike[1];
                    const mPlay = ctext.match(/([\\d.]+[万千]?)\\s*(播|次)/);
                    if (mPlay) plays = mPlay[1];

                    items.push({
                        aweme_id: awemeId,
                        desc: title || '',
                        liked_count_raw: String(likes),
                        play_count_raw: String(plays),
                    });
                }
                return items;
            }""")
        except Exception:
            return []

    def fetch_creator_data(self, creator_id: str) -> dict:
        """获取创作者主页信息 + 视频列表

        Args:
            creator_id: 抖音 sec_uid

        Returns:
            {"status": "ok"|"error",
             "profile": {"nickname": ..., "fans": ..., "desc": ...},
             "videos": [{...}],
             "total_raw": int,
             "error": "..."}
        """
        profile_dir = self._get_profile_dir()
        result: dict = {
            "status": "error",
            "profile": {"nickname": "", "fans": "0", "desc": ""},
            "videos": [],
            "total_raw": 0,
            "error": "",
        }

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    args=["--disable-http-cache"],
                )
                page = context.new_page()

                # 缓存破坏：时间戳参数确保浏览器不命中缓存页面
                cache_buster = f"?_ts={int(time.time() * 1000)}"
                page_url = DOUYIN_PROFILE_URL.format(user_id=creator_id) + cache_buster

                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)

                # 获取主页信息（从 DOM，不依赖缓存）
                profile = _extract_dy_profile(page)
                fans_raw = profile.get("fans", "0")
                result["profile"] = {
                    "nickname": profile.get("nickname", "") or "",
                    "follower_count": str(_parse_count(fans_raw)) if fans_raw else "0",
                    "desc": profile.get("desc", "") or "",
                }

                # 先滚动加载更多视频（长滚动，触发懒加载）
                _scrolldown(page, times=MAX_SCROLLS)

                # 策略 1：从已加载的 DOM 提取所有可见视频
                dom_videos = self._extract_dy_dom_videos(page)
                dom_by_id = {v["aweme_id"]: v for v in dom_videos}

                # 策略 2：通过 fetch API 获取完整数据（~前 30 条）
                api_videos = _extract_dy_creator_videos(page, creator_id)
                api_by_id = {v["aweme_id"]: v for v in api_videos}

                # 合并：优先用 API 完整数据，缺失的用 DOM 数据补充
                merged: list[dict] = []
                merged_ids: set[str] = set()

                # 先加 API 数据
                for v in api_videos:
                    if v["aweme_id"] and v["aweme_id"] not in merged_ids:
                        merged.append(v)
                        merged_ids.add(v["aweme_id"])

                # 用 DOM 补充 API 没返回的视频
                for v in dom_videos:
                    aid = v["aweme_id"]
                    if aid and aid not in merged_ids:
                        merged.append({
                            "aweme_id": aid,
                            "desc": v.get("desc", ""),
                            "liked_count": _extract_number(v.get("liked_count_raw", "0")),
                            "comment_count": 0,
                            "collected_count": 0,
                            "share_count": 0,
                            "video_play_count": _extract_number(v.get("play_count_raw", "0")),
                            "nickname": "",
                            "user_id": "",
                            "aweme_url": f"https://www.douyin.com/video/{aid}",
                            "create_time": "",
                        })
                        merged_ids.add(aid)

                if merged:
                    result["status"] = "ok"
                    result["total_raw"] = len(merged)
                    result["videos"] = merged
                else:
                    result["error"] = "未提取到视频数据"

                context.close()

        except ImportError:
            result["error"] = "playwright 未安装（请执行: pip install playwright && playwright install chromium）"
        except Exception as e:
            result["error"] = str(e)
        finally:
            self._cleanup()

        return result
