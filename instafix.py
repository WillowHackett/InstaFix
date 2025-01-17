import json
import os
import re
from http.cookiejar import MozillaCookieJar
from typing import Optional

import aioredis
import httpx
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

if "SENTRY_DSN" in os.environ:
    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
    )

app = FastAPI()
templates = Jinja2Templates(directory="templates")

cookies = MozillaCookieJar("cookies.txt")
cookies.load()

CRAWLER_UA = {
    "facebookcatalog/1.0",
    "facebookexternalhit/1.1",
    "TelegramBot (like TwitterBot)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (compatible; January/1.0; +https://gitlab.insrt.uk/revolt/january)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 11.6; rv:92.0) Gecko/20100101 Firefox/92.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.10; rv:38.0) Gecko/20100101 Firefox/38.0",
    "Mozilla/5.0 (Windows; U; Windows NT 10.0; en-US; Valve Steam Client/default/0; ) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.117 Safari/537.36",
    "Mozilla/5.0 (Windows; U; Windows NT 10.0; en-US; Valve Steam Client/default/1596241936; ) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.117 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_1) AppleWebKit/601.2.4 (KHTML, like Gecko) Version/9.0.1 Safari/601.2.4 facebookexternalhit/1.1 Facebot Twitterbot/1.0",
}


headers = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.instagram.com",
    "referer": "https://www.instagram.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1",
    "x-ig-app-id": "1217981644879628",
}


async def get_data(request: Request, post_id: str) -> Optional[dict]:
    r = request.app.state.redis
    client = app.state.client

    async def get_media_id(post_id):
        post_url = f"https://instagram.com/p/{post_id}"
        post_text = (await client.get(post_url)).text
        media_id = re.search(r'"media_id":"(\d+)"', post_text).group(1)
        await r.set(f"{post_id}_media_id", media_id)
        return media_id

    data = await r.get(post_id)
    if data is None:
        media_id = await r.get(f"{post_id}_media_id") or await get_media_id(post_id)
        api_resp = await client.get(
            f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        )
        data = api_resp.text
        await r.set(post_id, data, ex=12 * 3600)
    data = json.loads(data)
    return data


@app.on_event("startup")
async def create_redis():
    app.state.redis = await aioredis.from_url(
        "redis://localhost:6379", encoding="utf-8", decode_responses=True
    )
    app.state.client = httpx.AsyncClient(
        headers=headers, cookies=cookies, follow_redirects=True, timeout=15.0
    )


@app.on_event("shutdown")
async def close_redis():
    await app.state.redis.close()
    await app.state.client.aclose()


@app.get("/", response_class=HTMLResponse)
def root():
    with open("templates/home.html") as f:
        html = f.read()
    return HTMLResponse(content=html)


@app.get("/p/{post_id}", response_class=HTMLResponse)
@app.get("/p/{post_id}/{num}", response_class=HTMLResponse)
@app.get("/reel/{post_id}", response_class=HTMLResponse)
@app.get("/tv/{post_id}", response_class=HTMLResponse)
async def read_item(request: Request, post_id: str, num: Optional[int] = 1):
    post_url = f"https://instagram.com/p/{post_id}"
    # if request.headers.get("User-Agent") not in CRAWLER_UA:
    #     return RedirectResponse(post_url, status_code=302)

    data = await get_data(request, post_id)
    item = data["items"][0]

    media_lst = item["carousel_media"] if "carousel_media" in item else [item]
    media = media_lst[num - 1]

    description = item["caption"]["text"] if item["caption"] != None else ""
    full_name = item["user"]["full_name"]
    username = item["user"]["username"]

    ctx = {
        "request": request,
        "url": post_url,
        "description": description,
        "full_name": full_name,
        "username": username,
        "media_num": num,
        "media_total": len(media_lst),
    }

    if "video_versions" in media:
        video = media["video_versions"][-1]
        ctx["video"] = f"/videos/{post_id}/{num}"
        ctx["width"] = video["width"]
        ctx["height"] = video["height"]
        ctx["card"] = "player"
    else:
        image = media["image_versions2"]["candidates"][0]
        ctx["image"] = f"/images/{post_id}/{num}"
        ctx["width"] = image["width"]
        ctx["height"] = image["height"]
        ctx["card"] = "summary_large_image"

    return templates.TemplateResponse("base.html", ctx)


@app.get("/videos/{post_id}/{num}")
async def videos(request: Request, post_id: str, num: int):
    data = await get_data(request, post_id)
    item = data["items"][0]

    media_lst = item["carousel_media"] if "carousel_media" in item else [item]
    media = media_lst[num - 1]
    video_url = media["video_versions"][0]["url"]
    return RedirectResponse(video_url, status_code=302)


@app.get("/images/{post_id}/{num}")
async def images(request: Request, post_id: str, num: int):
    data = await get_data(request, post_id)
    item = data["items"][0]

    media_lst = item["carousel_media"] if "carousel_media" in item else [item]
    media = media_lst[num - 1]
    image_url = media["image_versions2"]["candidates"][0]["url"]
    return RedirectResponse(image_url, status_code=302)
