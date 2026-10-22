---
name: lit-mining
description: |
  文献挖掘工作流：检索 → 筛选 → 分层 → 下载 OA 全文 → 交付清单。
  用户给一句主题，自动拆解检索式、探测命中量、人工审核后采集分诊并下载。
  触发词：系统性检索、文献综述底表、帮我找某主题的所有文献、
  摸清这个方向有多少人在做、整理成表标注哪些能免费下。
  不适用：给定 DOI 列表批量下载、已有文献库整理、中文文献（CNKI/万方需机构订阅）。
agent_created: true
---

# 文献挖掘（Lit-Mining）

把一句话主题变成一份**分层、可下载、可复核**的文献清单。

**流程契约见 [PROTOCOL.md](PROTOCOL.md)** —— 那是所有 agent 共用的真源。
本文件只讲 WorkBuddy 下的用法。

---

## 什么时候用

✅ 「系统性检索一下 mRNA 疫苗稳定性这个方向」
✅ 「帮我找靶向 LNP 工艺开发的所有文献，能下的下载下来」
✅ 「把这个领域的论文整理成表，标注哪些能免费下」

❌ 「下载这几篇」（给了 DOI 列表）→ 直接用
[pmc-oa-pdf](https://github.com/rcrusoe88-bot/pmc-oa-pdf)，更快
❌ 「找中文硕博论文」→ 中文库需验证码 + 机构订阅

---

## 执行流程

### 第 0 步：连通性自检（建议）

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
2. 每组写 `lib`（PubMed 检索式）和 `terms`/`strong`（评分词）
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

把输出**原样**给用户看。重点看：有没有维度 <10（术语写错）、
有没有 >5000（太宽）、合并总数多少。

### 第 3 步：等确认

**必须等用户明确说「可以跑」**。

### 第 4 步：采集 + 分诊

```bash
python scripts/harvest.py --config search_config.yaml --out raw/
python scripts/triage.py --config search_config.yaml --raw raw/ --out out/scored.tsv
```

`triage.py` 跑完看分层比例：核心为 0 → 词表太严；核心 >30% → 弱信号被当强信号。

可选加 Google Scholar（补充源）：

```bash
python scripts/scholar.py --config search_config.yaml --out raw/
```

需要 `browser-use` + 日常 Chrome。**没有就跳过，不影响主流程。**

### 第 5 步：下载

```bash
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
```

**只下 OA**。拿不到的标 `status: none`，交给报告分类。

抢救一批（从 Scholar「所有版本」页找作者自存档）：

```bash
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core --scholar
```

### 第 6 步：交付

```bash
python scripts/report.py --in out/scored.tsv --pdfdir pdfs/ --out README.md
```

产出 `README.md`（分层导读）+ `out/pending.md`（未获取，按原因分类）。

**交付时主动说明**：核心层命中多少、占比多少；未获取的按原因分布；
这个领域的文献厚度（是不是很薄）。

---

## 下载通道（实测结论，不要改）

```
1. PMC OA S3      开放数据桶直取        首选，98.7% 覆盖，无需浏览器
2. OpenAlex PDF   locations[].pdf_url   出版商直供
3. EPMC XML       fullTextXML           仅当 PDF 不存在
4. Scholar 自存档  「所有版本」页的 PDF    --scholar 才启用
```

### 第 1 通道为什么是 S3 而不是浏览器

PMC 站点的 PDF 直链有**工作量证明（PoW）**挑战：返回 200 + HTML 挑战页，
要求算 `sha256(challenge+nonce)` 前缀后写 cookie 再刷新。

浏览器能过，但每篇都要重跑挑战，批量时叠加限速。
**换指纹、换反检测库、接管登录态全都无效** —— 这是层级错配，不是伪装不够。

官方开放数据桶无鉴权、无 PoW：

```
https://pmc-oa-opendata.s3.amazonaws.com/PMC13598902.1/PMC13598902.1.pdf
→ 200, 1,975,365 bytes
```

实测 150 篇跨主题抽样覆盖 **98.7%**。

**两个必须保留的防御**：

- 精确匹配 `{PMCID}.{ver}.pdf` —— 同目录混着 `*_MOESM*_ESM.pdf` 补充材料
- retry —— 不加会把约 1.3% 的正常文献误判成「无全文」

### OpenAlex / Scholar 仍需浏览器

出版社页面拦的是 403 / 包装页，`pg.evaluate(fetch)` 依然是对的办法。

---

## 常见问题

**Q：为什么核心层这么少？**
调整 `search_config.yaml` 里的 `strong` 词表和 `thresholds`，
重跑 `triage.py`（不用重新采集）。

**Q：为什么某篇明明有 OA 却下不到？**
1. `pmcid` 有没有值
2. S3 桶里有没有 `{PMCID}.{ver}.pdf`：
   ```bash
   curl -s "https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix=PMC13598902.&max-keys=200" \
     | grep -oE "<Key>[^<]*</Key>"
   ```
3. 是不是 embargo
4. **不要再去浏览器里试 PMC 页面** —— 页面能打开不代表能下 PDF，那是 PoW 的门

**Q：能不能下付费文献？**
不能。只做 OA。付费的进 `pending.md`，标注原因，用户自己走机构订阅。

**Q：Windows 控制台报 `UnicodeEncodeError: 'gbk' codec ...`？**
v1.1.0 起 `common.py` 在导入时把 stdout/stderr 切到 UTF-8，特殊字符可正常打印。

---

## 自检

```bash
python scripts/selfcheck.py            # 含网络
python scripts/selfcheck.py --offline  # 仅逻辑
```

---

## 合规

只做开放获取，不碰付费墙。下载后请遵守各篇的 CC 许可条款。
Google Scholar 通道仅用于**定位**已公开的自存档，不下载受限内容。
