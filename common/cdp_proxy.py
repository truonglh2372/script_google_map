"""Local CDP relay between Playwright and the signed-in Chrome (port 9222).

Since Chrome 155 (16 Oct 2026) Playwright's connect_over_cdp fails intermittently with
"Browser.setDownloadBehavior: Browser context management is not supported" (Chrome rejects it while
browser_ui targets such as the omnibox popup exist). We never download anything, so this relay
forwards every CDP message unchanged and only turns an *error* reply to Browser.setDownloadBehavior
into an empty success. Everything else (pages, evaluate, clicks, scrolling) is byte-for-byte Chrome.

    from cdp_proxy import endpoint
    browser = playwright.chromium.connect_over_cdp(endpoint())
"""
import asyncio, json, os, threading, urllib.request

_STATE = {}


def _chrome_ws(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
        return json.load(r)["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")


async def _pipe(client):
    import websockets
    port = _STATE["port"]
    async with websockets.connect(_chrome_ws(port), max_size=None, ping_interval=None) as up:
        pending = set()

        async def c2u():
            async for m in client:
                try:
                    j = json.loads(m)
                    if j.get("method") == "Browser.setDownloadBehavior":
                        pending.add((j.get("sessionId"), j.get("id")))
                except Exception:
                    pass
                await up.send(m)

        async def u2c():
            async for m in up:
                if pending:
                    try:
                        j = json.loads(m)
                        k = (j.get("sessionId"), j.get("id"))
                        if k in pending:
                            pending.discard(k)
                            if "error" in j:
                                j.pop("error"); j["result"] = {}; m = json.dumps(j)
                    except Exception:
                        pass
                await client.send(m)

        t = [asyncio.ensure_future(c2u()), asyncio.ensure_future(u2c())]
        await asyncio.wait(t, return_when=asyncio.FIRST_COMPLETED)
        for x in t: x.cancel()


def endpoint(port=None):
    """ws:// URL of the relay (started once per process, daemon thread)."""
    port = str(port or os.environ.get("AQD_CDP_PORT") or "9222")
    if os.environ.get("AQD_CDP_DIRECT"):
        return "http://127.0.0.1:" + port
    if "url" in _STATE:
        return _STATE["url"]
    _chrome_ws(port)  # fail fast (caller falls back to HTTP) if Chrome is not up
    _STATE["port"] = port
    ready = threading.Event()

    def run():
        import websockets
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)

        async def handler(ws, *_):
            try: await _pipe(ws)
            except Exception: pass

        async def main():
            srv = await websockets.serve(handler, "127.0.0.1", 0, max_size=None, ping_interval=None)
            _STATE["url"] = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/"
            ready.set()
            await asyncio.Future()
        loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True, name="cdp-relay").start()
    if not ready.wait(10):
        raise RuntimeError("cdp relay did not start")
    return _STATE["url"]
