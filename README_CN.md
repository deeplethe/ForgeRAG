<p align="center">
  <img src="web/public/text_logo_padding.png" alt="ForgeRAG" height="64">
</p>

<h2 align="center">生产级结构感知推理 RAG</h2>

<p align="center">
  <strong>LLM 树推理</strong> ◦ <strong>知识图谱多跳</strong> ◦ <strong>像素级精准引用</strong> ◦ <strong>无与伦比的表现</strong>
</p>

<p align="center">
  <a href="https://github.com/deeplethe/ForgeRAG/releases"><img src="https://img.shields.io/badge/version-0.2.2-brightgreen?style=for-the-badge" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/deeplethe/ForgeRAG"><img src="https://img.shields.io/github/stars/deeplethe/ForgeRAG?style=for-the-badge" alt="Stars"></a>
  <a href="https://github.com/deeplethe/ForgeRAG/issues"><img src="https://img.shields.io/github/issues/deeplethe/ForgeRAG?style=for-the-badge" alt="Issues"></a>
  <a href="docs/"><img src="https://img.shields.io/badge/Docs-docs%2F-blue?style=for-the-badge" alt="Docs"></a>
  <a href="https://discord.gg/XJadJHvxdQ"><img src="https://img.shields.io/badge/Discord-Join-7289da?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> •
  <a href="#强大之处">强大之处</a> •
  <a href="#技术方案">技术方案</a> •
  <a href="docs/">文档</a> •
  <a href="./README.md">English</a>
</p>

---

<p align="center"><img src="docs/images/architecture.png" alt="ForgeRAG 架构" width="700"></p>

### 现有方案的问题

业界已有许多超越朴素分块 RAG 的方案，但各有根本性局限：

| 方案 | 优势 | 局限 |
|------|------|------|
| **向量检索型**（如 naive RAG） | 快速语义搜索 | 语义相近 ≠ 真正相关，缺乏精确匹配和结构上下文 |
| **图谱型**（如 GraphRAG） | 跨文档实体关联 | 只有概念骨架，缺乏原文证据，实体抽取损失细节 |
| **混合图谱型**（如 LightRAG） | 双层检索（local + global） | 回答基于 KG 摘要合成而非原文，缺乏溯源能力，幻觉风险较高 |
| **推理型**（如 PageIndex） | 单文档准确率高 | 查询延迟随文档数线性增长，难以满足生产环境的性能要求 |

### 我们的方案：像领域专家一样思考

领域专家遇到问题时，不会逐页翻阅 —— 他们瞬间回忆起相关信息的位置，调用脑中概念之间的关联网络，再从多个来源综合出有据可查的答案。ForgeRAG 正是模拟这一工作流：**BM25 + 向量搜索**在毫秒内定位候选区域，**知识图谱**提供跨文档的概念关联，**LLM 树导航**对文档结构进行推理以精准定位关键章节，最终融合为一个附带可追溯引用的完整回答。

对于**多跳问题**（如 *"苹果和三星的共同供应商有哪些？"*），我们引入**知识图谱**路径：入库时抽取实体与关系，查询时走双层检索 —— **local**（query 实体 → 邻居遍历）与 **global**（关键词 → 基于名字向量的模糊/跨语言实体匹配），加上**关系语义**检索（relation description embedding）。受 LightRAG 的 context 组装启发，KG 路径把**合成的实体描述与关系描述**直接注入生成提示，为 LLM 提供原文分块之上的"蒸馏知识层"。

### 基准测试：ForgeRAG vs LightRAG

我们使用 **UltraDomain** 基准方法（LLM 裁判两两对比）与 [LightRAG](https://github.com/HKUDS/LightRAG) 进行对比。胜率以 **ForgeRAG% / LightRAG%** 呈现。

> 🚧 **针对更多 RAG 系统、领域和指标的详细基准测试正在进行中。**

| 领域 | 全面性 | 多样性 | 赋能性 | 总体 |
|------|:------:|:------:|:------:|:----:|
| **Agriculture** | **58.6** / 41.4 | 47.1 / **52.9** | **52.9** / 47.1 | **56.4** / 43.6 |
| **CS** | **55.6** / 44.4 | 48.4 / **51.6** | **54.0** / 46.0 | **54.8** / 45.2 |
| **Legal** | **57.0** / 43.0 | 46.5 / **53.5** | **53.5** / 46.5 | **55.6** / 44.4 |
| **Mix** | **56.3** / 43.7 | 47.8 / **52.2** | **54.3** / 45.7 | **55.1** / 44.9 |

<sub>裁判模型：qwen3-max · [复现脚本](scripts/compare_bench.py)</sub>

> **关于事实准确性：** UltraDomain 基准评估全面性、多样性和赋能性，但不评估事实准确性。ForgeRAG 为每个论断提供像素级精准的 `[c_N]` 引用，可追溯验证原文。LightRAG 从知识图谱摘要合成答案，缺乏可追溯引用，在广度上表现好但幻觉风险更高。

## 强大之处

<p align="center"><img src="docs/images/chat_demo.gif" alt="ForgeRAG 演示" width="700"></p>

相比 RAGFlow 等较重的平台，ForgeRAG 专注于**核心管道设计**——精简的检索-回答链 + 可自由拼装的组件。

🔍 **双推理检索** · BM25 + 向量预筛选 → LLM 树导航 + 知识图谱，RRF 融合

📌 **像素级引用** · 每个论断精确定位到 PDF 页码 + 边界框，点击即高亮

🔗 **检索链追踪** · 每次查询可检视路径分数、扩展决策、融合逻辑

💬 **多轮对话** · 支持上下文关联追问，完整对话历史

📄 **多格式入库** · PDF、DOCX、PPTX、XLSX、HTML、Markdown、TXT

⚙️ **YAML 单一真值** · 一个文件 + 一次重启——无隐藏运行时状态

🎛️ **按请求覆盖** · `QueryOverrides` 让你在单次查询里切检索路径 / top-k / rerank（SDK + A/B 利器）

🏆 **超越 LightRAG** · UltraDomain 基准测试 55.48% 总体胜率

<details>
<summary><strong><font size="4">📸 更多截图</font></strong></summary>
<br/>

**对话** · 结构化回答 + 像素级精准引用

<img src="docs/screenshots/chat_sample.png" alt="对话" width="700">

**文档入库** · 树构建 + 处理流水线

<img src="docs/screenshots/ingest_demo.png" alt="入库" width="700">

**知识图谱** · 实体关系可视化

<img src="docs/screenshots/kg_demo.png" alt="知识图谱" width="700">

</details>

## 快速开始

### 前置要求

- Python 3.10+
- Node.js 18+（用于构建前端）
- 一个 LLM API Key（OpenAI、DeepSeek 或任何 LiteLLM 兼容的提供商）
- 推荐：4+ 核 CPU，8GB+ 内存（大文档 + KG 抽取建议 16GB+）

### 方式 A：本地开发

```bash
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG

# Python 依赖
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 前端
cd web && npm install && npm run build && cd ..

# 配置（交互式引导：选择提供商、填写 Key、完成）
python scripts/setup.py

# 运行 —— 默认单 worker（与向导默认的 SQLite + ChromaDB-persistent + NetworkX 后端兼容）
python main.py
```

打开 [http://localhost:8000](http://localhost:8000) —— Web UI 自动加载。

> **提示：** 文档入库涉及大量 LLM 调用（树构建、KG 抽取、嵌入）。要让 UI 在并发入库时保持响应可以扩到多 worker，但 `--workers >1` 需要多进程安全的后端组合（PostgreSQL + Neo4j + 非 persistent 的 ChromaDB / Qdrant / Milvus / Weaviate / pgvector）。在 SQLite / NetworkX / persistent ChromaDB 这类单进程后端上启动 `--workers >1` 会以退出码 2 拒绝启动，避免静默数据损坏。

### 方式 B：Docker 部署

```bash
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG

python scripts/docker_setup.py   # 交互式引导：选择提供商、填写 Key、完成
docker compose up -d             # PostgreSQL + pgvector + ForgeRAG，一键启动
```

打开 [http://localhost:8000](http://localhost:8000)。详见[部署指南](docs/deployment.md)。

> **建议：** 强烈推荐启用 **MinerU** —— 它显著提升文档结构解析精度，尤其适用于复杂布局、表格和公式的 PDF。启动后在 Web UI 设置中开启。

### 支持的后端

| 组件 | 选项 |
|------|------|
| **PDF 解析** | 单选：`pymupdf`（快速，默认）/ `mineru`（布局感知，表格/公式）/ `mineru-vlm`（视觉语言，扫描件与复杂版面） |
| **关系数据库** | SQLite（默认）、PostgreSQL、MySQL |
| **向量数据库** | ChromaDB（默认）、pgvector（PostgreSQL）、Qdrant、Milvus、Weaviate |
| **文件存储** | 本地文件系统（默认）、Amazon S3、阿里云 OSS |
| **图数据库** | NetworkX 内存模式（默认）、Neo4j |
| **LLM / 嵌入** | 任何 [LiteLLM](https://docs.litellm.ai/docs/providers) 兼容提供商：OpenAI、Azure、Anthropic、Ollama、DeepSeek、Cohere 等 |

### 命令行选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config` | 自动检测 | `forgerag.yaml` 路径 |
| `--host` | `0.0.0.0` | 绑定地址（或 `$FORGERAG_HOST`） |
| `--port` | `8000` | 绑定端口（或 `$FORGERAG_PORT`） |
| `--reload` | 关闭 | 开发模式热重载 |
| `--workers` | `1` | Uvicorn worker 数量。`>1` 需要多进程安全后端（PostgreSQL + Neo4j + 非 persistent 向量库），否则启动以退出码 2 拒绝。 |

## 架构

上方示意图展示了完整的数据流。详细的管道文档和逐节点说明请参阅[架构概览](docs/architecture.md)。

## API

REST API 位于 `/api/v1/`。交互式文档：

- Swagger UI：[http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc：[http://localhost:8000/redoc](http://localhost:8000/redoc)

核心端点：

| 端点 | 说明 |
|------|------|
| `POST /api/v1/query` | 提问（流式 SSE 或同步）——支持 `path_filter` + `overrides` 做 per-request 定制 |
| `POST /api/v1/documents/upload-and-ingest` | 上传文档到指定文件夹（multipart，带 `folder_path` 表单字段） |
| `GET  /api/v1/documents?path_filter=…&recursive=…` | 按 folder 列出文档 |
| `GET  /api/v1/documents/{id}/tree` | 文档层级结构 |
| `GET  /api/v1/graph` | 知识图谱可视化 |
| `GET  /api/v1/settings` | 只读快照（yaml 为真值来源） |

## 文档

- **[快速入门](docs/getting-started.md)** —— 安装、首份文档、分步指南
- **[架构概览](docs/architecture.md)** —— 入库、检索、回答管道详解
- **[配置参考](docs/configuration.md)** —— 所有配置项及默认值
- **[API 参考](docs/api-reference.md)** —— REST API 端点、请求/响应格式、SSE 流式
- **[部署指南](docs/deployment.md)** —— Docker 部署、生产清单、Nginx、Ollama
- **[开发指南](docs/development.md)** —— 开发环境、测试、新增后端

## 项目结构

```
ForgeRAG/
├── api/              # FastAPI 路由和模型
├── answering/        # 回答生成管道
├── config/           # Pydantic 配置模型
├── embedder/         # 嵌入后端（LiteLLM、sentence-transformers）
├── graph/            # 知识图谱存储（NetworkX、Neo4j）
├── ingestion/        # 文档入库管道 + 格式转换
├── parser/           # PDF 解析、分块、树构建
├── persistence/      # 数据库层（关系型、向量、文件）
├── retrieval/        # 检索管道（BM25、向量、树、KG、融合）
├── scripts/          # CLI 工具（配置向导、Docker 部署、批量入库）
├── web/              # Vue 3 前端
├── docs/             # 详细文档
├── main.py           # 应用入口
└── forgerag.yaml     # 本地配置（已 git-ignore）
```

## 更新计划

- [ ] 🧪 更多基准测试，对标更多 RAG 系统和领域
- [ ] 🔄 百万级文档扩展 · 增量索引、异步 KG
- [ ] 🌐 多语言检索 · 跨语言查询与文档支持
- [ ] 📦 Python SDK · `pip install forgerag-sdk`
- [ ] 🛠️ 配置面板提示与诊断 · 缺失 provider 警告、参数校验反馈
- [ ] ⚡ 性能优化 · 更快的入库、查询缓存、异步嵌入

## 参与贡献

我们欢迎各种形式的贡献 —— Bug 修复、新功能、文档改进等。

提交 Pull Request 前请先阅读[贡献指南](CONTRIBUTING.md)。

## 相关项目

- [LightRAG](https://github.com/HKUDS/LightRAG) — 基于图的 RAG，双层（local + global）检索
- [GraphRAG](https://github.com/microsoft/graphrag) — 微软的图驱动 RAG，带社区摘要
- [PageIndex](https://github.com/VectifyAI/PageIndex) — 基于推理的无向量检索

## 许可证

[MIT License](LICENSE)
