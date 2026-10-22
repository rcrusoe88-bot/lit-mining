# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [1.1.0] - 2026-10-22

首个公开版本（此前为内部使用）。

### Added

- 完整六步工作流：拆解 → 探量 → 审核 → 采集 → 分诊 → 下载 → 交付
- `scripts/probe.py`：各维度命中量探测，先探后跑
- `scripts/harvest.py`：PubMed + Europe PMC + OpenAlex + DataCite 多源采集
- `scripts/triage.py`：去重 → 标题+摘要联合打分 → 核心/外围/背景分层
- `scripts/download.py`：OA 全文下载，PMC 走官方开放数据 S3 桶
- `scripts/report.py`：分层报告 + 按原因分类的待办清单
- `scripts/scholar.py`：Google Scholar 补充源 + 作者自存档救援（可选）
- `scripts/browser.py`：浏览器抽象，browser-use / CDP / HTTP 三级降级
- `scripts/common.py`：配置、HTTP、TSV、**连通性预检**
- `scripts/selfcheck.py`：离线逻辑检查 + 联网端到端检查
- `scripts/agent_entry.py`：Codex / ChatGPT 入口
- `templates/search_config.yaml.example`：带完整注释的检索式配置模板
- 多端适配：Claude Code（`SKILL.md`）、Codex/ChatGPT（`AGENTS.md` + `agents/openai.yaml`）、
  WorkBuddy（`workbuddy/`）
- GitHub Actions CI：3 平台 × 2 Python 版本

### Fixed

- **PMC 全文下载改走官方开放数据 S3 桶**。PMC 站点的 PDF 直链有工作量证明
  （PoW）挑战，返回 200 + HTML 而非 PDF；浏览器每篇都要重跑挑战，批量时叠加限速。
  换指纹、换反检测库、接管登录态全都无效 —— 这是层级错配。实测 150 篇跨主题
  抽样覆盖 98.7%。
- **修复网络抖动导致整批数据静默丢失**。NCBI E-utilities 偶发
  `SSL: UNEXPECTED_EOF_WHILE_READING`，此前 `http_json` 失败返回 `{}`，
  调用方当成「没数据」，实测一次 `esummary` 请求让 100/120 条数据消失。
  现改为 `http_json_checked()` 抛异常 + 降级到小批次重试。
- **修复 Windows GBK 控制台崩溃**。`probe.py` 在最后一行打印 `⚠` 时
  `UnicodeEncodeError`，而那行恰恰是给用户的诊断信息。`common.py` 导入时
  自动把 stdout/stderr 切到 UTF-8。
- **新增连通性预检**。网络坏的时候采集会静默返回空，错误信息指向「没命中」
  而不是「网络不通」。`harvest.py` 现在会预检并中止。
- **放宽 PMC S3 下载超时**到 240s。国内直连带宽实测只有 36KB/s，
  1.9MB 的 PDF 要 52 秒，用 60s 会把正常文件误判成超时。
- **PMC S3 key 精确匹配**。同目录混着 `*_MOESM*_ESM.pdf` 补充材料，
  取第一个会拿错（补充材料只有 Methods/NMR，不是正文）。

### Notes

- 只访问 `eutils.ncbi.nlm.nih.gov`、`www.ebi.ac.uk`、
  `pmc-oa-opendata.s3.amazonaws.com`、`api.openalex.org`，
  四个国内均可直连；只有 Google Scholar 强制需要代理。
- S3 国内直连 36KB/s，走代理 1MB/s。代码原生支持 `HTTPS_PROXY` 环境变量。
- 只做开放获取，不碰付费墙。
