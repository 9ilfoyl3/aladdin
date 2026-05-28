"""_HeaderTracker 单元测试"""

import pytest

from app.pipeline.chunker import _HeaderTracker


class TestHeaderTrackerPush:
    """push 方法测试"""

    def test_push_single_heading(self):
        """压入单个标题"""
        tracker = _HeaderTracker()
        tracker.push(1, "顶级标题")
        assert tracker.breadcrumb() == "# 顶级标题"

    def test_push_nested_headings(self):
        """压入嵌套标题，生成面包屑"""
        tracker = _HeaderTracker()
        tracker.push(1, "顶级标题")
        tracker.push(2, "二级标题")
        assert tracker.breadcrumb() == "# 顶级标题 > ## 二级标题"

    def test_push_three_levels(self):
        """三级嵌套"""
        tracker = _HeaderTracker()
        tracker.push(1, "一级")
        tracker.push(2, "二级")
        tracker.push(3, "三级")
        assert tracker.breadcrumb() == "# 一级 > ## 二级 > ### 三级"

    def test_push_same_level_replaces(self):
        """同级标题替换栈顶"""
        tracker = _HeaderTracker()
        tracker.push(1, "标题A")
        tracker.push(2, "子标题A")
        tracker.push(2, "子标题B")
        assert tracker.breadcrumb() == "# 标题A > ## 子标题B"

    def test_push_higher_level_pops_all_lower(self):
        """遇到更高级别标题时弹出所有低级别"""
        tracker = _HeaderTracker()
        tracker.push(1, "第一章")
        tracker.push(2, "第一节")
        tracker.push(3, "第一小节")
        tracker.push(1, "第二章")
        assert tracker.breadcrumb() == "# 第二章"

    def test_push_middle_level_pops_lower(self):
        """遇到中间级别标题时弹出更低级别"""
        tracker = _HeaderTracker()
        tracker.push(1, "一级")
        tracker.push(2, "二级")
        tracker.push(3, "三级")
        tracker.push(2, "新二级")
        assert tracker.breadcrumb() == "# 一级 > ## 新二级"


class TestHeaderTrackerBreadcrumb:
    """breadcrumb 方法测试"""

    def test_empty_stack(self):
        """空栈返回空字符串"""
        tracker = _HeaderTracker()
        assert tracker.breadcrumb() == ""

    def test_breadcrumb_format(self):
        """面包屑格式正确：# 标题 > ## 子标题"""
        tracker = _HeaderTracker()
        tracker.push(1, "Introduction")
        tracker.push(2, "Background")
        assert tracker.breadcrumb() == "# Introduction > ## Background"


class TestHeaderTrackerFeedLine:
    """feed_line 方法测试"""

    def test_detect_h1(self):
        """检测一级标题"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("# 标题")
        assert result is True
        assert tracker.breadcrumb() == "# 标题"

    def test_detect_h2(self):
        """检测二级标题"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("## 二级标题")
        assert result is True
        assert tracker.breadcrumb() == "## 二级标题"

    def test_detect_h6(self):
        """检测六级标题"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("###### 六级")
        assert result is True
        assert tracker.breadcrumb() == "###### 六级"

    def test_non_heading_line(self):
        """非标题行返回 False"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("这是普通文本")
        assert result is False
        assert tracker.breadcrumb() == ""

    def test_no_space_after_hash_not_heading(self):
        """# 后无空格不算标题"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("#没有空格")
        assert result is False
        assert tracker.breadcrumb() == ""

    def test_indented_heading(self):
        """带前导空格的标题行也能识别"""
        tracker = _HeaderTracker()
        result = tracker.feed_line("  ## 缩进标题")
        assert result is True
        assert tracker.breadcrumb() == "## 缩进标题"


class TestHeaderTrackerFeedText:
    """feed_text 方法测试"""

    def test_feed_multiline_text(self):
        """扫描多行文本，追踪所有标题"""
        tracker = _HeaderTracker()
        text = """# 第一章
这是第一章的内容。

## 第一节
这是第一节的内容。

### 详细说明
一些细节。
"""
        tracker.feed_text(text)
        assert tracker.breadcrumb() == "# 第一章 > ## 第一节 > ### 详细说明"

    def test_feed_text_with_level_change(self):
        """文本中标题级别变化时正确更新栈"""
        tracker = _HeaderTracker()
        text = """# 章节一
## 小节A
## 小节B
### 细节
# 章节二
"""
        tracker.feed_text(text)
        assert tracker.breadcrumb() == "# 章节二"

    def test_feed_text_no_headings(self):
        """无标题的文本不影响栈"""
        tracker = _HeaderTracker()
        text = "普通文本\n另一行\n还有一行"
        tracker.feed_text(text)
        assert tracker.breadcrumb() == ""


class TestHeaderTrackerMisc:
    """其他方法测试"""

    def test_current_level_empty(self):
        """空栈 current_level 返回 0"""
        tracker = _HeaderTracker()
        assert tracker.current_level() == 0

    def test_current_level_after_push(self):
        """push 后 current_level 返回栈顶级别"""
        tracker = _HeaderTracker()
        tracker.push(2, "二级")
        assert tracker.current_level() == 2

    def test_reset(self):
        """reset 清空栈"""
        tracker = _HeaderTracker()
        tracker.push(1, "标题")
        tracker.push(2, "子标题")
        tracker.reset()
        assert tracker.breadcrumb() == ""
        assert tracker.current_level() == 0
