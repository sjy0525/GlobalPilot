# 品牌知识库混合检索

知识库问答和营销工作流使用同一套召回规则。混合模式将 pgvector 语义召回和 PostgreSQL 全文关键词召回按 RRF 融合，再交给现有的重排服务。新片段同时写入向量和分词元数据，更新和删除也会同步反映在向量记录和关键词索引中。

## 升级已有数据库

1. 从仓库根目录进入后端，安装分词依赖。

   ```sh
   cd globalpilot-backend
   .venv/bin/python -m pip install -r requirements.txt
   ```

2. 部署新版服务时先设置 `RAG_RETRIEVAL_MODE=vector`。启动会应用现有 schema 迁移并并发安装 `ix_embedding_lexical`。滚动部署时，等所有写入文档的后端实例升级后再回填。

3. 在后端目录用部署环境提供的数据库连接串运行回填。每批默认 100 条，只更新当前版本片段的词法元数据，不重新计算向量。

   ```sh
   RAG_LEXICAL_DATABASE_URL="$DATABASE_URL" .venv/bin/python scripts/backfill_rag_lexical.py
   ```

   脚本显式读取 `RAG_LEXICAL_DATABASE_URL` 或 `--database-url`。中断后可直接重跑。若 `skipped` 大于 0，等并发写入结束后重跑一次。

4. 设置 `RAG_RETRIEVAL_MODE=hybrid` 并重启后端。需要切回单路向量检索时，将同一配置改为 `vector` 并重启。

全新安装会在启动时创建索引，默认使用混合模式。

## 默认配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `RAG_RETRIEVAL_MODE` | `hybrid` | 检索模式，可设为 `vector` 以启用单路向量召回 |
| `RAG_FETCH_K` | `20` | 向量召回上限 |
| `RAG_KEYWORD_FETCH_K` | `20` | 关键词召回上限 |
| `RAG_MAX_DISTANCE` | `0.64` | 向量余弦距离上限 |
| `RAG_RECALL_K` | `10` | 单路向量模式送去重排的候选上限 |
| `RAG_RRF_CONSTANT` | `60` | RRF 常数 |
| `RAG_FUSION_K` | `20` | 融合后送去重排的候选上限 |
| `RAG_SEARCH_K` | `3` | 回答或营销上下文使用的片段上限 |
