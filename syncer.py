import logging
from aiohttp import BasicAuth
from dateutil import tz
from datetime import datetime, date, time, timedelta
import json
import asyncio


def iterateDate(curdate, days):
    mdate = datetime.strptime(curdate, "%Y-%m-%d") + timedelta(days=days)
    return mdate.strftime("%Y-%m-%d")


class Syncer:
    active = {}
    alock = None

    buffer_days = 7

    def __init__(self, session, app, appid):
        self.app = app
        self.session = session
        self.log = logging.getLogger(f"fitbit:{appid}")

    async def init(self):
        # To start off, we get all the necessary initial data
        self.kv = await self.app.kv()
        self.auth = {
            "Authorization": f"{self.kv['token_type']} {self.kv['access_token']}"
        }

    async def get(self, uri):
        self.log.debug(f"Querying: {uri}")
        response = await self.session.get(uri, headers=self.auth)
        print(response.headers)
        if response.status >= 400:
            if response.status == 429:
                # Add on an extra couple seconds to make sure their end registers the reset
                waitfor = int(response.headers["Retry-After"]) + 10
                self.log.debug(
                    f"Waiting for {waitfor} seconds for fitbit API rate-limit to expire"
                )
                await asyncio.sleep(waitfor)
                return await self.get(uri)
            errdata = json.loads(await response.text())
            self.log.debug(f"Error response: {json.dumps(errdata)}")
            errtype = errdata["errors"][0]["errorType"]
            if errtype == "expired_token":
                await self.refresh_token()
                return await self.get(uri)
        return await response.json()

    async def refresh_token(self):
        self.log.debug("Refreshing token")
        settings = await self.app.settings
        response = await self.session.post(
            settings["refresh_uri"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.kv["refresh_token"],
            },
            auth=BasicAuth(settings["client_id"], settings["client_secret"]),
        )
        resjson = await response.json()
        await self.app.kv.update(**resjson)
        await self.init()  # Need to re-init

        self.log.debug("Access token updated")

    async def prepare(self, key, title, description, schema, resolution="1min"):
        o = await self.app.objects(key=key)
        if len(o) == 0:
            o = [
                await self.app.objects.create(
                    title, description=description, key=key, schema=schema
                )
            ]

        series = o[0]
        # First try the sync_query variable
        sync_query = await series.kv["sync_query"]
        if sync_query is None:
            sync_query = date.fromisoformat(self.joinDate)
        else:
            # If it is already set up, subtract the buffer days, to get a guess as to where to start sync
            sync_query = date.fromisoformat(sync_query) - timedelta(
                days=self.buffer_days
            )
        # Next, try comparing to the most recent datapoint in the series
        lastdp = await series[-1:]
        if len(lastdp) > 0:
            ts_date = datetime.fromtimestamp(lastdp[0]["t"], tz=self.timezone).date()
            if ts_date > sync_query:
                sync_query = ts_date
        return {
            "series": series,
            "sync_query": sync_query,
            "key": key,
            "resolution": resolution,
        }

    async def sync_intraday(self, a):
        if datetime.now(tz=self.timezone).date() < a["sync_query"]:
            # Skip if already finished sync
            return
        series = a["series"]
        data = await self.get(
            f"https://api.fitbit.com/1/user/-/activities/{a['key']}/date/{a['sync_query'].isoformat()}/1d/{a['resolution']}.json"
        )
        dpa = data[f"activities-{a['key']}-intraday"]["dataset"]
        formatted = [
            {
                "t": datetime.combine(
                    a["sync_query"],
                    time.fromisoformat(dp["time"]),
                    tzinfo=self.timezone,
                ).timestamp(),
                "d": dp["value"],
            }
            for dp in dpa
        ]
        await series.insert_array(formatted)
        await series.kv.update(sync_query=a["sync_query"].isoformat())
        a["sync_query"] = a["sync_query"] + timedelta(days=1)

    async def start(self):
        self.log.debug("Starting sync")
        await self.init()

        profile = await self.get(
            f"https://api.fitbit.com/1/user/{self.kv['user_id']}/profile.json"
        )
        usr = profile["user"]
        self.log.info(f"Syncing data for {usr['fullName']}")

        self.joinDate = usr["memberSince"]
        self.timezone = tz.gettz(usr["timezone"])

        #
        # There are a couple issues with syncing fitbit data using the API:
        # - There is a rate limit of 150 requests an hour
        # - intraday time series seem to be limited to 1 day per request
        # - If a day does not return data, it is not clear if there is no data there, or if the
        # user's device did not sync yet.
        #
        # Our syncing approach is to keep a "sync_query" variable in each series holding the date of the
        # most recently queried data. This allows us to keep place in series that have no data.
        # Since fitbit devices can only store detailed data for ~ 1 week before needing to sync, on each sync,
        # the sync_query variable is actually back-tracked a number of days to catch any datapoints that might have come
        # into fitbit recently for the past few days due to a device sync.
        #
        # This would normally be very inefficient, requiring a re-query of an entire week on each sync for each series.
        # Therefore, the most recent data in the timeseries is also used as a reference time - if there exists a datapoint at time t,
        # it is assumed that *all* data has been synced until time t, so we can just start with time t, instead of backtracking a whole week.

        # Start by finding all the timeseries, and initializing their metadata if necessary
        syncme = [
            await self.prepare("heart", "Heart Rate", "", {"type": "number"}, "1sec"),
            await self.prepare("steps", "Steps", "", {"type": "number"}),
            await self.prepare("elevation", "Elevation", "", {"type": "number"}),
        ]

        i = 0
        while i < 3 and any(
            map(
                lambda x: datetime.now(tz=self.timezone).date() >= x["sync_query"],
                syncme,
            )
        ):
            i += 1
            for s in syncme:
                await self.sync_intraday(s)
