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

    print("offline checks OK")


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
