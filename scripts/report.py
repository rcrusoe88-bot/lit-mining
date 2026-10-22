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
from common import log, read_tsv

PENDING_REASONS = {
    "paywall": "付费墙（无 OA 版本）",
    "no_oa": "该刊无 OA 政策",
    "network": "网络不可达",
    "no_si": "该刊无补充材料",
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
    note = (r.get("access_note") or "").lower()
    if "付费" in note or "paywall" in note or "订阅" in note:
        return "paywall"
    if "无 oa" in note or "no_oa" in note:
        return "no_oa"
    if "网络" in note or "network" in note or "403" in note or "超时" in note:
        return "network"
    if "si" in note or "补充" in note:
        return "no_si"
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
    got = [r for r in rows if (r.get("pdf_path") or "").strip()]
    pending = [r for r in rows if r.get("tier") in ("核心", "外围")
               and not (r.get("pdf_path") or "").strip()]

    topic = a.topic or "文献挖掘"
    L = [f"# {topic} · 文献清单\n"]

    # 概览
    L.append("## 概览\n")
    L.append("| 指标 | 核心层 | 外围层 | 合计 |")
    L.append("|---|---|---|---|")
    L.append(f"| 文献数 | {len(core)} | {len(periph)} | {len(core)+len(periph)} |")
    L.append(f"| 已获全文 | {len([r for r in core if (r.get('pdf_path') or '').strip()])} | "
             f"{len([r for r in periph if (r.get('pdf_path') or '').strip()])} | {len(got)} |")
    L.append(f"| 待获取 | {len([r for r in core if not (r.get('pdf_path') or '').strip()])} | "
             f"{len([r for r in periph if not (r.get('pdf_path') or '').strip()])} | {len(pending)} |")
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
            L.append(f"> 篇数 ≤3 的维度：{', '.join(f'{k}({v})' for k, v in sparse)}。"
                     "这些方向在公开文献里极薄，既是空白也是机会。\n")

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

    # 方法学
    L.append("## 方法学\n")
    L.append("- **数据源**：PubMed（E-utilities）、Europe PMC（REST）、OpenAlex；")
    L.append("  DataCite 可选（学位论文）。Google Scholar 视网络环境启用。")
    L.append("- **去重**：DOI 优先，标题归一化兜底。")
    L.append("- **打分**：标题 + 摘要联合打分。强信号词 +3，弱信号 +1，")
    L.append("  载荷 +2，有对照 +2；阈值由配置决定。")
    L.append("- **下载**：只取 OA。PMC PDF（页面上下文 fetch）→ OpenAlex PDF → EPMC XML。")
    L.append("- **状态**：`full_text` / `si_only` / `abstract_only` / `none`，")
    L.append("  不用「已下载」这种含糊表述。")
    L.append("")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    log(f"→ {a.out}")

    # 单独出一份待办
    if pending:
        pdir = os.path.dirname(a.infile) or "."
        ppath = os.path.join(pdir, "pending.md")
        P = ["# 待获取清单\n", f"共 {len(pending)} 篇\n"]
        buckets = defaultdict(list)
        for r in pending:
            buckets[classify_pending(r)].append(r)
        for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
            P.append(f"\n## {PENDING_REASONS.get(k, k)}（{len(v)} 篇）\n")
            P.append(table(v, ["year", "title", "doi", "pmid", "journal"],
                           ["年", "标题", "DOI", "PMID", "期刊"], [5, 60, 30, 10, 26]))
        with open(ppath, "w", encoding="utf-8") as f:
            f.write("\n".join(P))
        log(f"→ {ppath}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
