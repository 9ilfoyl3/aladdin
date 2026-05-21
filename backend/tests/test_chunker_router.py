"""ChunkerRouter 单元测试 - 边界条件验证

验证 ChunkerRouter.select() 各规则的边界条件：
- 法律关键词恰好 3 次 → laws
- 法律关键词 2 次 → 不触发 laws
- QA 配对恰好 10 次匹配（5 对） → qa
- QA 配对 9 次匹配 → 不触发 qa
- 表格文件优先级最高
- 论文特征识别
- 多规则同时满足时优先级正确
"""

import pytest

from app.pipeline.chunker_router import ChunkerRouter


class TestChunkerRouterTablePriority:
    """优先级 1：表格文件"""

    def test_csv_returns_table(self):
        """csv 文件类型返回 table"""
        result = ChunkerRouter.select("csv", "任何内容")
        assert result == "table"

    def test_xlsx_returns_table(self):
        """xlsx 文件类型返回 table"""
        result = ChunkerRouter.select("xlsx", "任何内容")
        assert result == "table"

    def test_table_overrides_law_content(self):
        """表格优先级高于法律内容"""
        content = "第一条 内容。第二条 内容。第三条 内容。"
        result = ChunkerRouter.select("csv", content)
        assert result == "table"

    def test_table_overrides_paper_content(self):
        """表格优先级高于论文内容"""
        content = "Abstract\nSome content.\n\nReferences\n[1] Smith."
        result = ChunkerRouter.select("xlsx", content)
        assert result == "table"


class TestChunkerRouterLawBoundary:
    """优先级 2：法律文书 - 关键词恰好 3 次边界"""

    def test_exactly_3_law_keywords_returns_laws(self):
        """法律关键词恰好出现 3 次 → laws"""
        content = "第一条 保护权益。\n第二条 调整关系。\n第三条 不得侵犯。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"

    def test_2_law_keywords_returns_naive(self):
        """法律关键词只出现 2 次 → 不触发 laws"""
        content = "第一条 保护权益。\n第二条 调整关系。\n其他普通内容。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "naive"

    def test_mixed_law_keywords_count(self):
        """混合法律关键词（第X条 + 本院认为 + 判决如下）恰好 3 次"""
        content = "第一条 内容。\n本院认为，被告违约。\n判决如下：赔偿。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"

    def test_4_law_keywords_returns_laws(self):
        """法律关键词 4 次 → laws"""
        content = "第一条 内容。\n第二条 内容。\n本院认为内容。\n判决如下内容。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"

    def test_numeric_article_numbers(self):
        """阿拉伯数字条款号也计入法律关键词"""
        content = "第1条 内容。\n第2条 内容。\n第3条 内容。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"

    def test_chinese_numeral_article_numbers(self):
        """中文数字条款号计入法律关键词"""
        content = "第十条 内容。\n第二十条 内容。\n第三十条 内容。"
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"


class TestChunkerRouterPaperDetection:
    """优先级 3：学术论文"""

    def test_abstract_and_references(self):
        """Abstract + References → paper"""
        content = "Abstract\nThis paper presents...\n\nReferences\n[1] Smith 2020."
        result = ChunkerRouter.select("pdf", content)
        assert result == "paper"

    def test_abstract_and_bibliography(self):
        """Abstract + Bibliography → paper"""
        content = "Abstract\nWe propose...\n\nBibliography\n[1] Jones 2021."
        result = ChunkerRouter.select("pdf", content)
        assert result == "paper"

    def test_abstract_without_references(self):
        """只有 Abstract 没有 References → 不触发 paper"""
        content = "Abstract\nThis paper presents a novel approach."
        result = ChunkerRouter.select("pdf", content)
        assert result == "naive"

    def test_references_without_abstract(self):
        """只有 References 没有 Abstract → 不触发 paper"""
        content = "Introduction\nSome content.\n\nReferences\n[1] Smith."
        result = ChunkerRouter.select("pdf", content)
        assert result == "naive"

    def test_case_sensitive_abstract(self):
        """Abstract 匹配区分大小写（小写 abstract 不触发）"""
        content = "abstract\nContent.\n\nReferences\n[1] Smith."
        result = ChunkerRouter.select("pdf", content)
        assert result == "naive"

    def test_law_overrides_paper(self):
        """法律关键词优先级高于论文特征"""
        content = "Abstract\n第一条 内容。\n第二条 内容。\n第三条 内容。\nReferences\n[1]."
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws"


class TestChunkerRouterQABoundary:
    """优先级 4：QA 格式 - 恰好 5 对（10 次匹配）边界"""

    def test_exactly_5_qa_pairs_returns_qa(self):
        """恰好 5 对 QA（10 次匹配） → qa"""
        pairs = "\n\n".join([f"Q: Question {i}?\nA: Answer {i}." for i in range(5)])
        result = ChunkerRouter.select("txt", pairs)
        assert result == "qa"

    def test_4_qa_pairs_returns_naive(self):
        """只有 4 对 QA（8 次匹配） → 不触发 qa"""
        pairs = "\n\n".join([f"Q: Question {i}?\nA: Answer {i}." for i in range(4)])
        result = ChunkerRouter.select("txt", pairs)
        assert result == "naive"

    def test_chinese_qa_5_pairs(self):
        """中文 问:/答: 格式 5 对 → qa"""
        pairs = "\n\n".join([f"问: 问题{i}？\n答: 回答{i}。" for i in range(5)])
        result = ChunkerRouter.select("txt", pairs)
        assert result == "qa"

    def test_mixed_qa_formats(self):
        """混合 Q:/A: 和 问:/答: 格式达到 10 次匹配 → qa"""
        content = "Q: Q1?\nA: A1.\n\nQ: Q2?\nA: A2.\n\n问: Q3？\n答: A3。\n\nQ: Q4?\nA: A4.\n\n问: Q5？\n答: A5。"
        result = ChunkerRouter.select("txt", content)
        assert result == "qa"

    def test_6_qa_pairs_returns_qa(self):
        """6 对 QA（12 次匹配） → qa"""
        pairs = "\n\n".join([f"Q: Question {i}?\nA: Answer {i}." for i in range(6)])
        result = ChunkerRouter.select("txt", pairs)
        assert result == "qa"

    def test_law_overrides_qa(self):
        """法律关键词优先级高于 QA"""
        content = "第一条 Q: 问题？\nA: 回答。\n第二条 Q: 问题？\nA: 回答。\n第三条 Q: 问题？\nA: 回答。\nQ: Q4?\nA: A4.\nQ: Q5?\nA: A5."
        result = ChunkerRouter.select("txt", content)
        assert result == "laws"


class TestChunkerRouterDefault:
    """默认路由 → naive"""

    def test_plain_text_returns_naive(self):
        """普通文本返回 naive"""
        result = ChunkerRouter.select("pdf", "这是一段普通文本。")
        assert result == "naive"

    def test_empty_content_returns_naive(self):
        """空内容返回 naive"""
        result = ChunkerRouter.select("pdf", "")
        assert result == "naive"

    def test_unknown_file_type_returns_naive(self):
        """未知文件类型返回 naive"""
        result = ChunkerRouter.select("docx", "Some content here.")
        assert result == "naive"

    def test_pdf_without_special_content(self):
        """PDF 无特殊内容返回 naive"""
        result = ChunkerRouter.select("pdf", "一般性的文档内容，没有特殊标记。")
        assert result == "naive"
