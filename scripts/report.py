#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 7：生成报告。

用法:
    python scripts/report.py --in out/scored.tsv --pdfdir pdfs/ --out README.md

产出:
    README.md          分层导读 + 方法学说明
    out/pending.md     未拿到的，按原因分类
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import has_full_text, log, read_tsv

# 只描述「这次没拿到」的直接原因，不推断期刊政策或付费墙。
PENDING_REASONS = {
    "network": "网络/请求失败",
    "http_error": "HTTP 错误",
    "invalid_content": "返回内容不是正文",
    "not_found": "未找到公开全文",
    "unknown": "未尝试或原因不明",
}


def esc(s, n=None):
    s = (s or "").replace("|", "\\|").replace("\n", " ")
    return (s[:n] + "…") if n and len(s) > n else s


def table(rows, cols, heads, widths):
    out = ["| " + " | ".join(heads) + " |",
           "|" + "|".join(["---"] * len(heads)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(esc(r.get(c, ""), w) for c, w in zip(cols, widths)) + " |")
    return "\n".join(out)


def classify_pending(r):
    """优先用下载阶段记录的结构化原因；旧表没有该列时才看 access_note。

    不再从「无 OA」这类模糊文字推断期刊政策 —— 下载失败证明不了政策。
    """
    reason = (r.get("failure_reason") or "").strip().lower()
    if reason in PENDING_REASONS:
        return reason
    note = (r.get("access_note") or "").lower()
    if "403" in note or "401" in note or "http" in note:
        return "http_error"
    if "超时" in note or "网络" in note or "timeout" in note:
        return "network"
    if "非 pdf" in note or "不是正文" in note:
        return "invalid_content"
    return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="out/scored.tsv")
    ap.add_argument("--pdfdir", default="pdfs")
    ap.add_argument("--out", default="README.md")
    ap.add_argument("--topic", default=None)
    a = ap.parse_args()

    rows = read_tsv(a.infile)
    if not rows:
        log(f"{a.infile} 为空", err=True)
        return 1

    core = [r for r in rows if r.get("tier") == "核心"]
    periph = [r for r in rows if r.get("tier") == "外围"]
    # 全文 = 状态是 full_text 且文件确实可读；SI、失效路径都不算
    pending = [r for r in rows if r.get("tier") in ("核心", "外围")
               and not has_full_text(r, a.pdfdir)]

    topic = a.topic or "文献挖掘"
    L = [f"# {topic} · 文献清单\n"]

    # 概览
    L.append("## 概览\n")
    L.append("| 指标 | 核心层 | 外围层 | 合计 |")
    L.append("|---|---|---|---|")
    core_got = len([r for r in core if has_full_text(r, a.pdfdir)])
    periph_got = len([r for r in periph if has_full_text(r, a.pdfdir)])
    L.append(f"| 文献数 | {len(core)} | {len(periph)} | {len(core)+len(periph)} |")
    L.append(f"| 已获全文 | {core_got} | {periph_got} | {core_got+periph_got} |")
    L.append(f"| 待获取 | {len(core)-core_got} | {len(periph)-periph_got} | {len(pending)} |")
    L.append("")

    # 领域判断：命中强信号的分布
    strong_rows = [r for r in core if (r.get("strong_hits") or "").strip()]
    L.append("## 关键观察\n")
    L.append(f"核心层 {len(core)} 篇中，命中**具体术语**（非泛化词）的有 "
             f"**{len(strong_rows)} 篇**（{len(strong_rows)/max(len(core),1):.0%}）。\n")

    # 维度分布：哪一类研究最多 / 最少
    dim_count = Counter()
    for r in core:
        for k in (r.get("dim_hits") or "").split("+"):
            if k:
                dim_count[k] += 1
    if dim_count:
        L.append("核心层按维度分布：\n")
        L.append("| 维度 | 篇数 |")
        L.append("|---|---|")
        for k, v in dim_count.most_common(12):
            L.append(f"| {k} | {v} |")
        L.append("")
        sparse = [(k, v) for k, v in dim_count.items() if v <= 3]
        if sparse:
            # 只陈述本次检索的结果，不推断「研究空白」—— 篇数受检索式和源覆盖影响
            L.append(f"> 篇数 ≤3 的维度：{', '.join(f'{k}({v})' for k, v in sparse)}。"
                     "这反映的是**本次检索**的命中量，不能直接等同于该方向的研究空白。\n")

    # 分层清单
    for name, subset in (("核心层", core), ("外围层", periph)):
        L.append(f"## {name}（{len(subset)} 篇）\n")
        if not subset:
            L.append("_无_\n")
            continue
        show = subset[:80]
        L.append(table(show,
                       ["score", "year", "strong_hits", "dim_hits", "status", "title", "doi"],
                       ["分", "年", "强信号", "维度", "状态", "标题", "DOI"],
                       [4, 5, 16, 18, 10, 58, 30]))
        if len(subset) > len(show):
            L.append(f"\n_其余 {len(subset)-len(show)} 篇见 `{a.infile}`_\n")
        L.append("")

    # 未获取分类
    if pending:
        L.append("## 未获取（按原因分类）\n")
        buckets = defaultdict(list)
        for r in pending:
            buckets[classify_pending(r)].append(r)
        L.append("| 原因 | 篇数 |")
        L.append("|---|---|")
        for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
            L.append(f"| {PENDING_REASONS.get(k, k)} | {len(v)} |")
        L.append("")
        for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
            L.append(f"### {PENDING_REASONS.get(k, k)}（{len(v)} 篇）\n")
            L.append(table(v[:30], ["year", "title", "doi", "journal"],
                           ["年", "标题", "DOI", "期刊"], [5, 60, 30, 26]))
            L.append("")

    # 方法学：只写实现事实，不写本次运行没有记录的信息
    srcs = Counter()
    for r in rows:
        for s in (r.get("src") or "").split("+"):
            if s:
                srcs[s] += 1
    L.append("## 方法学\n")
    L.append(f"- **数据源**（本次题录来源）：{', '.join(sorted(srcs)) or '无'}。")
    L.append("  各源均为限量采集：PubMed 按 retmax、OpenAlex 每维度前 200 条、")
    L.append("  DataCite 前两页，Europe PMC 按完整游标翻页。因此本清单**不是穷尽检索**。")
    L.append("- **去重**：DOI 优先，标题归一化兜底。")
    L.append("- **打分**：标题 + 摘要联合打分。维度强信号按配置权重计分，")
    L.append("  弱信号在有强信号时才 +1，有对照设计 +2；阈值由配置决定。")
    L.append("  分数反映**词表命中**，不是研究质量或证据等级。")
    L.append("- **下载**：只取 OA。PMC 官方 S3 → 题录已有 PDF 直链 → OpenAlex PDF → EPMC XML。")
    L.append("- **状态**：`full_text` / `si_only` / `abstract_only` / `none`；")
    L.append("  只有状态为 `full_text` **且文件通过最低校验**才计入已获全文。")
    L.append("  该校验只排除 HTML 伪装与明显截断，不证明文件内容与题录一致。")
    L.append("")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"→ {a.out}")

    # 待办每次都重写：待获取清零后不能留下上一轮的清单
    pdir = os.path.dirname(a.infile) or "."
    ppath = os.path.join(pdir, "pending.md")
    P = ["# 待获取清单\n", f"共 {len(pending)} 篇\n"]
    buckets = defaultdict(list)
    for r in pending:
        buckets[classify_pending(r)].append(r)
    for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
        P.append(f"\n## {PENDING_REASONS.get(k, k)}（{len(v)} 篇）\n")
        P.append(table(v, ["year", "title", "doi", "pmid", "journal", "access_note"],
                       ["年", "标题", "DOI", "PMID", "期刊", "说明"], [5, 60, 30, 10, 26, 40]))
    with open(ppath, "w", encoding="utf-8") as f:
        f.write("\n".join(P))
    log(f"→ {ppath}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
