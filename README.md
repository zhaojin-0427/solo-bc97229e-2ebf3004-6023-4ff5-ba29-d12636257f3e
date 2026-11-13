# 档案 OCR 跨页检索与引用定位服务（FastAPI + SQLite）

为档案数据工程师提供的两类能力：

1. **跨页检索**：研究者用一段 OCR 文字就能定位扫描件中的**原句**，即使该
   句子跨块、跨页切分，也能返回可直接用于高亮的块 ID、矩形坐标与原文字符范围。
2. **引用定位（换版续定位）**：研究者摘录 OCR 原句保存为引文；文档校订换版后，
   后台自动把引文重新定位到新版本的扫描件上，无法唯一确定时进入人工复核，绝不猜位置。

## 能力对照

| 需求 | 实现 |
| --- | --- |
| 按文档上传 UTF-8 NDJSON，行序即阅读顺序 | `PUT /documents/{doc_id}`，逐块流式接收，字段：`block_id`（文档内唯一）、`page`、`text`、`rect{x0,y0,x1,y1}` |
| 查询去空白后 1–80 字符，原文匹配也忽略空白 | 服务端对查询与存储文本统一做 Unicode 空白剥离后匹配 |
| 允许跨块、跨页，重叠命中分别保留 | 倒排表含跨块边界二元组；每个位置都是独立行，重叠命中天然各自保留 |
| 返回最早 100 处命中 | 按阅读顺序（全局紧凑字符位置）返回，`has_more` 表示还有第 101 处真命中 |
| 命中含块 ID、坐标、原文字符范围 | 每个 hit 的 `spans[]` 给出 `block_id/page/rect/char_start/char_end` |
| 文档可大于内存 | 接收、建索引、搜索均流式/分批，不载入整篇文本或全部位置映射（见下文设计） |
| 整篇重传更正，后台建索引期间旧版仍可查 | 新版本独立暂存表，旧版本持续可查；就绪后单事务原子切换 |
| 原文、位置映射、索引一起切换 | 切换只在三者全部落盘后发生，单事务更新目录指针 |
| 同文档同时只接受一次替换 | 进程内 asyncio 锁 + SQLite 目录状态双重保证，冲突返回 409 |
| 坏行 / 写盘失败 / 进程中断保留旧版 | 失败版本标记 `failed` 并清理暂存表；启动恢复把未完成版本统一标失败 |
| 可启动的 Docker Compose + 交互式文档演示 | `docker compose up` 后访问 Swagger UI，含一键演示数据 |

### 引用定位（换版续定位）

| 需求 | 实现 |
| --- | --- |
| 提交当前版本号、单个 `block_id`、非空原文字符范围，保存引文并返回引用 ID | `POST /documents/{doc_id}/citations`；范围按块原始 `text` 校验，过期版本一律 `409` 拒绝 |
| 换版后同 ID 块原文未变则沿用范围 | 块原文以 SHA-256 落库（`block_text_hash`），哈希一致即原样沿用字符范围（页/坐标取新版） |
| 原文变化时仅在引文逐字出现一次时迁移 | 块内逐字 `str.find` 计数（重叠分别计数）；恰好 1 次才迁移，否则 `pending_review` |
| 缺块、零次、多次一律待复核，不猜位置 | `review_reason` 区分 `block_missing` / `quote_not_found` / `quote_ambiguous` |
| 分页查询引文与当前版本块、页号、坐标、字符范围 | `GET /documents/{doc_id}/citations?limit=&offset=&status=`，返回引文、状态与定位 |
| 未完成定位不返回旧坐标 | 换版瞬间所有位置翻为 `relocating`，该状态下 `block_id/page/rect/char_*` 全部为 `null` |
| 人工为待复核引文提交当前版本新范围 | `POST /citations/{id}/confirm`，校验通过后更新引文并置 `located` |
| 慢定位期间可继续换版与人工确认 | 后台守护线程 + 持久化任务队列为独立连接，不阻塞请求；任务可被新换版标记 `superseded` |
| 迟到计算不得覆盖后来的确认或恢复旧版位置 | 所有重定位 UPDATE 均带条件（`state='relocating'` 且目标版本仍生效），人工确认同理 |
| 定位不占业务库写事务、内存不随引文数增长 | 文本扫描在读快照中完成，仅每批（默认 200 条）一次短写事务；单批即最大驻留集合 |
| 重启接续未完成定位，失败换版不影响引文 | 任务行持久化，启动把 `processing` 复位为 `pending` 续跑；换版失败不产生任务、引文不动 |

## 快速开始（Docker Compose）

```bash
# 1. 启动 API（自动构建镜像，SQLite 数据存放在命名卷 archive-data）
docker compose up --build -d

# 2. 灌入演示文档（gazette 公报、ledger 税册，幂等，可重复执行）
docker compose --profile demo run --rm seed

# 3. 打开交互式 API 文档
#    Swagger UI:  http://localhost:8000/docs
#    ReDoc:       http://localhost:8000/redoc
```

在 Swagger UI 中可直接试：

1. `PUT /documents/{doc_id}` —— 上传/替换文档，`doc_id` 填 `gazette`，
   Request body 选 `application/x-ndjson`，粘贴 `demo/gazette.ndjson` 内容；
2. `GET /documents/{doc_id}/search` —— `doc_id=gazette`，试这些查询：
   - `可用。第二條` —— **跨第 1、2 页**的命中，`spans` 含两页的块与坐标；
   - `轉型，保障` —— 同页跨块命中；
   - `檢索 系統應當 忽略排版空白` —— 查询自带任意空白仍命中；
   - `檔` —— 单字查询，多处重叠/重复命中按顺序返回；
3. 再次执行 `PUT` 粘贴修改后的 NDJSON，即完成整篇更正；
   `GET /documents/gazette/versions` 可看到旧 `ready` 版本与新版本。
4. **引用定位演示**（`citations` 标签下）：
   - `POST /documents/{doc_id}/citations` 用当前 `version_id`、`block_id`
     （如 `gazette-p2-b2`）与非空字符范围保存一条引文，返回 `citation_id`；
     故意填旧版本号可看到 `409` 拒绝；
   - 再做一次整篇 `PUT` 换版后，`GET /documents/{doc_id}/citations` 先显示
     `relocating`（坐标字段全为 `null`），后台跑完后变为 `located`（同块原文未变，
     范围沿用）或 `pending_review`（缺块/0 次/多次，含 `review_reason`）；
   - 对待复核引文调用 `POST /citations/{citation_id}/confirm` 提交当前版本新范围，
     人工确认后即恢复 `located`；
   - `GET /documents/{doc_id}/relocation-jobs` 可审计后台任务的状态与进度。

命令行可一键串起“检索 → 摘录 → 换版 → 自动重定位 → 复核”的完整流程：

```bash
ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/demo_requests.py
```

不用 Docker 时（需 Python 3.11 并能安装依赖）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/seed_demo.py
ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/demo_requests.py
```

## NDJSON 输入格式

每行一个 JSON 对象，UTF-8 编码；空行忽略；行顺序即阅读顺序：

```json
{"block_id": "gazette-p1-b4", "page": 1, "text": "保障電子文檔的真實、完整與可用。", "rect": {"x0": 96.2, "y0": 199.8, "x1": 470.5, "y1": 232.0}}
```

- `block_id`：该文档内非空且唯一，重复即整次上传失败（400）；
- `page`：正整数页码（扫描页，1 起）；
- `text`：OCR 原文，空白原样保留（用于高亮回映），匹配时忽略所有空白；
- `rect`：扫描件上的矩形，`x1>=x0`、`y1>=y0`，允许零面积矩形。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含 Compose 健康检查） |
| `GET` | `/documents` | 文档清单及当前生效版本 |
| `PUT` | `/documents/{doc_id}` | 整篇上传/替换（202，后台建索引）；坏行 400、写盘失败 507、并发替换 409 |
| `GET` | `/documents/{doc_id}/search?q=...` | 检索，去空白后 1–80 字符；未知文档 404 |
| `GET` | `/documents/{doc_id}/versions` | 版本与状态（ready/failed/ingesting/indexing） |
| `POST` | `/documents/{doc_id}/citations` | 保存引文（201）；过期版本 409、块不存在 404、范围非法 400 |
| `GET` | `/documents/{doc_id}/citations` | 分页查询引文（`limit/offset/status`），含当前版本定位 |
| `GET` | `/citations/{citation_id}` | 单个引文及其当前定位 |
| `POST` | `/citations/{citation_id}/confirm` | 人工确认待复核引文的新范围（仅 `pending_review` 可确认） |
| `GET` | `/documents/{doc_id}/relocation-jobs` | 后台重定位任务队列审计（pending/processing/done/superseded） |
| `GET` | `/recovery` | 本次启动完成的崩溃恢复记录 |

命中结构（节选）：

```json
{
  "compact_start": 44, "compact_end": 50,
  "matched_text": "可用。第二條",
  "crosses_blocks": true, "crosses_pages": true,
  "spans": [
    {"block_id": "gazette-p1-b4", "page": 1,
     "rect": {"x0": 96.2, "y0": 199.8, "x1": 470.5, "y1": 232.0},
     "char_start": 13, "char_end": 16, "block_order": 3},
    {"block_id": "gazette-p2-b1", "page": 2,
     "rect": {"x0": 96.2, "y0": 110.0, "x1": 520.4, "y1": 142.2},
     "char_start": 0, "char_end": 3, "block_order": 4}
  ]
}
```

`char_start/char_end` 是该块**原始 `text`**（含空白）上的半开区间，前端可直接
切片高亮；矩形坐标可用于扫描件上画框。

## 引用定位工作流

引文（citation）是研究者对某一文档版本中某个 OCR 块原文的逐字摘录。两份表把
**不可变的摘录**与**随当前版本变化的定位**分开：`citations` 保存摘录事实，
`citation_locations` 保存当前放置；`relocation_jobs` 是可重启的后台任务队列。

### 1. 保存引文（必须锚定当前版本）

```bash
curl -s -X POST http://localhost:8000/documents/gazette/citations \
  -H 'content-type: application/json' \
  -d '{"version_id": 3, "block_id": "gazette-p2-b2",
       "char_start": 7, "char_end": 13}'
```

```json
{
  "citation_id": 1, "doc_id": "gazette",
  "created_version_id": 3, "version_id": 3, "state": "located",
  "quote": "忽略排版空白", "block_id": "gazette-p2-b2", "page": 2,
  "rect": {"x0": 96.2, "y0": 149.6, "x1": 512.8, "y1": 181.9},
  "char_start": 7, "char_end": 13, "review_reason": null
}
```

- `version_id` 必须等于该文档当前生效版本，否则 `409` 并在 `detail` 中回带当前版本号；
- 范围须满足 `0 <= char_start < char_end <= len(block text)`，且至少含一个非空白字符；
- 创建即对当前版本 `located`，坐标立即可用。

### 2. 换版后后台自动重定位

对文档执行整篇 `PUT` 换版并原子切换后，服务在同一短事务内：为该文档排队一个
重定位任务，并把其全部引文位置翻为 `relocating`。随后后台守护线程按批（默认每批
200 条）处理，每条引文按以下规则决策：

1. 新版本存在同 `block_id` 且块原文 SHA-256 未变 → **沿用原字符范围**（页号/矩形取新版）；
2. 否则在新块原文中逐字查找引文：**恰好出现一次**才迁移到该位置（重叠出现分别计数）；
3. 块缺失、出现 0 次或多次 → `pending_review`，记录 `block_missing` /
   `quote_not_found` / `quote_ambiguous`，等待人工，绝不猜测。

`relocating` 与 `pending_review` 状态下，列表/单查接口的 `block_id`、`page`、
`rect`、`char_start`、`char_end` 一律为 `null`——**未完成定位绝不返回上一版的旧坐标**。

### 3. 人工复核与确认

```bash
curl -s -X POST http://localhost:8000/citations/1/confirm \
  -H 'content-type: application/json' \
  -d '{"block_id": "gazette-p2-b2", "char_start": 2, "char_end": 6}'
```

确认范围按当前版本块原文校验；成功后引文变为 `located`，`quote` 更新为确认的
新原文。仅 `pending_review` 可确认：`located` 再确认返回 `409`，`relocating`
期间确认也返回 `409`（请等后台跑完）。

### 4. 并发与一致性保证

- **慢定位不阻塞业务**：重定位跑在独立守护线程与独立 SQLite 连接上；文本扫描在
  只读快照（`BEGIN DEFERRED`）中进行，计算完成后每批仅一次短写事务，不长期占用
  业务库写事务。换版与人工确认始终可继续。
- **迟到计算不可覆盖新结果**：每条重定位写入都带条件
  `WHERE ... AND state = 'relocating'`；人工确认写入带条件
  `AND state = 'pending_review'`。若重定位期间又换版，旧任务在下次批处理时检测到
  目标版本已非当前版本而整体置 `superseded`，其迟到 UPDATE 因而不可能覆盖人工确认，
  也不可能把坐标恢复到旧版本。
- **内存有界**：任何时刻只持有一个批次的引文与块数据；测试用 4000 条引文断言
  `tracemalloc` 峰值 < 2MB，与引文总量无关。
- **重启接续**：任务是持久化行，启动恢复把残留 `processing` 复位为 `pending`，
  工作线程续跑；引文行在等待期间保持 `relocating`，重启前后都不会泄露旧坐标。
- **失败换版无副作用**：入库失败的版本不会激活、不产生重定位任务，既有引文保持
  `located` 不变。

### 状态机一览

```
创建引文 ──► located
换版排队 ──► relocating ──┬─ 同块原文未变 / 引文唯一出现 ──► located
                          └─ 缺块 / 0 次 / 多次 ──► pending_review
人工确认 pending_review ──► located（条件写，迟到任务无法覆盖）
再次换版：所有 located/pending_review ──► relocating，旧任务 superseded
```

## 设计要点：为什么大文档不会撑爆内存

- **接收**：`request.stream()` 分块进入有界队列（默认 32 块），工作线程逐行
  增量 UTF-8 解码与 JSON 解析，单行超过 4MB 直接判失败；
- **建索引**：每读一块就增量计算紧凑字符位置，按批（默认 500 块）写入该版本
  独立的三张表，绝不累积整篇文本：
  - `blocks_<vid>`：原文、矩形、每块紧凑起止位置（回表与字符回映）；
  - `unigrams_<vid>(ch, pos)`：单字倒排；
  - `bigrams_<vid>(bg, pos)`：二元组倒排，含**跨块边界二元组**（含跨页）；
- **搜索**：单字查询走 unigram；多字查询选取频次最低的二元组，按位置有序
  游标读取候选，逐个只回表读取命中跨度涉及的少数块做验证，单查询只读一致
  快照，不加载全量位置映射；
- **内存自测**：`tests` 中用 2 万块的文档断言 `tracemalloc` 峰值 < 4MB。

## 版本、原子切换与崩溃恢复

- 每次替换创建新版本号与独立暂存表，状态机
  `ingesting → indexing → ready`（异常 → `failed`）；
- 只有 `ready` 版本可被检索；激活在单事务内翻转 `documents.active_version_id`，
  旧版表在切换提交后的第二个事务中删除，读者始终看到完整的一版；
- 坏行、重复块 ID、写盘异常都在请求内把新版本标 `failed` 并清表，旧版不动；
- 进程被杀死后，重启时 `startup_recovery()` 把所有非终态版本标 `failed`、
  删除残留暂存表，并通过 `/recovery` 暴露处理记录。
- 引用定位的三张表（`citations` / `citation_locations` / `relocation_jobs`）
  是跨版本的目录级表，不随旧版块表删除；重启时 `recover_jobs()` 把残留
  `processing` 任务复位为 `pending` 由工作线程接续。

## 测试

```bash
# 标准环境（有 fastapi/pytest 时）
pytest -q

# 离线/无包索引环境：stdlib 垫片 + unittest（本仓库沙箱即此模式）
python -I scripts/run_tests.py     # 41 个单元/集成测试（含 19 个引用定位用例）
python -I scripts/smoke_test.py    # 真实 HTTP 端到端冒烟（28 项检查，含后台工作线程）
```

`tests/_shim/` 仅是离线环境对 fastapi/pydantic 的最小垫片，不进入 Docker 镜像
（见 `.dockerignore`），生产代码只依赖 `requirements.txt` 中的真实组件。垫片在
独立守护线程中运行与 uvicorn 等价的 lifespan，使后台重定位工作线程在离线
WSGI 冒烟下行为与生产一致。

## 目录结构

```
app/
  main.py        FastAPI 应用、流式入库、检索与引用 API、lifespan
  db.py          SQLite 目录、版本状态机、崩溃恢复、引用/任务表结构
  ingest.py      流式 NDJSON 入库与增量索引
  search.py      去空白跨块/跨页检索引擎
  citations.py   引文保存、分页查询、人工确认、重定位引擎与持久化任务队列
  worker.py      后台重定位守护线程（有界批处理、独立连接、可重启）
  config.py      环境变量与限额
  schemas.py     请求/响应模型（驱动 OpenAPI/Swagger）
demo/            两份中文 NDJSON 示例文档
scripts/         演示播种、演示请求、测试运行器与离线冒烟
tests/           测试与离线垫片
Dockerfile, docker-compose.yml, requirements.txt
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ARCHIVE_DATA_DIR` / `ARCHIVE_DB_PATH` | `/data` / `/data/archive.sqlite3` | 数据目录与 SQLite 文件 |
| `ARCHIVE_RELOCATE_BATCH` | `200` | 重定位单批引文数（内存上界） |
| `ARCHIVE_RELOCATE_POLL` | `0.25` | 无任务时工作线程轮询间隔秒 |
| `ARCHIVE_RELOCATE_THREAD` | `1` | 置 `0` 关闭后台线程（测试用，任务仍可手动逐批处理） |
| `ARCHIVE_INSERT_BATCH` / `ARCHIVE_MAX_LINE_BYTES` | `500` / `4MB` | 入库批大小与单行上限 |
