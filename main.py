from heedy import Plugin
from aiohttp import web
import logging

logging.basicConfig(level=logging.DEBUG)

p = Plugin()

routes = web.RouteTableDef()

server_url = p.config["config"]["url"]


@routes.post("/app_create")
async def app_create(request):
    evt = await request.json()
    print("REQUEST", request.headers, evt)
    await p.notify("setup", "Link your fitbit account to heedy", app=evt["app"], _global=True,
                   description=f"To give heedy access to detailed data, you need to register a new application with fitbit. The application must be of `personal` type, and must have the following callback URL: `{server_url}/api/fitbit/auth/{evt['app']}`\nAfter registering an application with fitbit, copy its details into your fitbit settings.",
                   actions=[{"title": "Register App", "href": "https://dev.fitbit.com/apps/new", "new_window": True},
                            {"title": "Settings", "icon": "fas fa-cog", "href": f"#/apps/{evt['app']}/settings"}],
                   dismissible=False)
    return web.Response(text="ok")


@routes.post("/app_settings_update")
async def app_settings_update(request):
    evt = await request.json()
    print("REQUEST", request.headers, evt)
    # Read the app, getting the necessary settings
    await p.notify("setup", "Setting up...", app=evt["app"], description="", actions=[])
    return web.Response(text="ok")


@routes.get("/api/fitbit/auth/{app}")
async def auth_callback(request):
    appid = request.match_info['app']
    print(request.rel_url.query)
    print(request.headers)
    raise web.HTTPFound(location=f"{server_url}/#/apps/{appid}")

app = web.Application()
app.add_routes(routes)

print("Running fitbit")
# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")
