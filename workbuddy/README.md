# lit-mining · WorkBuddy 版

> 把一句话主题，变成一份分层、可下载、可复核的文献清单。

这是同一个技能的 **WorkBuddy 原生版本**。与根目录的 Claude Code / Codex 版共用
同一套脚本、同一份流程契约（`PROTOCOL.md`），仅针对 WorkBuddy 做了两点适配：

1. frontmatter 增加 `agent_created: true`，标记为 AI 创建的技能，后续可直接修改更新；
2. `description` 补充中文触发词，让 WorkBuddy 能更准确地自动唤起。

`scripts/` 从仓库根目录复制过来，两版共用同一份代码。

## 安装

**方式一：💬 自然语言安装（推荐）**

在 WorkBuddy 对话窗口直接粘贴：

```
帮我在 WorkBuddy 上安装 lit-mining 技能。
文件来自 https://github.com/rcrusoe88-bot/lit-mining 仓库的 workbuddy/ 子目录和 scripts/ 子目录，
请装到我的个人技能目录（Windows：%USERPROFILE%\.workbuddy\skills\lit-mining，
macOS/Linux：~/.workbuddy/skills/lit-mining），
注意 templates/ 和 PROTOCOL.md 也要一起放进去，装完告诉我重启 WorkBuddy 生效。
```

**方式二：🖥️ 命令行安装**

```bash
# Windows (PowerShell)
git clone https://github.com/rcrusoe88-bot/lit-mining $env:TEMP\lit-mining
$dst = "$env:USERPROFILE\.workbuddy\skills\lit-mining"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$env:TEMP\lit-mining\workbuddy\*" $dst -Recurse -Force
Copy-Item "$env:TEMP\lit-mining\scripts" $dst -Recurse -Force
Copy-Item "$env:TEMP\lit-mining\templates" $dst -Recurse -Force
Copy-Item "$env:TEMP\lit-mining\PROTOCOL.md" $dst -Force

# macOS / Linux
git clone https://github.com/rcrusoe88-bot/lit-mining /tmp/lit-mining
mkdir -p ~/.workbuddy/skills/lit-mining
cp /tmp/lit-mining/workbuddy/* ~/.workbuddy/skills/lit-mining/
cp -r /tmp/lit-mining/scripts /tmp/lit-mining/templates ~/.workbuddy/skills/lit-mining/
cp /tmp/lit-mining/PROTOCOL.md ~/.workbuddy/skills/lit-mining/
```

装完重启 WorkBuddy。

## 依赖

```bash
pip install pyyaml defusedxml
```

只有 Google Scholar 通道才需要 `playwright` 和 `browser-use`，
主流程（检索 → 分诊 → 下载 PMC/OpenAlex）用不到。

## 触发

说「帮我系统性检索一下 X 方向」「帮我找这个主题的所有文献」
「把这个领域的论文整理成表」会自动唤起。也可手动 `@lit-mining`。

## 执行流程

```
1. 拆解    生成 search_config.yaml（照 templates/search_config.yaml.example）
2. 探量    probe.py → 各维度命中数
3. 审核    把命中数给用户看，等确认
4. 采集    harvest.py → raw/*.tsv
5. 分诊    triage.py → out/scored.tsv
6. 下载    download.py → pdfs/
7. 交付    report.py → README.md + pending.md
```

步骤 1–3 是人和模型的循环，4–7 是纯脚本。完整契约见 `PROTOCOL.md`。

## 目录结构

```
workbuddy/
├── SKILL.md            # 技能主文件（WorkBuddy 适配版）
└── README.md           # 本说明
```

还需要从仓库根目录复制：`scripts/`、`templates/`、`PROTOCOL.md`。

## 国内网络

只有 Google Scholar 强制需要代理，其余端点国内直连可用：

| 域名 | 用途 | 国内直连 |
|---|---|---|
| `eutils.ncbi.nlm.nih.gov` | PubMed | 通 |
| `www.ebi.ac.uk` | Europe PMC | 通 |
| `pmc-oa-opendata.s3.amazonaws.com` | PMC OA PDF | 通但慢 |
| `api.openalex.org` | OpenAlex | 通 |
| `scholar.google.com` | Google Scholar（可选） | 需代理 |

S3 直连只有 36KB/s，批量下载建议设 `HTTPS_PROXY` 环境变量，代码原生支持。

## License

[MIT](../LICENSE) © 2026 rcrusoe88-bot
