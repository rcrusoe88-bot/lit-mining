#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 2：探测命中量。

只查 count，不抓题录。跑完把结果给用户看，等确认。

用法:
    python scripts/probe.py --config search_config.yaml
    python scripts/probe.py --config search_config.yaml --json out/probe.json
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (EUTILS, EPMC, build_query, http_json, load_config, log)


def pubmed_count(term):
    q = urllib.parse.urlencode({"db": "pubmed", "term": term, "retmode": "json", "retmax": "0"})
    d = http_json(f"{EUTILS}/esearch.fcgi?{q}")
    return int(d.get("esearchresult", {}).get("count", 0))


def epmc_count(query):
    u = f"{EPMC}?" + urllib.parse.urlencode({"query": query, "format": "json", "pageSize": "1"})
    d = http_json(u)
    return int(d.get("hitCount", 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--json", help="同时输出机器可读结果")
    ap.add_argument("--source", default="pubmed", choices=["pubmed", "epmc", "both"])
    a = ap.parse_args()

    cfg = load_config(a.config)
    dims = cfg.get("dimensions") or []
    if not dims:
        log("配置里没有 dimensions，无法探测", err=True)
        return 1

    topic = (cfg.get("meta") or {}).get("topic", "(未命名)")
    print(f"\n主题: {topic}")
    print(f"维度: {len(dims)} 组")
    print(f"源:   {a.source}\n")

    results = {}
    # 按 group 分组显示
    groups = {}
    for d in dims:
        groups.setdefault(d.get("group", "其他"), []).append(d)

    for gname, items in groups.items():
        print(f"--- {gname} ---")
        for d in items:
            q = build_query(cfg, d["lib"])
            n_pm = pubmed_count(q) if a.source in ("pubmed", "both") else None
            time.sleep(0.35)
            n_ep = epmc_count(q) if a.source in ("epmc", "both") else None
            time.sleep(0.35)
            label = d.get("label") or d["key"]
            if a.source == "both":
                print(f"  {d['key']:24s} {label:16s} PubMed={n_pm:<6d} EPMC={n_ep}")
            elif a.source == "epmc":
                print(f"  {d['key']:24s} {label:16s} EPMC={n_ep}")
            else:
                print(f"  {d['key']:24s} {label:16s} {n_pm}")
            results[d["key"]] = {"label": label, "group": gname,
                                 "pubmed": n_pm, "epmc": n_ep}
        print()

    # 全集（各维度 OR 起来）
    union_lib = " OR ".join(f"({d['lib']})" for d in dims)
    union_q = build_query(cfg, union_lib)
    n_union = pubmed_count(union_q)
    print(f"--- 合并（各维度 OR）---")
    print(f"  唯一命中           PubMed={n_union}")
    results["_union"] = {"pubmed": n_union}

    # 诊断提示
    print("\n诊断:")
    narrow = [k for k, v in results.items()
              if k != "_union" and isinstance(v.get("pubmed"), int) and v["pubmed"] < 10]
    wide = [k for k, v in results.items()
            if k != "_union" and isinstance(v.get("pubmed"), int) and v["pubmed"] > 5000]
    if narrow:
        print(f"  ⚠ 命中 <10 的维度（可能太窄或术语有误）: {', '.join(narrow)}")
    if wide:
        print(f"  ⚠ 命中 >5000 的维度（可能太宽）: {', '.join(wide)}")
    if not narrow and not wide:
        print("  ✓ 各维度命中量在合理区间")

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "results": results, "union": n_union},
                      f, ensure_ascii=False, indent=2)
        print(f"\n机器可读结果: {a.json}")

    print("\n下一步: 把上面的数字给用户看，确认检索式后再跑 harvest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
