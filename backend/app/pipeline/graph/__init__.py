"""知识图谱（knowledge-graph）流水线包。

承载图谱抽取写路径相关组件：KB 级图谱配置读写（config）、LLM 结构化抽取
（extractor）、实体归一化/消歧（resolver）、队列触发与抽取 worker（trigger/worker）。

本包不在导入期连接 Neo4j 或加载重依赖，保证全局开关关闭时零额外成本。
"""
