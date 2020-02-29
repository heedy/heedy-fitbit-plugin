from heedy import Plugin
from aiohttp import web, BasicAuth, ClientSession
import logging
import urllib.parse
import json
from datetime import datetime, date, time, timedelta

from syncer import Syncer
import asyncio

logging.basicConfig(level=logging.DEBUG)

p = Plugin()

routes = web.RouteTableDef()

server_url = p.config["config"]["url"]

l = logging.getLogger("fitbit")


def redirector(x):
    if server_url.startswith("https://"):
        return f"{server_url}/api/fitbit/{x}/auth"
    return f"http://localhost:{p.config['config']['port']}/api/fitbit/{x}/auth"


# Initialize the client session on server start, because otherwise aiohttp complains
s = None


async def getApp(request):
    h = request.headers
    if h["X-Heedy-As"] == "public" or "/" in h["X-Heedy-As"]:
        raise web.HTTPForbidden(text="You do not have access to this resource")
    appid = request.match_info["app"]
    try:
        a = await p.apps[appid]
        appvals = await a.read()
        if (appvals["plugin"] != "fitbit:fitbit"):
            l.error(f"The app {appid} is not managed by fitbit")
            raise "fail"
        if (appvals["owner"] != h["X-Heedy-As"] and h["X-Heedy-As"] != "heedy"):
            l.error(f"Only the owner of {appid} can run fitbit commands on it")
            raise "fail"
        return appid, a
    except:
        raise web.HTTPForbidden(text="You do not have access to this resource")


@routes.post("/app_create")
async def app_create(request):
    evt = await request.json()
    l.debug(f"App created: {evt}")
    redir = redirector(evt['app'])
    msg = f"To give heedy access to your data, you need to register an application with fitbit. The application must be of 'personal' type, and must use the following callback URL: `{redir}`"
    msg += "\n\nAfter registering an application with fitbit, copy its details into your fitbit app's settings."

    # Try creating the notification 5 times, in case the database is still committing the app
    for i in range(10):
        try:
            await p.notify(
                "setup",
                "Link your fitbit account to heedy",
                app=evt["app"],
                _global=True,
                description=msg,
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
        except:
            l.warning("Failed to notify on app create")
            await asyncio.sleep(0.1)

    return web.Response(text="ok")


@routes.post("/app_settings_update")
async def app_settings_update(request):
    evt = await request.json()
    l.debug(f"Settings updated: {evt}")
    a = await p.apps[evt["app"]]
    # Read the app, getting the necessary settings
    # await a.notify("setup", "Setting up...", description="", actions=[])
    settings = await a.settings

    desc = "Now that heedy has fitbit app credentials, you need to give it access to your data."

    if redirector(evt['app']).startswith("http://localhost"):
        desc += f"\n\n**NOTE:** Due to http restrictions on fitbit's servers, you must press the Authorize button from the computer running heedy (i.e. heedy must be accessible at `http://localhost:{p.config['config']['port']}`). If running heedy on a remote server, you will need to forward port `{p.config['config']['port']}` to `localhost:{p.config['config']['port']}` for authorization to succeed."

    await a.notify(
        "setup",
        "Authorize Access",
        description=desc,
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
    appid, a = await getApp(request)
    code = request.rel_url.query["code"]
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
    l.info(f"Successfully authenticated {appid}")
    await a.notifications.delete("setup")
    await a.notify(
        "sync",
        "Synchronizing...",
        description="Heedy is syncing your historical fitbit data. Due to fitbit's download limits, this might take several days. This message will disappear once synchronization is complete.",
        actions=[],
    )
    await Syncer.sync(s, a, appid)

    # redirect the user back to heedy
    if server_url.startswith("https://"):
        raise web.HTTPFound(location=f"{server_url}/#/apps/{appid}")
    # Makes it work also when ssh port forwarding the server
    raise web.HTTPFound(
        location=f"http://localhost:{p.config['config']['port']}/#/apps/{appid}")


@routes.get("/api/fitbit/{app}/sync")
async def sync(request):
    appid, a = await getApp(request)
    l.debug(f"Sync requested for {appid}")
    await a.notify(
        "sync",
        "Synchronizing...",
        description="Heedy is syncing your fitbit data. This might take a while...",
        actions=[],
    )
    await Syncer.sync(s, a, appid)

    return web.Response(text="ok")


async def run_sync():
    l.debug("Starting sync of all fitbit accounts...")
    applist = await p.apps(plugin="fitbit:fitbit")
    for a in applist:
        appid = a["id"]
        await Syncer.sync(s, a, appid)


async def syncloop():
    l.debug("Waiting 10 seconds before syncing")
    await asyncio.sleep(10)
    while True:
        try:
            await run_sync()
        except Exception as e:
            l.error(e)
        wait_until = p.config["config"]["plugin"]["fitbit"]["settings"]["sync_every"]
        l.debug(f"Waiting {wait_until} seconds until next auto-sync initiated")
        await asyncio.sleep(wait_until)


async def startup(app):
    global s
    s = ClientSession()
    asyncio.create_task(syncloop())


async def cleanup(app):
    await s.close()
    await p.session.close()

app = web.Application()
app.add_routes(routes)
app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")
