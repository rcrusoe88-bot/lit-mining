#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共用工具：配置加载、HTTP、日志、连通性预检。

只用标准库，保证任何 agent / 任何环境都能跑。
"""
import csv
import io
import json
import os
import re
import tempfile
import socket
import sys
import time
import urllib.parse
import urllib.request

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OPENALEX = "https://api.openalex.org"
DATACITE = "https://api.datacite.org/dois"
PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com"

# 采集中途的标记：harvest 开工创建、全部指定源成功后删除；triage 见到就拒绝。
INCOMPLETE = ".incomplete"
# 已知的采集落盘文件。新采集要求空目录，任一存在就说明这目录已被用过。
SOURCE_TSVS = ("pubmed.tsv", "epmc.tsv", "openalex.tsv", "datacite.tsv", "scholar.tsv")


def log(msg, err=False):
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def force_utf8_stdout():
    """让 Windows GBK 控制台也能打印 ✓ ⚠ → 这类字符。

    不做这件事的话，Windows 上 probe.py 会在最后一行
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'`
    直接崩掉 —— 而那行恰恰是给用户的诊断信息，崩了等于没输出。

    只在编码不是 UTF-8 时才换包装器，UTF-8 终端保持原样。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc and "utf" not in enc:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# 导入即生效：所有脚本都 from common import ...，一次搞定，不用逐个改入口
force_utf8_stdout()


def load_config(path):
    """读 YAML。优先 PyYAML，没有就用极简解析器。

    不自造完整 YAML 解析器 —— 复杂配置要求装 PyYAML。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        raise ImportError(
            "需要 PyYAML: pip install pyyaml\n"
            "（配置是多层嵌套结构，标准库无法可靠解析）"
        )


class _FixRedirect(urllib.request.HTTPRedirectHandler):
    """处理 Europe PMC 的 302 —— 目标主机不可达。

    EPMC 在负载高时会把请求 302 到 rest-api-live.europepmc.org，
    而那个主机名直连会失败（连接被掐）。
    改写成原来的 www + https 就稳定了。

    注意 Location 的路径是 `/europepmc/webservices/rest/?query=...` ——
    结尾的 `rest/` 后面直接跟 `?`，少了 `search` 这一段，
    所以替换时要把 `search` 补回去，否则会 404。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if "rest-api-live.europepmc.org" in newurl:
            # Location: http://rest-api-live.europepmc.org/europepmc/webservices/rest/?query=...
            #          ^^^^^^ 这个主机名直连不可达；而且路径结尾是 rest/? —— 少了 search
            # 所以换主机 + 换协议 + 补回 search
            newurl = newurl.replace(
                "http://rest-api-live.europepmc.org/europepmc/webservices/rest/?",
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search?")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 带重定向修复的 opener，全局复用
_OPENER = urllib.request.build_opener(_FixRedirect)


def http_json(url, tries=5, timeout=90, delay=1.0):
    """带重试的 JSON 请求。失败返回 {}。

    注意：返回 {} 无法区分「请求失败」和「响应里本来就没数据」。
    需要区分的调用点（会整批丢数据的）用 http_json_checked()。

    退避从 1 秒起、封顶 10 秒 —— 起步 2 秒会让一个失败的源
    白等 40 秒，采集整体拖垮。实测各端点单次成功率 85–95%，
    4 次重试足够覆盖抖动。
    """
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            if k == tries - 1:
                log(f"  !! {url[:80]} :: {str(e)[:80]}", err=True)
                return {}
            time.sleep(delay)
            delay = min(delay * 1.7, 10)
    return {}


def http_json_checked(url, tries=6, timeout=90, delay=2.0):
    """同 http_json，但失败抛异常。

    用于「一次请求代表一批数据」的调用点：网络抖动时如果静默返回空，
    整批数据就无声消失了（实测 esummary 一次 100 条，抖动后拿到 0 条，
    而重试一次就全好）。这类地方必须能感知失败。

    实测 NCBI E-utilities 偶发 SSL EOF / RemoteDisconnected，
    概率不低但重试即可恢复 —— 所以重试次数要给够。
    """
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            if k < tries - 1:
                time.sleep(delay)
                delay = min(delay * 1.7, 20)
    raise RuntimeError(f"请求失败（重试 {tries} 次）: {url[:80]} :: {str(last)[:80]}")


def http_bytes(url, tries=3, timeout=120):
    """带重试的二进制请求。失败返回 (None, 状态)。

    timeout 放宽到 180s：实测 PMC S3 单连接在国内直连只有 36KB/s，
    1.9MB 的 PDF 要 52 秒，60s 会把正常文件误判成超时。
    """
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None, e.code      # 这两种重试无意义
            if k == tries - 1:
                return None, e.code
            time.sleep(3 * (k + 1))
        except Exception:
            if k == tries - 1:
                return None, 0
            time.sleep(3 * (k + 1))
    return None, 0


# ---------- 连通性预检 ----------

# (名称, URL, 必需)
PROBES = [
    ("PubMed E-utilities", f"{EUTILS}/einfo.fcgi", True),
    ("Europe PMC", f"{EPMC}/search?query=probe&format=json&pageSize=1", True),
    ("OpenAlex", f"{OPENALEX}/works?per-page=1", False),
    # S3 用 HEAD 探一个已知文件：list API 偶尔 RemoteDisconnected，
    # 而 HEAD 只回 headers，稳得多也快得多
    ("PMC OA S3", f"{PMC_S3}/PMC13598902.1/PMC13598902.1.pdf", False),
]


def preflight(verbose=True):
    """开工前探一次网络。返回 (ok, results)。

    存在的理由：网络层故障（DNS 劫持、TLS 握手被掐、代理失效）会让
    harvest 跑完才发现是空的，错误信息指向「没命中」而不是「网络不通」，
    排查成本极高。实测遇到过 fake-IP 劫持导致所有端点 SSL 握手超时。
    """
    results = []
    ok = True
    for name, url, required in PROBES:
        t0 = time.time()
        status = "FAIL"
        # S3 偶发 RemoteDisconnected（实测 1/3 概率），必须重试；
        # 必需端点也重试一次：网络抖动一次不该让整个 harvest 中止。
        # S3 的超时收紧到 12s —— 可选端点不值得让整个预检卡 20 秒
        attempts = 2
        tmo = 12 if name == "PMC OA S3" else 25
        for _ in range(attempts):
            try:
                # S3 探针用 HEAD：只回 headers，不拉 1.9MB 的 body
                method = "HEAD" if name == "PMC OA S3" else "GET"
                req = urllib.request.Request(url, headers=UA, method=method)
                with urllib.request.urlopen(req, timeout=tmo) as r:
                    r.read(1)
                status = "OK"
                break
            except Exception as e:
                status = f"FAIL {type(e).__name__}"
        dt = time.time() - t0
        results.append((name, status, dt, required))
        if required and status != "OK":
            ok = False

    if verbose:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "(未设置)"
        log(f"代理: {proxy}")
        log("连通性预检:")
        for name, status, dt, req_ in results:
            # 纯 ASCII 标记：Windows GBK 控制台打印 U+2713 会 UnicodeEncodeError
            mark = "OK " if status == "OK" else "FAIL"
            tag = "" if req_ else "  (可选)"
            log(f"  [{mark}] {name:22s} {status:22s} {dt:5.1f}s{tag}")
        if not ok:
            log("\n必需端点不可达。先解决网络再跑，否则 harvest 会静默返回空。", err=True)
            log("  常见原因：VPN/代理未启动或规则把它分流到了失效的策略组；"
                "DNS 被劫持成 fake-IP 但 TUN 转发断了。", err=True)
            log("  排查：curl -sI <端点URL> 看是否 5 秒超时或 SSL 报错。", err=True)
    return ok, results


def norm_doi(doi):
    """统一 DOI 格式：小写、去前缀。"""
    if not doi:
        return ""
    d = doi.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d.strip()


def norm_title(title):
    """标题归一化，用于去重。"""
    import re
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"<[^>]+>", "", t)              # 去 HTML 标签
    t = re.sub(r"[^\w一-鿿]+", "", t)  # 只留字母数字中文
    return t[:120]


def safe_name(s, maxlen=120):
    """可用于文件名的字符串。"""
    import re
    return re.sub(r"[^\w\-.]", "_", s or "")[:maxlen]


def read_tsv(path):
    import csv
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def atomic_write(path, data, mode="wb"):
    """同目录临时文件写完再替换。中途失败时保留旧文件。

    用于 TSV 和 PDF/XML：半截文件一旦被当成缓存，比没下载更糟。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.basename(path)[-16:])
    try:
        with os.fdopen(fd, mode) as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_tsv(path, rows, fields=None, encoding="utf-8-sig"):
    """原子写 TSV。encoding 用 utf-8-sig 让 Excel 正确识别中文。

    空 rows 也必须替换旧文件 —— 否则上次运行的数据会被下一次当成结果。
    未指定 fields 时汇总所有行的键：后面行新增的字段（如 failure_reason）
    不能被 extrasaction="ignore" 悄悄丢掉。
    """
    if fields is None:
        fields = []
        seen = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
    buf = io.StringIO(newline="")
    if fields:
        w = csv.DictWriter(buf, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: str(r.get(k, "") if r.get(k) is not None else "")
                        .replace("\t", " ").replace("\n", " ") for k in fields})
    atomic_write(path, buf.getvalue().encode(encoding))


# ---------- 全文最低校验 ----------

_PDF_TAIL = 2048


def file_ok(path, kind=None):
    """判断本地文件是不是「像样的正文」。

    只排除 HTML 伪装、明显截断、坏 XML —— 不证明 PDF 结构可解析，
    也不证明文件对应该篇论文。真正需要解析全文时另说。
    """
    if not path or not os.path.isfile(path):
        return False
    kind = kind or os.path.splitext(path)[1].lower().lstrip(".")
    try:
        if kind == "pdf":
            with open(path, "rb") as f:
                if f.read(5) != b"%PDF-":
                    return False
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size < 1024:
                    return False
                f.seek(max(0, size - _PDF_TAIL))
                return b"%%EOF" in f.read()
        if kind == "xml":
            from defusedxml import ElementTree as ET
            root = ET.parse(path).getroot()
            if root.tag.split("}")[-1] != "article":
                return False
            body = next((e for e in root.iter() if e.tag.split("}")[-1] == "body"), None)
            return body is not None and bool("".join(body.itertext()).strip())
    except Exception:
        return False
    return False


def has_full_text(row, base_dir="."):
    """记录本身是否真的拿到了正文（状态 + 文件双重条件）。

    si_only 不算正文；路径存在但文件坏了也不算。

    相对路径有歧义：下载器存的是 `pdfs/x.pdf`（相对当时的工作目录），
    而报告拿到的 --pdfdir 可能是 `pdfs` 也可能是绝对路径。所以按
    「原样 → base_dir → base_dir 的父目录」依次试，命中即算。
    最后一项是为了避免 `pdfs/pdfs/x.pdf` 这种双重拼接。
    """
    if (row.get("status") or "") != "full_text":
        return False
    p = (row.get("pdf_path") or "").strip()
    if not p:
        return False
    if os.path.isabs(p):
        return file_ok(p)
    bases = [".", base_dir]
    parent = os.path.dirname(os.path.abspath(base_dir))
    if parent not in bases:
        bases.append(parent)
    return any(file_ok(os.path.join(b, p)) for b in bases)


# ---------- Europe PMC 查询转换 ----------

# 只认模板实际用到的 PubMed 字段。其余字段显式拒绝，别猜。
_EPMC_FIELDS = {"title/abstract": "TITLE_ABS", "title": "TITLE", "abstract": "ABSTRACT"}
# 括号单独成 token，即使紧贴词项（"(mRNA" → "(" "mRNA"）
# 方括号内允许空格（"[MeSH Terms]"），所以字段标签单独匹配，不能靠 \S+ 贪吃
_TOKEN = re.compile(r'"[^"]*"(?:\s*\[[^\]]*\])?|[()]|[^\s()\[\]]+(?:\s*\[[^\]]*\])?')
_EPMC_SAFE_BARE = re.compile(r"^[A-Za-z0-9_\-*]+$")


def _epmc_term(tok):
    """一个词项（可带 [字段]）→ EPMC 原生写法。"""
    m = re.match(r"^(.*?)(\[[^\]]*\])$", tok)
    term, field = (m.group(1), m.group(2)[1:-1].strip().lower()) if m else (tok, None)
    if field is not None:
        if field not in _EPMC_FIELDS:
            raise ValueError(f"Europe PMC 不支持字段 [{field}]")
        if not (term.startswith('"') or _EPMC_SAFE_BARE.match(term)):
            raise ValueError(f"无法安全转换的词项: {tok}")
        return f"{_EPMC_FIELDS[field]}:{term}"
    if term.upper() in ("AND", "OR", "NOT") or term in ("(", ")"):
        return term
    if term.startswith('"') or _EPMC_SAFE_BARE.match(term):
        return term
    raise ValueError(f"无法安全转换的词项: {tok}")


def epmc_query(cfg, dim_lib):
    """把 PubMed 检索式有限地翻成 Europe PMC 原生语法。

    只支持词/短语 + [Title|Abstract] + 布尔与括号。不支持的写法直接报错，
    不删标签硬搜 —— 静默改写查询比报错危险得多。
    """
    parts = [dim_lib, (cfg.get("load") or {}).get("lib", ""),
             (cfg.get("platform") or {}).get("lib", "")]
    out = []
    for p in (x for x in parts if x):
        try:
            toks = [_epmc_term(t) for t in _TOKEN.findall(p)]
        except ValueError as e:
            raise ValueError(f"{e}（原文: {p[:120]}）") from None
        if (toks[0].upper() in ("AND", "OR", "NOT") or toks[-1].upper() in ("AND", "OR", "NOT")
                or not toks):
            raise ValueError(f"检索式以布尔运算符开头或结尾（原文: {p[:120]}）")
        out.append(" ".join(toks))
    return " AND ".join(f"({p})" for p in out)


def build_query(cfg, dim_lib):
    """拼装完整检索式：(载荷) AND (平台) AND (维度)。"""
    load = (cfg.get("load") or {}).get("lib", "")
    plat = (cfg.get("platform") or {}).get("lib", "")
    parts = [p for p in (dim_lib, load, plat) if p]
    return " AND ".join(f"({p})" if not p.startswith("(") else p for p in parts)


def check_deps():
    """检查必需依赖，返回 (ok, 缺失列表)。"""
    missing = []
    try:
        import yaml  # noqa
    except ImportError:
        missing.append("pyyaml")
    try:
        import playwright  # noqa
    except ImportError:
        missing.append("playwright")
    return (not missing), missing


if __name__ == "__main__":
    ok, missing = check_deps()
    print("依赖检查:", "OK" if ok else f"缺 {missing}")
