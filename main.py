from aiohttp import web

from heedy import Plugin

p = Plugin()

routes = web.RouteTableDef()


@routes.get("/api/fitbit/hello")
async def index(request):
    print("REQUEST", request)
    return web.Response(text="Hello World!")

@routes.get("/api/fitbit/fwd")
async def index(request):
    print("REQUEST", request)
    return await p.respond_forwarded(request)


app = web.Application()
app.add_routes(routes)


# Runs the server over a unix domain socket. The socket is automatically placed in the data folder,
# and not the plugin folder.
web.run_app(app, path=f"{p.name}.sock")