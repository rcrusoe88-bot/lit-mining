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
from common import (DATACITE, EPMC, EUTILS, INCOMPLETE, OPENALEX, UA,
                    build_query, epmc_query, http_json_checked, load_config,
                    log, norm_doi, preflight, write_tsv)

# 每维度一个完整 EPMC 查询；各维度并发，cursor 页内串行。
EPMC_JOBS = 6


# ---------- PubMed ----------

def pm_search(term, retmax):
    """先查 count，再分页取 ID。请求失败和短页不能当成零命中。"""
    if retmax <= 0:
        raise ValueError("PubMed retmax 必须大于 0")
    q = urllib.parse.urlencode({"db": "pubmed", "term": term, "retmode": "json", "retmax": "0"})
    data = http_json_checked(f"{EUTILS}/esearch.fcgi?{q}")
    result = data.get("esearchresult") if isinstance(data, dict) else None
    if not isinstance(result, dict) or result.get("errorlist") or "count" not in result:
        raise RuntimeError("PubMed count 响应无效")
    total = int(result["count"])
    if total < 0:
        raise RuntimeError("PubMed count 不能为负数")
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
        data = http_json_checked(f"{EUTILS}/esearch.fcgi?{q}")
        result = data.get("esearchresult") if isinstance(data, dict) else None
        page = result.get("idlist") if isinstance(result, dict) else None
        if (not isinstance(page, list) or result.get("errorlist")
                or len(page) < min(step, total - start)
                or any(not isinstance(pid, str) or not pid.strip() for pid in page)):
            raise RuntimeError(f"PubMed ID 页 {start} 响应无效或不足 count")
        ids.extend(page)
        if len(set(ids)) != len(ids):
            raise RuntimeError(f"PubMed ID 页 {start} 出现重复 ID")
        time.sleep(0.4)
    return ids, total


def _pm_summary_batch(pmids):
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    data = http_json_checked(f"{EUTILS}/esummary.fcgi?{q}", tries=4)
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("PubMed esummary 响应无效")
    missing = [pid for pid in pmids if not isinstance(result.get(pid), dict)
               or result[pid].get("error") or not (result[pid].get("title") or "").strip()]
    if missing:
        raise RuntimeError(f"PubMed 缺题录: {', '.join(missing[:10])}")
    return {pid: result[pid] for pid in pmids}


def pm_summary(pmids, batch=100):
    """批量取题录；大批失败拆成 20 条重试，子批仍失败则向上抛出。"""
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        try:
            out.update(_pm_summary_batch(chunk))
        except RuntimeError as e:
            log(f"  !! esummary 批次 {i} 失败，降级到 20/批: {str(e)[:50]}", err=True)
            for j in range(0, len(chunk), 20):
                out.update(_pm_summary_batch(chunk[j:j + 20]))
                time.sleep(0.5)
            continue
        time.sleep(0.6)
    return out


def _pm_abstract_batch(pmids):
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    req = urllib_request.Request(f"{EUTILS}/efetch.fcgi?{q}", headers=UA)
    with urllib_request.urlopen(req, timeout=120) as r:
        root = ET.fromstring(r.read())
    if root.tag != "PubmedArticleSet":
        raise RuntimeError("PubMed efetch 响应无效")
    out = {}
    for art in root:
        if art.tag not in ("PubmedArticle", "PubmedBookArticle"):
            continue
        pid = art.findtext(".//PMID")
        if not pid:
            raise RuntimeError("PubMed efetch 题录缺 PMID")
        parts = []
        for ab in art.iter("AbstractText"):
            label = ab.get("Label") or ""
            txt = "".join(ab.itertext()).strip()
            if txt:
                parts.append(f"{label}: {txt}" if label else txt)
        out[pid] = " ".join(parts)  # 题录本身没有摘要是合法的空串。
    missing = set(pmids) - out.keys()
    if missing:
        raise RuntimeError(f"PubMed efetch 缺题录: {', '.join(sorted(missing)[:10])}")
    return out


def pm_abstracts(pmids, batch=50):
    """批量取摘要；保留三次重试和 10 条子批降级，最终失败必须传播。"""
    out = {}
    for i in range(0, len(pmids), batch):
        chunk = pmids[i:i + batch]
        for attempt in range(3):
            try:
                out.update(_pm_abstract_batch(chunk))
                break
            except Exception as e:
                log(f"  !! 摘要批次 {i} 第 {attempt+1} 次失败: {str(e)[:50]}", err=True)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        else:
            for j in range(0, len(chunk), 10):
                out.update(_pm_abstract_batch(chunk[j:j + 10]))
                time.sleep(0.34)
            continue
        time.sleep(0.5)
    return out


def run_pubmed(cfg, dims):
    log("\n=== PubMed ===")
    retmax = (cfg.get("harvest", {}).get("pubmed", {}) or {}).get("retmax", 3000)
    hits = {}
    for d in dims:
        q = build_query(cfg, d["lib"])
        ids, total = pm_search(q, retmax)
        log(f"  {d['key']:24s} {total:>6d} (取 {len(ids)})")
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

    missing = set(ids) - recs.keys() | (set(ids) - abs_map.keys())
    if missing:
        raise RuntimeError(f"PubMed 题录或摘要请求未覆盖: {', '.join(sorted(missing)[:10])}")
    rows = []
    for pid in ids:
        rec = recs[pid]
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

def _epmc_one_query(q, page_size):
    """取完 cursor 全页；不足 hitCount 或响应异常必须失败。"""
    if not 1 <= page_size <= 1000:
        raise ValueError("EPMC page_size 必须在 1–1000 之间")
    out, seen, cursors = [], set(), set()
    cursor, hits = "*", None
    while True:
        cursors.add(cursor)
        params = {"query": q, "format": "json", "pageSize": str(page_size),
                  "cursorMark": cursor, "resultType": "core"}
        data = http_json_checked(f"{EPMC}/search?" + urllib.parse.urlencode(params), tries=4)
        result_list = data.get("resultList") if isinstance(data, dict) else None
        res = result_list.get("result") if isinstance(result_list, dict) else None
        if (not isinstance(data, dict) or data.get("error") or data.get("errMsg")
                or "hitCount" not in data or not isinstance(res, list)):
            raise RuntimeError("EPMC 响应无效：缺 hitCount/resultList.result 或返回错误")
        count = int(data["hitCount"])
        if count < 0 or (hits is not None and hits != count):
            raise RuntimeError(f"EPMC hitCount 无效或分页间变化: {hits} → {count}")
        hits = count
        for rec in res:
            if (not isinstance(rec, dict) or not rec.get("source") or not rec.get("id")
                    or not (rec.get("title") or "").strip()):
                raise RuntimeError("EPMC 缺题录: source/id/title")
            key = (rec["source"], rec["id"])
            if key in seen:
                raise RuntimeError(f"EPMC 分页重复题录: {key}")
            seen.add(key)
            out.append(rec)
        if len(out) > hits:
            raise RuntimeError(f"EPMC 返回题录超过 hitCount: {len(out)}/{hits}")
        if len(out) == hits:
            return out, hits
        next_cursor = data.get("nextCursorMark")
        if isinstance(next_cursor, str) and next_cursor in cursors:
            raise RuntimeError(f"EPMC 重复 cursor: {next_cursor}")
        if not res or len(res) < page_size or not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError(f"EPMC 分页未完成: {len(out)}/{hits}")
        cursor = next_cursor
        time.sleep(0.3)


def run_epmc(cfg, dims):
    """每维度只跑一个共用转换后的完整查询，跨维度累积 hits。"""
    log("\n=== Europe PMC ===")
    page_size = (cfg.get("harvest", {}).get("epmc", {}) or {}).get("page_size", 100)
    jobs = [(d["key"], epmc_query(cfg, d["lib"])) for d in dims]
    log(f"  {len(jobs)} 个完整查询，并发 {EPMC_JOBS}")
    rows, by_id, dim_hits = [], {}, {}
    with cf.ThreadPoolExecutor(max_workers=EPMC_JOBS) as pool:
        futs = {pool.submit(_epmc_one_query, q, page_size): key for key, q in jobs}
        for fut in cf.as_completed(futs):
            dim = futs[fut]
            res, hits = fut.result()
            log(f"  {dim:24s} {hits:>6d} (取 {len(res)})")
            for r in res:
                key = (r["source"], r["id"])
                dim_hits.setdefault(key, set()).add(dim)
                if key in by_id:
                    continue
                row = {
                    "src": "epmc", "uid": r["id"], "epmc_source": r["source"],
                    "doi": r.get("doi", "") or "",
                    "pmid": r.get("pmid", "") or "",
                    "pmcid": r.get("pmcid", "") or "",
                    "title": r["title"].rstrip("."),
                    "journal": ((r.get("journalInfo") or {}).get("journal") or {}).get("title", "")
                               or (r.get("bookOrReportDetails") or {}).get("publisher", ""),
                    "year": str(r.get("pubYear", "")),
                    "first_author": (r.get("authorString") or "").split(",")[0],
                    "pubtype": ";".join((r.get("pubTypeList") or {}).get("pubType", [])),
                    "abstract": (r.get("abstractText") or "")[:3000],
                    "is_oa": r.get("isOpenAccess", ""),
                    "in_pmc": r.get("inPMC", ""),
                }
                by_id[key] = row
                rows.append(row)
    for key, row in by_id.items():
        row.update(hits=";".join(sorted(dim_hits[key])), hit_count=len(dim_hits[key]))
    log(f"  合计 {len(rows)}")
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
    """OA 补充源：每维度最多 200 条；请求失败必须传播。"""
    log("\n=== OpenAlex ===")
    log("  采集上限: 每维度仅取首 200 条（OA 补充，不保证全量召回）")
    rows, seen = [], set()
    for d in dims:
        q = _openalex_query(cfg, d)
        params = {"search": q, "per-page": "200",
                  "select": "id,doi,title,publication_year,primary_location,open_access"}
        data = http_json_checked(f"{OPENALEX}/works?" + urllib.parse.urlencode(params), tries=2)
        res = data.get("results") if isinstance(data, dict) else None
        meta = data.get("meta") if isinstance(data, dict) else None
        if (not isinstance(res, list) or not isinstance(meta, dict)
                or "count" not in meta or data.get("error")):
            raise RuntimeError("OpenAlex 响应无效: 缺 results/meta.count")
        count = int(meta["count"])
        if count < 0 or len(res) != min(count, 200):
            raise RuntimeError(f"OpenAlex 首 200 条响应不完整: {len(res)}/{count}")
        log(f"  {d['key']:24s} {count} (取 {len(res)}，上限 200)")
        for w in res:
            if (not isinstance(w, dict) or not (w.get("id") or "").strip()
                    or not (w.get("title") or "").strip()):
                raise RuntimeError("OpenAlex 缺题录: work ID/title")
            doi = norm_doi(w.get("doi"))
            work_id = w["id"].rstrip("/").split("/")[-1]
            if not work_id:
                raise RuntimeError("OpenAlex 缺 work ID")
            key = ("doi", doi) if doi else ("work", work_id)
            if key in seen:
                continue
            seen.add(key)
            loc = w.get("primary_location") or {}
            rows.append({
                "src": "openalex", "uid": work_id,
                "doi": doi, "pmid": "", "pmcid": "",
                "title": w["title"],
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
    return rows


# ---------- DataCite ----------

def run_datacite(cfg, dims):
    log("\n=== DataCite（学位论文）===")
    log("  采集上限: 每维度最多 2 页 × 100 条（补充源，不保证全量召回）")
    rtype = (cfg.get("harvest", {}).get("datacite", {}) or {}).get("resource_type", "dissertation")
    rows = []
    for d in dims:
        q = " AND ".join(f"({p})" for p in (d["lib"],))
        q = q.replace("[Title/Abstract]", "")
        fetched = 0
        for page in range(1, 3):
            params = {"query": q[:800], "page[size]": "100", "page[number]": str(page)}
            if rtype:
                params["resource-type-id"] = rtype
            data = http_json_checked(f"{DATACITE}?" + urllib.parse.urlencode(params), tries=3)
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or data.get("errors"):
                raise RuntimeError("DataCite 响应无效: 缺 data 或返回 errors")
            fetched += len(items)
            total = (data.get("meta") or {}).get("total")
            if total is not None and fetched < min(int(total), page * 100):
                raise RuntimeError(f"DataCite 第 {page} 页不完整: {fetched}/{total}")
            for it in items:
                a = it.get("attributes") if isinstance(it, dict) else None
                if not isinstance(a, dict) or not it.get("id"):
                    raise RuntimeError("DataCite 缺题录: id/attributes")
                titles = a.get("titles") or []
                creators = a.get("creators") or []
                if not titles or not (titles[0].get("title") or "").strip():
                    raise RuntimeError(f"DataCite 缺标题: {it['id']}")
                rows.append({
                    "src": "datacite", "uid": it["id"],
                    "doi": a.get("doi", ""), "pmid": "", "pmcid": "",
                    "title": titles[0]["title"].rstrip("."),
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
        log(f"  {d['key']:24s} 取 {fetched}（上限 2 页/200 条）")
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

    want = set(x.strip() for x in a.sources.split(",") if x.strip())
    if a.with_theses:
        want.add("datacite")
    unknown = want - {"pubmed", "epmc", "openalex", "datacite", "scholar"}
    if not want or unknown:
        log(f"必须指定有效 source，未知源: {', '.join(sorted(unknown)) or '(空)'}", err=True)
        return 1

    try:
        if os.path.exists(a.out) and (not os.path.isdir(a.out) or os.listdir(a.out)):
            raise RuntimeError(f"输出目录必须不存在或为空: {a.out}；请换一个新目录重跑")
        cfg = load_config(a.config)
        dims = cfg.get("dimensions") or []
        if not dims:
            raise RuntimeError("配置里没有 dimensions")
        os.makedirs(a.out, exist_ok=True)
        marker = os.path.join(a.out, INCOMPLETE)
        with open(marker, "x", encoding="utf-8") as f:
            f.write("采集中；仅当所有指定源成功后删除此标记。\n")

        if not a.skip_preflight:
            ok, _ = preflight()
            if not ok:
                log("\n网络不可用，已中止。修好网络后换新目录重跑。", err=True)
                return 2
            log("")

        for source, run in (("pubmed", run_pubmed), ("epmc", run_epmc),
                            ("openalex", run_openalex), ("datacite", run_datacite)):
            if source in want:
                rows = run(cfg, dims)
                path = os.path.join(a.out, source + ".tsv")
                write_tsv(path, rows)
                log(f"  → {path}  ({len(rows)} 行)")

        if "scholar" in want:
            import scholar as scholar_mod
            scholar_mod.run_harvest(cfg, a.out, managed=True)
        os.remove(marker)
    except Exception as e:
        log(f"采集失败: {e}", err=True)
        return 1

    log("\n下一步: python scripts/triage.py --config ... --raw raw/ --out out/scored.tsv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
