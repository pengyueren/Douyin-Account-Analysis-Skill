"""账号诊断 CLI 综合测试

覆盖此前 review 发现的所有问题：
  - P0: CLI import 路径
  - P1: JSON stdout 承诺
  - P1: MediaCrawler returncode
  - P2: CLI 参数校验
  - P2: 数据充分性判定边缘情况
  - 数据标准化/去重
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

# ── 项目根路径 ──
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# 确保 bridge/ 和 store/ 可从不同路径导入
sys.path.insert(0, str(_PROJECT_ROOT / "bridge"))
sys.path.insert(0, str(_PROJECT_ROOT / "store"))


# ═══════════════════════════════════════════════════════
# P0: CLI import 路径 — 确保模块可导入
# ═══════════════════════════════════════════════════════

class TestImportPaths(unittest.TestCase):
    """P0: CLI import 路径测试"""

    def test_import_videoagent_bridge(self):
        """项目根在 sys.path 时，bridge.videoagent_bridge 可导入"""
        import bridge.videoagent_bridge
        self.assertTrue(hasattr(bridge.videoagent_bridge, 'main'))

    def test_import_store_storage(self):
        """项目根在 sys.path 时，store.storage 可导入"""
        from store.storage import AccountStorage
        self.assertTrue(hasattr(AccountStorage, 'save_raw_data'))


# ═══════════════════════════════════════════════════════
# P2: CLI 参数校验 — 缺参数返回 JSON error
# ═══════════════════════════════════════════════════════

class TestCLIArgs(unittest.TestCase):
    """P2: CLI 参数校验测试"""

    def _run_cli(self, args: list[str]) -> tuple[str, str, int]:
        """运行 CLI 并捕获 stdout/stderr/exit_code"""
        cmd = [sys.executable, str(_PROJECT_ROOT / "bridge" / "videoagent_bridge.py")] + args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode

    def test_no_args_returns_json(self):
        """无参数时输出 JSON error"""
        out, err, code = self._run_cli([])
        self.assertEqual(code, 1)
        data = json.loads(out)  # 应是合法 JSON
        self.assertIn("error", data)
        self.assertIn("usage", data)

    def test_invalid_command_returns_json(self):
        """未知命令返回 JSON error"""
        out, err, code = self._run_cli(["invalid_cmd"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_search_missing_args_returns_json(self):
        """search 缺 platform+keyword 返回 JSON error"""
        out, err, code = self._run_cli(["search"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)
        self.assertIn("参数不足", data["error"])

    def test_fetch_creator_missing_args_returns_json(self):
        """fetch-creator 缺参数返回 JSON error"""
        out, err, code = self._run_cli(["fetch-creator"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_analyze_video_missing_args_returns_json(self):
        """analyze-video 缺参数返回 JSON error"""
        out, err, code = self._run_cli(["analyze-video"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_extract_audio_missing_args_returns_json(self):
        """extract-audio 缺参数返回 JSON error"""
        out, err, code = self._run_cli(["extract-audio"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_analyze_article_missing_args_returns_json(self):
        """analyze-article 缺参数返回 JSON error"""
        out, err, code = self._run_cli(["analyze-article"])
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertIn("error", data)


# ═══════════════════════════════════════════════════════
# P1: JSON stdout 承诺 — stdout 输出必须是合法 JSON
# ═══════════════════════════════════════════════════════

class TestJSONOutput(unittest.TestCase):
    """P1: CLI 输出必须是合法 JSON"""

    def _run_cli(self, args: list[str]) -> tuple[str, int]:
        cmd = [sys.executable, str(_PROJECT_ROOT / "bridge" / "videoagent_bridge.py")] + args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return proc.stdout.strip(), proc.returncode

    def test_error_output_is_valid_json(self):
        """所有参数错误场景的 stdout 都是合法 JSON"""
        for args in ([], ["x"], ["search"], ["fetch-creator"], ["analyze-video"], ["extract-audio"]):
            with self.subTest(args=args):
                out, code = self._run_cli(args)
                self.assertEqual(code, 1)
                try:
                    json.loads(out)
                except json.JSONDecodeError:
                    self.fail(f"stdout 不是合法 JSON (args={args}): {out[:200]}")


# ═══════════════════════════════════════════════════════
# 数据充分性判定 — 边缘情况
# ═══════════════════════════════════════════════════════

class TestDataSufficiency(unittest.TestCase):
    """_assess_data_sufficiency 边缘情况测试"""

    def setUp(self):
        from bridge.videoagent_bridge import _assess_data_sufficiency
        self.func = _assess_data_sufficiency

    def test_empty_videos(self):
        """空列表返回 empty flag"""
        result = self.func([])
        self.assertEqual(result["flag"], "empty")
        self.assertEqual(result["video_count"], 0)

    def test_single_video(self):
        """只有一条视频时返回 insufficient_date_info"""
        videos = [{"create_time": "2025-01-01"}]
        result = self.func(videos)
        self.assertEqual(result["flag"], "insufficient_date_info")

    def test_short_span_few_videos(self):
        """跨度<30天且视频<30条"""
        videos = [{"create_time": f"2025-01-{i:02d}"} for i in range(1, 20)]
        result = self.func(videos)
        self.assertEqual(result["flag"], "partial_very_short_span")

    def test_large_gaps(self):
        """大量月份缺失"""
        videos = [{"create_time": "2025-01-01"}, {"create_time": "2025-06-01"}]
        result = self.func(videos)
        self.assertEqual(result["flag"], "partial_large_gaps")

    def test_low_density(self):
        """跨度超12个月，月均<3条"""
        videos = []
        for i in range(13):  # 13 个月, 从 2024-12 到 2025-12
            year = 2024 + ((11 + i) // 12)
            month = ((11 + i) % 12) + 1
            videos.append({"create_time": f"{year}-{month:02d}-01"})
        result = self.func(videos)
        # avg_per_month ≈ 13/13.4 ≈ 1.0 < 3
        self.assertEqual(result["flag"], "partial_low_density")

    def test_ok_sufficient_data(self):
        """正常数据返回 ok"""
        videos = [{"create_time": f"2025-{m:02d}-{d:02d}"}
                  for m in range(1, 7) for d in range(1, 6)]  # 30 videos over 5 months
        result = self.func(videos)
        self.assertEqual(result["flag"], "ok")

    def test_malformed_dates_handled(self):
        """畸形日期不应导致崩溃"""
        videos = [
            {"create_time": "2025-01-01"},
            {"create_time": "invalid-date"},
            {"create_time": ""},
            {"create_time": None},
        ]
        result = self.func(videos)
        # 至少有一个有效日期，但 < 2 个 → insufficient_date_info
        self.assertIn(result["flag"], ("insufficient_date_info", "partial_very_short_span"))


# ═══════════════════════════════════════════════════════
# 视频数据标准化 — 去重 + 字段映射
# ═══════════════════════════════════════════════════════

class TestVideoStandardization(unittest.TestCase):
    """_standardize_videos 去重和字段映射测试"""

    def setUp(self):
        from bridge.videoagent_bridge import _standardize_videos
        self.func = _standardize_videos

    def test_dedup_by_aweme_id(self):
        """相同 aweme_id 去重"""
        raw = [
            {"aweme_id": "1", "liked_count": 100, "comment_count": 10, "collected_count": 20, "share_count": 5},
            {"aweme_id": "1", "liked_count": 200, "comment_count": 20, "collected_count": 40, "share_count": 10},
            {"aweme_id": "2", "liked_count": 300, "comment_count": 30, "collected_count": 60, "share_count": 15},
        ]
        result = self.func(raw)
        self.assertEqual(len(result), 2)

    def test_empty_aweme_id_skipped(self):
        """空 aweme_id 跳过"""
        raw = [
            {"aweme_id": "", "liked_count": 100},
            {"aweme_id": "1", "liked_count": 200},
        ]
        result = self.func(raw)
        self.assertEqual(len(result), 1)

    def test_field_mapping(self):
        """字段映射正确"""
        raw = [{
            "aweme_id": "123",
            "liked_count": "100",
            "comment_count": "10",
            "collected_count": "20",
            "share_count": "5",
            "desc": "test video",
            "create_time": 1735689600,  # 2025-01-01
            "aweme_type": "0",
        }]
        result = self.func(raw)
        self.assertEqual(len(result), 1)
        v = result[0]
        self.assertEqual(v["id"], "123")
        self.assertEqual(v["likes"], 100)
        self.assertEqual(v["comments"], 10)
        self.assertEqual(v["favorites"], 20)
        self.assertEqual(v["shares"], 5)
        self.assertEqual(v["title"], "test video")
        self.assertEqual(v["create_time"], "2025-01-01")
        self.assertTrue(v["is_video"])

    def test_aweme_type_2_is_not_video(self):
        """aweme_type=2 是图文"""
        raw = [{
            "aweme_id": "123",
            "liked_count": 100, "comment_count": 0, "collected_count": 0, "share_count": 0,
            "aweme_type": "2",
        }]
        result = self.func(raw)
        self.assertFalse(result[0]["is_video"])

    def test_missing_fields_default_zero(self):
        """缺失字段时默认 0"""
        raw = [{"aweme_id": "1"}]
        result = self.func(raw)
        self.assertEqual(result[0]["likes"], 0)
        self.assertEqual(result[0]["comments"], 0)
        self.assertEqual(result[0]["favorites"], 0)
        self.assertEqual(result[0]["shares"], 0)
        self.assertEqual(result[0]["title"], "")


# ═══════════════════════════════════════════════════════
# URL 解析
# ═══════════════════════════════════════════════════════

class TestCreatorIDParsing(unittest.TestCase):
    """_extract_creator_id URL 解析测试"""

    def setUp(self):
        from bridge.videoagent_bridge import _extract_creator_id
        self.func = _extract_creator_id

    def test_douyin_user_url(self):
        """标准抖音主页 URL 解析成功"""
        uid = self.func("https://www.douyin.com/user/MS4wLjABAAAA", "dy")
        self.assertEqual(uid, "MS4wLjABAAAA")

    def test_douyin_url_with_query(self):
        """带查询参数的 URL 正确去除 query"""
        uid = self.func("https://www.douyin.com/user/MS4wLjABAAAA?prev_id=123", "dy")
        self.assertEqual(uid, "MS4wLjABAAAA")

    def test_unsupported_platform(self):
        """不支持平台时抛 ValueError"""
        with self.assertRaises(ValueError):
            self.func("https://example.com/user/123", "unknown")


class TestResolveTargetAuthor(unittest.TestCase):
    """_resolve_target_author 作者匹配测试"""

    def setUp(self):
        from bridge.videoagent_bridge import _resolve_target_author
        self.func = _resolve_target_author

    def _make_item(self, sec_uid: str, nickname: str) -> dict:
        return {"sec_uid": sec_uid, "nickname": nickname, "user_id": "12345"}

    def test_exact_nickname_match(self):
        """昵称精确匹配时返回该作者"""
        items = [
            self._make_item("uid1", "目标账号"),
            self._make_item("uid2", "目标账号小号"),
            self._make_item("uid3", "其他账号"),
        ]
        result = self.func(items, "目标账号")
        self.assertIsNotNone(result)
        self.assertEqual(result["nickname"], "目标账号")
        self.assertEqual(result["sec_uid"], "uid1")

    def test_most_videos_fallback(self):
        """无精确匹配时返回视频数最多的作者"""
        items = []
        for i in range(5):
            items.append(self._make_item("uid_a", "作者A"))
        for i in range(2):
            items.append(self._make_item("uid_b", "作者B"))
        result = self.func(items, "搜索词")
        self.assertIsNotNone(result)
        self.assertEqual(result["nickname"], "作者A")

    def test_ambiguous_returns_none(self):
        """前两名视频数差距≤1时返回 None"""
        items = []
        for i in range(3):
            items.append(self._make_item("uid_a", "作者A"))
        for i in range(2):
            items.append(self._make_item("uid_b", "作者B"))
        result = self.func(items, "搜索词")
        self.assertIsNone(result)

    def test_empty_input(self):
        """空列表返回 None"""
        result = self.func([], "test")
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════
# P1: MediaCrawler returncode 检查
# ═══════════════════════════════════════════════════════

class TestMediaCrawlerReturncode(unittest.TestCase):
    """P1: MediaCrawler subprocess returncode 检查"""

    def setUp(self):
        from bridge.mediacrawler_runner import MediaCrawlerRunner
        self.runner_class = MediaCrawlerRunner

    @patch('bridge.mediacrawler_runner.subprocess.run')
    def test_search_creator_returns_empty_on_failure(self, mock_run):
        """search_creator 在 returncode!=0 时返回空列表"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error: login required"
        mock_run.return_value = mock_result

        runner = self.runner_class(repo_path="/tmp/fake_mediacrawler")
        result = runner.search_creator("dy", "MS4wLjABAAAA")
        self.assertEqual(result, [])

    @patch('bridge.mediacrawler_runner.subprocess.run')
    def test_search_keyword_returns_empty_on_failure(self, mock_run):
        """search_keyword 在 returncode!=0 时返回空列表"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        mock_run.return_value = mock_result

        runner = self.runner_class(repo_path="/tmp/fake_mediacrawler")
        result = runner.search_keyword("dy", "test")
        self.assertEqual(result, [])

    @patch('bridge.mediacrawler_runner.subprocess.run')
    def test_search_creator_timeout_falls_back_to_cache(self, mock_run):
        """search_creator 超时后尝试读缓存（不崩溃）"""
        from bridge.mediacrawler_runner import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="main.py", timeout=600)

        runner = self.runner_class(repo_path="/tmp/fake_mediacrawler")
        # 超时时没有缓存文件，应返回 []
        result = runner.search_creator("dy", "MS4wLjABAAAA")
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════
# AccountStorage 基本功能
# ═══════════════════════════════════════════════════════

class TestAccountStorage(unittest.TestCase):
    """AccountStorage 目录结构和文件操作测试"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_store_root = None
        # 临时修改 _STORE_ROOT
        import store.storage
        self._orig_store_root = store.storage._STORE_ROOT
        store.storage._STORE_ROOT = self.tmpdir / "store"

    def tearDown(self):
        import store.storage
        store.storage._STORE_ROOT = self._orig_store_root
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_storage_creates_dirs(self):
        """初始化 AccountStorage 创建所需子目录"""
        from store.storage import AccountStorage
        storage = AccountStorage("测试账号")
        self.assertTrue((storage.root / "raw_data").exists())
        self.assertTrue((storage.root / "analysis" / "transcripts").exists())
        self.assertTrue((storage.root / "analysis" / "video_analysis").exists())
        self.assertTrue((storage.root / "reports").exists())

    def test_save_raw_data_writes_json(self):
        """save_raw_data 写入合法 JSON 文件"""
        from store.storage import AccountStorage
        storage = AccountStorage("test_acct")
        path = storage.save_raw_data({"key": "value"})
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data["key"], "value")

    def test_save_videos_writes_json(self):
        """save_videos 写入合法 JSON 文件"""
        from store.storage import AccountStorage
        storage = AccountStorage("test_acct")
        path = storage.save_videos([{"id": "1"}])
        self.assertTrue(path.exists())

    def test_save_report_writes_html(self):
        """save_report 写入 HTML 内容"""
        from store.storage import AccountStorage
        storage = AccountStorage("test_acct")
        path = storage.save_report("<html><body>test</body></html>", "test.html")
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("test", content)

    def test_save_transcript_writes_txt(self):
        """save_transcript 写入纯文本"""
        from store.storage import AccountStorage
        storage = AccountStorage("test_acct")
        path = storage.save_transcript("video_123", "转写文本内容")
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertEqual(content, "转写文本内容")

    def test_list_stored_data(self):
        """list_stored_data 返回正确结构"""
        from store.storage import AccountStorage
        storage = AccountStorage("test_acct")
        storage.save_raw_data({"k": "v"})
        listing = storage.list_stored_data()
        self.assertIn("raw_data", listing)
        self.assertEqual(len(listing["raw_data"]), 1)
        self.assertTrue(listing["raw_data"][0].endswith(".json"))


# ═══════════════════════════════════════════════════════
# 语法检查 — 所有 .py 文件可编译
# ═══════════════════════════════════════════════════════

class TestSyntax(unittest.TestCase):
    """所有 Python 文件语法检查"""

    def test_all_py_files_compile(self):
        """项目内所有 .py 文件能通过 py_compile"""
        import py_compile
        root = _PROJECT_ROOT
        failed = []
        for pyfile in root.rglob("*.py"):
            if "venv" in str(pyfile) or ".venv" in str(pyfile):
                continue
            try:
                py_compile.compile(str(pyfile), doraise=True)
            except py_compile.PyCompileError as e:
                failed.append(str(pyfile))
        self.assertEqual(failed, [], f"以下文件编译失败: {failed}")


# ═══════════════════════════════════════════════════════
# JSON 输出
# ═══════════════════════════════════════════════════════

def main():
    """运行所有测试"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 按优先级排序：快速失败的先跑
    test_classes = [
        TestSyntax,          # 语法检查最基础
        TestImportPaths,     # import 路径
        TestVideoStandardization,  # 纯函数
        TestDataSufficiency,       # 纯函数
        TestCreatorIDParsing,      # 纯函数
        TestResolveTargetAuthor,   # 新的作者匹配逻辑
        TestAccountStorage,        # 存储
        TestMediaCrawlerReturncode, # mock 测试
        TestCLIArgs,               # CLI 集成
        TestJSONOutput,            # CLI 集成
    ]
    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
