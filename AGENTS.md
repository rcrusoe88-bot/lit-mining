# Lit-Mining · Agent 适配说明

本文件给 Codex 及其他 agent 用。**流程契约以 [PROTOCOL.md](PROTOCOL.md) 为准**，
本文件只说 Codex 环境下怎么跑。

---

## 与 Claude Code 的差异

| | Claude Code | Codex / ChatGPT |
|---|---|---|
| 入口 | `SKILL.md`（frontmatter 触发） | 本文件 + `agents/openai.yaml` |
| 审核环节 | 对话中确认 | 同样在对话中确认 |
| 脚本调用 | 直接 Bash | 直接 shell |

**没有功能差异。** 脚本是纯 CLI，不依赖任何 agent 特性。

---

## 前置检查

```bash
cd <skill 目录>

# 1. 依赖
python -c "import yaml, playwright; print('OK')"

# 2. 浏览器（下载阶段需要）
python scripts/browser.py
#   → 打印各适配器可用性 + Google Scholar 可用性
```

如果 `playwright` 缺：

```bash
pip install playwright pyyaml defusedxml
playwright install chromium
```

---

## 执行顺序

```bash
# 0. 生成配置（你来做，参考 templates/search_config.yaml.example）
#    写到工作目录的 search_config.yaml

python scripts/probe.py    --config search_config.yaml
#    ↑ 把输出给用户看，等确认

# raw/ 必须是全新或空目录：已有源 TSV 会被拒绝覆盖，避免旧结果混进新运行
python scripts/harvest.py  --config search_config.yaml --out raw/

# 可选：Google Scholar 补充源（必须在 triage 之前，否则不会进入本次分诊）
#   前置条件见 PROTOCOL.md「Google Scholar」一节
python scripts/scholar.py  --config search_config.yaml --out raw/

python scripts/triage.py   --config search_config.yaml --raw raw/ --out out/scored.tsv
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
python scripts/report.py   --in out/scored.tsv --pdfdir pdfs/ --out README.md

# 可选：自存档救援（在下载之后）
python scripts/scholar.py --rescue --in out/scored.tsv --pdfdir pdfs/ --tier core
```

全程在一个工作目录内完成，产物路径：

```
<工作目录>/
├── search_config.yaml
├── raw/            pubmed.tsv / epmc.tsv / openalex.tsv / scholar.tsv
├── out/            scored.tsv / pending.md
├── pdfs/           全文
└── README.md       交付报告
```

---

## 拆解检索式时的自查（必做）

生成 `search_config.yaml` 前，确认三件事：

1. **缩写是否真实存在**
   实测案例：`APCCS` 在 PubMed 里 84 篇全是「醛固酮生成细胞簇」（原发性醛固酮增多症），
   跟偶联化学完全无关。如果不查就直接构造检索式，整条维度会归零。

   检查方法：拿缩写 `probe.py` 跑一次，看命中标题是不是同一领域。

2. **平台/载荷口径**
   相邻领域要不要收？`siRNA` 混进 `mRNA`、`聚合物载体` 混进 `LNP`，
   都会让结果池变脏。

3. **强信号 vs 弱信号**
   具体名词（`maleimide`、`SPAAC`）给 +3；
   泛化词（`targeted`、`conjugated`）给 +1，放 `weak_terms`。
   混在一起会导致 `neutralizing antibody`（中和抗体）被误判成靶向配体。

---

## 下载阶段的浏览器依赖

**主流程不需要浏览器。** PMC 全文走官方开放数据 S3 桶，纯 HTTP 直取：

```
https://pmc-oa-opendata.s3.amazonaws.com/PMC13598902.1/PMC13598902.1.pdf
```

只有 OpenAlex / Scholar 通道和出版社 PDF 才需要浏览器，三种适配器自动降级：

```
browser-use  →  CDP(Playwright)  →  纯 HTTP
```

- 有 `browser-use` 且能连日常浏览器 → 用它（能过更多反爬）
- 否则用 CDP（需要 `--remote-debugging-port=9223` 的 Chrome）
- 都没有 → 纯 HTTP（出版社能下的还能下几个）

**启动 CDP Chrome**（Windows）：

```bash
"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --user-data-dir="%USERPROFILE%\chrome-cdp-profile"
```

### Google Scholar 通道（可选）

`scholar.py` 用 `browser-use` 操作**日常 Chrome**（不是 9223 那个独立实例）。
Chrome 155 的远程调试不走标准 CDP HTTP 端点，Playwright 连不上日常浏览器。

前置检查：

```bash
browser-use --doctor        # daemon alive + active connections
```

日常 Chrome 需要开 `chrome://inspect/#remote-debugging` 并勾选
「Allow remote debugging for this browser instance」。

**三个必踩的坑**（详见 PROTOCOL.md）：

| 坑 | 应对 |
|---|---|
| 维度检索词是中文 | `scholar_query` 必须纯英文 |
| 提取 DOM 用 `js()` helper | 改用 `cdp("Runtime.evaluate", returnByValue=True)` |
| stdout 遇 surrogate 字符炸 | 结果写文件 + 片段结尾 `os._exit(0)` |

---

## 不要做的事

| 不要 | 原因 |
|---|---|
| 只抓标题就打分 | 实测核心层会从 276 掉到 7 |
| 用浏览器去啃 PMC PDF | 那是 PoW 挑战页，每篇都要重跑；走 S3 桶 |
| 直接取 S3 目录里第一个 `.pdf` | 同目录混着 `*_MOESM*_ESM.pdf` 补充材料 |
| 把 PubMed 检索式直接喂 OpenAlex | 它不认 `[Title/Abstract]`，会 400 |
| 给 OpenAlex 的 search 带 `*` | 不接受通配符，会 400 |
| 用大 OR 组查 Europe PMC | 超过 6 个词就归零，命中数变 0 |
| 绕过付费墙 | 不出现在 skill 里，也不该临时加 |
| 把出版商 SI 当全文 | SI 只有 Methods/NMR，不是正文 |

---

## 输出约定

`scored.tsv` 的 `status` 只有四个值：

```
full_text       拿到正文（PDF 或 XML，且文件通过最低校验）
si_only         只有补充材料（不算全文）
abstract_only   只有摘要
none            什么都没有
```

失败细节在 `failure_reason`（network / http_error / invalid_content / not_found /
unknown）与 `access_note`（通道 + HTTP 状态 + 异常摘要）。**不要据此推断期刊
OA 政策或是否付费墙**——那需要另外的证据。最低校验只排除 HTML 伪装与明显截断，
不证明论文身份。

报告里不要用「已下载」这种模糊表述 —— 使用者需要知道到底拿到了什么。

---

## 自检

```bash
python scripts/selfcheck.py            # 含网络（真下 PDF 验证）
python scripts/selfcheck.py --offline  # 仅逻辑
python scripts/common.py               # 连通性自检
```
