#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""步骤 6：OA 全文下载。

用法:
    python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core
    python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier all --limit 20

通道优先级（实测排序，别改）:
    1. PMC OA S3      开放数据桶直取         首选，98.7% 覆盖，无需浏览器
    2. 行内 pdf_url   采集阶段存下的 OA 直链（无 DOI 也能用）
    3. OpenAlex PDF   locations[].pdf_url    出版商直供
    4. oa_url         题录里的 OA 候选，必须校验
    5. EPMC XML       fullTextXML            仅当 PDF 不存在
    6. Scholar        作者自存档（需 --scholar）

不碰付费墙。拿不到就记 failure_reason（network/http_error/invalid_content/
not_found/unknown），交给 report.py 分类；access_note 保留通道细节。
"""
import argparse
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (EPMC, OPENALEX, atomic_write, file_ok, has_full_text,
                    http_bytes, http_json_checked, log, read_tsv, safe_name,
                    write_tsv)
from browser import get_adapter

STATUS_FIELDS = ["status", "oa_status", "pdf_path", "access_note", "failure_reason"]


def _save_verified(path, data):
    """先写盘再校验内容；不合格立刻删掉。返回是否可用。

    绝不在校验前报成功 —— 老版本直接 write 就算拿到了，WAF 回的
    200 + HTML 会被当成全文，直到下游解析才炸。
    """
    atomic_write(path, data)
    if file_ok(path):
        return True
    try:
        os.remove(path)
    except OSError:
        pass
    return False


# ponytail: 关键词粗分类，够 report.py 分组；通道要是能直接给出结构化原因再换掉
# 注意先判 invalid/http，再判 not_found：404 是 HTTP 层结果，不能算「没找到公开副本」
_NOT_FOUND = ("无正文", "no pdf", "not found", "无 pdf 直链", "无可用 pdf")
_INVALID = ("非 pdf", "非pdf", "too small", "内容无效", "invalid")
_HTTP = ("401", "403", "404", "429", "500", "502", "503", "forbidden",
         "unauthorized", "too many requests")
_NETWORK = ("超时", "timeout", "timed out", "连接", "ssl", "remote", "reset",
            "urlopen", "失败", "network", "getaddrinfo", "temporary failure")


def _classify(note):
    """从通道说明里粗分类失败原因。"""
    n = (note or "").lower()
    if not n:
        return "unknown"
    for kws, reason in ((_NOT_FOUND, "not_found"), (_INVALID, "invalid_content"),
                        (_HTTP, "http_error"), (_NETWORK, "network")):
        if any(k in n for k in kws):
            return reason
    return "unknown"


def _pick(reasons):
    """多通道失败时取最值得重试的那个：网络故障别记成「无 OA」。"""
    for k in ("network", "http_error", "invalid_content", "not_found"):
        if k in reasons:
            return k
    return "unknown"


def _cached(r, pdfdir, base):
    """记录里的原路径优先，再看 pdfdir 下的默认命名。返回可用正文路径或 None。

    判定统一走 has_full_text（状态 + 文件内容），不用旧的 size>8000 ——
    那既会放过 HTML 伪装，也会误杀小 PDF。
    """
    p = (r.get("pdf_path") or "").strip()
    if p:
        # 记录状态可能是 si_only/旧值：只有原本就是 full_text 才允许复用。
        # 否则一份 SI 会被「缓存命中」自动提升成正文。
        if (r.get("status") or "") == "full_text":
            for bd in (".", pdfdir):
                if has_full_text({"status": "full_text", "pdf_path": p}, bd):
                    return p if os.path.isabs(p) else os.path.normpath(os.path.join(bd, p))
    # si_only 是「只有补充材料」，无论文件叫什么都不能当正文复用
    if (r.get("status") or "") == "si_only":
        return None
    for name in (base + ".pdf", base + ".xml"):
        if has_full_text({"status": "full_text", "pdf_path": name}, pdfdir):
            return os.path.join(pdfdir, name)
    return None


def openalex_pdf_url(doi):
    """从 OpenAlex 拿出版商直供的 pdf_url。返回 (url, 失败原因)。

    请求失败要如实上报：静默返回空会把网络问题记成「无 OA」。
    """
    if not doi:
        return "", ""
    try:
        d = http_json_checked(f"{OPENALEX}/works/doi:{urllib.parse.quote(doi)}",
                              tries=2, timeout=45)
    except Exception as e:
        return "", f"请求失败 {str(e)[:60]}"
    for loc in (d.get("locations") or []):
        if loc.get("is_oa") and loc.get("pdf_url"):
            return loc["pdf_url"], ""
    return "", ""


def epmc_xml(pmcid):
    """Europe PMC 全文 XML，作为 PDF 的 fallback。"""
    if not pmcid:
        return None
    pid = pmcid if pmcid.startswith("PMC") else "PMC" + pmcid
    data, code = http_bytes(f"{EPMC}/{pid}/fullTextXML", tries=2, timeout=90)
    # 不再按体积/子串预判：交给 file_ok 安全解析后判断，小 XML 也是合法正文
    return data or None


S3_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

# 超时必须放宽：实测国内直连 S3 单连接只有 36KB/s，1.9MB 的 PDF 要 52 秒。
# 用 60s 会把正常文件误判成超时（实测 5 次下载有 3 次被截断）。
# 走代理（设 HTTPS_PROXY）则 1MB/s，秒下。
S3_TIMEOUT = 240


def _s3_pdf_key(pmcid):
    """在 PMC 开放数据桶里定位正文 PDF 的 key。返回 (key, 失败原因)。

    目录里混着补充材料（*_MOESM*_ESM.pdf），必须精确匹配 {PMCID}.{ver}.pdf。
    版本号不一定是 1（修订过的是 .2/.3），所以要先 list 再取。

    list 失败和「桶里真没有正文」是两回事：前者是网络，后者是查无此文，
    混成一个空 key 会让 failure_reason 永远记成 not_found。
    """
    url = f"{S3_BUCKET}/?list-type=2&prefix={urllib.parse.quote(pmcid)}.&max-keys=200"
    body, code = http_bytes(url, tries=3, timeout=90)
    if not body:
        return None, f"S3 列表失败（code={code}）"
    keys = re.findall(r"<Key>([^<]+)</Key>", body.decode("utf-8", "replace"))
    pat = re.compile(rf"^{re.escape(pmcid)}\.\d+/{re.escape(pmcid)}\.\d+\.pdf$")
    key = next((k for k in keys if pat.match(k)), None)
    return (key, "") if key else (None, "PMC OA 桶无正文 PDF")


def try_pmc(adapter, pmcid, dest_pdf):
    """PMC PDF：走官方开放数据 S3 桶，不用浏览器。返回 (path, note)。

    PMC 站点的 PDF 直链有工作量证明（PoW）挑战：返回 200 + HTML，
    要求算 sha256 前缀、写 cookie 再刷新页面。浏览器每篇都要重跑一次，
    批量时还叠加限流 —— 这才是老版本「HTTP 200 但返回 HTML」的真正原因，
    不是限流，加等待和重建会话都治不好。

    官方 S3 桶无鉴权、无 PoW、无 cookie，纯 HTTP 直取。
    实测 150 篇跨主题抽样覆盖 98.7%，单篇 2–4 秒。

    adapter 参数保留只为兼容调用方签名，这条通道不用它。
    note 带真实原因前缀（S3 列表失败 / 无正文 PDF / 下载失败 / 非 PDF），
    调用方据此分类，别一律写成「无 OA」。
    """
    if not pmcid:
        return None, ""
    pid = pmcid if pmcid.startswith("PMC") else "PMC" + pmcid

    key, why = _s3_pdf_key(pid)
    if not key:
        return None, why

    data, code = http_bytes(f"{S3_BUCKET}/{urllib.parse.quote(key)}",
                            tries=4, timeout=S3_TIMEOUT)
    if not data:
        return None, f"下载失败（code={code}，直连带宽低，可设 HTTPS_PROXY 走代理）"
    if not data.startswith(b"%PDF-"):
        return None, "取回非 PDF"
    if not _save_verified(dest_pdf, data):
        return None, "取回内容无效（已删除）"
    return dest_pdf, f"PMC OA S3 ({len(data)//1024}KB)"


def scholar_download(r, pdfdir):
    """从 Scholar cluster 找作者自存档的 PDF。返回 (path, note) 或 (None, 原因)。

    价值: 很多论文没有 PMC ID 也没有 OA 标记，但作者把 preprint /
    accepted manuscript 放在实验室主页或机构库里 —— Scholar 的
    「所有版本」页会列出这些。这是从付费墙里捞回全文的最后机会。
    """
    try:
        from scholar import download_pdf, mine_cluster, scholar_search
    except Exception as e:
        return None, f"scholar 模块不可用: {str(e)[:50]}"

    title = (r.get("title") or "").strip()
    cluster = (r.get("cluster_url") or "").strip()
    try:
        if not cluster:
            # 采集阶段没存 cluster，就现搜一次
            if not title:
                return None, "无 cluster 且无标题"
            res = scholar_search(f'"{title[:120]}"', pages=1)
            cluster = next((x.get("cluster") for x in res if x.get("cluster")), "")
        if not cluster:
            return None, "无 cluster"
        pdfs = [u for u in mine_cluster(cluster)
                if u.startswith("http") and "scholar.google.com" not in u]
        if not pdfs:
            return None, "cluster 无 PDF 直链"
        base = safe_name(r.get("doi") or r.get("pmid") or title[:40])
        dest = os.path.join(pdfdir, base + ".pdf")
        for u in pdfs:
            note = download_pdf(u, dest)
            if note:
                return dest, note
        return None, "候选 PDF 全部失败"
    except Exception as e:
        # _run_snippet 现在会因超时/非零退出抛异常，这里必须接住，
        # 否则一篇 Scholar 抽风会打断整批下载
        log(f"  !! scholar 失败: {str(e)[:60]}", err=True)
        return None, f"scholar 异常: {str(e)[:60]}"


def fetch_full_text(r, pdfdir, adapter, dest_pdf, use_scholar, notes):
    """按优先级试通道。成功返回 (path, note)；全失败返回 (None, "")。

    每条通道的真实原因追加到 notes，调用方据此写 access_note / failure_reason。
    """
    pmcid = (r.get("pmcid") or "").strip()
    doi = (r.get("doi") or "").strip()

    # 1. PMC OA S3
    if pmcid:
        p, note = try_pmc(adapter, pmcid, dest_pdf)
        if p:
            return p, f"PMC PDF ({note})"
        notes.append(f"PMC: {note}")

    # 2/3/4. 候选 PDF 直链：行内 pdf_url（无 DOI 也能用）→ OpenAlex → oa_url
    # 没有 DOI 就没有 OpenAlex 可查，直接跳过，别发一个必然无效的请求
    u, why = openalex_pdf_url(doi) if doi else ("", "")
    if why:
        notes.append(f"OpenAlex: {why}")
    cands = [(r.get("pdf_url") or "").strip(), u, (r.get("oa_url") or "").strip()]
    for cand in dict.fromkeys(c for c in cands if c):
        data, note = adapter.fetch_pdf(cand)
        if not data:
            notes.append(f"PDF {cand[:60]}: {note}")
            continue
        if _save_verified(dest_pdf, data):
            return dest_pdf, f"PDF ({note}) ← {cand[:80]}"
        notes.append(f"PDF {cand[:60]}: 内容无效（已删除）")

    # 5. EPMC XML fallback
    if pmcid:
        data = epmc_xml(pmcid)
        if data:
            dest_xml = os.path.splitext(dest_pdf)[0] + ".xml"
            if _save_verified(dest_xml, data):
                return dest_xml, "EuropePMC XML（无 PDF 版本）"
            notes.append("EPMC: XML 内容无效（已删除）")
        else:
            notes.append("EPMC: 无全文 XML")

    # 6. Scholar cluster 里的作者自存档（没有 OA 标记的论文的最后机会）
    if use_scholar:
        p, note = scholar_download(r, pdfdir)
        if p:
            return p, f"Scholar 自存档 ({note})"
        notes.append(f"Scholar: {note}")

    return None, ""


def _one(r, a, adapter, i, total):
    """处理一篇：缓存 → 各通道。就地更新 r，返回是否拿到全文。"""
    doi = (r.get("doi") or "").strip()
    pmcid = (r.get("pmcid") or "").strip()
    title = (r.get("title") or "")[:50]
    base = safe_name(pmcid or doi or r.get("pmid") or f"row{i}")
    r["failure_reason"] = ""

    hit = _cached(r, a.pdfdir, base)
    if hit:
        r["pdf_path"], r["status"] = hit, "full_text"
        r["access_note"] = "已存在"
        print(f"[{i}/{total}] OK   {title}  (缓存)")
        return True

    dest_pdf = os.path.join(a.pdfdir, base + ".pdf")
    notes = []
    p, note = fetch_full_text(r, a.pdfdir, adapter, dest_pdf, a.scholar, notes)
    if p:
        r["pdf_path"], r["status"] = p, "full_text"
        r["access_note"] = note
        print(f"[{i}/{total}] OK   {title}")
        return True

    r["status"] = "none"
    # 只有「所有通道都查过、都没找到公开副本」才记 not_found；
    # 有通道报了网络/HTTP 错误时，那才是真实原因。
    r["access_note"] = "; ".join(notes) or "无可用的公开全文通道"
    r["failure_reason"] = _pick([_classify(n) for n in notes]) if notes else "unknown"
    print(f"[{i}/{total}] --   {title}  ({r['failure_reason']})")
    return False


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

    # 字段取所有行的并集：只看 rows[0] 会把后面行才有的列悄悄丢掉
    fields = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    fields += [f for f in STATUS_FIELDS if f not in fields]

    tier_map = {"core": "核心", "periphery": "外围", "all": None}
    want_tier = tier_map[a.tier]
    todo = [r for r in rows
            if (want_tier is None or r.get("tier") == want_tier)
            and not has_full_text(r, a.pdfdir)]
    if a.limit:
        todo = todo[:a.limit]
    log(f"待下载 {len(todo)} 篇（tier={a.tier}）")

    adapter = get_adapter(a.browser)
    log(f"浏览器适配器: {adapter.name}\n")

    ok = 0
    try:
        for i, r in enumerate(todo, 1):
            try:
                ok += _one(r, a, adapter, i, len(todo))
            except Exception as e:
                # 单篇异常不中断整批；原因照实记，别让最终结果写成「无 OA」
                r["status"] = "none"
                r["access_note"] = f"异常: {str(e)[:120]}"
                r["failure_reason"] = "unknown"
                log(f"[{i}/{len(todo)}] !!   {(r.get('title') or '')[:50]}  "
                    f"{str(e)[:80]}", err=True)
            # 逐篇原子回写：中断时已完成的状态不会丢
            write_tsv(a.infile, rows, fields)
            time.sleep(a.delay)
    finally:
        adapter.close()

    log(f"\n下载 {ok}/{len(todo)}")
    log(f"→ 状态已回写 {a.infile}")
    log("\n下一步: python scripts/report.py --in out/scored.tsv --out README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
