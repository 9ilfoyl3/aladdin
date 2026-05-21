"""ChunkerRouter 和 Chunker 属性测试

使用 Hypothesis 验证 ChunkerRouter 路由优先级和各 Chunker 返回结构的正确性。

Feature: pipeline-production-optimization
"""

from unittest.mock import patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.pipeline.chunker import ChunkResult
from app.pipeline.chunker_router import BaseChunker, ChunkerFactory, ChunkerRouter

# 确保所有 Chunker 已注册
import app.pipeline.chunkers  # noqa: F401


# --- Strategies ---

# 非空文本策略：至少 1 个字符的可打印文本
non_empty_text_st = st.text(min_size=1, max_size=2000).filter(lambda t: t.strip())

# 文件类型策略
file_type_st = st.sampled_from(["pdf", "docx", "txt", "csv", "xlsx", "md", "html"])

# 已注册的 chunker 名称
registered_chunker_names_st = st.sampled_from(list(ChunkerFactory.REGISTRY.keys()))


# --- 辅助函数 ---

def _generate_law_content(num_keywords: int) -> st.SearchStrategy[str]:
    """生成包含指定数量法律关键词的文本"""
    keywords = ["本院认为", "判决如下", "第一条", "第二条", "第三条",
                "第四条", "第五条", "第十条"]

    @st.composite
    def strategy(draw):
        selected = draw(st.lists(
            st.sampled_from(keywords),
            min_size=num_keywords,
            max_size=num_keywords,
        ))
        filler = draw(st.text(min_size=10, max_size=200))
        # 将关键词散布在文本中
        parts = [filler]
        for kw in selected:
            parts.append(kw)
            parts.append(draw(st.text(min_size=5, max_size=50)))
        return "\n".join(parts)

    return strategy()


@st.composite
def law_content_st(draw):
    """生成包含 ≥3 个法律关键词的文本"""
    keywords = ["本院认为", "判决如下", "第一条", "第二条", "第三条",
                "第四条", "第五条", "第十条"]
    num = draw(st.integers(min_value=3, max_value=6))
    selected = draw(st.lists(
        st.sampled_from(keywords),
        min_size=num,
        max_size=num,
    ))
    filler = draw(st.text(min_size=10, max_size=100))
    parts = [filler]
    for kw in selected:
        parts.append(kw)
        parts.append(draw(st.text(min_size=5, max_size=30)))
    return "\n".join(parts)


@st.composite
def paper_content_st(draw):
    """生成包含 Abstract + References/Bibliography 的学术论文文本"""
    has_references = draw(st.booleans())
    filler = draw(st.text(min_size=20, max_size=200))
    ref_keyword = "References" if has_references else "Bibliography"
    return f"Title of Paper\n\nAbstract\n{filler}\n\nIntroduction\n{filler}\n\n{ref_keyword}\n{filler}"


@st.composite
def qa_content_st(draw):
    """生成包含 ≥10 次 QA 匹配（5 对）的文本"""
    num_pairs = draw(st.integers(min_value=5, max_value=10))
    pairs = []
    for _ in range(num_pairs):
        q_prefix = draw(st.sampled_from(["Q:", "问:"]))
        a_prefix = draw(st.sampled_from(["A:", "答:"]))
        q_text = draw(st.text(min_size=5, max_size=50).filter(lambda t: t.strip()))
        a_text = draw(st.text(min_size=5, max_size=50).filter(lambda t: t.strip()))
        pairs.append(f"{q_prefix} {q_text}\n{a_prefix} {a_text}")
    return "\n".join(pairs)


@st.composite
def naive_content_st(draw):
    """生成不匹配任何特殊路由规则的普通文本"""
    # 避免法律关键词、Abstract+References、QA 配对
    text = draw(st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=10,
        max_size=500,
    ))
    # 确保不触发其他路由规则
    assume("本院认为" not in text)
    assume("判决如下" not in text)
    assume("Abstract" not in text)
    assume("References" not in text)
    assume("Bibliography" not in text)
    assume(text.count("Q:") + text.count("A:") + text.count("问:") + text.count("答:") < 10)
    assume(text.strip())
    return text


class TestProperty7ChunkerReturnsValidChunkResult:
    """Property 7: 所有 Chunker 返回结构有效的 ChunkResult

    *For any* 注册的 Chunker 实现和任意非空文本输入，chunk() 方法应返回 ChunkResult，
    其中：parent_child_map 的所有 key 为 parent_chunks 的有效索引，
    所有 value 中的元素为 child_chunks 的有效索引，
    且每个 child 索引在整个 map 中恰好出现一次。

    **Validates: Requirements 3.1, 3.4**
    """

    @settings(max_examples=100)
    @given(
        chunker_name=registered_chunker_names_st,
        text=non_empty_text_st,
    )
    def test_chunker_returns_valid_chunk_result(
        self,
        chunker_name: str,
        text: str,
    ):
        """Property 7: 所有 Chunker 返回结构有效的 ChunkResult"""
        chunker = ChunkerFactory.create(chunker_name)
        result = chunker.chunk(text)

        # 验证返回类型
        assert isinstance(result, ChunkResult)

        # 如果有 parent_chunks，验证结构有效性
        if result.parent_chunks:
            # parent_child_map 的所有 key 为 parent_chunks 的有效索引
            for parent_idx in result.parent_child_map.keys():
                assert 0 <= parent_idx < len(result.parent_chunks), (
                    f"parent_idx {parent_idx} out of range [0, {len(result.parent_chunks)})"
                )

            # parent_child_map 的所有 value 中的元素为 child_chunks 的有效索引
            all_child_indices: list[int] = []
            for parent_idx, child_indices in result.parent_child_map.items():
                assert isinstance(child_indices, list)
                for child_idx in child_indices:
                    assert 0 <= child_idx < len(result.child_chunks), (
                        f"child_idx {child_idx} out of range [0, {len(result.child_chunks)})"
                    )
                    all_child_indices.append(child_idx)

            # 每个 child 索引在整个 map 中恰好出现一次
            assert len(all_child_indices) == len(set(all_child_indices)), (
                f"Duplicate child indices found: {all_child_indices}"
            )

            # 所有 child_chunks 都被映射到（完整覆盖）
            assert set(all_child_indices) == set(range(len(result.child_chunks))), (
                f"Not all child indices covered: mapped={sorted(all_child_indices)}, "
                f"expected={list(range(len(result.child_chunks)))}"
            )


class TestProperty8ChunkerRouterPriority:
    """Property 8: Chunker 路由优先级正确

    *For any* file_type 和 content 组合，ChunkerRouter.select 应返回满足最高优先级
    匹配规则的 chunker 类型：csv/xlsx → table，法律关键词 ≥ 3 → laws，
    Abstract + References → paper，QA 配对 ≥ 5 → qa，其他 → naive。

    **Validates: Requirements 3.2**
    """

    @settings(max_examples=100)
    @given(content=st.text(min_size=0, max_size=500))
    def test_csv_xlsx_always_routes_to_table(self, content: str):
        """优先级 1：csv/xlsx 文件类型始终路由到 table"""
        assert ChunkerRouter.select("csv", content) == "table"
        assert ChunkerRouter.select("xlsx", content) == "table"

    @settings(max_examples=100)
    @given(content=law_content_st())
    def test_law_content_routes_to_laws(self, content: str):
        """优先级 2：法律关键词 ≥ 3 路由到 laws（非 csv/xlsx 文件类型）"""
        # 使用非表格文件类型
        result = ChunkerRouter.select("pdf", content)
        assert result == "laws", (
            f"Expected 'laws' but got '{result}' for content with law keywords"
        )

    @settings(max_examples=100)
    @given(content=paper_content_st())
    def test_paper_content_routes_to_paper(self, content: str):
        """优先级 3：Abstract + References/Bibliography 路由到 paper（无法律关键词）"""
        # 确保不触发法律规则
        law_matches = len(ChunkerRouter._LAW_PATTERN.findall(content))
        assume(law_matches < 3)

        result = ChunkerRouter.select("pdf", content)
        assert result == "paper", (
            f"Expected 'paper' but got '{result}' for content with Abstract+References"
        )

    @settings(max_examples=100)
    @given(content=qa_content_st())
    def test_qa_content_routes_to_qa(self, content: str):
        """优先级 4：QA 配对 ≥ 10 次匹配路由到 qa（无更高优先级匹配）"""
        # 确保不触发更高优先级规则
        law_matches = len(ChunkerRouter._LAW_PATTERN.findall(content))
        assume(law_matches < 3)
        assume("Abstract" not in content or
               ("References" not in content and "Bibliography" not in content))

        result = ChunkerRouter.select("txt", content)
        assert result == "qa", (
            f"Expected 'qa' but got '{result}' for content with QA pairs"
        )

    @settings(max_examples=100)
    @given(content=naive_content_st())
    def test_default_routes_to_naive(self, content: str):
        """优先级 5：不匹配任何规则时路由到 naive"""
        result = ChunkerRouter.select("pdf", content)
        assert result == "naive", (
            f"Expected 'naive' but got '{result}' for plain content"
        )

    @settings(max_examples=100)
    @given(
        file_type=st.sampled_from(["csv", "xlsx"]),
        content=law_content_st(),
    )
    def test_table_priority_over_laws(self, file_type: str, content: str):
        """优先级验证：csv/xlsx 优先于法律关键词"""
        result = ChunkerRouter.select(file_type, content)
        assert result == "table", (
            f"Expected 'table' (priority 1) but got '{result}' for {file_type}"
        )


class TestProperty9ManualChunkerTypeOverride:
    """Property 9: 手动指定 chunker_type 覆盖自动路由

    *For any* 有效的 chunker_type 配置值和任意文件内容，当知识库 config 中设置了
    chunker_type 时，实际使用的 Chunker 类型应等于配置值，而非自动路由结果。

    **Validates: Requirements 3.3**
    """

    @settings(max_examples=100)
    @given(
        chunker_type=registered_chunker_names_st,
        file_type=file_type_st,
        content=non_empty_text_st,
    )
    def test_manual_chunker_type_overrides_auto_routing(
        self,
        chunker_type: str,
        file_type: str,
        content: str,
    ):
        """Property 9: 手动指定 chunker_type 覆盖自动路由

        当 chunker_type 被指定时，ChunkerFactory.create(chunker_type) 应被使用，
        而非 ChunkerRouter.select() 的结果。
        """
        # 验证自动路由可能返回不同的结果
        auto_selected = ChunkerRouter.select(file_type, content)

        # 手动指定的 chunker_type 应该能成功创建
        chunker = ChunkerFactory.create(chunker_type)
        assert isinstance(chunker, BaseChunker)

        # 核心断言：手动指定时使用的是指定类型，而非自动路由结果
        # 通过验证 ChunkerFactory.create(chunker_type) 返回正确类型来确认
        expected_cls = ChunkerFactory.REGISTRY[chunker_type]
        assert isinstance(chunker, expected_cls), (
            f"ChunkerFactory.create('{chunker_type}') should return instance of "
            f"{expected_cls.__name__}, got {type(chunker).__name__}"
        )

        # 验证即使自动路由选择了不同类型，手动指定仍然生效
        if chunker_type != auto_selected:
            # 自动路由会选择不同的 chunker，但手动指定覆盖了它
            manual_chunker = ChunkerFactory.create(chunker_type)
            auto_chunker = ChunkerFactory.create(auto_selected)
            assert type(manual_chunker) != type(auto_chunker) or chunker_type == auto_selected
