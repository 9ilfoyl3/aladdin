"""MCP（Model Context Protocol）标准协议实现。

本包同时服务两个方向，二者共用协议编解码与调用方上下文定义：

- **inbound**（Artoo 作为 MCP server）：``app/mcp_server.py`` 的路由层调用
  :func:`app.mcp.server.handle_message`，把 Artoo 知识库能力按标准 MCP
  （JSON-RPC 2.0 over Streamable HTTP）暴露给任意标准客户端。
- **outbound**（Artoo 作为 MCP client）：``app/agent/tools/mcp_client.py`` 复用
  :mod:`app.mcp.jsonrpc` 与 :mod:`app.mcp.context`，调用第三方标准 MCP server。

模块划分（按"协议 / 上下文 / 能力 / 分发"四层，避免单文件继续膨胀）：

- :mod:`app.mcp.jsonrpc`   JSON-RPC 2.0 信封与错误码，无业务语义。
- :mod:`app.mcp.context`   调用方上下文（CallerContext）的定义与 header/_meta 双通道
                           编解码 + HMAC 可验证签名。inbound 解析、outbound 注入共用。
- :mod:`app.mcp.url_guard` outbound 目标 URL 校验（SSRF 护栏）。
- :mod:`app.mcp.tools`     Artoo 对外暴露的工具定义与实现（身份范围收敛在此）。
- :mod:`app.mcp.server`    MCP 方法分发（initialize / tools/list / tools/call / ping）
                           + 会话管理 + 限流。
"""
