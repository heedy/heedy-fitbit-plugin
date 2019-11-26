from heedy import Plugin
from aiohttp import web
import logging

logging.basicConfig(level=logging.DEBUG)

p = Plugin()

routes = web.RouteTableDef()


@routes.post("/app_create")
async def index(request):
    evt = await request.json()
    print("REQUEST", request.headers, evt)
    await p.notify({'app': evt["app"], 'key': "setup", "global": True,
                    'title': "Link your fitbit account to heedy",
                    'description': "Follow the instructions [here](https://google.com) to get a fitbit API key, and save it to the [app settings](https://google.com)"})
    return web.Response(text="ok")

app = web.Application()
app.add_routes(routes)

print("Running fitbit")
# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")
