import logging
from aiohttp import BasicAuth
from dateutil import tz
from dateutil.parser import isoparse
from datetime import datetime, date, time, timedelta
import json
import asyncio


def iterateDate(curdate, days):
    mdate = datetime.strptime(curdate, "%Y-%m-%d") + timedelta(days=days)
    return mdate.strftime("%Y-%m-%d")


def series_compress(data, duration=60, zero_only=False):
    # Elevation data has a bunch of 0s... we can simply compress all the 0s
    dataset = []
    curdp = {"t": data[0]["t"] - duration, "d": data[0]["d"], "dt": duration}
    for dp in data[1:]:
        if (
            (curdp["t"] + curdp["dt"] < dp["t"] - duration)
            or curdp["d"] != dp["d"]
            or (curdp["d"] != 0 and zero_only)
        ):
            dataset.append(curdp)
            curdp = {"t": dp["t"] - duration, "d": dp["d"], "dt": duration}

        else:
            curdp["dt"] += duration
    dataset.append(curdp)
    return dataset


class Syncer:
    active = {}
    alock = asyncio.Lock()

    buffer_days = 10

    @staticmethod
    async def sync(session,app,appid):
        await Syncer.alock.acquire()
        if not appid in Syncer.active:
            Syncer.active[appid] = Syncer(session,app,appid)
        cursyncer = Syncer.active[appid]
        if cursyncer.task is not None:
            if not cursyncer.task.done():
                logging.getLogger(f"fitbit:{appid}").debug("Sync is ongoing - not starting new sync")
                Syncer.alock.release()
                return # There is currently a sync happening
        # We have a free task!
        cursyncer.task = asyncio.create_task(cursyncer.start())
        
        Syncer.alock.release()


    def __init__(self, session, app, appid):
        self.app = app
        self.session = session
        self.log = logging.getLogger(f"fitbit:{appid}")
        self.task = None

    async def init(self):
        # To start off, we get all the necessary initial data
        self.kv = await self.app.kv()
        self.auth = {
            "Authorization": f"{self.kv['token_type']} {self.kv['access_token']}"
        }

    async def get(self, uri):
        self.log.debug(f"Querying: {uri}")
        response = await self.session.get(uri, headers=self.auth)
        # print(response.headers)
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

    async def prepare(
        self, key, tags, title, description, schema, icon="", owner_scope="read", resolution="1min", transform=lambda x: x,ignore_zero=False
    ):
        o = await self.app.objects(key=key)
        if len(o) == 0:
            o = [
                await self.app.objects.create(
                    title, description=description, key=key, tags=tags, meta={"schema": schema}, icon=icon, owner_scope=owner_scope
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
        # Next, try comparing to the most recent several datapoints in the series, to check how far back we actually need to query
        lastdp = await series[-10:]
        for dp in reversed(lastdp):
            ts_date = datetime.fromtimestamp(
                dp["t"], tz=self.timezone).date()
            if ts_date > sync_query and (dp["d"]!=0 or not ignore_zero):
                sync_query = ts_date
                break
        return {
            "series": series,
            "sync_query": sync_query,
            "key": key,
            "resolution": resolution,
            "transform": transform,
            "ignore_zero": ignore_zero
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
        formatted = a["transform"](
            [
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
        )
        # Add the data if we're not ignoring zeros
        if len(formatted)>0:
            if len(formatted) > 1 or formatted[0]["d"]!=0 or not a["ignore_zero"]:
                await series.insert_array(formatted)
        await series.kv.update(sync_query=a["sync_query"].isoformat())
        a["sync_query"] = a["sync_query"] + timedelta(days=1)

    async def sync_sleep(self, a):
        curdate = datetime.now(tz=self.timezone).date()
        if curdate < a["sync_query"]:
            # Skip if already finished sync
            return
        series = a["series"]
        sync_query = a["sync_query"]
        query_end = sync_query + timedelta(days=10)  # Query by 10 days
        if curdate < query_end:
            query_end = curdate  # ... but don't go past today
        data = await self.get(
            f"https://api.fitbit.com/1.2/user/-/sleep/date/{sync_query.isoformat()}/{query_end.isoformat()}.json"
        )

        for s in data["sleep"]:
            formatted = [
                {
                    "t": isoparse(dp["dateTime"])
                    .replace(tzinfo=self.timezone)
                    .timestamp(),
                    "d": dp["level"],
                    "dt": dp["seconds"],
                }
                for dp in s["levels"]["data"]
            ]
            await series.insert_array(formatted)

        await series.kv.update(sync_query=query_end.isoformat())
        a["sync_query"] = query_end + timedelta(days=1)

    async def start(self):
        # It is assumed that self.isrunning was already set to True
        try:
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
                await self.prepare("heart","fitbit heartrate", "Heart Rate", "", {"type": "number"}, icon="fas fa-heartbeat", resolution="1sec"),
                await self.prepare(
                    "steps","fitbit steps",
                    "Steps",
                    "",
                    {"type": "number"},
                    icon="fas fa-shoe-prints",
                    transform=lambda x: series_compress(x, zero_only=True),
                    ignore_zero=True
                ),
                await self.prepare(
                    "elevation","fitbit elevation",
                    "Elevation",
                    "",
                    {"type": "number"},
                    icon="fas fa-mountain",
                    transform=lambda x: series_compress(x),
                    ignore_zero=True
                ),
            ]

            # These are not intraday, so they need to be handled manually
            sleep = await self.prepare("sleep","fitbit sleep", "Sleep", "", {"type": "string"}, icon="fas fa-bed")

            curdate = datetime.now(tz=self.timezone).date()
            while (
                any(map(lambda x: curdate >= x["sync_query"], syncme))
                or curdate >= sleep["sync_query"]
            ):
                for s in syncme:
                    await self.sync_intraday(s)

                # Handle non-intraday requests
                await self.sync_sleep(sleep)

                # The current date might have changed during sync
                curdate = datetime.now(tz=self.timezone).date()

            await self.app.notifications.delete("sync")

            await Syncer.alock.acquire()
            self.task = None
            Syncer.alock.release()
            self.log.debug("Sync finished")
        except:
            await Syncer.alock.acquire()
            self.task = None
            Syncer.alock.release()
            self.log.exception("Sync failed")
            await self.app.notifications.delete("sync")
            
