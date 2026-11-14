"""Bounded local scheduler. All send/recovery paths share one kernel lock."""
import asyncio

from sandbox_campaigns import FakeTransport, FakeTransportFailure, SandboxCampaigns
from sandbox_store import SandboxBusy, instant


class SandboxScheduler:
    def __init__(self, campaigns, transport):
        if type(campaigns) is not SandboxCampaigns or type(transport) is not FakeTransport:
            raise TypeError('Only local campaigns and FakeTransport are supported')
        self.campaigns, self.transport, self.store = campaigns, transport, campaigns.store
        self.last_error = None
        self.store.schema('''CREATE TABLE IF NOT EXISTS sandbox_scheduler_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
            recovered INTEGER NOT NULL, outcome TEXT NOT NULL
        );''')

    def tick(self, now, *, campaign=None):
        instant(now)
        try:
            with self.store.exclusive('campaign'):
                # The lock proves there is no live coordinated sender. A crash
                # releases the kernel lock while durable reservations survive.
                recovered = self.campaigns._recover_reserved(now)
                try:
                    outcome = self.campaigns._deliver_next(self.transport, now, campaign=campaign)
                except Exception:
                    # Fake I/O has returned/failed. Unknown sends are quarantined,
                    # never requeued. Failures are still surfaced to the caller.
                    count = self.campaigns._recover_reserved(now)
                    self.record(now, recovered+count, 'uncertain_error')
                    raise
                self.record(now, recovered, outcome or 'idle')
                return {'outcome': outcome, 'recovered': recovered, 'busy': False}
        except SandboxBusy:
            return {'outcome': None, 'recovered': 0, 'busy': True}

    def record(self, now, recovered, outcome):
        if outcome == 'idle' and not recovered:
            return  # Do not grow the journal just for idle polling.
        with self.store.transaction() as db:
            db.execute('INSERT INTO sandbox_scheduler_ticks(at,recovered,outcome) VALUES (?,?,?)',
                       (now, recovered, outcome))

    async def run(self, stop, clock, *, interval=0.25, before_tick=None):
        if type(interval) not in (int, float) or not 0.01 <= interval <= 60:
            raise ValueError('Use a finite bounded local scheduler interval')
        while not stop.is_set():
            try:
                if before_tick is not None:
                    await before_tick()
                await asyncio.to_thread(self.tick, clock())
            except (FakeTransportFailure, TimeoutError) as error:
                # tick persisted uncertainty; only the known fake I/O failures
                # allow continuing to other recipients. Storage faults stop.
                self.last_error = type(error).__name__
            except Exception as error:
                self.last_error = type(error).__name__
                raise
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
