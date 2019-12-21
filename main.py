from heedy import Plugin
from aiohttp import web, BasicAuth, ClientSession
import logging
import urllib.parse
import json
import dateparser
from datetime import datetime, date, time, timedelta
from dateutil import tz

from syncer import Syncer

logging.basicConfig(level=logging.DEBUG)

p = Plugin()

routes = web.RouteTableDef()

server_url = p.config["config"]["url"]

redirector = lambda x: f"{server_url}/api/fitbit/{x}/auth"


s = ClientSession()


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
    appid = request.match_info["app"]
    code = request.rel_url.query["code"]
    print("CODE", code)
    a = await p.apps[appid]
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
    await a.notify(
        "setup",
        "Synchronizing...",
        description="Heedy is syncing your historical fitbit data. Due to fitbit's download limits, this might take several days. This message will disappear once synchronization is complete.",
        actions=[],
    )
    # print(request.headers)
    raise web.HTTPFound(location=f"{server_url}/#/apps/{appid}")


@routes.get("/api/fitbit/{app}/sync")
async def sync(request):
    appid = request.match_info["app"]
    a = await p.apps[appid]
    snc = Syncer(s, a, appid)
    await snc.start()

    return web.Response(text="ok")


app = web.Application()
app.add_routes(routes)

# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")
