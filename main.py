from heedy import Plugin
from aiohttp import web, BasicAuth, ClientSession
import logging
import urllib.parse
import json
import dateparser
from datetime import datetime, date, time, timedelta
from dateutil import tz

from syncer import Syncer
import asyncio

logging.basicConfig(level=logging.DEBUG)

p = Plugin()

routes = web.RouteTableDef()

server_url = p.config["config"]["url"]


def redirector(x): return f"{server_url}/api/fitbit/{x}/auth"


s = ClientSession()


async def getApp(request):
    h = request.headers
    if h["X-Heedy-As"] == "public" or "/" in h["X-Heedy-As"]:
        raise web.HTTPForbidden(text="You do not have access to this resource")
    appid = request.match_info["app"]
    try:
        a = await p.apps[appid]
        owner = (await a.read())["owner"]
        if ( owner != h["X-Heedy-As"]):
            print("BAD",h["X-Heedy-As"],owner)
            raise "Fail"
        return appid,a
    except:
        raise web.HTTPForbidden(text="You do not have access to this resource")


@routes.post("/app_create")
async def app_create(request):
    evt = await request.json()
    print("REQUEST", request.headers, evt)
    await p.notify(
        "setup",
        "Link your fitbit account to heedy",
        app=evt["app"],
        _global=True,
        description=f"To give heedy access to detailed data, you need to register a new application with fitbit. The application must be of `personal` type, and must have the following callback URL: `{redirector(evt['app'])}`\n\nAfter registering an application with fitbit, copy its details into your fitbit settings.",
        actions=[
            {
                "title": "Register App",
                "href": "https://dev.fitbit.com/apps/new",
                "new_window": True,
            },
            {
                "title": "Settings",
                "icon": "fas fa-cog",
                "href": f"#/apps/{evt['app']}/settings",
            },
        ],
        dismissible=False,
    )
    return web.Response(text="ok")


@routes.post("/app_settings_update")
async def app_settings_update(request):
    evt = await request.json()
    print("REQUEST", request.headers, evt)
    a = await p.apps[evt["app"]]
    # Read the app, getting the necessary settings
    # await a.notify("setup", "Setting up...", description="", actions=[])
    settings = await a.settings
    await a.notify(
        "setup",
        "Authorize Access",
        description="Now that heedy has fitbit app credentials, you need to give it access to your data.",
        actions=[
            {
                "title": "Authorize",
                "href": settings["authorization_uri"]
                + "?"
                + urllib.parse.urlencode(
                    {
                        "response_type": "code",
                        "client_id": settings["client_id"],
                        "redirect_uri": redirector(evt["app"]),
                        "scope": "activity heartrate location nutrition profile settings sleep social weight",
                    }
                ),
            }
        ],
    )
    return web.Response(text="ok")


@routes.get("/api/fitbit/{app}/auth")
async def auth_callback(request):
    appid,a = await getApp(request)
    code = request.rel_url.query["code"]
    print("CODE", code)
    settings = await a.settings
    response = await s.post(
        settings["refresh_uri"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirector(appid),
        },
        auth=BasicAuth(settings["client_id"], settings["client_secret"]),
    )
    resjson = await response.json()
    await a.kv.set(**resjson)
    await a.notifications.delete("setup")
    await a.notify(
        "sync",
        "Synchronizing...",
        description="Heedy is syncing your historical fitbit data. Due to fitbit's download limits, this might take several days. This message will disappear once synchronization is complete.",
        actions=[],
    )

    await Syncer.sync(s, a, appid)

    # print(request.headers)
    raise web.HTTPFound(location=f"{server_url}/#/apps/{appid}")


@routes.get("/api/fitbit/{app}/sync")
async def sync(request):
    appid,a = await getApp(request)
    await a.notify(
        "sync",
        "Synchronizing...",
        description="Heedy is syncing your fitbit data. This might take a while...",
        actions=[],
    )
    await Syncer.sync(s, a, appid)

    return web.Response(text="ok")


async def run_sync():
    logging.debug("Starting sync")
    applist = await p.apps(plugin="fitbit:fitbit")
    for a in applist:
        appid = (await a.read())["id"]
        await Syncer.sync(s,a,appid)

async def syncloop():
    logging.debug("Waiting 10 seconds before syncing")
    await asyncio.sleep(10)
    while True:
        try:
            await run_sync()
        except Exception as e:
            print(e)
        wait_until = p.config["config"]["plugin"]["fitbit"]["settings"]["sync_every"]
        logging.debug(f"Waiting {wait_until} seconds until next sync initiated")
        await asyncio.sleep(wait_until)

async def startup(app):
    asyncio.create_task(syncloop())

app = web.Application()
app.add_routes(routes)
app.on_startup.append(startup)

# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")
