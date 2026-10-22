#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 4：三源采集。

用法:
    python scripts/harvest.py --config search_config.yaml --out raw/

产出:
    raw/pubmed.tsv     题录 + 摘要
    raw/epmc.tsv       补漏 + 预印本 + 摘要
    raw/openalex.tsv   OA 判定 + pdf_url
    raw/datacite.tsv   学位论文（需 --with-theses）

坑（都踩过）:
    - esearch 的 retmax 会静默截断 → 先查 count 再分页
    - EPMC pageSize=1000 + resultType=core 会超时 → 降到 300 + cursorMark
    - efetch XML 偶发连接重置 → 分批小一点（100）
"""
import argparse
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.parse
import urllib.request as urllib_request
from defusedxml import ElementTree as ET   # 防 XXE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (DATACITE, EPMC, EUTILS, OPENALEX, UA, build_query,
                    http_json, http_json_checked, load_config, log, preflight,
                    write_tsv)

# Europe PMC 的查询数 = 维度项 × 载荷项 × 平台项，可能上百，
# 串行要好几分钟。各查询彼此独立，用线程池并发。
EPMC_JOBS = 6


# ---------- PubMed ----------

def pm_search(term, retmax):
    """先查 count，再分页取 ID。避免静默截断。"""
    q = urllib.parse.urlencode({"db": "pubmed", "term": term, "retmode": "json", "retmax": "0"})
    total = int(http_json(f"{EUTILS}/esearch.fcgi?{q}").get("esearchresult", {}).get("count", 0))
    if total == 0:
        return [], 0
    if total > retmax:
        log(f"  ⚠ 命中 {total} > retmax {retmax}，只取前 {retmax}", err=True)
    ids = []
    step = min(500, retmax)
    for start in range(0, min(total, retmax), step):
        q = urllib.parse.urlencode({
            "db": "pubmed", "term": term, "retmode": "json",
            "retmax": str(step), "retstart": str(start),
        })
        d = http_json(f"{EUTILS}/esearch.fcgi?{q}")
        ids.extend(d.get("esearchresult", {}).get("idlist", []))
        time.sleep(0.4)
    return ids, total


def pm_summary(pmids, batch=100):
    """批量取题录。

    每批 100 个 ID 是一次请求 = 一批数据。网络抖动时静默返回空
    会让整批消失（实测拿到 0/120，而重试一次就全好），
    所以这里用 checked 版本，失败就拆小批次重试。
    """
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(chunk), "retmode": "json"})
        try:
            d = http_json_checked(f"{EUTILS}/esummary.fcgi?{q}", tries=4)
        except RuntimeError as e:
            # 降级：拆成小批重试，实在不行才丢
            log(f"  !! esummary 批次 {i} 失败，降级到 20/批: {str(e)[:50]}", err=True)
            for j in range(0, len(chunk), 20):
                sub = chunk[j:j + 20]
                q2 = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(sub), "retmode": "json"})
                try:
                    d2 = http_json_checked(f"{EUTILS}/esummary.fcgi?{q2}", tries=4)
                except RuntimeError as e2:
                    log(f"  !! esummary 子批 {i}.{j} 也失败，丢弃 {len(sub)} 条: {str(e2)[:40]}", err=True)
                    continue
                for pid in sub:
                    if pid in d2.get("result", {}):
                        out[pid] = d2["result"][pid]
                time.sleep(0.5)
            continue
        for pid in chunk:
            if pid in d.get("result", {}):
                out[pid] = d["result"][pid]
        time.sleep(0.6)
    return out


def pm_abstracts(pmids, batch=50):
    """批量取摘要。同样是「一次请求 = 一批数据」，失败必须能感知。

    降级策略同 pm_summary：大批失败就拆成 10 条一批重试。
    batch 默认 50 而不是 100 —— efetch 返回的 XML 比 esummary 大得多，
    实测 100 条一批时连接被掐的概率明显更高。
    """
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"})
        root = None
        for attempt in range(3):
            try:
                req = urllib_request.Request(f"{EUTILS}/efetch.fcgi?{q}", headers=UA)
                with urllib_request.urlopen(req, timeout=120) as r:
                    root = ET.fromstring(r.read())
                break
            except Exception as e:
                log(f"  !! 摘要批次 {i} 第 {attempt+1} 次失败: {str(e)[:50]}", err=True)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if root is None:
            # 降级：拆成 10 条一批
            for j in range(0, len(chunk), 10):
                sub = chunk[j:j + 10]
                q2 = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(sub), "retmode": "xml"})
                try:
                    req = urllib_request.Request(f"{EUTILS}/efetch.fcgi?{q2}", headers=UA)
                    with urllib_request.urlopen(req, timeout=120) as r:
                        root2 = ET.fromstring(r.read())
                except Exception as e2:
                    log(f"  !! 摘要子批 {i}.{j} 也失败，丢弃 {len(sub)} 条: {str(e2)[:40]}", err=True)
                    continue
                for art in root2.iter("PubmedArticle"):
                    pid = art.findtext(".//PMID")
                    if not pid:
                        continue
                    parts = []
                    for ab in art.iter("AbstractText"):
                        label = ab.get("Label") or ""
                        txt = "".join(ab.itertext()).strip()
                        if txt:
                            parts.append(f"{label}: {txt}" if label else txt)
                    out[pid] = " ".join(parts)
                time.sleep(0.34)
            continue
        for art in root.iter("PubmedArticle"):
            pid = art.findtext(".//PMID")
            if not pid:
                continue
            parts = []
            for ab in art.iter("AbstractText"):
                label = ab.get("Label") or ""
                txt = "".join(ab.itertext()).strip()
                if txt:
                    parts.append(f"{label}: {txt}" if label else txt)
            out[pid] = " ".join(parts)
        time.sleep(0.5)
    return out


def run_pubmed(cfg, dims):
    log("\n=== PubMed ===")
    retmax = (cfg.get("harvest", {}).get("pubmed", {}) or {}).get("retmax", 3000)
    hits = {}
    total_union = 0
    for d in dims:
        q = build_query(cfg, d["lib"])
        ids, total = pm_search(q, retmax)
        log(f"  {d['key']:24s} {total:>6d} (取 {len(ids)})")
        total_union = max(total_union, total)
        for pid in ids:
            hits.setdefault(pid, set()).add(d["key"])
        time.sleep(0.4)

    log(f"  唯一 PMID: {len(hits)}")
    ids = sorted(hits)
    log("  抓题录...")
    recs = pm_summary(ids)
    log(f"    题录 {len(recs)}/{len(ids)}")
    log("  抓摘要...")
    abs_map = pm_abstracts(ids)
    log(f"    摘要 {len(abs_map)}/{len(ids)}")

    rows = []
    for pid in ids:
        rec = recs.get(pid)
        if not rec:
            continue
        aids = {x.get("idtype"): x.get("value") for x in rec.get("articleids", [])}
        authors = rec.get("authors", [])
        rows.append({
            "src": "pubmed", "uid": pid,
            "doi": aids.get("doi", ""), "pmid": pid,
            "pmcid": aids.get("pmc", ""),
            "title": (rec.get("title") or "").rstrip("."),
            "journal": rec.get("fulljournalname") or rec.get("source", ""),
            "year": (rec.get("pubdate") or "")[:4],
            "first_author": authors[0]["name"] if authors else "",
            "pubtype": ";".join(rec.get("pubtype", [])),
            "abstract": abs_map.get(pid, ""),
            "hits": ";".join(sorted(hits[pid])),
            "hit_count": len(hits[pid]),
        })
    return rows


# ---------- Europe PMC ----------

def _or_branches(lib):
    """把一个检索式的顶层 OR 项拆开，返回各项的文本。

    Europe PMC 的查询解析器对长 OR 组有缺陷：OR 的项越多，结果越少，
    甚至直接归零。实测同一组词：
        A AND (mRNA)                               -> 102
        A AND (mRNA OR circRNA)                    -> 2
        A AND (mRNA OR "messenger RNA")            -> 21
        A AND (mRNA OR "messenger RNA" OR circRNA) -> 0   <- 三个词就没了

    PubMed 同一查询是 878。所以这是 EPMC 侧的解析问题，不是语料差异。

    修法：OR 组拆成单分支分别查，结果合并去重。
    合并后约等于各分支之和，远好于 0。
    """
    lib = (lib or "").strip()
    m = re.match(r"^\((.*)\)$", lib, re.S)
    if not m:
        return [lib]
    inner = m.group(1)
    parts, depth, buf = [], 0, ""
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and inner[i:i + 4] == " OR ":
            parts.append(buf)
            buf = ""
            i += 4
            continue
        buf += ch
        i += 1
    parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _epmc_queries(cfg, dim):
    """给 Europe PMC 生成一组等价查询（每个都尽量简单）。

    不用 build_query 的结果再解析 —— 直接按配置结构生成，
    避免脆弱的查询串解析。

    EPMC 的 OR 组有硬上限：组越长结果越少，6 个词就归零。
    所以三个 OR 组（维度 / 载荷 / 平台）全部拆成单分支，
    组合成多个简单查询，分别跑完再合并去重。

    查询数 = 维度项 × 载荷项 × 平台项，可能爆掉。
    上限 12 个，超了就按「维度固定、载荷平台取前几个」收敛。
    """
    # 先展开通配符（"T cell*" -> "T cell" OR "T cells"），再拆 OR 组。
    # 顺序反了的话，展开会把 OR 组重新撑大，EPMC 那边就又归零了。
    def expand_then_split(lib):
        out = []
        for p in _or_branches(lib or ""):
            for v in _or_branches(_expand_wildcards(p)):
                out.append(v)
        return out or [""]

    dim_parts = expand_then_split(dim.get("lib", ""))
    load_parts = expand_then_split((cfg.get("load") or {}).get("lib", ""))
    plat_parts = expand_then_split((cfg.get("platform") or {}).get("lib", ""))

    # 维度词最长最具体，优先全保留
    combos = []
    for dp in dim_parts:
        for lp in load_parts:
            for pp in plat_parts:
                segs = [s for s in (dp, lp, pp) if s]
                combos.append(" AND ".join(f"({s})" for s in segs))

    if len(combos) > 12:
        # 收敛：维度全留，载荷平台按顺序取，直到 12
        combos = combos[:12]

    return combos


def _expand_wildcards(q):
    """把 PubMed 风格通配符展开成 OR 变体，供 Europe PMC 用。

    Europe PMC 的通配符语义和 PubMed 不一致，实测差异很大：
        liposom*[Title/Abstract]                → 18 命中（PubMed 4788）
        (liposom*) AND (mRNA)                   → 4 命中
        (liposom*) AND (microfluidic*)          → 0 命中（！）
        (liposome OR liposomes) 各带字段标签      → 22 命中 ✅

    带 * 的词在 EPMC 上几乎匹配不到东西，和其他条件 AND 时更是直接归零 ——
    整路数据变成 0 条，却不报任何错，看起来像「这个方向没有文献」。

    修法：把 `词*` 换成 `("词"[字段] OR "词s"[字段] OR "词es"[字段])`。
    每个变体都自带字段标签 —— 括号整体带字段标签语义是错的。

    PubMed 那条路不用改：那边通配符工作正常。
    """
    # "词*"（带引号，后面可能跟字段标签）
    def repl_quoted(m):
        base = m.group(1).rstrip("*").strip()
        if not base:
            return m.group(0)
        field = m.group(2) or ""
        vs = sorted({base, base + "s", base + "es"})
        # 避免 "liposome" + es = "liposomees" 这种蠢变体
        vs = [v for v in vs if not (v.endswith("es") and v[:-2].endswith("e"))]
        return "(" + " OR ".join(f'"{v}"{field}' for v in vs) + ")"

    q = re.sub(r'"([A-Za-z][A-Za-z0-9 _\-]*)\*"(\[Title/Abstract\])?', repl_quoted, q)

    # 词* 紧跟 [ 字段（不带引号）
    def repl_bare(m):
        base = m.group(1).rstrip("*")
        field = m.group(2) or ""
        if not base:
            return m.group(0)
        vs = sorted({base, base + "s", base + "es"})
        vs = [v for v in vs if not (v.endswith("es") and v[:-2].endswith("e"))]
        return "(" + " OR ".join(f'"{v}"{field}' for v in vs) + ")"

    q = re.sub(r'(?<![A-Za-z0-9"])([A-Za-z][A-Za-z0-9\-]*)\*(\[Title/Abstract\])?',
               repl_bare, q)
    return q


def _epmc_one_query(q, page_size, max_pages=5):
    """跑一个 EPMC 查询的所有页。返回 (结果列表, 命中数, 失败页数)。"""
    out, hits, failed = [], 0, 0
    cursor = "*"
    for _ in range(max_pages):
        params = {"query": q, "format": "json", "pageSize": str(page_size),
                  "cursorMark": cursor, "resultType": "core"}
        data = http_json(f"{EPMC}?" + urllib.parse.urlencode(params), tries=4)
        if not data:
            failed += 1
            time.sleep(1.5)
            continue
        res = data.get("resultList", {}).get("result", [])
        if not hits:
            hits = int(data.get("hitCount", 0) or 0)
        if not res:
            break
        out.extend(res)
        cursor = data.get("nextCursorMark", "")
        if not cursor or len(res) < page_size:
            break
        time.sleep(0.3)
    return out, hits, failed


def run_epmc(cfg, dims):
    """Europe PMC 补漏 + 预印本。

    EPMC 的查询解析器有三个怪癖（详见 PROTOCOL.md）：
    通配符不工作、OR 组超过 6 个词就归零、负载高时 302 到不可达主机。
    所以这里的查询由 _epmc_queries 拆成单分支的简单查询。

    各查询彼此独立，可以并发跑 —— 串行的话 36 个查询要 6 分钟。
    页内仍串行，因为 cursorMark 依赖上一页。
    """
    log("\n=== Europe PMC ===")
    page_size = (cfg.get("harvest", {}).get("epmc", {}) or {}).get("page_size", 100)
    jobs = []
    for d in dims:
        for q in _epmc_queries(cfg, d):
            jobs.append((d["key"], q))

    log(f"  {len(jobs)} 个查询，并发 {EPMC_JOBS}")
    rows, seen = [], set()
    failed = 0
    with cf.ThreadPoolExecutor(max_workers=EPMC_JOBS) as pool:
        futs = {pool.submit(_epmc_one_query, q, page_size): (k, q)
                for k, q in jobs}
        for fut in cf.as_completed(futs):
            key, q = futs[fut]
            try:
                res, hits, nfail = fut.result()
            except Exception as e:
                log(f"  {key:24s} 查询异常: {str(e)[:50]}", err=True)
                failed += 1
                continue
            failed += nfail
            for r in res:
                k = r.get("id", "")
                if not k or k in seen:
                    continue
                seen.add(k)
                rows.append({
                    "src": "epmc", "uid": k,
                    "doi": r.get("doi", "") or "",
                    "pmid": r.get("pmid", "") or "",
                    "pmcid": r.get("pmcid", "") or "",
                    "title": (r.get("title") or "").rstrip("."),
                    "journal": (r.get("journalInfo", {}) or {}).get("journal", {}).get("title", "")
                               or (r.get("bookOrReportDetails", {}) or {}).get("publisher", ""),
                    "year": str(r.get("pubYear", "")),
                    "first_author": (r.get("authorString") or "").split(",")[0],
                    "pubtype": ";".join((r.get("pubTypeList") or {}).get("pubType", [])),
                    "abstract": (r.get("abstractText") or "")[:3000],
                    "is_oa": r.get("isOpenAccess", ""),
                    "in_pmc": r.get("inPMC", ""),
                    "hits": key, "hit_count": 1,
                })
    log(f"  合计 {len(rows)}")
    if failed:
        log(f"  注意: {failed} 个页面失败，这路数据不完整", err=True)
    return rows


# ---------- OpenAlex ----------

# OpenAlex 自然语言检索里没意义的词
_OA_STOP = {
    "or", "and", "not", "title", "abstract", "publication", "type",
    "mesh", "term", "all", "text", "words",
}


def _openalex_query(cfg, dim):
    """给 OpenAlex 构造自然语言检索词。

    不能用 build_query —— 那个返回的是 PubMed 检索式，含
    [Title/Abstract] 字段标签，而 OpenAlex 的 search 参数是自然语言。
    实测：把 PubMed 检索式原样传进去，三个维度全部 400 Bad Request，
    采到 0 条 —— 而且不报错，看起来像「这个方向没有文献」。

    词也不能多。OpenAlex 的 search 对长查询召回会塌掉：
      "T cell CD3 CD8 CAR-T mRNA messenger RNA circRNA lipid nanoparticle LNP
       ionizable lipid liposom"  → 0 条
      "T cell lipid nanoparticle mRNA"                          → 97552 条
    所以只取最有区分度的几个词：维度的 strong 词（最多 2 个）+
    平台词 + 载荷词，总共不超过 6 个。
    """
    def words_of(src, limit):
        out = []
        for quoted, bare in re.findall(r'"([^"]+)"|([A-Za-z][A-Za-z0-9\-_]*)', src or ""):
            w = quoted or bare
            if not w or w.lower() in _OA_STOP:
                continue
            # OpenAlex 的 search 不接受通配符，带 * 直接 400 Bad Request
            w = w.replace("*", "").strip()
            if w:
                out.append(w)
        return out[:limit]

    # 维度：优先 strong（区分度最高），没有就用 terms
    dim_words = words_of(" ".join(dim.get("strong") or dim.get("terms") or []), 2)
    plat = words_of((cfg.get("platform") or {}).get("lib", ""), 2)
    load = words_of((cfg.get("load") or {}).get("lib", ""), 1)

    seen = set()
    out = []
    for w in dim_words + plat + load:
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
    return " ".join(out[:6])


def run_openalex(cfg, dims):
    """OpenAlex 主要用于补 OA 判定和 pdf_url，采集量有限（免费 API 慢）。

    注意：国内直连 api.openalex.org 会超时（实测 21s 无响应），
    需要设 HTTPS_PROXY 走代理。失败必须告警，不能静默。
    """
    log("\n=== OpenAlex ===")
    rows = []
    seen = set()
    failed = 0
    for d in dims:
        q = _openalex_query(cfg, d)
        # OpenAlex 的 search 是自然语言，不是 PubMed 语法
        params = {"search": q, "per-page": "200",
                  "select": "id,doi,title,publication_year,primary_location,open_access"}
        data = http_json(f"{OPENALEX}/works?" + urllib.parse.urlencode(params), tries=2)
        if not data:
            failed += 1
            log(f"  {d['key']:24s} 失败（OpenAlex 直连国内常超时，可设 HTTPS_PROXY）", err=True)
            continue
        res = data.get("results", [])
        log(f"  {d['key']:24s} {data.get('meta', {}).get('count', '?')}")
        for w in res:
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            if doi in seen:
                continue
            seen.add(doi)
            loc = w.get("primary_location") or {}
            rows.append({
                "src": "openalex", "uid": w.get("id", "").split("/")[-1],
                "doi": doi, "pmid": "", "pmcid": "",
                "title": w.get("title") or "",
                "journal": ((loc.get("source") or {}).get("display_name") or ""),
                "year": str(w.get("publication_year", "")),
                "first_author": "", "pubtype": "", "abstract": "",
                "is_oa": "Y" if (w.get("open_access") or {}).get("is_oa") else "N",
                "oa_url": (w.get("open_access") or {}).get("oa_url", ""),
                "pdf_url": loc.get("pdf_url") or "",
                "hits": d["key"], "hit_count": 1,
            })
        time.sleep(0.5)
    log(f"  合计 {len(rows)}")
    if failed:
        log(f"  注意: {failed}/{len(dims)} 个维度失败，OpenAlex 这路数据不完整", err=True)
    return rows


# ---------- DataCite ----------

def run_datacite(cfg, dims):
    log("\n=== DataCite（学位论文）===")
    rtype = (cfg.get("harvest", {}).get("datacite", {}) or {}).get("resource_type", "dissertation")
    rows = []
    for d in dims:
        q = " AND ".join(f"({p})" for p in (d["lib"],))
        q = q.replace("[Title/Abstract]", "")
        for page in range(1, 3):
            params = {"query": q[:800], "page[size]": "100", "page[number]": str(page)}
            if rtype:
                params["resource-type-id"] = rtype
            data = http_json(f"{DATACITE}?" + urllib.parse.urlencode(params), tries=3)
            items = data.get("data", [])
            if not items:
                break
            for it in items:
                a = it.get("attributes", {})
                titles = a.get("titles", [{}])
                creators = a.get("creators", [{}])
                rows.append({
                    "src": "datacite", "uid": it.get("id", ""),
                    "doi": a.get("doi", ""), "pmid": "", "pmcid": "",
                    "title": (titles[0].get("title", "") if titles else "").rstrip("."),
                    "journal": a.get("publisher", ""),
                    "year": str(a.get("publicationYear", "")),
                    "first_author": creators[0].get("name", "") if creators else "",
                    "pubtype": "Dissertation", "abstract": "",
                    "url": a.get("url", ""),
                    "hits": d["key"], "hit_count": 1,
                })
            if len(items) < 100:
                break
            time.sleep(0.5)
        log(f"  {d['key']:24s} done")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="raw")
    ap.add_argument("--sources", default="pubmed,epmc,openalex",
                    help="逗号分隔: pubmed,epmc,openalex,datacite,scholar")
    ap.add_argument("--with-theses", action="store_true", help="加 DataCite 学位论文")
    ap.add_argument("--with-scholar", action="store_true",
                    help="加 Google Scholar（需 browser-use + 日常 Chrome，慢且需限速）")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="跳过连通性预检（网络已确认可用时）")
    a = ap.parse_args()

    cfg = load_config(a.config)
    dims = cfg.get("dimensions") or []
    if not dims:
        log("配置里没有 dimensions", err=True)
        return 1

    # 开工前先探网络。网络层故障会让采集静默返回空，
    # 错误信息指向「没命中」而不是「网络不通」，排查成本极高。
    if not a.skip_preflight:
        ok, _ = preflight()
        if not ok:
            log("\n网络不可用，已中止。修好网络再跑（可加 --skip-preflight 强制跳过）。", err=True)
            return 2
        log("")

    os.makedirs(a.out, exist_ok=True)
    want = set(x.strip() for x in a.sources.split(",") if x.strip())
    if a.with_theses:
        want.add("datacite")

    if "pubmed" in want:
        rows = run_pubmed(cfg, dims)
        write_tsv(os.path.join(a.out, "pubmed.tsv"), rows)
        log(f"  → {a.out}/pubmed.tsv  ({len(rows)} 行)")

    if "epmc" in want:
        rows = run_epmc(cfg, dims)
        write_tsv(os.path.join(a.out, "epmc.tsv"), rows)
        log(f"  → {a.out}/epmc.tsv  ({len(rows)} 行)")

    if "openalex" in want:
        rows = run_openalex(cfg, dims)
        write_tsv(os.path.join(a.out, "openalex.tsv"), rows)
        log(f"  → {a.out}/openalex.tsv  ({len(rows)} 行)")

    if "datacite" in want:
        rows = run_datacite(cfg, dims)
        write_tsv(os.path.join(a.out, "datacite.tsv"), rows)
        log(f"  → {a.out}/datacite.tsv  ({len(rows)} 行)")

    if "scholar" in want:
        # Scholar 单独跑（要 browser-use 子进程 + 限速），不复用本文件的 HTTP 栈
        import scholar as scholar_mod
        scholar_mod.run_harvest(cfg, a.out)

    log("\n下一步: python scripts/triage.py --config ... --raw raw/ --out out/scored.tsv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
