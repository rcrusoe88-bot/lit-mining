name: lit-mining
description: |
  从零开始的新主题文献挖掘：检索 → 筛选 → 分层 → 下载 OA 全文 → 交付清单。
  用户给一句主题，自动拆解检索式、探测命中量、人工审核后采集分诊并下载。
  触发场景：用户要求"帮我找某主题的所有文献"、"系统性检索"、"文献综述的底表"、
  "把这个方向的论文都找出来并下载"、"帮我摸清这个方向有多少人在做"。
  不适用：给定 DOI 列表批量下载（用 pmc-oa-pdf）、已有文献库整理、
  中文文献（CNKI/万方需机构订阅）。
---

# 文献挖掘（Lit-Mining）

把一句话主题变成一份**分层、可下载、可复核**的文献清单。

**流程契约见 [PROTOCOL.md](PROTOCOL.md)** —— 那是所有 agent 共用的真源。
本文件只讲 Claude Code 下的用法。

---

## 什么时候用

✅ **用**：

- 「帮我找靶向 LNP 工艺开发的所有文献，能下的下载下来」
- 「系统性检索一下 mRNA 疫苗稳定性这个方向」
- 「把这个领域的论文整理成表，标注哪些能免费下」

❌ **不用**：

- 「下载这几篇」（给了 DOI 列表）→ 用
  [pmc-oa-pdf](https://github.com/rcrusoe88-bot/pmc-oa-pdf)，更快
- 「整理我电脑里的 PDF」→ 不需要检索
- 「找中文硕博论文」→ 中文库需验证码 + 机构订阅

---

## 执行流程

### 第 0 步：连通性自检（可选但建议）

```bash
python scripts/common.py
```

打印各端点连通情况和当前代理设置。`harvest.py` 会自动跑预检，
必需端点不通时直接中止（`--skip-preflight` 可跳过）。

### 第 1 步：拆解（你来做）

用户给主题句后，**你**要生成 `search_config.yaml`。参考
`templates/search_config.yaml.example`。

**必须做**：

1. 把主题拆成**多组维度**（细胞类型 × 化学方法 × 工艺环节 这样的交叉）
2. 给每组写 `lib`（PubMed 检索式）和 `terms`/`strong`（评分词）
3. 区分强信号（具体名词，+3）和弱信号（泛化词，+1）

**拆解前先自查**（这三个坑都踩过）：

- 主题里的缩写，在 PubMed 里是不是那个意思？
  （实测：`APCCS` 实际是「醛固酮生成细胞簇」，与偶联化学无关，整条线归零）
- 平台/载荷口径会不会太宽？（`siRNA` 混进 `mRNA` 会稀释）
- 相邻领域收不收？（脂质体 vs 聚合物载体）

### 第 2 步：探量

```bash
python scripts/probe.py --config search_config.yaml
```

把输出**原样**给用户看。重点看：

- 有没有维度命中 <10（可能术语写错）
- 有没有维度命中 >5000（可能太宽）
- 合并总数是多少

### 第 3 步：等确认

**必须等用户明确说「可以跑」**。用户可以改配置。

### 第 4 步：采集 + 分诊

**采集必须写进一个全新的（或空的）目录**。`harvest.py` 见到已有源 TSV
会拒绝覆盖——旧结果混进新运行，比跑失败难发现得多。想重跑就换目录。

```bash
python scripts/harvest.py --config search_config.yaml --out raw/
```

采集期间目录里有 `.incomplete` 标记；只有**所有指定源都成功**才会删掉。
中途失败会保留标记并以非零退出。`triage.py` 见到该标记直接拒绝，
不会把半批数据当完整结果。

可选加 Google Scholar（补充源，能捞到 PubMed 漏掉的论文）。**必须在 triage 之前跑完**，
否则它写出的 `scholar.tsv` 不会进入本次分诊：

```bash
python scripts/scholar.py --config search_config.yaml --out raw/
```

需要 `browser-use` + 日常 Chrome（见下文「Google Scholar」）。**没有就跳过，不影响主流程。**

```bash
python scripts/triage.py --config search_config.yaml --raw raw/ --out out/scored.tsv
```

`triage.py` 跑完看分层比例：

- 核心为 0 → 词表太严或阈值太高
- 核心 >30% → 弱信号被当成强信号了

### 第 5 步：下载

```bash
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
```

**只下 OA**。状态与真实原因写在 `status` / `failure_reason` / `access_note`：
`failure_reason` 只说明这次为什么没拿到（network / http_error / invalid_content /
not_found / unknown），**不能据此推断期刊 OA 政策或是否付费墙**。

只有 `status=full_text` **且文件通过最低校验**（存在、PDF 头尾完整或 XML 可解析且有正文）
才计入「已获全文」。校验只排除 HTML 伪装和明显截断，不证明文件与题录一致。

每篇下载完立即回写 TSV，中断后已完成的状态不会丢。

想再抢救一批（从 Scholar 的「所有版本」页找作者自存档）：

```bash
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core --scholar
# 或单独跑
python scripts/scholar.py --rescue --in out/scored.tsv --pdfdir pdfs/ --tier core
```

如果 CDP 没启动，先起浏览器：

```bash
# Windows
cmd //c "<项目>/start_cdp.bat"
# 或直接
"C:\Program Files\Google\Chrome\Application\chrome.exe" \
  --remote-debugging-port=9223 --remote-allow-origins=* \
  --user-data-dir="%USERPROFILE%\chrome-cdp-profile"
```

### 第 6 步：交付

```bash
python scripts/report.py --in out/scored.tsv --pdfdir pdfs/ --out README.md
```

产出 `README.md`（分层导读）+ `out/pending.md`（未获取，按原因分类）。

**交付时主动说明**：

- 核心层命中多少、占比多少
- 未获取的按原因分布
- 这个领域的文献厚度（是不是很薄）

---

## 下载通道（实测结论，不要改）

```
1. PMC OA S3      开放数据桶直取        首选，98.7% 覆盖，无需浏览器
2. OpenAlex PDF   locations[].pdf_url   出版商直供
3. EPMC XML       fullTextXML           仅当 PDF 不存在
4. Scholar 自存档  「所有版本」页的 PDF    --scholar 才启用
```

### 第 1 通道为什么是 S3 而不是浏览器

PMC 站点的 PDF 直链有**工作量证明（PoW）**挑战，不是指纹检测：

```
GET https://pmc.ncbi.nlm.nih.gov/articles/PMC13598902/pdf/DDR-87-e70392.pdf
→ 200, content-type: text/html, 1814 bytes
```

返回的是挑战页，要求找 nonce 使 `sha256(challenge + nonce)` 以 `0000` 开头，
通过后写 cookie 再 `location.reload()`。

用浏览器能过，但每篇都要重跑一次挑战，批量时还叠加限速。
**换指纹、换反检测库、接管登录态，全都治不好** —— 这是层级错配，不是伪装不够。

官方开放数据桶无鉴权、无 PoW、无 cookie：

```
https://pmc-oa-opendata.s3.amazonaws.com/PMC13598902.1/PMC13598902.1.pdf
→ 200, 1,975,365 bytes
```

实测 150 篇跨主题抽样覆盖 **98.7%**（LNP/mRNA 100%、CAR-T 100%、circRNA 96%）。

**两个必须保留的防御**：

- 精确匹配 `{PMCID}.{ver}.pdf` —— 同目录混着 `*_MOESM*_ESM.pdf` 补充材料，
  取第一个会拿错
- retry —— 不加会把约 1.3% 的正常文献误判成「无全文」（并发慢响应，不是真没有）

单独批量下载 PDF 用 [pmc-oa-pdf](https://github.com/rcrusoe88-bot/pmc-oa-pdf)，
它有并发和 manifest，更适合大批量。

### OpenAlex / Scholar 通道仍需浏览器

这两个通道面对的是出版社页面和实验室主页，它们拦的是 403 / 包装页，
`pg.evaluate(fetch)` 依然是对的办法：

| 方式 | 结果 |
|---|---|
| `curl` / `urllib` | 200 但返回 HTML |
| `ctx.request.get()` | 403 |
| `pg.goto(pdf_url)` | 200 但返回 HTML 包装页 |
| **`pg.evaluate(fetch)`** | ✅ `%PDF-` |

`fetch` 跑在页面 JS 环境，请求头、UA、Client Hints、cookie、TLS 指纹全部由浏览器生成。

Scholar 通道里的 PDF 直链同理 —— 实验室主页经常 403，但用 browser-use
先 goto 该域根页、再在页面里 fetch 同域相对路径就能拿到。

---

## Google Scholar

**默认关闭，但强烈建议打开** —— 它是唯一能挖到「作者自存档」的地方：
很多论文没有 PMC ID 也没有 OA 标记，但作者把 accepted manuscript 放在
实验室主页或机构库里，Scholar 的「所有版本」页会列出这些。

**启用条件**（缺了就跳过，不影响主流程）：

1. 装 `browser-use`
2. 日常 Chrome 里开 `chrome://inspect/#remote-debugging`，勾选
   「Allow remote debugging for this browser instance」（一次性设置）
3. 日常 Chrome 有 Google 账号登录态

**CDP 干净实例必被拦** —— Google 的反自动化检测认的是浏览器画像，
不是 IP。实测过 CDP 出口 IP 已经是美国洛杉矶，仍然被拦。

**配置要求**：每个维度的 `scholar_query` 必须是**纯英文**。
用中文词搜出来的是中文/日文泛综述，跟主题完全无关。

**检测与跑通**：

```bash
python scripts/browser.py    # 报告各适配器可用性
```

Scholar 相关的坑（都踩过，详见 PROTOCOL.md）：
`&start=0` 返回 0 条、`js()` helper 返回空、surrogate 字符炸 stdout、
高频请求触发 24h 封禁。

---

## 常见问题

**Q：为什么核心层这么少？**
先看 `triage.py` 的输出。如果强信号词表太严，核心会很少；如果太宽，核心会膨胀。
调整 `search_config.yaml` 里的 `strong` 词表和 `thresholds` 重跑 `triage.py`（不用重新采集）。

**Q：为什么某篇明明有 OA 却下不到？**
依次检查：
1. `pmcid` 有没有值
2. S3 桶里有没有正文 PDF：
   ```bash
   curl -s "https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC13598902.&max-keys=200" \
     | grep -oE "<Key>[^<]*</Key>"
   ```
   没有 `{PMCID}.{ver}.pdf` 就说明这篇在 PMC 但不在开放数据子集
3. 是不是 embargo（作者稿，正文 PDF 尚未放出）
4. **不要再去浏览器里试 PMC 页面了** —— 页面能打开不代表能下 PDF，那是 PoW 的门，
   跟权限无关

**Q：能不能下付费文献？**
不能。只做 OA。付费的进 `pending.md`，标注原因，用户自己走机构订阅。

**Q：Windows 控制台报 `UnicodeEncodeError: 'gbk' codec ...`？**
旧版本有这问题。v1.1.0 起 `common.py` 在导入时会把 stdout/stderr 切到 UTF-8，
`✓ ⚠ →` 这类字符可以正常打印。若仍遇到，确认脚本是从 `scripts/` 目录导入 `common` 的。

---

## 自检

```bash
python scripts/selfcheck.py            # 含网络
python scripts/selfcheck.py --offline  # 仅逻辑
```

---

## 文件结构

```
lit-mining/
├── SKILL.md                    本文件（Claude 适配）
├── AGENTS.md                   Codex / ChatGPT 适配
├── PROTOCOL.md                 流程契约（通用，改主题看这个）
├── README.md                   人类入口
├── scripts/
│   ├── common.py               配置/HTTP/TSV/连通性预检
│   ├── probe.py                命中量探测
│   ├── harvest.py              多源采集
│   ├── triage.py               去重 + 打分 + 分层
│   ├── browser.py              浏览器抽象（browser-use / CDP / HTTP）
│   ├── scholar.py              Google Scholar（补充源 + 自存档救援）
│   ├── download.py             OA 下载
│   ├── report.py               报告生成
│   ├── agent_entry.py          Codex/ChatGPT 入口
│   └── selfcheck.py            自检
└── templates/
    └── search_config.yaml.example
```

---

## 合规

只做开放获取，不碰付费墙。下载后请遵守各篇的 CC 许可条款。
Google Scholar 通道仅用于**定位**已公开的自存档，不下载受限内容。
