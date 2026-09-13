import asyncio
from tests.integration.test_browser import test_denied_control_is_never_activated
from casetrace.browser import BrowserSurface
orig = BrowserSurface.act
async def wrapped(self, *a, **k):
    result=await orig(self,*a,**k)
    await asyncio.sleep(0.3)
    print('EVENTS',self.manual_events(), self._blocked_request)
    print('AFTER', a[0].step_id, [(f.name, f.url) for f in self._page.frames])
    for f in self._page.frames:
        print(await f.locator('body').inner_text())
        print(await f.locator('input').evaluate_all('(els)=>els.map(e=>e.value)'))
    return result
BrowserSurface.act=wrapped
asyncio.run(test_denied_control_is_never_activated())

