"""
Read-only access to H&M Turkey (www2.hm.com/tr_tr) for the new-products pipeline.

H&M (Akamai) blocks plain HTTP clients and Playwright-launched browsers, so -- like
refresh_hm_stock.py -- this starts a normal Chrome with its own profile and remote
debugging, attaches over CDP, warms up on the home page and fetches pages from inside it.

    with HMBrowser() as hm:
        hits, total_pages = hm.listing("/tr_tr/kadin/urune-gore-satin-al/elbise.html", page=1)
        detail = hm.product("1375249001")

listing() hits (from the category page's __NEXT_DATA__) carry, per colour article:
articleCode, title (Turkish), category (e.g. ladies_dresses_knitted), productColor.colorName,
prices[0].price (TRY), sizes [{name, sizeCode, stock}] -- stock 2 = in stock, 1 = few left,
0 = sold out (H&M's own codes, checked against the availability feed 2026-11-25).

Uses the same Chrome port/profile as refresh_hm_stock.py (02:00), so don't run both at once.
"""
import json
import subprocess
import time
import urllib.request

from refresh_hm_stock import CHROME, PORT, PROFILE

PAUSE = 0.8

JS_FETCH_NEXT = """async (u) => {
  const c = new AbortController(); setTimeout(() => c.abort(), 30000);
  const r = await fetch(u, {credentials: 'include', signal: c.signal});
  if (r.status !== 200) return {status: r.status};
  if (r.redirected && !r.url.includes(u.split('?')[0])) return {status: 404, redirect: r.url};
  const h = await r.text();
  const m = h.match(/<script id="__NEXT_DATA__"[^>]*>([\\s\\S]*?)<\\/script>/);
  return m ? {status: 200, next: m[1]} : {status: 200, error: 'no __NEXT_DATA__'};
}"""


class HMError(RuntimeError):
    pass


class HMBrowser:
    def __init__(self, pause=PAUSE):
        self.pause = pause
        self._proc = self._pw = self._browser = self.page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        PROFILE.mkdir(exist_ok=True)
        self._proc = subprocess.Popen([CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                                       "--no-first-run", "--no-default-browser-check", "--window-size=1100,800",
                                       "--window-position=40,40", "about:blank"])
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).close()
                break
            except OSError:
                time.sleep(0.5)
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            self.page = self._browser.contexts[0].pages[0]
            self.page.goto("https://www2.hm.com/tr_tr/index.html", wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(4000)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        for step in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                step()
            except Exception:
                pass
        if self._proc:
            self._proc.terminate()
        return False

    def _next(self, url, retries=2):
        last = None
        for attempt in range(retries + 1):
            try:
                r = self.page.evaluate(JS_FETCH_NEXT, url)
            except Exception as e:
                r = {"status": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"}
            time.sleep(self.pause)
            if r.get("status") == 200 and r.get("next"):
                return json.loads(r["next"])
            if r.get("status") == 404:
                return None
            last = r
            time.sleep(2 + 3 * attempt)
            if r.get("status") == 0 and attempt < retries:
                # an aborted in-page fetch (2026-11-27 03:10) leaves the tab stuck: reload it first
                try:
                    self.page.goto("https://www2.hm.com/tr_tr/index.html", wait_until="domcontentloaded",
                                   timeout=60000)
                    self.page.wait_for_timeout(3000)
                except Exception:
                    pass
        raise HMError(f"{url}: {last}")

    def listing(self, path, page=1):
        """-> (hits, total_pages). Raises HMError if the page can't be read."""
        url = path if page == 1 else f"{path}?page={page}"
        j = self._next(url)
        if j is None:
            return [], 0
        try:
            data = j["props"]["pageProps"]["plpProps"]["productListingSectionProps"]["productListingData"]
        except (KeyError, TypeError):
            raise HMError(f"{url}: no product listing on the page")
        return data.get("hits") or [], int((data.get("pagination") or {}).get("totalPages") or 0)

    def product(self, article):
        """-> {"details": productArticleDetails, "variation": this article's variation} or None if gone."""
        j = self._next(f"/tr_tr/productpage.{article}.html")
        if j is None:
            return None
        try:
            d = j["props"]["pageProps"]["productPageProps"]["aemData"]["productArticleDetails"]
        except (KeyError, TypeError):
            raise HMError(f"product {article}: unexpected page data")
        v = (d.get("variations") or {}).get(article)
        return {"details": d, "variation": v} if v else None
