#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器抽象层：把「怎么拿到网页内容」和「业务流程」解耦。

三个实现，按可用性自动降级：

    1. BrowserUseAdapter   有 browser-use 就用（能操作日常浏览器，带登录态）
    2. CdpAdapter          退到 Playwright 连 CDP
    3. HttpAdapter         最后退到纯 HTTP（很多站会 403，但至少能试）

关键经验（实测，别改）:

    PMC PDF 只能通过「页面上下文里的 fetch」拿到：
        pg.evaluate("fetch(url)")   → %PDF-     ✅
        ctx.request.get(url)        → 403       ❌
        urllib / curl               → HTML      ❌

    原因是 fetch 跑在页面 JS 环境里，请求头、UA、Client Hints、
    cookie、TLS 指纹全部由浏览器自己生成，WAF 看不出区别。
"""
import base64
import os
import subprocess
import sys
import time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

# 页面内 fetch：分块转 base64，避免大数组 spread 爆栈
FETCH_JS = """async (url) => {
    try {
        const r = await fetch(url, {credentials: 'include'});
        if (!r.ok) return {ok: false, status: r.status};
        const buf = await r.arrayBuffer();
        const bytes = new Uint8Array(buf);
        if (bytes.length < 1000) return {ok: false, status: r.status, reason: 'too small'};
        let bin = '';
        const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        return {ok: true, len: bytes.length, b64: btoa(bin)};
    } catch (e) {
        return {ok: false, reason: String(e).slice(0, 100)};
    }
}"""


class BaseAdapter:
    name = "base"

    def fetch_pdf(self, url, referer=None):
        """返回 (bytes, note)。bytes 为 None 表示失败。"""
        raise NotImplementedError

    def fetch_text(self, url):
        raise NotImplementedError

    def close(self):
        pass


class HttpAdapter(BaseAdapter):
    """纯 HTTP。最轻，但很多站点会 403。"""
    name = "http"

    def __init__(self):
        import urllib.request
        self._req = urllib.request

    def fetch_pdf(self, url, referer=None):
        headers = {"User-Agent": UA, "Accept": "application/pdf,*/*"}
        if referer:
            headers["Referer"] = referer
        try:
            r = self._req.Request(url, headers=headers)
            with self._req.urlopen(r, timeout=120) as resp:
                data = resp.read()
            if data[:5] == b"%PDF-":
                return data, f"http {len(data)//1024}KB"
            return None, f"http 返回非 PDF ({data[:20]!r})"
        except Exception as e:
            return None, f"http {str(e)[:60]}"

    def fetch_text(self, url):
        try:
            r = self._req.Request(url, headers={"User-Agent": UA})
            with self._req.urlopen(r, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception:
            return ""


class CdpAdapter(BaseAdapter):
    """Playwright 连 CDP。核心能力：页面上下文 fetch。"""
    name = "cdp"

    def __init__(self, endpoint="http://127.0.0.1:9223"):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(endpoint)
        self.ctx = self.browser.contexts[0] if self.browser.contexts else None
        if self.ctx is None:
            raise RuntimeError("CDP 连上了但没有 context，浏览器可能未正常启动")
        self.pg = self.ctx.new_page()

    def _goto(self, url, timeout=60000):
        try:
            self.pg.goto(url, timeout=timeout, wait_until="domcontentloaded")
            return True
        except Exception:
            return False

    def fetch_pdf(self, url, referer=None):
        """referer 用于先建立会话（通常是文章页）。"""
        if referer:
            self._goto(referer)
            time.sleep(2)
        try:
            res = self.pg.evaluate(FETCH_JS, url)
        except Exception as e:
            return None, f"cdp-eval {str(e)[:50]}"

        if not res.get("ok"):
            return None, f"cdp {res.get('status', '')} {res.get('reason', '')}".strip()
        data = base64.b64decode(res["b64"])
        if data[:5] != b"%PDF-":
            return None, "cdp 返回非 PDF"
        return data, f"cdp {len(data)//1024}KB"

    def fetch_text(self, url):
        if not self._goto(url):
            return ""
        try:
            return self.pg.inner_text("body")
        except Exception:
            return ""

    def evaluate(self, js, arg=None):
        """暴露给需要自定义 JS 的场景（如从 DOM 取链接）。"""
        return self.pg.evaluate(js, arg) if arg is not None else self.pg.evaluate(js)

    def close(self):
        try:
            self.pg.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


class BrowserUseAdapter(BaseAdapter):
    """browser-use：能操作日常浏览器（带登录态），适合过反爬。

    通过子进程调用 `browser-use` CLI，不内嵌它的 Python 包 ——
    避免跟随上游变更，也避免和 Playwright 的版本冲突。
    """
    name = "browser-use"

    def __init__(self):
        self.bin = self._find_bin()
        if not self.bin:
            raise RuntimeError("找不到 browser-use 命令")

    @staticmethod
    def _find_bin():
        for cand in ("browser-use",
                     os.path.expanduser("~/.local/bin/browser-use"),
                     os.path.expanduser("~/.local/bin/browser-use.exe")):
            try:
                r = subprocess.run([cand, "--help"], capture_output=True, timeout=20)
                if r.returncode == 0:
                    return cand
            except Exception:
                continue
        return None

    def _run(self, code, timeout=300):
        """执行一段 browser-use Python 片段，返回 stdout。"""
        try:
            r = subprocess.run([self.bin], input=code, capture_output=True,
                               text=True, timeout=timeout, encoding="utf-8")
            if r.returncode != 0:
                return None, (r.stderr or "")[-300:]
            return r.stdout, ""
        except Exception as e:
            return None, str(e)[:200]

    def fetch_pdf(self, url, referer=None):
        """browser-use 模式下，PDF 交回给 CDP/HTTP 处理。

        理由：browser-use 的强项是「过反爬拿页面」，不是批量传二进制。
        大文件走它的 stdout 会把内存打爆。所以这里只用于探路。
        """
        return None, "browser-use 不直接传二进制，请用 cdp 抓 PDF"

    def fetch_text(self, url):
        code = f"""
new_tab({url!r})
import time
time.sleep(3)
print(get_page_text()[:20000])
"""
        out, err = self._run(code)
        return out or ""


def probe_scholar_available():
    """检测 Google Scholar 是否可用。返回 (ok, note)。

    注意：CDP 干净实例基本都会被 Google 拦，这是反自动化检测
    （不是地区封禁 —— 实测出口 IP 已在洛杉矶，照样被拦）。
    唯一可靠的是 browser-use 走日常浏览器。

    坑（都踩过）:
        - browser-use 的 js() helper 在 Scholar 上返回空 → 必须用
          cdp("Runtime.evaluate", returnByValue=True)
        - Scholar 标题带 surrogate 字符 → 结果写文件，片段结尾
          os._exit(0) 绕开 harness 回显
        - 第 1 页不能带 &start=0 → 返回 0 条
        - 连续请求会触发限流，探测本身也可能撞上 → 空结果先等 45s 重试
    """
    try:
        import scholar
    except Exception as e:
        return False, f"scholar 模块不可用: {str(e)[:40]}"
    try:
        rows = scholar.scholar_search("mRNA vaccine", pages=1)
        if rows:
            return True, f"可用（{len(rows)} 条结果）"
        # 空结果最常见的原因是刚被限流，等一下再试一次
        time.sleep(45)
        rows = scholar.scholar_search("mRNA vaccine stability", pages=1)
        if rows:
            return True, f"可用（{len(rows)} 条结果，首次为空是限流）"
        return False, "返回内容异常（可能是限流，等几分钟再试）"
    except Exception as e:
        return False, str(e)[:80]


def get_adapter(prefer=None):
    """返回用于**下载二进制**的适配器。

    注意：browser-use 不适合传二进制（stdout 会爆内存），
    所以这里默认跳级到 cdp，只用 http 兜底。

    browser-use 只用于「拿页面内容」和「过 Scholar 反爬」。
    """
    order = [prefer] if prefer else ["cdp", "http"]
    order = [x for x in order if x != "browser-use" or prefer == "browser-use"]
    for name in order:
        try:
            if name == "cdp":
                return CdpAdapter()
            if name == "http":
                return HttpAdapter()
        except Exception:
            continue
    return HttpAdapter()


def get_page_adapter(prefer=None):
    """返回用于**拿页面内容**的适配器。

    这里才优先 browser-use —— 它能操作日常浏览器，过反爬更强。
    """
    order = [prefer] if prefer else ["browser-use", "cdp", "http"]
    for name in order:
        try:
            if name == "browser-use":
                return BrowserUseAdapter()
            if name == "cdp":
                return CdpAdapter()
            if name == "http":
                return HttpAdapter()
        except Exception:
            continue
    return HttpAdapter()


if __name__ == "__main__":
    # 自检：报告各适配器可用性
    print("适配器可用性:")
    for name in ("browser-use", "cdp", "http"):
        try:
            if name == "browser-use":
                ad = BrowserUseAdapter()
                print(f"  {name:14s} OK ({ad.bin})")
            elif name == "cdp":
                ad = CdpAdapter()
                print(f"  {name:14s} OK")
                ad.close()
            else:
                ad = HttpAdapter()
                print(f"  {name:14s} OK")
        except Exception as e:
            print(f"  {name:14s} 不可用: {str(e)[:60]}")

    ok, note = probe_scholar_available()
    print(f"\nGoogle Scholar: {'可用' if ok else '不可用'} ({note})")
