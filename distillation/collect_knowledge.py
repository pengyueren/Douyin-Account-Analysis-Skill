"""
知识采集脚本 — 采集账号分析方法论知识

策略调整（基于实际测试发现）：
- 抖音关键词搜索返回的多是大众爆款，不是方法论内容
- 改为以【特定创作者定向采集】+【WebSearch 全网搜索】为主
- 关键词搜索作为补充（拉大量数据后再筛选）

用法：
  python3 distillation/collect_knowledge.py --mode search     # 长尾关键词搜索
  python3 distillation/collect_knowledge.py --mode creators   # 采集指定创作者的内容
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
from videoagent_bridge import cmd_search, _run_videoagent

RAW_DATA_DIR = Path(__file__).parent / "raw_data"
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════
# 模式 A: 特定创作者采集
# ═══════════════════════════════════════════════════════════

# 已知的账号分析类创作者
CREATOR_NAMES = ["干饭人伊伊", "小义子小", "谢老板"]


def find_creator_ids(platform: str, names: list[str]) -> dict[str, str]:
    """通过关键词搜索找到创作者的 author_id"""
    plat_flag = "dy" if platform == "douyin" else "xhs"
    result = {}
    for name in names:
        print(f"  [找ID] {name}...")
        try:
            items = cmd_search(plat_flag, name, min_likes=0)
            for item in items:
                if name in item.get("author_name", ""):
                    result[name] = item["author_id"]
                    print(f"    → {item['author_name']} = {item['author_id']}")
                    break
        except Exception as e:
            print(f"    → 失败: {e}")
    return result


def collect_creator_videos(platform: str, creator_id: str, creator_name: str, limit: int = 15):
    """采集创作者主页的视频列表"""
    plat_flag = "dy" if platform == "douyin" else "xhs"

    code = f"""
import json
from data_collector.mediacrawler_bridge import MediaCrawlerBridge

bridge = MediaCrawlerBridge(prefer='cli')
raw = bridge.search_creator('{plat_flag}', '{creator_id}')
results = []
for item in raw[:{limit}]:
    item_id = item.get('aweme_id', '') or item.get('note_id', '') or ''
    is_video = True
    if '{plat_flag}' == 'dy':
        aweme_type = item.get('aweme_type', 0)
        is_video = aweme_type != 2
        item_url = f"https://www.douyin.com/video/{{item_id}}"
    else:
        item_type = item.get('type', 'video')
        is_video = item_type == 'video'
        item_url = item.get('note_url', '') or f"https://www.xiaohongshu.com/explore/{{item_id}}"
    likes = int(item.get('liked_count', 0) or 0)
    if likes < 500:
        continue
    results.append({{
        'id': item_id, 'title': (item.get('title','') or item.get('desc',''))[:80],
        'url': item_url, 'author_name': item.get('nickname','{creator_name}'),
        'author_id': '{creator_id}', 'likes': likes,
        'comments': int(item.get('comment_count',0) or 0),
        'favorites': int(item.get('collected_count',0) or 0),
        'shares': int(item.get('share_count',0) or 0),
        'is_video': is_video,
    }})
print(json.dumps(results, ensure_ascii=False))
"""
    output = _run_videoagent(code, timeout=300)
    for line in output.strip().split("\n"):
        if line.startswith("["):
            return json.loads(line)
    return []


# ═══════════════════════════════════════════════════════════
# 模式 B: 长尾关键词搜索
# ═══════════════════════════════════════════════════════════

LONGTAIL_TERMS = {
    "dy": ["账号拆解方法论", "爆款视频分析", "短视频数据分析", "账号定位诊断", "抖音运营方法论", "自媒体数据复盘"],
    "xhs": ["小红书账号分析", "爆款笔记拆解", "小红书运营方法", "博主数据分析", "小红书诊断"],
}


def search_keywords(platform: str, min_likes: int = 500):
    """长尾关键词搜索"""
    plat_flag = "dy" if platform == "douyin" else "xhs"
    all_items = []
    for kw in LONGTAIL_TERMS.get(plat_flag, []):
        print(f"  '{kw}'...")
        try:
            items = cmd_search(plat_flag, kw, min_likes=min_likes)
            all_items.extend(items[:3])
            print(f"    {len(items[:3])} 条")
        except Exception as e:
            print(f"    ✗ {e}")

    # 去重
    seen = set()
    result = []
    for r in all_items:
        rid = r.get("id", "") or r.get("title", "")
        if rid not in seen:
            seen.add(rid)
            result.append(r)
    return result


def save(data, name: str):
    path = RAW_DATA_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(data) if isinstance(data, list) else len(data)
    print(f"  → 已保存: {path} ({n} 条)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["search", "creators", "find-creators"], default="search")
    parser.add_argument("--platform", choices=["dy", "xhs", "both"], default="both")
    parser.add_argument("--min-likes", type=int, default=500)
    args = parser.parse_args()

    platforms = []
    if args.platform in ("dy", "both"):
        platforms.append(("douyin", "dy"))
    if args.platform in ("xhs", "both"):
        platforms.append(("xiaohongshu", "xhs"))

    if args.mode == "find-creators":
        for plat, _ in platforms:
            print(f"\n=== {plat} 查找创作者 ===")
            cmap = find_creator_ids(plat, CREATOR_NAMES)
            save(cmap, f"{plat}_creator_ids")

    elif args.mode == "creators":
        # 先读取 find-creators 的结果
        for plat, _ in platforms:
            ids_path = RAW_DATA_DIR / f"{plat}_creator_ids.json"
            if not ids_path.exists():
                print(f"请先运行 --mode find-creators")
                continue
            cmap = json.loads(ids_path.read_text())
            all_videos = []
            for name, cid in cmap.items():
                print(f"\n  [{plat}] 采集 {name} ({cid})...")
                videos = collect_creator_videos(plat, cid, name)
                all_videos.extend(videos)
                print(f"    → {len(videos)} 条视频")
            save(all_videos, f"{plat}_creator_videos")

    elif args.mode == "search":
        for plat, pf in platforms:
            print(f"\n=== {plat} 长尾搜索 ===")
            data = search_keywords(pf, min_likes=args.min_likes)
            save(data, f"{plat}_search_results")

    print("\n✅ 完成")
