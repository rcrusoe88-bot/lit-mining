#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 6：OA 全文下载。

用法:
    python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
    python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier all --limit 20

通道优先级（实测排序，别改）:
    1. PMC OA S3      开放数据桶直取         首选，98.7% 覆盖，无需浏览器
    2. OpenAlex PDF   locations[].pdf_url    出版商直供
    3. EPMC XML       fullTextXML            仅当 PDF 不存在

不碰付费墙。拿不到就标原因，交给 report.py 分类。
"""
import argparse
import csv
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (EPMC, OPENALEX, http_bytes, http_json, log,
                    read_tsv, safe_name, write_tsv)
from browser import get_adapter

STATUS_FIELDS = ["status", "oa_status", "pdf_path", "access_note"]


def openalex_pdf_url(doi):
    """从 OpenAlex 拿出版商直供的 pdf_url。"""
    if not doi:
        return ""
    d = http_json(f"{OPENALEX}/works/doi:{urllib.parse.quote(doi)}", tries=2, timeout=45)
    if not d:
        return ""
    for loc in (d.get("locations") or []):
        if loc.get("is_oa") and loc.get("pdf_url"):
            return loc["pdf_url"]
    return ""


def epmc_xml(pmcid):
    """Europe PMC 全文 XML，作为 PDF 的 fallback。"""
    if not pmcid:
        return None
    pid = pmcid if pmcid.startswith("PMC") else "PMC" + pmcid
    from common import http_bytes
    data, code = http_bytes(f"{EPMC}/{pid}/fullTextXML", tries=2, timeout=90)
    if data and len(data) > 8000 and b"<article" in data:
        return data
    return None


S3_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

# 超时必须放宽：实测国内直连 S3 单连接只有 36KB/s，1.9MB 的 PDF 要 52 秒。
# 用 60s 会把正常文件误判成超时（实测 5 次下载有 3 次被截断）。
# 走代理（设 HTTPS_PROXY）则 1MB/s，秒下。
S3_TIMEOUT = 240


def _s3_pdf_key(pmcid):
    """在 PMC 开放数据桶里定位正文 PDF 的 key。

    目录里混着补充材料（*_MOESM*_ESM.pdf），必须精确匹配 {PMCID}.{ver}.pdf。
    版本号不一定是 1（修订过的是 .2/.3），所以要先 list 再取。
    """
    url = f"{S3_BUCKET}/?list-type=2&prefix={urllib.parse.quote(pmcid)}.&max-keys=200"
    body, code = http_bytes(url, tries=3, timeout=90)
    if not body:
        return None
    keys = re.findall(r"<Key>([^<]+)</Key>", body.decode("utf-8", "replace"))
    pat = re.compile(rf"^{re.escape(pmcid)}\.\d+/{re.escape(pmcid)}\.\d+\.pdf$")
    return next((k for k in keys if pat.match(k)), None)


def try_pmc(adapter, pmcid, dest_pdf):
    """PMC PDF：走官方开放数据 S3 桶，不用浏览器。

    PMC 站点的 PDF 直链有工作量证明（PoW）挑战：返回 200 + HTML，
    要求算 sha256 前缀、写 cookie 再刷新页面。浏览器每篇都要重跑一次，
    批量时还叠加限流 —— 这才是老版本「HTTP 200 但返回 HTML」的真正原因，
    不是限流，加等待和重建会话都治不好。

    官方 S3 桶无鉴权、无 PoW、无 cookie，纯 HTTP 直取。
    实测 150 篇跨主题抽样覆盖 98.7%，单篇 2–4 秒。

    adapter 参数保留只为兼容调用方签名，这条通道不用它。
    """
    if not pmcid:
        return None, ""
    pid = pmcid if pmcid.startswith("PMC") else "PMC" + pmcid

    key = _s3_pdf_key(pid)
    if not key:
        return None, "PMC OA 桶无正文 PDF"

    data, code = http_bytes(f"{S3_BUCKET}/{urllib.parse.quote(key)}",
                            tries=4, timeout=S3_TIMEOUT)
    if not data:
        return None, "PMC S3 超时（直连带宽低，可设 HTTPS_PROXY 走代理）"
    if not data.startswith(b"%PDF-"):
        return None, "PMC S3 取回非 PDF"
    with open(dest_pdf, "wb") as f:
        f.write(data)
    return dest_pdf, f"PMC OA S3 ({len(data)//1024}KB)"


def scholar_download(r, pdfdir):
    """从 Scholar cluster 找作者自存档的 PDF。

    价值: 很多论文没有 PMC ID 也没有 OA 标记，但作者把 preprint /
    accepted manuscript 放在实验室主页或机构库里 —— Scholar 的
    「所有版本」页会列出这些。这是从付费墙里捞回全文的最后机会。

    返回 (本地路径, 说明) 或 None。
    """
    try:
        from scholar import download_pdf, mine_cluster, scholar_search
    except Exception as e:
        log(f"  !! scholar 模块不可用: {str(e)[:50]}", err=True)
        return None

    title = (r.get("title") or "").strip()
    cluster = (r.get("cluster_url") or "").strip()
    if not cluster:
        # 采集阶段没存 cluster，就现搜一次
        if not title:
            return None
        res = scholar_search(f'"{title[:120]}"', pages=1)
        cluster = next((x.get("cluster") for x in res if x.get("cluster")), "")
    if not cluster:
        return None

    pdfs = [u for u in mine_cluster(cluster)
            if u.startswith("http") and "scholar.google.com" not in u]
    base = safe_name(r.get("doi") or r.get("pmid") or title[:40])
    dest = os.path.join(pdfdir, base + ".pdf")
    for u in pdfs:
        note = download_pdf(u, dest)
        if note:
            return dest, note
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="out/scored.tsv")
    ap.add_argument("--pdfdir", default="pdfs")
    ap.add_argument("--tier", default="core", choices=["core", "periphery", "all"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--browser", default=None, help="browser-use / cdp / http")
    ap.add_argument("--scholar", action="store_true",
                    help="核心通道全失败时，去 Scholar「所有版本」找作者自存档")
    a = ap.parse_args()

    os.makedirs(a.pdfdir, exist_ok=True)
    rows = read_tsv(a.infile)
    if not rows:
        log(f"{a.infile} 为空，先跑 triage.py", err=True)
        return 1
    fields = list(rows[0].keys()) + [f for f in STATUS_FIELDS if f not in rows[0]]

    tier_map = {"core": "核心", "periphery": "外围", "all": None}
    want_tier = tier_map[a.tier]
    todo = [r for r in rows
            if (want_tier is None or r.get("tier") == want_tier)
            and not (r.get("status") == "full_text")]
    if a.limit:
        todo = todo[:a.limit]
    log(f"待下载 {len(todo)} 篇（tier={a.tier}）")

    adapter = get_adapter(a.browser)
    log(f"浏览器适配器: {adapter.name}\n")

    ok = 0
    for i, r in enumerate(todo, 1):
        doi = (r.get("doi") or "").strip()
        pmcid = (r.get("pmcid") or "").strip()
        title = (r.get("title") or "")[:50]
        base = safe_name(pmcid or doi or r.get("pmid") or f"row{i}")

        # 已有文件就跳过
        hit = None
        for ext in (".pdf", ".xml"):
            p = os.path.join(a.pdfdir, base + ext)
            if os.path.exists(p) and os.path.getsize(p) > 8000:
                hit = p
                break
        if hit:
            r["pdf_path"] = hit
            r["status"] = "full_text"
            r["access_note"] = "已存在"
            ok += 1
            continue

        dest_pdf = os.path.join(a.pdfdir, base + ".pdf")
        got = False

        # 1. PMC PDF
        if pmcid:
            p, note = try_pmc(adapter, pmcid, dest_pdf)
            if p:
                r["pdf_path"], r["status"] = p, "full_text"
                r["access_note"] = f"PMC PDF ({note})"
                got = True

        # 2. OpenAlex PDF
        if not got and doi:
            u = openalex_pdf_url(doi)
            if u:
                data, note = adapter.fetch_pdf(u)
                if data:
                    with open(dest_pdf, "wb") as f:
                        f.write(data)
                    r["pdf_path"], r["status"] = dest_pdf, "full_text"
                    r["access_note"] = f"OpenAlex PDF ({note})"
                    got = True

        # 3. EPMC XML fallback
        if not got and pmcid:
            data = epmc_xml(pmcid)
            if data:
                dest_xml = os.path.join(a.pdfdir, base + ".xml")
                with open(dest_xml, "wb") as f:
                    f.write(data)
                r["pdf_path"], r["status"] = dest_xml, "full_text"
                r["access_note"] = "EuropePMC XML（无 PDF 版本）"
                got = True

        # 4. Scholar cluster 里的作者自存档（没有 OA 标记的论文的最后机会）
        if not got and doi and a.scholar:
            note = scholar_download(r, a.pdfdir)
            if note:
                r["pdf_path"], r["status"] = note[0], "full_text"
                r["access_note"] = f"Scholar 自存档 ({note[1]})"
                got = True

        if got:
            ok += 1
            print(f"[{i}/{len(todo)}] OK   {title}")
        else:
            r["status"] = "none"
            r["access_note"] = "无 OA 全文"
            print(f"[{i}/{len(todo)}] --   {title}")

        time.sleep(a.delay)

    adapter.close()
    write_tsv(a.infile, rows, fields)
    log(f"\n下载 {ok}/{len(todo)}")
    log(f"→ 状态已回写 {a.infile}")
    log("\n下一步: python scripts/report.py --in out/scored.tsv --out README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
