"""Tests for TextCleaner - 测试去噪不误删正文、正确识别页眉页脚和页码"""

import pytest

from app.pipeline.cleaner import TextCleaner


@pytest.fixture
def cleaner():
    return TextCleaner()


# ============================================================
# _filter_by_bbox 测试
# ============================================================


class TestFilterByBbox:
    """测试 bbox 位置过滤逻辑"""

    def test_short_text_in_top_5_percent_is_filtered(self, cleaner):
        """顶部 5% 区域的短文本被过滤"""
        page_height = 842.0
        page_blocks = [[
            {"bbox": [0, 10, 200, 30], "text": "公司机密"},  # y0=10 < 42.1 (5%)
        ]]

        result = cleaner._filter_by_bbox(page_blocks, page_height)

        assert result == [[]]

    def test_short_text_in_bottom_5_percent_is_filtered(self, cleaner):
        """底部 5% 区域的短文本被过滤"""
        page_height = 842.0
        page_blocks = [[
            {"bbox": [0, 800, 200, 835], "text": "第 1 页"},  # y1=835 > 799.9 (95%)
        ]]

        result = cleaner._filter_by_bbox(page_blocks, page_height)

        assert result == [[]]

    def test_long_text_in_edge_area_is_preserved(self, cleaner):
        """边缘区域的长文本（>100字符）被保留"""
        page_height = 842.0
        long_text = "这是一段很长的正文内容" * 20  # 超过 100 字符
        page_blocks = [[
            {"bbox": [0, 10, 500, 40], "text": long_text},  # 顶部区域但文本长
        ]]

        result = cleaner._filter_by_bbox(page_blocks, page_height)

        assert len(result[0]) == 1
        assert result[0][0]["text"] == long_text

    def test_text_in_middle_area_is_always_preserved(self, cleaner):
        """中间区域的文本始终保留"""
        page_height = 842.0
        page_blocks = [[
            {"bbox": [50, 200, 500, 250], "text": "正文内容"},
            {"bbox": [50, 400, 500, 450], "text": "短"},
        ]]

        result = cleaner._filter_by_bbox(page_blocks, page_height)

        assert len(result[0]) == 2

    def test_empty_page_blocks_returns_empty(self, cleaner):
        """空 page_blocks 返回空列表"""
        result = cleaner._filter_by_bbox([], 842.0)

        assert result == []


# ============================================================
# _detect_repeated_headers 测试
# ============================================================


class TestDetectRepeatedHeaders:
    """测试跨页重复文本检测"""

    def test_text_appearing_in_more_than_50_percent_pages_detected(self, cleaner):
        """出现在 >50% 页面中的文本被检测为页眉页脚"""
        page_texts = [
            "公司内部文件\n正文内容第一页\n版权所有",
            "公司内部文件\n正文内容第二页\n版权所有",
            "公司内部文件\n正文内容第三页\n版权所有",
            "正文内容第四页\n其他内容\n结尾",
        ]

        result = cleaner._detect_repeated_headers(page_texts)

        assert "公司内部文件" in result
        assert "版权所有" in result

    def test_text_appearing_in_less_than_50_percent_pages_not_detected(self, cleaner):
        """出现在 <50% 页面中的文本不被检测"""
        page_texts = [
            "页眉A\n正文第一页\n页脚A",
            "页眉B\n正文第二页\n页脚B",
            "页眉C\n正文第三页\n页脚C",
            "页眉D\n正文第四页\n页脚D",
        ]

        result = cleaner._detect_repeated_headers(page_texts)

        assert len(result) == 0

    def test_single_page_returns_empty_set(self, cleaner):
        """单页文档返回空集合"""
        page_texts = ["这是唯一一页的内容\n包含多行文本\n结尾"]

        result = cleaner._detect_repeated_headers(page_texts)

        assert result == set()

    def test_text_length_less_than_2_is_ignored(self, cleaner):
        """长度 <=2 的文本被忽略"""
        page_texts = [
            "AB\n正文第一页\nCD",
            "AB\n正文第二页\nCD",
            "AB\n正文第三页\nCD",
        ]

        result = cleaner._detect_repeated_headers(page_texts)

        # "AB" 和 "CD" 长度为 2，不满足 len > 2 条件
        assert "AB" not in result
        assert "CD" not in result

    def test_text_length_more_than_50_is_ignored(self, cleaner):
        """长度 >=50 的文本被忽略"""
        long_header = "A" * 50  # 恰好 50 字符，不满足 len < 50
        page_texts = [
            f"{long_header}\n正文第一页\n结尾一",
            f"{long_header}\n正文第二页\n结尾二",
            f"{long_header}\n正文第三页\n结尾三",
        ]

        result = cleaner._detect_repeated_headers(page_texts)

        assert long_header not in result

    def test_only_checks_first_3_and_last_3_lines(self, cleaner):
        """仅检查每页首 3 行和尾 3 行"""
        # 中间行的重复文本不应被检测
        page_texts = [
            "行1\n行2\n行3\n重复中间行\n行5\n行6\n行7",
            "行1\n行2\n行3\n重复中间行\n行5\n行6\n行7",
            "行1\n行2\n行3\n重复中间行\n行5\n行6\n行7",
        ]

        result = cleaner._detect_repeated_headers(page_texts)

        # "重复中间行" 在第4行，不在首3行也不在尾3行，不应被检测
        assert "重复中间行" not in result
        # 但首尾行中的短文本应被检测（如果满足频率条件）
        assert "行1" not in result  # 长度为2，不满足 len > 2


# ============================================================
# _remove_page_numbers 测试
# ============================================================


class TestRemovePageNumbers:
    """测试页码正则去除"""

    def test_dash_format_removed(self, cleaner):
        """'- 3 -' 格式被去除"""
        text = "正文内容\n- 3 -\n继续正文"

        result = cleaner._remove_page_numbers(text)

        assert "- 3 -" not in result
        assert "正文内容" in result
        assert "继续正文" in result

    def test_chinese_page_format_removed(self, cleaner):
        """'第 3 页' 格式被去除"""
        text = "正文内容\n第 3 页\n继续正文"

        result = cleaner._remove_page_numbers(text)

        assert "第 3 页" not in result
        assert "正文内容" in result

    def test_page_of_format_removed(self, cleaner):
        """'Page 3 of 10' 格式被去除"""
        text = "Some content\nPage 3 of 10\nMore content"

        result = cleaner._remove_page_numbers(text)

        assert "Page 3 of 10" not in result
        assert "Some content" in result
        assert "More content" in result

    def test_slash_format_removed(self, cleaner):
        """'3/10' 格式被去除"""
        text = "正文内容\n3/10\n继续正文"

        result = cleaner._remove_page_numbers(text)

        assert "3/10" not in result

    def test_pure_1_to_4_digit_numbers_removed(self, cleaner):
        """纯 1-4 位数字被去除"""
        text = "正文内容\n1\n42\n123\n9999\n继续正文"

        result = cleaner._remove_page_numbers(text)

        lines = result.split("\n")
        assert "1" not in lines
        assert "42" not in lines
        assert "123" not in lines
        assert "9999" not in lines

    def test_5_plus_digit_numbers_not_removed(self, cleaner):
        """5 位及以上数字不被去除"""
        text = "正文内容\n12345\n999999\n继续正文"

        result = cleaner._remove_page_numbers(text)

        assert "12345" in result
        assert "999999" in result

    def test_normal_text_lines_preserved(self, cleaner):
        """普通文本行被保留"""
        text = "这是正文第一段\n包含数字123的句子\nPage 3 讨论了重要内容\n结尾"

        result = cleaner._remove_page_numbers(text)

        assert "这是正文第一段" in result
        assert "包含数字123的句子" in result
        assert "Page 3 讨论了重要内容" in result
        assert "结尾" in result


# ============================================================
# clean() 集成测试
# ============================================================


class TestCleanIntegration:
    """测试 clean() 主方法的集成行为"""

    def test_normal_body_text_not_deleted(self, cleaner):
        """正常正文不被删除"""
        content = "这是一段正常的正文内容，描述了系统的架构设计。\n第二段讨论了性能优化方案。\n第三段总结了实施计划。"

        result = cleaner.clean(content)

        assert "这是一段正常的正文内容，描述了系统的架构设计。" in result
        assert "第二段讨论了性能优化方案。" in result
        assert "第三段总结了实施计划。" in result

    def test_page_numbers_removed_from_content(self, cleaner):
        """页码从内容中被去除"""
        content = "正文第一段\n- 1 -\n正文第二段\n第 2 页\n正文第三段"

        result = cleaner.clean(content)

        assert "- 1 -" not in result
        assert "第 2 页" not in result
        assert "正文第一段" in result
        assert "正文第二段" in result
        assert "正文第三段" in result

    def test_repeated_headers_removed(self, cleaner):
        """跨页重复的页眉页脚被去除"""
        # 每页需要超过 6 行，确保正文不在首尾 3 行中
        page_texts = [
            "公司机密文件\n第一章标题\n摘要信息\n正文第一页内容A\n正文第一页内容B\n正文第一页内容C\n正文第一页内容D\n附录说明\n版权所有翻印必究",
            "公司机密文件\n第二章标题\n摘要信息\n正文第二页内容A\n正文第二页内容B\n正文第二页内容C\n正文第二页内容D\n附录说明\n版权所有翻印必究",
            "公司机密文件\n第三章标题\n摘要信息\n正文第三页内容A\n正文第三页内容B\n正文第三页内容C\n正文第三页内容D\n附录说明\n版权所有翻印必究",
        ]
        content = "\n".join(page_texts)

        result = cleaner.clean(content, page_texts=page_texts)

        # 重复出现在首尾的页眉页脚被去除
        assert "公司机密文件" not in result
        assert "版权所有翻印必究" not in result
        # 正文内容保留
        assert "正文第一页内容A" in result
        assert "正文第二页内容A" in result
        assert "正文第三页内容A" in result

    def test_long_text_in_edge_areas_preserved(self, cleaner):
        """边缘区域的长文本保留（保守策略）"""
        long_text = "这是一段非常重要的正文内容" * 10  # 确保 >100 字符
        assert len(long_text) > 100
        page_blocks = [[
            {"bbox": [0, 10, 500, 30], "text": long_text},  # 顶部区域
            {"bbox": [0, 200, 500, 400], "text": "中间正文内容"},
        ]]

        result = cleaner.clean(
            content=f"{long_text}\n中间正文内容",
            page_blocks=page_blocks,
        )

        # 长文本即使在边缘区域也应保留
        assert long_text in result
        assert "中间正文内容" in result
