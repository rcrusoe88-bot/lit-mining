# lit-mining

**把一句话主题，变成一份分层、可下载、可复核的文献清单。**

```
主题句 → 检索式拆解 → 命中量探测 → 人工审核 → 多源采集 → 打水分层
       → 下载 OA 全文 → 分层报告 + 待办清单
```

---

## 它解决什么问题

「帮我系统性检索一下 X 方向」这件事，手工做会卡在三个地方：

1. **检索式写不对** —— 一条巨式检索命中 8000 篇，等于没筛
2. **分层靠感觉** —— 读完 50 篇摘要才敢判断哪些是真相关
3. **下不到全文** —— 98% 的开放获取全文其实有官方通道，但没人告诉你

这个 skill 把这三件事固化下来：维度化检索式 → 命中量先探后跑 → 强/弱信号
自动打分分层 → 官方 OA 通道批量下 PDF。

**它不替你做判断。** 检索式必须人工确认一次（步骤 3），因为搜错术语
会让整条维度归零，而这个问题只有人能一眼看出来。

---

## 快速开始

```bash
git clone https://github.com/rcrusoe88-bot/lit-mining
cd lit-mining

# 依赖（只有两个，Scholar 通道才需要第二个）
pip install pyyaml defusedxml
pip install playwright          # 可选：Scholar 通道

# 自检
python scripts/selfcheck.py
```

### 在 agent 里用

| Agent | 装法 |
|---|---|
| Claude Code | 复制到 `~/.claude/skills/lit-mining/` |
| Codex / ChatGPT | 复制到 `~/.codex/skills/lit-mining/`，看 `AGENTS.md` |
| WorkBuddy | 见 `workbuddy/README.md` |

### 手动跑

```bash
# 1. 写检索式（照 templates/search_config.yaml.example 改）
cp templates/search_config.yaml.example search_config.yaml

# 2. 探命中量 —— 把输出给用户看
python scripts/probe.py --config search_config.yaml

# 3. 采集 + 分诊
python scripts/harvest.py --config search_config.yaml --out raw/
python scripts/triage.py  --config search_config.yaml --raw raw/ --out out/scored.tsv

# 4. 下载 OA 全文
python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core

# 5. 生成报告
python scripts/report.py --in out/scored.tsv --pdfdir pdfs/ --out README.md
```

产物结构：

```
<工作目录>/
├── search_config.yaml
├── raw/            pubmed.tsv / epmc.tsv / openalex.tsv
├── out/            scored.tsv / pending.md
├── pdfs/           全文 PDF
└── README.md       分层报告
```

---

## 六个步骤

| 步 | 做什么 | 谁做 |
|---|---|---|
| 1 拆解 | 主题 → 多维度检索式 + 强/弱信号词表 | **LLM + 人工** |
| 2 探量 | 各维度命中数、合并总数 | 脚本 |
| 3 审核 | 看命中量，改配置或确认 | **人工（必须）** |
| 4 采集 | PubMed + Europe PMC + OpenAlex（+ Scholar 可选） | 脚本 |
| 5 分诊 | 去重 → 打分 → 核心/外围/背景 | 脚本 |
| 6 下载 | PMC OA S3 → OpenAlex → EPMC XML → Scholar | 脚本 |
| 7 交付 | 分层报告 + 待办清单 | 脚本 |

步骤 1–3 是**人和 LLM 的循环**，4–7 是**纯脚本**。

完整契约见 [PROTOCOL.md](PROTOCOL.md)。

---

## 三个关键设计

### 1. 命中量先探后跑（步骤 2、3）

跑之前先看每条检索式命中多少。**这一步是整个流程最省时间的部分**：

- 某维度 <10 → 术语写错了，整条线白跑
- 某维度 >5000 → 太宽，会淹掉真信号
- 合并总数 → 领域厚度的第一个信号

实测教训：`APCCS` 在 PubMed 里 84 篇全是「醛固酮生成细胞簇」，
与偶联化学完全无关。如果不探量直接跑，整条维度归零你都不知道。

### 2. 标题 + 摘要联合打分

**只看标题，核心层会从 276 篇掉到 7 篇。**

原因很实在：偶联化学和工艺参数几乎从不写在标题里，标题全是
「Targeted delivery of mRNA via lipid nanoparticles」这种泛泛表述。

打分规则：

```
+3  命中强信号词（具体、可验证的名词：maleimide、SPAAC）
+1  命中弱信号词（泛化：targeted、conjugated）
+2  有对照设计（compared with / versus / control）
+2  命中载荷词（mRNA / saRNA / circRNA）
+1  命中对象词（细胞类型 / 靶器官）
```

### 3. PMC 走官方 S3 桶，不用浏览器

PMC 站点的 PDF 直链有**工作量证明（PoW）**挑战 —— 返回 200 但内容是
HTML 挑战页，要求算 `sha256(challenge+nonce)` 前缀后写 cookie 再刷新。

浏览器能过，但每篇都要重跑一次挑战，批量时叠加限速。
**换指纹、换反检测库、接管登录态，全都治不好** —— 这是层级错配，不是伪装不够。

官方开放数据桶无鉴权、无 PoW：

```
https://pmc-oa-opendata.s3.amazonaws.com/PMC13598902.1/PMC13598902.1.pdf
→ 200, 1,975,365 bytes
```

实测 150 篇跨主题抽样覆盖 **98.7%**（LNP/mRNA 100%、CAR-T 100%、circRNA 96%）。

两个必须保留的防御：

- **精确匹配** `{PMCID}.{ver}.pdf` —— 同目录混着 `*_MOESM*_ESM.pdf` 补充材料，
  取第一个会拿错（补充材料只有 Methods/NMR，不是正文）
- **retry** —— 不加会把约 1.3% 的正常文献误判成「无全文」

---

## 依赖

| 依赖 | 必需 | 用途 |
|---|---|---|
| Python 3.9+ | ✅ | — |
| `pyyaml` | ✅ | 读配置 |
| `defusedxml` | ✅ | 解析 PubMed XML（防 XXE） |
| `playwright` | 仅 Scholar 通道 | 浏览器通道 |
| `browser-use` | 仅 Scholar 通道 | 复用日常 Chrome 登录态 |

主流程（检索 → 分诊 → 下载 PMC/OpenAlex）只需要前两个。

所有网络请求都带重试和降级：单次成功率 85–95% 是常态，
抖动一下不会让整批数据消失。

```bash
python scripts/common.py    # 连通性自检，顺便打印当前代理设置
```

---

## 状态字段约定

`scored.tsv` 的 `status` 只有四个值：

```
full_text       拿到正文
si_only         只有补充材料（不算全文）
abstract_only   只有摘要
none            什么都没有
```

不用「已下载」这种模糊表述 —— 使用者需要知道到底拿到了什么。

`out/pending.md` 按原因分类：付费墙 / 该刊无 OA / 网络不可达 / 未尝试。

---

## 只做开放获取

不碰付费墙。付费的进 `pending.md`，标注原因，用户自己走机构订阅。
Google Scholar 通道仅用于**定位**已公开的自存档。

下载后请遵守各篇的 CC 许可条款。

---

## 延伸

- 只下载、不检索 → [pmc-oa-pdf](https://github.com/rcrusoe88-bot/pmc-oa-pdf)
  （有并发和 manifest，更适合大批量 PDF）

## License

[MIT](LICENSE) © 2026 rcrusoe88-bot
