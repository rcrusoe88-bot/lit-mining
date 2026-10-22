#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lit-Mining · Codex / ChatGPT 适配入口。

流程契约见 PROTOCOL.md。本文件只讲在这些 agent 环境下怎么跑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, log  # noqa: E402

CONFIG_HINT = "templates/search_config.yaml.example"


def main():
    print("Lit-Mining · Agent 适配说明")
    print()
    print("流程契约: PROTOCOL.md   人类入口: README.md")
    print()
    print("执行顺序:")
    print("  # 0. 生成配置（你来做，参考 %s）" % CONFIG_HINT)
    print("  #    写到工作目录的 search_config.yaml")
    print("  python scripts/probe.py    --config search_config.yaml")
    print("  #    ↑ 把输出给用户看，等确认")
    print("  python scripts/harvest.py  --config search_config.yaml --out raw/")
    print("  python scripts/triage.py   --config search_config.yaml --raw raw/ --out out/scored.tsv")
    print("  python scripts/download.py --in out/scored.tsv --pdfdir pdfs/ --tier core")
    print("  python scripts/report.py   --in out/scored.tsv --pdfdir pdfs/ --out README.md")
    print()
    print("可选: Google Scholar 补充源 + 自存档救援")
    print("  python scripts/scholar.py --config search_config.yaml --out raw/")
    print("  python scripts/scholar.py --rescue --in out/scored.tsv --pdfdir pdfs/ --tier core")
    print()
    print("全程在一个工作目录内完成:")
    print("  <工作目录>/")
    print("  ├── search_config.yaml")
    print("  ├── raw/          pubmed.tsv / epmc.tsv / openalex.tsv / scholar.tsv")
    print("  ├── out/          scored.tsv / pending.md")
    print("  ├── pdfs/         全文")
    print("  └── README.md     交付报告")
    print()
    print("自检: python scripts/selfcheck.py")
    print("网络预检: python scripts/common.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
