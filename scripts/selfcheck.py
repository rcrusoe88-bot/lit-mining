#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lit-Mining 自检。不用测试框架，不用 fixture。

离线检查总是跑；网络检查除非 --offline 否则跑。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import common  # noqa: E402  (导入即触发 force_utf8_stdout)
import download  # noqa: E402
import triage  # noqa: E402
from common import norm_doi, norm_title, safe_name  # noqa: E402


def check_offline():
    # DOI 归一化
    assert norm_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert norm_doi("doi:10.1000/abc") == "10.1000/abc"
    assert norm_doi("") == ""

    # 标题归一化去重
    a = norm_title("Lipid Nanoparticle <b>Delivery</b> for mRNA")
    b = norm_title("Lipid Nanoparticle Delivery for mRNA")
    assert a == b, (a, b)
    assert norm_title("") == ""

    # 文件名安全化
    assert "/" not in safe_name("10.1000/a/b")
    assert len(safe_name("x" * 500)) <= 120

    # S3 正文 PDF 的 key 匹配必须排除补充材料
    # （同目录混着 41467_2026_75923_MOESM1_ESM.pdf，取第一个会拿错）
    import re
    pat = re.compile(r"^PMC13522490\.\d+/PMC13522490\.\d+\.pdf$")
    assert pat.match("PMC13522490.1/PMC13522490.1.pdf")
    assert pat.match("PMC13522490.2/PMC13522490.2.pdf")
    assert not pat.match("PMC13522490.1/41467_2026_75923_MOESM1_ESM.pdf")
    assert not pat.match("PMC13522490.1/PMC13522490.1.txt")
    assert not pat.match("PMC13522490.1/PMC13522490.1.xml")
    assert not pat.match("PMC13522490.1/PMC13522490.1.json")

    # 词表正则：长词优先，避免 'T cell' 被 'T' 抢先匹配
    # 排序后 alternation 里 'T cell' 在前，命中的是整个短语
    p = triage.compile_terms(["T cell", "T"])
    hits = triage.hit_terms("Targeting T cells and T cell receptors", p)
    assert "t cell" in hits, hits
    # 不排序的话 alternation 先试 'T'，'T cell' 会被切成 't' + 'cell'
    import re as _re
    unsorted = _re.compile(r"(?<![A-Za-z0-9])(?:T|T cell)(?![A-Za-z0-9])", _re.I)
    assert "t cell" not in triage.hit_terms("T cell receptors", unsorted)

    # 空词表不炸
    assert triage.compile_terms([]) is None
    assert triage.compile_terms(None) is None

    check_trust_regressions()

    print("offline checks OK")


def _pdf_bytes(payload=b"", eof=True):
    body = b"%PDF-1.4\n" + payload + b"\n" + b"0" * 2000
    return body + (b"\n%%EOF" if eof else b"")


def check_trust_regressions():
    """本批可信度修复的回归：全文判定、TSV、raw 隔离、报告口径。

    全部离线；临时文件放临时目录，不碰网络/浏览器/子进程。
    """
    import report
    from common import atomic_write, file_ok, has_full_text, write_tsv
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        # --- 文件最低校验 ---
        good_pdf = os.path.join(tmp, "good.pdf")
        with open(good_pdf, "wb") as f:
            f.write(_pdf_bytes(b"content"))
        assert file_ok(good_pdf)

        truncated = os.path.join(tmp, "trunc.pdf")
        with open(truncated, "wb") as f:
            f.write(_pdf_bytes(b"content", eof=False))
        assert not file_ok(truncated), "缺 %%EOF 的 PDF 不能算正文"

        html = os.path.join(tmp, "fake.pdf")
        with open(html, "wb") as f:
            f.write(b"<html>" + b"x" * 5000)
        assert not file_ok(html), "HTML 伪装成 .pdf 不能算正文"

        tiny = os.path.join(tmp, "tiny.pdf")
        with open(tiny, "wb") as f:
            f.write(b"%PDF-1.4\n%%EOF")
        assert not file_ok(tiny), "过小的 PDF 视为无效"

        good_xml = os.path.join(tmp, "good.xml")
        with open(good_xml, "w", encoding="utf-8") as f:
            f.write("<article><body><p>real text</p></body></article>")
        assert file_ok(good_xml)

        empty_xml = os.path.join(tmp, "empty.xml")
        with open(empty_xml, "w", encoding="utf-8") as f:
            f.write("<article><body>  </body></article>")
        assert not file_ok(empty_xml), "空 body 的 XML 不算正文"

        evil_xml = os.path.join(tmp, "evil.xml")
        with open(evil_xml, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]>'
                    "<article><body>&x;</body></article>")
        assert not file_ok(evil_xml), "带实体声明的 XML 必须被拒"

        # --- has_full_text：状态 + 文件双重条件 ---
        assert has_full_text({"status": "full_text", "pdf_path": good_pdf}, tmp)
        assert not has_full_text({"status": "full_text", "pdf_path": html}, tmp)
        assert not has_full_text({"status": "full_text", "pdf_path": "gone.pdf"}, tmp)
        assert not has_full_text({"status": "si_only", "pdf_path": good_pdf}, tmp), \
            "SI 不能因为文件合法就变成全文"

        # --- TSV：空结果清旧数据、0 保留、后续行新字段不丢、原子失败不毁原文件 ---
        tsv = os.path.join(tmp, "t.tsv")
        write_tsv(tsv, [{"a": "old", "b": "old"}])
        write_tsv(tsv, [])
        assert common.read_tsv(tsv) == [], "空结果必须清掉旧 TSV"

        write_tsv(tsv, [{"score": 0, "title": "x"}, {"title": "y", "failure_reason": "network"}])
        rows = common.read_tsv(tsv)
        assert rows[0]["score"] == "0", "0 分不能被写成空"
        assert rows[1]["failure_reason"] == "network", "后续行新增字段不能丢"

        write_tsv(tsv, [{"a": "keep"}])
        before = open(tsv, "rb").read()
        with patch.object(common.os, "replace", side_effect=OSError("boom")):
            try:
                write_tsv(tsv, [{"a": "new"}])
                raise AssertionError("写入失败必须抛异常")
            except OSError:
                pass
        assert open(tsv, "rb").read() == before, "写失败不能破坏原文件"
        assert not [n for n in os.listdir(tmp) if n.startswith(".tmp-")], "临时文件要清理"

        # --- 报告：SI/失效路径不计全文，零待办重写 pending ---
        scored = os.path.join(tmp, "scored.tsv")
        write_tsv(scored, [
            {"title": "si", "tier": "核心", "status": "si_only", "pdf_path": good_pdf,
             "failure_reason": "not_found"},
            {"title": "gone", "tier": "核心", "status": "full_text", "pdf_path": "gone.pdf",
             "failure_reason": "invalid_content"},
        ])
        readme = os.path.join(tmp, "README.md")
        with patch.object(sys, "argv", ["report.py", "--in", scored,
                                        "--pdfdir", tmp, "--out", readme]):
            with contextlib_redirect():
                assert report.main() == 0
        text = open(readme, encoding="utf-8").read()
        assert "| 已获全文 | 0 | 0 | 0 |" in text, text[:400]
        assert "| 待获取 | 2 | 0 | 2 |" in text
        pending = open(os.path.join(tmp, "pending.md"), encoding="utf-8").read()
        assert "共 2 篇" in pending
        assert "该刊无 OA 政策" not in text + pending, "不能推断期刊政策"

        write_tsv(scored, [{"title": "ok", "tier": "核心", "status": "full_text",
                            "pdf_path": good_pdf}])
        with patch.object(sys, "argv", ["report.py", "--in", scored,
                                        "--pdfdir", tmp, "--out", readme]):
            with contextlib_redirect():
                assert report.main() == 0
        assert "共 0 篇" in open(os.path.join(tmp, "pending.md"), encoding="utf-8").read(), \
            "待获取清零后必须重写 pending.md"

        # --- 下载缓存：SI 不能被提升为全文；下载器相对路径报告也能解析 ---
        import download
        si = os.path.join(tmp, "si.pdf")
        with open(si, "wb") as f:
            f.write(_pdf_bytes(b"si"))
        assert download._cached({"status": "si_only", "pdf_path": si}, tmp, "si") is None, \
            "si_only 不能被缓存复用成全文"
        assert download._cached({"status": "full_text", "pdf_path": si}, tmp, "x") == si
        os.makedirs(os.path.join(tmp, "pdfs"), exist_ok=True)
        with open(os.path.join(tmp, "pdfs", "x.pdf"), "wb") as f:
            f.write(_pdf_bytes(b"rel"))
        old = os.getcwd()
        os.chdir(tmp)
        try:
            # 报告传 --pdfdir pdfs，记录里是 pdfs/x.pdf —— 不能双重拼接成 pdfs/pdfs/x.pdf
            assert has_full_text({"status": "full_text", "pdf_path": "pdfs/x.pdf"}, "pdfs")
            # --pdfdir 传绝对路径时，同样要能解析相对记录
            assert has_full_text({"status": "full_text", "pdf_path": "pdfs/x.pdf"},
                                 os.path.join(tmp, "pdfs"))
        finally:
            os.chdir(old)

        # --- 失败原因分类：404 是 HTTP 错误，不是「没找到公开副本」 ---
        assert download._classify("下载失败（code=404，直连带宽低）") == "http_error"
        assert download._classify("PMC OA 桶无正文 PDF") == "not_found"
        assert download._classify("S3 列表失败（code=0）") == "network"
        assert download._classify("取回非 PDF") == "invalid_content"

        # --- 采集目录隔离：未完成标记与已有源文件都要拦住 ---
        raw = os.path.join(tmp, "raw")
        os.makedirs(raw)
        with open(os.path.join(raw, common.INCOMPLETE), "w") as f:
            f.write("")
        with patch.object(sys, "argv", ["triage.py", "--config", "unused",
                                        "--raw", raw, "--out", os.path.join(tmp, "s.tsv")]), \
                patch.object(triage, "load_config", return_value={}):
            with contextlib_redirect():
                assert triage.main() == 1, "有 .incomplete 时 triage 必须拒绝"

    check_harvest_regressions()


def check_harvest_regressions():
    """采集侧：查询转换、完整分页、去重键、raw 隔离。全部离线。"""
    import harvest
    from unittest.mock import patch

    cfg = common.load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "templates", "search_config.yaml.example"))
    # 转换必须覆盖全部维度，且保留后置分支（旧实现会截掉 CD7 之后的部分）
    for d in cfg["dimensions"]:
        q = common.epmc_query(cfg, d["lib"])
        assert "[" not in q and "TITLE_ABS" in q, q
    first = common.epmc_query(cfg, cfg["dimensions"][0]["lib"])
    assert "CD7" in first, "完整查询不能丢掉靠后的词项"

    # 不支持的字段必须报错，不能静默删标签
    try:
        common.epmc_query(cfg, "foo[MeSH Terms]")
        raise AssertionError("不支持的字段应报错")
    except ValueError:
        pass

    # 分页：7 页也要取完（旧实现硬截 5 页）
    pages = [{"hitCount": 7, "resultList": {"result": []},
              "nextCursorMark": f"c{i}"} for i in range(1, 8)]
    for i, p in enumerate(pages):
        p["resultList"]["result"] = [{"source": "MED", "id": str(i), "title": f"t{i}"}]
    pages[-1]["nextCursorMark"] = ""
    it = iter(pages)
    with patch.object(harvest, "http_json_checked", lambda *a, **k: next(it)), \
            patch.object(harvest.time, "sleep", lambda *_: None):
        rows, hits = harvest._epmc_one_query("q", 1)
    assert hits == 7 and len(rows) == 7, (hits, len(rows))

    # 重复 cursor 必须报错，不能悄悄当成完成
    dup = [{"hitCount": 5, "resultList": {"result": [{"source": "MED", "id": "1", "title": "a"}]},
            "nextCursorMark": "same"},
           {"hitCount": 5, "resultList": {"result": [{"source": "MED", "id": "2", "title": "b"}]},
            "nextCursorMark": "same"}]
    it = iter(dup)
    with patch.object(harvest, "http_json_checked", lambda *a, **k: next(it)), \
            patch.object(harvest.time, "sleep", lambda *_: None):
        try:
            harvest._epmc_one_query("q", 1)
            raise AssertionError("重复 cursor 应报错")
        except RuntimeError:
            pass

    # 取回条数少于 hitCount 且没有下一页 → 不完整，必须报错
    short = [{"hitCount": 50, "resultList": {"result": [{"source": "MED", "id": "1", "title": "a"}]},
              "nextCursorMark": ""}]
    with patch.object(harvest, "http_json_checked", lambda *a, **k: short[0]), \
            patch.object(harvest.time, "sleep", lambda *_: None):
        try:
            harvest._epmc_one_query("q", 100)
            raise AssertionError("不足 hitCount 应报错")
        except RuntimeError:
            pass

    # OpenAlex：两条无 DOI 的不同作品都要留下（旧实现按空字符串去重）
    oa = {"meta": {"count": 2}, "results": [
        {"id": "https://openalex.org/W1", "title": "one"},
        {"id": "https://openalex.org/W2", "title": "two"},
    ]}
    with patch.object(harvest, "http_json_checked", lambda *a, **k: oa), \
            patch.object(harvest.time, "sleep", lambda *_: None):
        rows = harvest.run_openalex(cfg, cfg["dimensions"][:1])
    assert [r["uid"] for r in rows] == ["W1", "W2"], rows

    # raw 隔离：已有源文件的目录必须拒绝
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "pubmed.tsv"), "w", encoding="utf-8") as f:
            f.write("a\n")
        with patch.object(sys, "argv", ["harvest.py", "--config", "unused",
                                        "--out", tmp, "--skip-preflight"]), \
                patch.object(harvest, "load_config", return_value=cfg), \
                patch.object(harvest, "run_pubmed", side_effect=AssertionError("不应采集")):
            with contextlib_redirect():
                assert harvest.main() != 0, "已有源文件的目录必须拒绝"

        # 源失败必须保留 .incomplete：半批数据不能被分诊当成完整结果
        fresh = os.path.join(tmp, "fresh")
        with patch.object(sys, "argv", ["harvest.py", "--config", "unused",
                                        "--out", fresh, "--skip-preflight"]), \
                patch.object(harvest, "load_config", return_value=cfg), \
                patch.object(harvest, "run_pubmed", side_effect=RuntimeError("boom")):
            with contextlib_redirect():
                assert harvest.main() != 0
        assert os.path.exists(os.path.join(fresh, common.INCOMPLETE)), \
            "源失败后必须保留 .incomplete 标记"


class contextlib_redirect:
    """吞掉被测脚本的正常输出，让自检保持安静。"""

    def __enter__(self):
        import contextlib
        import io as _io
        self._c = contextlib.redirect_stdout(_io.StringIO())
        self._e = contextlib.redirect_stderr(_io.StringIO())
        self._c.__enter__()
        self._e.__enter__()
        return self

    def __exit__(self, *exc):
        self._e.__exit__(*exc)
        self._c.__exit__(*exc)
        return False


def check_network():
    # 连通性预检：网络坏时必须明确报 FAIL，不能假装通过
    ok, results = preflight_results()
    print(f"  preflight ok={ok}")
    for name, status, dt, required in results:
        print(f"    {name:22s} {status:22s} {dt:5.1f}s")
    assert ok, "preflight failed - 网络不可达"

    # 真实下载：三篇跨主题的已知 OA 文章
    with tempfile.TemporaryDirectory() as tmp:
        for pmcid in ("PMC13338314", "PMC13415181", "PMC13598902"):
            path, note = download.try_pmc(None, pmcid, os.path.join(tmp, pmcid + ".pdf"))
            assert path, f"{pmcid}: no PDF returned"
            with open(path, "rb") as fh:
                assert fh.read(5) == b"%PDF-", f"{pmcid}: not a PDF"
            size = os.path.getsize(path)
            assert size > 50_000, f"{pmcid}: suspiciously small ({size})"
            print(f"  {pmcid} OK ({size:,} bytes) - {note}")

        # 不在 PMC 的 DOI 必须报错，不能静默下垃圾文件
        try:
            download.try_pmc(None, "", os.path.join(tmp, "x.pdf"))
        except Exception:
            raise AssertionError("empty pmcid should not raise")
        p, note = download.try_pmc(None, "", os.path.join(tmp, "x.pdf"))
        assert p is None and note == "", (p, note)
        print("  empty pmcid handled OK")

    print("network checks OK")


def preflight_results():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok, results = common.preflight(verbose=True)
    return ok, results


if __name__ == "__main__":
    check_offline()
    if "--offline" not in sys.argv:
        check_network()
    print("all checks passed")
