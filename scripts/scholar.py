#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Google Scholar 通道（可选增强源，需 browser-use + 日常 Chrome）。

为什么单独一个模块:
    Scholar 没有官方 API，反自动化极严。CDP 干净实例会被拦，
    browser-use 挂日常 Chrome（带 Google 会话）才稳。

实测要点（2026-10-22，全部踩过）:
    1. browser-use 的 js()/evaluate() helper 返回空 → 必须用
       cdp("Runtime.evaluate", ..., returnByValue=True)
    2. 页面标题/正文带 lone surrogate（emoji 等）会炸 stdout →
       片段内把结果写文件，结尾 os._exit(0) 绕开 harness 回显
    3. 在 scholar.google.com 页面里跨域 fetch 外站 PDF 被 CORS 拦 →
       下载时先 goto PDF 所在域根页，再同源 fetch（download_pdf 里做了）
    4. 搜索间隔 ≥8s。Scholar 对连续请求极敏感，宁可慢，别触发
       "Sorry / automated queries"（触发后 24h 内基本废掉）

用法:
    采集:   python scripts/scholar.py --config search_config.yaml --out raw/
    补救:   python scripts/scholar.py --rescue --in out/scored.tsv --pdfdir pdfs/ [--tier core]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_bytes, load_config, log, norm_title, read_tsv, safe_name, write_tsv

SCHOLAR = "https://scholar.google.com/scholar"

# 搜索结果页：标题/链接/meta/摘要/cluster/PDF 直链
SEARCH_JS = """JSON.stringify([...document.querySelectorAll('.gs_ri')].map(e => {
    const cl = [...e.querySelectorAll('.gs_fl a')].find(a => a.href.includes('cluster='));
    const ggs = e.querySelector('.gs_ggs a');
    return {
        title: (e.querySelector('.gs_rt')||{}).innerText || '',
        link: ((e.querySelector('.gs_rt a')||{}).href) || '',
        meta: (e.querySelector('.gs_a')||{}).innerText || '',
        snippet: ((e.querySelector('.gs_rs')||{}).innerText || '').slice(0, 300),
        cluster: cl ? cl.href : '',
        pdf: (ggs && ggs.href && !ggs.href.startsWith('javascript')) ? ggs.href : ''
    };
}))"""

# cluster 页（「所有版本」）：全部版本的 PDF 直链
CLUSTER_JS = """JSON.stringify([...document.querySelectorAll('.gs_ggs a')]
    .map(a => a.href)
    .filter(h => h && !h.startsWith('javascript')))"""

# 页内 fetch PDF 头 5 字节（只探不动，下载走独立通道）
PROBE_JS = """(async () => {
    try {
        const r = await fetch(URL_ARG, {credentials: 'include'});
        const buf = await r.arrayBuffer();
        const head = new Uint8Array(buf.slice(0, 5));
        let s = '';
        for (const b of head) s += String.fromCharCode(b);
        return JSON.stringify({ok: r.status === 200 && s === '%PDF-', size: buf.byteLength});
    } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 60)}); }
})()""".replace("URL_ARG", "__URL__")

# browser-use 内的同源 fetch → base64 文本（ASCII，可安全过文件通道）
FETCH_B64_JS = """(async () => {
    try {
        const r = await fetch(PATH_ARG, {credentials: 'include'});
        if (!r.ok) return 'ERRSTATUS' + r.status;
        const buf = await r.arrayBuffer();
        const bytes = new Uint8Array(buf);
        if (bytes.length < 1000) return 'ERRTOOSMALL';
        let bin = '';
        const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        return btoa(bin);
    } catch (e) { return 'ERRFETCH'; }
})()""".replace("PATH_ARG", "__PATH__")


def _run_snippet(code, timeout=180):
    """跑一段 browser-use 片段。片段必须自己把结果写 TMP 文件并 os._exit(0)。

    为什么这么绕（三个编码坑，全踩过）:
        - stdout 会被 lone surrogate 炸 → 片段永远不 print 数据，只写文件
        - CDP 返回值带 surrogate（Scholar 标题常见）→ 文件读写都要
          errors="surrogatepass"，用 "replace" 会损坏 JSON 转义串
        - Windows 外层进程默认 GBK → 传 errors="replace" 给 subprocess，
          并建议以 UTF-8 模式跑本模块（python -X utf8）
    """
    binname = shutil.which("browser-use") or os.path.expanduser("~/.local/bin/browser-use")
    if not os.path.exists(binname):
        raise RuntimeError("browser-use 不可用")
    try:
        subprocess.run([binname], input=code, capture_output=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        pass


def _collect(navigate_js, extract_js, wait=4):
    """导航 → 等 → 提取 → 写临时文件。返回解析后的 list/str，失败返回 None。"""
    tmp = tempfile.mktemp(suffix=".json")
    code = f"""
import time, json, os
{navigate_js}
wait_for_load()
time.sleep({wait})
r = cdp("Runtime.evaluate", expression={extract_js!r}, returnByValue=True)
with open({tmp!r}, "w", encoding="utf-8", errors="surrogatepass") as f:
    f.write(str(r.get("result", {{}}).get("value", "")))
os._exit(0)
"""
    _run_snippet(code)
    if not os.path.exists(tmp):
        return None
    try:
        with open(tmp, encoding="utf-8", errors="surrogatepass") as f:
            raw = f.read()
        return json.loads(raw) if raw else None
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def scholar_search(query, pages=1, delay=10):
    """搜索。返回结果行列表。间隔 delay 秒翻页，防反爬。

    坑: 第 1 页不能带 &start=0 —— Scholar 会把带 start=0 的请求
    跳到别的页面（实测直接返回 0 条 .gs_ri）。所以首页无 start，
    后续页才带。
    """
    rows = []
    for p in range(pages):
        params = {"q": query}
        if p > 0:
            params["start"] = p * 10
        url = f"{SCHOLAR}?{urllib.parse.urlencode(params)}"
        nav = f"{'new_tab' if p == 0 else 'goto_url'}({url!r})"
        res = _collect(nav, SEARCH_JS)
        if not res:
            break
        if isinstance(res, str) and "Sorry" in res:   # 被拦
            log("  !! Scholar 反自动化拦截，停止翻页", err=True)
            break
        rows.extend(res)
        if len(res) < 10:
            break
        if p < pages - 1:
            time.sleep(delay)
    return rows


def mine_cluster(cluster_url):
    """打开「所有版本」页，返回全部 PDF 直链。"""
    return _collect(f"goto_url({cluster_url!r})", CLUSTER_JS) or []


def _browser_fetch_pdf(url, dest):
    """用 browser-use 的日常 Chrome 下载 PDF（跨域站点的唯一可行路径）。

    原理同 PMC：先 goto PDF 所在域的根页建立同源上下文，再在页面里
    fetch 同域相对路径 —— 请求头/UA/TLS 指纹全是真浏览器的，WAF 认不出。

    Chrome 155 的远程调试不走标准 CDP HTTP 端点（/json/version 返 404），
    所以 Playwright 连不上日常 Chrome，browser-use 是唯一通道。
    """
    origin = "/".join(url.split("/")[:3]) + "/"
    path = url[len(origin):]
    tmp = tempfile.mktemp(suffix=".b64")
    js = FETCH_B64_JS.replace("__PATH__", json.dumps(path))
    code = f"""
import time, json, os, base64
goto_url({origin!r})
wait_for_load()
time.sleep(2)
r = cdp("Runtime.evaluate", expression={js!r}, returnByValue=True, awaitPromise=True)
v = r.get("result", {{}}).get("value") or ""
with open({tmp!r}, "w", encoding="ascii", errors="ignore") as f:
    f.write(v)
os._exit(0)
"""
    _run_snippet(code)
    if not os.path.exists(tmp):
        return None
    try:
        with open(tmp, encoding="ascii", errors="ignore") as f:
            raw = f.read().strip()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not raw or raw.startswith("ERR"):
        return None
    try:
        import base64
        data = base64.b64decode(raw)
    except Exception:
        return None
    if data[:5] != b"%PDF-":
        return None
    with open(dest, "wb") as f:
        f.write(data)
    return f"browser-use {len(data)//1024}KB"


def download_pdf(url, dest, adapter=None):
    """下 PDF：先 HTTP（快），失败再走浏览器同源 fetch。

    同源 fetch 的原理：先 goto 该 PDF 所在域的根页建立上下文，
    再在页面里 fetch 同域相对路径 —— 此时请求头/UA/TLS 全是
    真浏览器的，WAF 认不出来（和 PMC PDF 同一个套路）。
    """
    # 1. HTTP（大多数实验室主页/机构库够用）
    data, code = http_bytes(url, tries=2, timeout=90)
    if data and data[:5] == b"%PDF-":
        with open(dest, "wb") as f:
            f.write(data)
        return f"http {len(data)//1024}KB"

    # 2. Playwright CDP（如果 9223 那个独立 Chrome 开着）
    if adapter is None:
        from browser import get_adapter
        try:
            adapter = get_adapter()
        except Exception:
            adapter = None
    if adapter is not None and hasattr(adapter, "_goto"):
        origin = "/".join(url.split("/")[:3]) + "/"
        try:
            adapter._goto(origin)
            time.sleep(2)
            from browser import FETCH_JS
            res = adapter.pg.evaluate(FETCH_JS, url)
            if res.get("ok"):
                import base64
                data = base64.b64decode(res["b64"])
                if data[:5] == b"%PDF-":
                    with open(dest, "wb") as f:
                        f.write(data)
                    return f"cdp {len(data)//1024}KB"
        except Exception as e:
            log(f"  !! CDP 同源 fetch 失败: {str(e)[:60]}", err=True)

    # 3. browser-use 日常 Chrome（跨域站点的兜底）
    note = _browser_fetch_pdf(url, dest)
    return note


def build_queries(cfg):
    """从配置自动拼 Scholar 查询（Scholar 不吃 PubMed 语法，用自然语言词组）。

    坑（实测）: 查询词必须纯英文。用中文词（如「巯基-马来酰亚胺」）
    搜出来的是中文/日文的泛综述，跟主题无关。所以：
        1. 维度显式配 scholar_query 最好
        2. 否则从 terms 里挑第一个纯 ASCII 的词
        3. 都没有就跳过该维度（宁可少搜，不要污染）
    """
    load_terms = [t for t in ((cfg.get("load") or {}).get("terms") or []) if _is_ascii(t)]
    load = load_terms[0] if load_terms else ""
    plat = cfg.get("platform") or {}
    plat_term = _first_ascii(plat.get("lib") or "") or "nanoparticle"
    queries = {}
    for d in cfg.get("dimensions") or []:
        # 显式指定优先
        kw = d.get("scholar_query")
        if not kw:
            # 取第一个纯 ASCII 的词（不是整串拼接 —— Scholar 对长查询反而更窄）
            # terms 和 strong 都要看：配置里强信号维度常只写 strong
            kw = ""
            for t in list(d.get("terms") or []) + list(d.get("strong") or []):
                if _is_ascii(t):
                    kw = t
                    break
            if not kw:
                kw = _first_ascii(d.get("label", ""))
        if not kw:
            log(f"  {d['key']:24s} 跳过（无英文检索词，建议加 scholar_query）")
            continue
        queries[d["key"]] = f"{load} {plat_term} {kw}".strip()
    return queries


def _is_ascii(s):
    return bool(s) and all(ord(c) < 128 for c in s)


def _first_ascii(s):
    """从字符串里取第一段连续的 ASCII 词组（长度 > 2 才有检索价值）。"""
    if not s:
        return ""
    cur = []
    for ch in s:
        if ord(ch) < 128 and (ch.isalnum() or ch in "-* "):
            cur.append(ch)
        else:
            if len("".join(cur).strip()) > 2:
                return "".join(cur).strip()
            cur = []
    return "".join(cur).strip()


def _parse_meta(meta):
    """拆 Scholar 的 meta 行。

    典型格式: "MM Billingsley, N Gong… - Small, 2024 - Wiley Online Library"
    也可能是:  "I Tombacz… - Molecular Therapy, 2021 - cell.com"
    返回 (year, journal, first_author)。
    """
    if not meta:
        return "", "", ""
    parts = [p.strip() for p in meta.split(" - ")]
    year, journal, author = "", "", ""
    for p in parts:
        m = re.search(r"\b(19|20)\d{2}\b", p)
        if m:
            year = m.group(0)
            # 期刊 = 年份所在段去掉年份和逗号
            journal = (p[:m.start()] + p[m.end():]).strip(" ,")
        elif not author and "," in p and not p.startswith("…"):
            author = p.split(",")[0].strip()
    if not journal and len(parts) > 1:
        journal = parts[1]
    if not author and parts:
        author = parts[0].split(",")[0].strip()
    return year, journal, author


def run_harvest(cfg, outdir):
    """采集模式：逐维度搜索 → raw/scholar.tsv。"""
    scfg = (cfg.get("harvest") or {}).get("scholar") or {}
    pages = int(scfg.get("pages", 1))
    queries = build_queries(cfg)
    rows = []
    for key, q in queries.items():
        log(f"  {key:24s} «{q[:50]}»")
        res = scholar_search(q, pages=pages)
        for r in res:
            meta = r.get("meta") or ""
            year, journal, first_author = _parse_meta(meta)
            rows.append({
                "src": "scholar", "uid": r.get("cluster") or r.get("link") or "",
                "doi": "", "pmid": "", "pmcid": "",
                "title": (r.get("title") or "").rstrip("."),
                "journal": journal, "year": year,
                "first_author": first_author,
                "pubtype": "", "abstract": r.get("snippet") or "",
                "cluster_url": r.get("cluster") or "",
                "pdf_url": r.get("pdf") or "",
                "hits": key, "hit_count": 1,
            })
        log(f"    {len(res)} 条")
        time.sleep(10)      # 维度之间也隔 10s
    path = os.path.join(outdir, "scholar.tsv")
    write_tsv(path, rows)
    log(f"  → {path}  ({len(rows)} 行)")
    return rows


def run_rescue(infile, pdfdir, tier=None, limit=20, delay=12):
    """补救模式：给「无 OA 全文」的论文去 Scholar cluster 里找自存档 PDF。

    搜索用标题精确匹配（加引号），取第一个 cluster，逐个试 PDF。
    每篇间隔 delay 秒 —— 这不是快车道，是最后一根稻草。
    """
    rows = read_tsv(infile)
    if not rows:
        log(f"{infile} 为空", err=True)
        return 1
    want_tier = {"core": "核心", "periphery": "外围"}.get(tier)
    todo = [r for r in rows
            if r.get("tier") in ("核心", "外围")
            and (want_tier is None or r.get("tier") == want_tier)
            and (r.get("status") or "") != "full_text"
            and not (r.get("pdf_path") or "").strip()]
    todo = todo[:limit]
    log(f"待补救 {len(todo)} 篇")
    os.makedirs(pdfdir, exist_ok=True)

    ok = 0
    for i, r in enumerate(todo, 1):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        res = scholar_search(f'"{title[:120]}"', pages=1)
        cluster = next((x.get("cluster") for x in res if x.get("cluster")), "")
        if not cluster:
            print(f"[{i}/{len(todo)}] --  {title[:50]}（Scholar 无 cluster）")
            time.sleep(delay)
            continue
        # 只留真 PDF 直链，排除 scholar 自身的跳转链接
        pdfs = [u for u in mine_cluster(cluster)
                if u.startswith("http") and "scholar.google.com" not in u]
        got = None
        for u in pdfs:
            base = safe_name(r.get("doi") or r.get("pmid") or norm_title(title)[:40])
            dest = os.path.join(pdfdir, base + ".pdf")
            note = download_pdf(u, dest)
            if note:
                got = (dest, u, note)
                break
        if got:
            r["pdf_path"], r["oa_url"] = got[0], got[1]
            r["status"], r["access_note"] = "full_text", f"Scholar cluster ({got[2]})"
            ok += 1
            print(f"[{i}/{len(todo)}] OK  {title[:50]}")
        else:
            r["access_note"] = "Scholar cluster 无可用 PDF"
            print(f"[{i}/{len(todo)}] --  {title[:50]}")
        time.sleep(delay)

    if todo:
        write_tsv(infile, rows)      # 回写状态
    log(f"\n补救成功 {ok}/{len(todo)}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--out", default="raw")
    ap.add_argument("--rescue", action="store_true")
    ap.add_argument("--in", dest="infile", default="out/scored.tsv")
    ap.add_argument("--pdfdir", default="pdfs")
    ap.add_argument("--tier", default=None, choices=["core", "periphery"])
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    if a.rescue:
        tier = {"core": "核心", "periphery": "外围"}.get(a.tier)
        return run_rescue(a.infile, a.pdfdir, tier, a.limit)

    if not a.config:
        log("采集模式需要 --config", err=True)
        return 1
    cfg = load_config(a.config)
    scfg = (cfg.get("harvest") or {}).get("scholar") or {}
    if scfg.get("enabled") == "false":
        log("配置里 scholar.enabled=false，跳过", err=True)
        return 0
    os.makedirs(a.out, exist_ok=True)
    log("=== Google Scholar（browser-use + 日常 Chrome）===")
    return run_harvest(cfg, a.out) and 0


if __name__ == "__main__":
    sys.exit(main())
