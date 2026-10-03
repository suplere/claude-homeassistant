"""Vytvoří (pokud chybí) a uloží storage dashboard z YAML souboru přes Lovelace WS API.

Použití (z kořene repa):
    source venv/bin/activate && set -a && source .env && set +a
    python dashboards/push_dashboard.py lovelace dashboards/energie-v2.yaml   # hlavní dashboard

Potřebuje HA_URL a HA_TOKEN v prostředí (.env). YAML kotvy (&/<<:) se rozbalí.
ThreadedResolver: aiodns neumí .local (mDNS) adresy.
"""
import asyncio
import os
import sys

import aiohttp
import yaml

URL_PATH, SRC = sys.argv[1], sys.argv[2]
cfg = yaml.safe_load(open(SRC, encoding="utf-8"))
title = cfg.get("title", URL_PATH)
base = os.environ["HA_URL"].rstrip("/")
ws_url = base.replace("http", "ws", 1) + "/api/websocket"

async def main():
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as s, s.ws_connect(ws_url) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": os.environ["HA_TOKEN"]})
        assert (await ws.receive_json())["type"] == "auth_ok"
        n = 0
        async def call(**msg):
            nonlocal n
            n += 1
            await ws.send_json({"id": n, **msg})
            while True:
                r = await ws.receive_json()
                if r.get("id") == n:
                    return r
        # hlavní dashboard („lovelace“) existuje vždy a ukládá se bez url_path
        target = {} if URL_PATH == "lovelace" else {"url_path": URL_PATH}
        dashes = (await call(type="lovelace/dashboards/list"))["result"]
        if target and not any(d["url_path"] == URL_PATH for d in dashes):
            r = await call(type="lovelace/dashboards/create", url_path=URL_PATH, title=title,
                           icon="mdi:home-lightning-bolt", show_in_sidebar=True, require_admin=False, mode="storage")
            print("create:", r.get("success"), r.get("error"))
        r = await call(type="lovelace/config/save", config=cfg, **target)
        print("save:", r.get("success"), r.get("error"))
        r = await call(type="lovelace/config", **target)
        print("views:", [v.get("path") for v in r["result"]["views"]])

asyncio.run(main())
