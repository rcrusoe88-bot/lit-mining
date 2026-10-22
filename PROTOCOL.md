# 文献挖掘协议（Lit-Mining Protocol）

这个文件是所有 agent（Claude Code / Codex / 其他）共用的流程契约。
`SKILL.md` 和 `AGENTS.md` 只是各自的入口适配，实际步骤以本文件为准。

---

## 适用场景

**从零开始一个新主题的文献挖掘。** 输入一句话主题，输出分层文献清单 + 可下载的全集。

不适用：

- 给定 DOI 列表批量下载 → 直接跑 `download.py`
- 已有文献库的整理 → 不需要 `probe` / `harvest`
- 中文文献（CNKI / 万方 / 维普需验证码且要机构订阅）

---

## 六步流程

```
1. 拆解    用户给主题 → LLM 生成 search_config.yaml
2. 探量    probe.py → 各检索式命中数
3. 审核    用户看命中数，改配置或确认
4. 采集    harvest.py → raw/*.tsv
5. 分诊    triage.py → out/scored.tsv
6. 下载    download.py → pdfs/
7. 交付    report.py → README.md + 待人工清单
```

步骤 1–3 是**人和 LLM 的循环**，4–7 是**纯脚本执行**。

---

## 步骤 1：拆解

输入：用户的一句话主题。

输出：`search_config.yaml`（见 `templates/search_config.yaml.example`）。

关键要求：

- **维度必须拆成多组**，不能写一条巨式。每组对应一个 `dimensions` 条目。
- 每组都要有 `lib`（检索式主体），脚本会自动拼成 `(lib) AND (platform) AND (load)`。
- 必须提供 `strong_terms` 和 `weak_terms` 两套评分词。

**拆解时先自查三件事**（这一步是踩过坑的）：

1. 主题里的缩写，在 PubMed 里是否真是那个意思？（实测过 `APCCS` 实际是「醛固酮生成细胞簇」，与偶联化学无关）
2. 平台/载荷口径是否过宽？（`siRNA` 混进 `mRNA` 会稀释结果）
3. 相邻领域要不要收？（脂质体 vs 聚合物 vs 无机载体）

---

## 步骤 2：探量

```bash
python scripts/probe.py --config search_config.yaml
```

只查 `count`，不抓题录。耗时约 1 分钟。

输出形如：

```
DIM_cell_T        948
DIM_cell_NK        83
DIM_chem_maleimide 12
TOTAL (union)    2914
```

**低于 10 条**说明检索式太窄；**高于 5000 条**说明太宽，要收。

---

## 步骤 3：审核

把 `probe.py` 的输出原样给用户看，**等确认**。

用户可以：

- 改 YAML 里的检索式 / 词表
- 加 / 删维度
- 直接说「就这样跑」

**非交互场景**（脚本批跑、CI）可以加 `--skip-review` 跳过，但最终报告必须标注「检索式未经人工审核」。

---

## 步骤 4：采集

```bash
python scripts/harvest.py --config search_config.yaml --out raw/
```

四个源，顺序执行，各自落 TSV：

| 源 | 文件 | 说明 |
|---|---|---|
| PubMed | `raw/pubmed.tsv` | E-utilities，题录 + 摘要 |
| Europe PMC | `raw/epmc.tsv` | REST，补漏 + 预印本 |
| OpenAlex | `raw/openalex.tsv` | OA 判定 + pdf_url |
| DataCite | `raw/datacite.tsv` | 学位论文（可选，`--with-theses`） |
| Google Scholar | `raw/scholar.tsv` | 补充源（可选，`--with-scholar`） |

**坑**：`esearch` 默认 `retmax` 会静默截断。`harvest.py` 内部已改成先查 count 再分页，不要绕过。

**坑（实测，网络抖动导致静默丢数据）**：NCBI E-utilities 偶发
`SSL: UNEXPECTED_EOF_WHILE_READING` 和 `RemoteDisconnected`。实测一次
`esummary` 请求 100 个 PMID，重试 4 次全挂，**整批 100 条数据静默消失**
（返回 `{}`，调用方当成「没数据」）。

后果很严重：采集跑完了，报告说「这个方向只有 20 篇文献」，其实有 120 篇。

对策（已实现在 `harvest.py`）：

- 一次请求代表一批数据的调用点，用 `http_json_checked()` 而不是 `http_json()`，
  失败要抛异常
- 失败后**降级到小批次重试**（100 → 20 → 10），而不是直接放弃这一批
- `efetch` 摘要批次用 50 而不是 100 —— 返回的 XML 更大，连接被掐概率更高
- 采集结束后**核对条数**：`esummary` 拿到的 ID 数必须等于 `esearch` 给的 ID 数，
  少了就说明丢了

不要用「重试几次还是失败就跳过」的方式 —— 那正是丢数据的原因。

### Google Scholar（可选增强源）

Scholar 能捞到 PubMed 漏掉的论文（会议论文、预印本、社论），**更是唯一
能挖到「作者自存档」的地方** —— 很多论文没有 PMC ID 也没有 OA 标记，
但作者把 accepted manuscript 放在实验室主页或机构库里，Scholar 的
「所有版本」页会列出这些。

**前置条件**（缺了就跳过，别卡主流程）：

1. 装 `browser-use`，并在日常 Chrome 里开 `chrome://inspect/#remote-debugging`
   勾选「Allow remote debugging for this browser instance」
2. 日常 Chrome 要有 Google 账号登录态（CDP 干净实例会被反爬拦死）
3. 维度配了 `scholar_query`（**纯英文**）

```bash
python scripts/scholar.py --config search_config.yaml --out raw/
```

**坑（Europe PMC 查询解析器的三个怪癖，都实测过）**：

1. **通配符几乎不工作。** `liposom*[Title/Abstract]` 在 PubMed 上 4788 命中，
   在 EPMC 上只有 18；和其他词 AND 时更是直接归零。
   *对策*：把 `词*` 展开成 `("词"[字段] OR "词s"[字段])`，每个变体都自带字段标签。

2. **OR 组越长结果越少，超过 6 个词就归零。** 实测同一组词：
   ```
   A AND (mRNA)                               → 102
   A AND (mRNA OR circRNA)                    → 2
   A AND (mRNA OR "messenger RNA")            → 21
   A AND (mRNA OR "messenger RNA" OR circRNA) → 0    ← 三个词就没了
   ```
   同一查询在 PubMed 上是 878 —— 所以这是 EPMC 侧的解析缺陷，不是语料差异。
   *对策*：把三个 OR 组（维度/载荷/平台）全部拆成单分支，组合成多个简单查询，
   分别跑完再合并去重。实测命中从 0 变成 11633 / 1167 / 3314。

3. **负载高时 302 到不可达主机。** Location 指向
   `http://rest-api-live.europepmc.org/europepmc/webservices/rest/?query=...`，
   那个主机名直连会失败，而且路径结尾少了 `search` 这一段。
   *对策*：`common.py` 的 `_FixRedirect` 把主机换成 `www.ebi.ac.uk`、
   协议换成 https、补回 `search`。不处理就是随机 302 → RemoteDisconnected。

**坑（都踩过）**：

| 坑 | 现象 | 应对 |
|---|---|---|
| 检索词含中文 | 搜出中文/日文泛综述 | `scholar_query` 必须纯英文 |
| 第 1 页带 `&start=0` | 返回 0 条 | 首页不带 `start`，后续页才带 |
| `js()` helper 返回空 | 提取不到 DOM | 必须用 `cdp("Runtime.evaluate", ...)` |
| surrogate 字符 | stdout 报 UnicodeEncodeError | 结果写文件 + `os._exit(0)` 绕开回显 |
| `errors="replace"` 写文件 | JSON 转义串损坏，解析失败 | 用 `errors="surrogatepass"` |
| 高频请求 | 「Sorry / automated queries」 | 维度间隔 ≥10s；触发后基本要等 24h |

---

## 步骤 5：分诊

```bash
python scripts/triage.py --config search_config.yaml --raw raw/ --out out/scored.tsv
```

做三件事：

1. **去重**：DOI 优先，标题兜底（去标点、小写、截 120 字符）
2. **打分**：标题 + 摘要联合打分（**绝不能只看标题**，实测只看标题会让核心层从 276 掉到 7）
3. **分层**：`核心` / `外围` / `背景`

打分规则：

```
+3  命中 strong_terms（具体的、可验证的名词）
+1  命中 weak_terms（泛化词汇）
+2  有对照设计（compared with / versus / control）
+2  命中 load_terms（载荷）
+1  命中对象词（细胞类型 / 靶器官等）
≥threshold → 核心，≥3 → 外围，否则背景
```

---

## 步骤 6：下载

```bash
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
```

**只下 OA，不碰付费墙。**

通道优先级：

```
1. PMC OA S3        开放数据桶直取        ← 首选，98.7% 覆盖，无需浏览器
2. OpenAlex PDF     locations[].pdf_url
3. Europe PMC XML   fullTextXML           ← 仅当 PDF 不存在
4. Scholar 自存档    「所有版本」页的 PDF   ← 加 --scholar 才启用
```

**PMC 通道的关键**（这是实测出来的，别改）：

PMC 站点的 PDF 直链有**工作量证明（PoW）**挑战，返回 200 + HTML 而非 PDF。
浏览器每篇都要重跑一次挑战，批量时叠加限速 —— 换指纹、换反检测库、接管登录态
全都无效，这是层级错配。

```python
# ❌ 浏览器去啃 PMC  → 每篇重跑 PoW，批量必崩
# ✅ 官方开放数据桶  → 无鉴权、无 PoW、无 cookie
#   https://pmc-oa-opendata.s3.amazonaws.com/PMC13598902.1/PMC13598902.1.pdf
```

两个必须保留的防御：

```python
# 1. 精确匹配，排除补充材料
pat = re.compile(rf"^{pmcid}\.\d+/{pmcid}\.\d+\.pdf$")
#    同目录还有 41467_2026_75923_MOESM1_ESM.pdf，取第一个会拿错

# 2. retry，否则约 1.3% 的正常文献被误判成「无全文」
#    并发慢响应不是真没有，串行重试立刻 200
```

**OpenAlex / Scholar 通道仍需浏览器**（出版社页面拦的是 403 / 包装页）：

```python
# ❌ curl / urllib / ctx.request  → 403 或返回 HTML
# ✅ 页面上下文里 fetch
pg.goto(文章页)                    # 建立会话
pg.evaluate("fetch(pdf_url)")      # 带完整浏览器指纹
```

详见 `scripts/browser.py` 的注释。

**Scholar 自存档通道**（`--scholar`）：

```
搜标题（加引号精确匹配）→ 拿 cluster → 进「所有版本」页 → 逐个 PDF 直链试下
```

下载走三段降级：`HTTP → Playwright CDP 同源 fetch → browser-use 同源 fetch`。
后两者的原理和 PMC 一样：**先 goto 目标域根页建立同源上下文，再在页面里
fetch 同域相对路径** —— 请求头/UA/TLS 指纹全是真浏览器的，WAF 认不出来。

也可单独跑（救一批已有记录）：

```bash
python scripts/scholar.py --rescue --in out/scored.tsv --pdfdir pdfs/ --tier core
```

每篇下载后记录状态：

```
full_text   拿到正文（PDF 或 XML）
none        没有 OA 版本
paywall     有 OA 记录但实际被拦
```

---

## 步骤 7：交付

```bash
python scripts/report.py --in out/scored.tsv --pdfdir pdfs/ --out README.md
```

产出：

- `README.md`：分层清单 + 方法学说明 + 领域判断
- `out/pending.md`：未拿到的，**按原因分类**（付费墙 / 无 OA / 网络不可用 / 该刊无全文）

---

## 可复用的经验（换主题直接看这里）

### 检索

- **拆成多组检索式**，每组命中数就是研究结论本身
- 先 `retmax=0` 只取 count，超限再分页
- 不认识的缩写，先查命中标题，别直接构造检索式

### 筛选

- **必须用标题 + 摘要打分**。工艺参数、化学方法几乎从不写在标题里
- 强/弱信号词必须分开。`conjugated` 太泛（+1），`maleimide` 具体（+3）
- 硬门槛：必须像目标平台的论文，否则踢到背景

### 下载

- **PMC 走官方开放数据 S3 桶**，不要用浏览器啃 —— 那是 PoW 的门，跟权限无关
- **OpenAlex / Scholar 走 `pg.evaluate(fetch)`**，`ctx.request` 和 `curl` 都是 403
- 任何 403 先试「预热 + 等待」，别急着判定「付费墙」
- 出版商 SI（补充材料）不是全文，**不算已下载**
- S3 桶里取 PDF 必须精确匹配 `{PMCID}.{ver}.pdf`，否则会拿到 SI

### 平台特性

| 站点 | 特性 | 应对 |
|---|---|---|
| PubMed | `retmax` 静默截断 | 先查 count |
| PubMed | 偶发 SSL EOF / RemoteDisconnected | `http_json_checked` + 降级小批重试 |
| Europe PMC | `pageSize=1000` 超时 | 降到 300 + cursorMark |
| Europe PMC | 偶发 502 | 批次重试；失败要能看到 |
| OpenAlex | **不支持 PubMed 语法**，`[Title/Abstract]` 会 400 | 单独构造自然语言查询 |
| OpenAlex | **不接受通配符**，带 `*` 会 400 | 查询词剥掉 `*` |
| OpenAlex | 词太多召回塌掉（14 词 → 0 条） | 只取 strong 词 + 平台词，≤6 个 |
| OpenAlex | 国内直连超时 21s | 设 `HTTPS_PROXY` |
| PMC | 站点 PDF 有 PoW 挑战 | 走官方 S3 桶，不要用浏览器硬啃 |
| PMC OA S3 | 偶发 RemoteDisconnected | retry + 超时放宽到 240s |
| Google Scholar | 反自动化 + 需登录态 | browser-use 走日常 Chrome；没有就跳过 |
| 中文库 | 验证码 / 机构登录 | 不做，输出检索式给用户 |

### 网络预检

```bash
python scripts/common.py
```

网络坏的时候（代理没起、DNS 被 fake-IP 劫持但 TUN 转发断了），
采集会静默返回空，错误信息指向「没命中」而不是「网络不通」，排查成本极高。

`harvest.py` 会自动跑预检，网络不通直接中止（`--skip-preflight` 跳过）。
预检失败时先看输出里的端点名和耗时 —— 5 秒固定超时通常是 DNS/握手被掐。

### 浏览器通道的三个坑（Windows 尤其明显）

1. **任何 CDP 返回值直接 print 都会炸** —— Scholar 标题带 emoji/特殊字符时
   stdout 报 `UnicodeEncodeError: surrogates not allowed`。解法：结果写文件，
   片段结尾 `os._exit(0)` 绕开 harness 回显；或统一 `ensure_ascii=True`。
2. **`js()` helper 会返回空** —— 必须用 `cdp("Runtime.evaluate", returnByValue=True)`。
3. **Chrome 155 的远程调试不走标准 CDP HTTP 端点**（`/json/version` 返 404），
   Playwright 连不上日常 Chrome。所以日常浏览器只有 browser-use 一条路。

---

## 状态字段约定

`scored.tsv` 的 `status` 列只有这四个值：

```
full_text       拿到正文
si_only         只有补充材料（不算全文）
abstract_only   只有摘要
none            什么都没有
```

不要用「已下载」这种模糊表述。
