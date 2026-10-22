#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 5：去重 + 打分 + 分层。

用法:
    python scripts/triage.py --config search_config.yaml --raw raw/ --out out/scored.tsv

核心教训（务必保留）:
    只看标题打分会让核心层从 276 掉到 7。
    偶联化学、工艺参数几乎从不写在标题里 —— 必须标题 + 摘要联合打分。
"""
import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log, norm_doi, norm_title, read_tsv, write_tsv

FIELDS = [
    "tier", "score", "reasons", "year", "first_author", "title", "journal",
    "doi", "pmid", "pmcid", "src", "n_hits", "hits",
    "strong_hits", "weak_hits", "dim_hits",
    "load_terms_hit", "status", "oa_status", "pdf_path", "access_note",
    "cluster_url",
]


def compile_terms(terms):
    """词表 → 单个正则。长词优先，避免 'T cell' 被 'T' 抢先匹配。"""
    if not terms:
        return None
    esc = sorted({re.escape(t) for t in terms if t}, key=len, reverse=True)
    if not esc:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(esc) + r")(?![A-Za-z0-9])", re.I)


def hit_terms(text, pattern):
    if not pattern:
        return []
    return sorted({m.group(0).lower() for m in pattern.finditer(text)})


def build_patterns(cfg):
    """从配置构造所有评分正则。"""
    dims = cfg.get("dimensions") or []

    # 维度级：优先用 strong，没有就用 terms
    dim_patterns = []
    for d in dims:
        terms = d.get("strong") or d.get("terms") or []
        pat = compile_terms(terms)
        if pat:
            dim_patterns.append({
                "key": d["key"],
                "group": d.get("group", ""),
                "label": d.get("label") or d["key"],
                "pattern": pat,
                "weight": d.get("weight", 1),
                "strong": bool(d.get("strong")),
            })

    # 全局弱信号
    weak_pat = compile_terms(cfg.get("weak_terms") or [])
    # 载荷
    load_pat = compile_terms((cfg.get("load") or {}).get("terms") or [])
    # 对照
    ctrl_pat = re.compile(cfg.get("control_pattern"), re.I) if cfg.get("control_pattern") else None
    # 排除
    excl_pat = re.compile(cfg.get("exclude_pattern"), re.I) if cfg.get("exclude_pattern") else None
    # 平台门槛
    plat_pat = re.compile((cfg.get("platform") or {}).get("required_pattern", "."), re.I)

    return dim_patterns, weak_pat, load_pat, ctrl_pat, excl_pat, plat_pat


def score_row(row, pats, thresholds):
    dim_patterns, weak_pat, load_pat, ctrl_pat, excl_pat, plat_pat = pats

    title = row.get("title", "") or ""
    abstract = row.get("abstract", "") or ""
    text = f"{title} {abstract}"

    score = 0
    reasons = []
    strong_hits, weak_hits, dim_hits = [], [], []

    # 维度命中：每组只计一次，取最高权重
    for dp in dim_patterns:
        if dp["pattern"].search(text):
            dim_hits.append(dp["key"])
            score += dp["weight"]
            reasons.append(f"{dp['label']}(+{dp['weight']})")
            if dp["strong"]:
                strong_hits.extend(hit_terms(text, dp["pattern"]))

    # 弱信号：只 +1，且仅当有强信号时才算（避免纯泛词堆分）
    if weak_pat:
        wh = hit_terms(text, weak_pat)
        if wh:
            weak_hits.extend(wh)
            if strong_hits or any(dp["strong"] for dp in dim_patterns if dp["key"] in dim_hits):
                score += 1
                reasons.append("weak(+1)")

    # 载荷：只作为「必要但不够」的条件，不加分
    # （实测：mRNA 单独 +2 会让大量只提到 mRNA 的无关文献涌进外围）
    lh = hit_terms(text, load_pat) if load_pat else []

    # 对照
    if ctrl_pat and ctrl_pat.search(text):
        score += 2
        reasons.append("control(+2)")

    # 硬门槛
    is_platform = bool(plat_pat.search(title)) or bool(plat_pat.search(abstract))
    excluded = bool(excl_pat and excl_pat.search(title))
    has_dim = bool(dim_hits)          # 必须命中至少一个主题维度
    has_load = bool(lh) or load_pat is None

    if not is_platform or excluded:
        tier = "背景"
    elif not has_dim:
        tier = "背景"                  # 没有主题维度 = 跑题
    elif score >= thresholds["core"]:
        tier = "核心"
    elif score >= thresholds["periphery"]:
        tier = "外围"
    else:
        tier = "背景"

    row.update({
        "score": score,
        "tier": tier,
        "reasons": ";".join(reasons),
        "strong_hits": "+".join(strong_hits[:8]),
        "weak_hits": "+".join(weak_hits[:8]),
        "dim_hits": "+".join(dim_hits),
        "load_terms_hit": "+".join(lh[:5]),
        "has_load": "Y" if has_load else "N",
    })
    return row


def merge_rows(all_rows):
    """去重：DOI 优先，标题兜底。合并时保留多源信息。"""
    by_doi, by_title = {}, {}
    merged = []
    for r in all_rows:
        d = norm_doi(r.get("doi"))
        t = norm_title(r.get("title"))
        hit = by_doi.get(d) if d else None
        if hit is None and t:
            hit = by_title.get(t)

        if hit is not None:
            srcs = set((hit.get("src") or "").split("+")) | {r.get("src", "")}
            hit["src"] = "+".join(sorted(x for x in srcs if x))
            hits = set((hit.get("hits") or "").split(";")) | set((r.get("hits") or "").split(";"))
            hit["hits"] = ";".join(sorted(x for x in hits if x))
            hit["n_hits"] = len([x for x in hit["hits"].split(";") if x])
            for k in ("pmid", "pmcid", "doi", "abstract", "is_oa", "oa_url", "pdf_url",
                      "journal", "year", "first_author", "url", "cluster_url"):
                if not hit.get(k) and r.get(k):
                    hit[k] = r[k]
            # 摘要取更长的那个
            if len(r.get("abstract") or "") > len(hit.get("abstract") or ""):
                hit["abstract"] = r["abstract"]
            continue

        rr = dict(r)
        merged.append(rr)
        if d:
            by_doi[d] = rr
        if t:
            by_title[t] = rr
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--out", default="out/scored.tsv")
    a = ap.parse_args()

    cfg = load_config(a.config)
    thr = cfg.get("thresholds") or {}
    thresholds = {
        "core": int(thr.get("core", 8)),
        "periphery": int(thr.get("periphery", 3)),
    }

    # 读所有源
    all_rows = []
    srcs = 0
    for fn in ("pubmed.tsv", "epmc.tsv", "openalex.tsv", "datacite.tsv", "scholar.tsv"):
        p = os.path.join(a.raw, fn)
        rows = read_tsv(p)
        if rows:
            log(f"  {fn:18s} {len(rows):>6d}")
            all_rows.extend(rows)
            srcs += 1
    if not all_rows:
        log(f"{a.raw}/ 里没有数据，先跑 harvest.py", err=True)
        return 1

    log(f"\n合并前 {len(all_rows)}")
    merged = merge_rows(all_rows)
    log(f"去重后 {len(merged)}")

    pats = build_patterns(cfg)
    log("打分中...")
    for r in merged:
        score_row(r, pats, thresholds)
        if "n_hits" not in r or not r.get("n_hits"):
            r["n_hits"] = len([x for x in (r.get("hits") or "").split(";") if x])
        r.setdefault("status", "none")

    merged.sort(key=lambda x: (-int(x.get("score") or 0), x.get("year") or ""))

    write_tsv(a.out, merged, FIELDS)
    log(f"\n→ {a.out}  ({len(merged)} 行)")

    from collections import Counter
    tiers = Counter(r["tier"] for r in merged)
    log(f"分层: 核心 {tiers['核心']} | 外围 {tiers['外围']} | 背景 {tiers['背景']}")

    # 分层合理性提示
    core = tiers["核心"]
    if core == 0:
        log("⚠ 核心层为空。检查 strong 词表是否太严，或阈值是否过高", err=True)
    elif core > len(merged) * 0.3:
        log(f"⚠ 核心层占比 {core/len(merged):.0%} 偏高，检查 weak_terms 是否被当成了强信号", err=True)

    log("\n下一步: python scripts/download.py --in out/scored.tsv --pdfdir pdfs/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
