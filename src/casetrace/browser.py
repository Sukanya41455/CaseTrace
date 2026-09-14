"""Playwright implementation of the model-neutral, policy-guarded surface."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    FrameLocator,
    Locator,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .contracts import (
    AbsentPredicate,
    AccessibleRoleStrategy,
    AdjacentControlStrategy,
    AllPredicate,
    AnyPredicate,
    ClickStep,
    CountOperator,
    CountPredicate,
    EqualsPredicate,
    FailureCode,
    FillStep,
    NavigateStep,
    Observation,
    ObservedControl,
    PaymentQuery,
    Predicate,
    ReadParser,
    ReadStep,
    RunStopped,
    Step,
    TableRelationStrategy,
    TargetSpec,
    VisiblePredicate,
    VisibleTextStrategy,
    resolve_value,
)
from .policy import Policy
from .surface import ResolvedTarget

_CONTROL_SELECTOR = (
    "a,button,input:not([type=hidden]),select,textarea,[role=button],"
    "table[aria-label],h1,h2,p.summary"
    ",p:has(> strong)"
)
_MEMBER_ROUTE = re.compile(r"^/bank/members/[A-Za-z0-9._-]+$")
_ACTIVITY_ROUTE = re.compile(r"^/bank/members/[A-Za-z0-9._-]+/accounts/[A-Za-z0-9._-]+/activity$")
_DETAIL_ROUTE = re.compile(
    r"^/bank/members/[A-Za-z0-9._-]+/accounts/[A-Za-z0-9._-]+/transactions/[A-Za-z0-9._-]+$"
)
_SENSITIVE_TEXT = (
    re.compile(r"\b\d{5}\b"),
    re.compile(r"\bSYNTH-[A-Z0-9-]+\b"),
    re.compile(r"\bREF-[A-Z0-9-]+\b"),
)
_HEADER_KEYS = {
    "account id": "account_id",
    "transaction reference": "reference",
    "reference": "reference",
    "transaction date": "transaction_date",
    "displayed status": "status",
    "status": "status",
    "activity source": "source",
    "member id": "member_id",
    "direction": "direction",
    "amount": "amount",
    "currency": "currency",
    "description": "description",
    "product": "product",
    "action": "action",
    "activity": "activity",
    "payments": "payments",
    "start date": "date_from",
    "end date": "date_to",
}


_CONTROL_INFO_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const explicit = el.getAttribute('aria-label');
  const labelled = el.labels && el.labels.length
    ? Array.from(el.labels).map(x => x.innerText.trim()).filter(Boolean).join(' ')
    : '';
  const cell = el.closest('td');
  const adjacent = cell && cell.previousElementSibling &&
    cell.previousElementSibling.tagName === 'TH'
    ? cell.previousElementSibling.innerText.trim() : '';
  const text = (el.innerText || '').trim();
  const paragraphLabel = tag === 'p' && el.querySelector(':scope > strong');
  const role = el.getAttribute('role') ||
    (tag === 'table' ? 'table' : /^h[1-6]$/.test(tag) ? 'heading' : tag === 'p' ? 'paragraph' :
      tag === 'a' ? 'link' : tag === 'button' ? 'button' :
      tag === 'select' ? 'combobox' : type === 'submit' ? 'button' : 'textbox');
  const form = el.form;
  return {
    tag,
    type,
    role,
    label: (paragraphLabel && paragraphLabel.innerText.trim()) ||
      explicit || labelled || text || adjacent || el.getAttribute('placeholder') || role,
    name: el.getAttribute('name') || '',
    href: tag === 'a' ? el.href : null,
    formAction: form ? form.action : null,
    formMethod: form ? (form.method || 'get').toUpperCase() : null,
    enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
    filledByAutomation: el.dataset.casetraceFilled === 'true',
    hasValue: (tag === 'input' || tag === 'select' || tag === 'textarea') && el.value !== ''
    ,adjacent: Boolean(adjacent && !explicit && !labelled)
  };
}
"""

_TABLE_ROWS_JS = """
(table) => {
  const headerRow = table.querySelector('thead tr') || table.querySelector('tr');
  if (!headerRow) return [];
  const headers = Array.from(headerRow.querySelectorAll('th')).map(x => x.innerText.trim());
  const body = table.querySelector('tbody');
  const rows = body ? Array.from(body.querySelectorAll(':scope > tr')) :
    Array.from(table.querySelectorAll('tr')).slice(1);
  return rows.filter(row => !row.querySelector('td[colspan]')).map(row => {
    const cells = Array.from(row.querySelectorAll(':scope > td'));
    const result = {};
    cells.forEach((cell, index) => {
      result[headers[index] || `column_${index + 1}`] = cell.innerText.trim();
    });
    return result;
  });
}
"""

_RECORDING_JS = """
(() => {
  if (globalThis.__casetraceInstalled) return;
  globalThis.__casetraceInstalled = true;
  globalThis.__casetraceHumanControl = false;
  globalThis.__casetraceOwnershipGeneration = -1;
  globalThis.__casetraceSetOwnership = state => {
    if (state.generation < globalThis.__casetraceOwnershipGeneration) return;
    globalThis.__casetraceOwnershipGeneration = state.generation;
    globalThis.__casetraceHumanControl = state.enabled;
  };
  globalThis.__casetraceOwnership().then(globalThis.__casetraceSetOwnership);
  for (const type of ['pointerdown', 'click', 'keydown', 'beforeinput', 'input',
                       'change', 'paste', 'drop', 'submit']) {
    addEventListener(type, event => {
      if (!event.isTrusted) return;
      const el = event.target;
      if (el.__casetraceApprovedAction ||
          (event.submitter && event.submitter.__casetraceApprovedAction)) return;
      const meta = {
        event_type: type,
        role: el.getAttribute && el.getAttribute('role') || el.tagName.toLowerCase(),
        label: el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('name')) || ''
      };
      if (!globalThis.__casetraceHumanControl) {
        event.preventDefault();
        event.stopImmediatePropagation();
        meta.blocked = true;
      } else {
        meta.blocked = false;
      }
      globalThis.__casetraceRecord(meta);
    }, true);
  }
})();
"""


@dataclass(slots=True)
class _BrowserTarget:
    locator: Locator
    metadata: ResolvedTarget


@dataclass(slots=True)
class _ObservedEntry:
    spec: TargetSpec
    locator: Locator


ManualEventHandler = Callable[[dict[str, str | bool]], Awaitable[None] | None]


class BrowserSurface:
    action_timeout_ms = 10_000

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        policy: Policy,
        targets: Sequence[TargetSpec],
        manual_event_handler: ManualEventHandler | None,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._policy = policy
        self._targets = {target.target_id: target for target in targets}
        self._manual_event_handler = manual_event_handler
        self._manual_events: list[dict[str, str | bool]] = []
        self._blocked_request = False
        self._observation_index = 0
        self._observed: dict[str, dict[str, _ObservedEntry]] = {}
        self._latest_observation_id: str | None = None
        self._sensitive_values: set[str] = set()
        self._fingerprint_salt = uuid4().bytes
        self._human_control = False
        self._native_dialog_present = False
        self._ownership_generation = 0
        self._ownership_lock = asyncio.Lock()
        self.browser_id = f"browser-{uuid4().hex}"
        self.context_id = f"context-{uuid4().hex}"
        self.page_id = f"page-{uuid4().hex}"
        self._original_session = (browser, context, page)

    @classmethod
    async def launch(
        cls,
        policy: Policy,
        targets: Sequence[TargetSpec],
        *,
        headless: bool = True,
        manual_event_handler: ManualEventHandler | None = None,
    ) -> BrowserSurface:
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(service_workers="block")
            holder: dict[str, BrowserSurface] = {}

            async def record(_source: object, metadata: object) -> None:
                surface = holder.get("surface")
                if surface is not None:
                    await surface._record_manual_event(metadata)

            def ownership(_source: object) -> dict[str, object]:
                surface = holder.get("surface")
                if surface is None:
                    return {"enabled": False, "generation": 0}
                return surface._ownership_state()

            await context.expose_binding("__casetraceRecord", record)
            await context.expose_binding("__casetraceOwnership", ownership)
            await context.add_init_script(_RECORDING_JS)
            page = await context.new_page()
            surface = cls(
                playwright,
                browser,
                context,
                page,
                policy,
                targets,
                manual_event_handler,
            )
            holder["surface"] = surface
            cdp = await context.new_cdp_session(page)
            page.on("dialog", lambda _: setattr(surface, "_native_dialog_present", True))
            cdp.on(
                "Page.javascriptDialogClosed",
                lambda _: setattr(surface, "_native_dialog_present", False),
            )
            await cdp.send("Page.enable")

            async def guard_response(event: dict[str, Any]) -> None:
                request_id = event["requestId"]
                try:
                    headers = {
                        item["name"].lower(): item["value"]
                        for item in event.get("responseHeaders", [])
                    }
                    location = headers.get("location")
                    status = event.get("responseStatusCode", 0)
                    request = event["request"]
                    method = "GET" if status in {301, 302, 303} else request["method"]
                    if (
                        location
                        and 300 <= status < 400
                        and not policy.request_allowed(
                            urljoin(request["url"], location), method, "document"
                        )
                    ):
                        surface._blocked_request = True
                        await cdp.send(
                            "Fetch.failRequest",
                            {"requestId": request_id, "errorReason": "BlockedByClient"},
                        )
                    else:
                        await cdp.send("Fetch.continueRequest", {"requestId": request_id})
                except Exception:
                    surface._blocked_request = True
                    try:
                        await cdp.send(
                            "Fetch.failRequest",
                            {"requestId": request_id, "errorReason": "BlockedByClient"},
                        )
                    except Exception:
                        pass  # Closing the controlled page invalidates paused requests.

            cdp.on("Fetch.requestPaused", guard_response)
            await cdp.send("Fetch.enable", {"patterns": [{"requestStage": "Response"}]})
            await context.route("**/*", surface._guard_request)
            page.on("popup", lambda popup: asyncio.create_task(surface._reject_popup(popup)))
            page.set_default_timeout(surface.action_timeout_ms)
            page.set_default_navigation_timeout(surface.action_timeout_ms)
            return surface
        except BaseException:
            await playwright.stop()
            raise

    async def close(self) -> None:
        try:
            await self._context.close()
            await self._browser.close()
        finally:
            await self._playwright.stop()

    def bind_query(self, args: PaymentQuery) -> None:
        for value in args.model_dump(mode="json").values():
            if value is not None:
                self._sensitive_values.add(str(value))

    def set_targets(self, targets: Sequence[TargetSpec]) -> None:
        self._targets = {target.target_id: target for target in targets}

    def observed_targets(self, observation_id: str | None = None) -> list[TargetSpec]:
        selected = observation_id or self._latest_observation_id
        if selected is None or selected not in self._observed:
            return []
        return [entry.spec for entry in self._observed[selected].values()]

    def target_for_handle(self, observation_id: str, handle: str) -> TargetSpec:
        try:
            return self._observed[observation_id][handle].spec
        except KeyError as error:
            raise KeyError("unknown or stale observation target handle") from error

    async def observe(self) -> Observation:
        await self._validate_open_surfaces()
        observation_id = f"observation-{self._observation_index}"
        self._observation_index += 1
        entries: dict[str, _ObservedEntry] = {}
        controls: list[ObservedControl] = []
        ordinal = 0
        for frame, frame_path in self._visible_frames():
            candidates = frame.locator(_CONTROL_SELECTOR)
            for index in range(await candidates.count()):
                locator = candidates.nth(index)
                if not await locator.is_visible():
                    continue
                info = await locator.evaluate(_CONTROL_INFO_JS)
                label = self._sanitize_text(str(info["label"]))
                handle = f"target-{self._observation_index}-{ordinal}"
                ordinal += 1
                spec = TargetSpec(
                    target_id=handle,
                    surface_kind="web",
                    frame_path=frame_path,
                    strategies=[
                        VisibleTextStrategy(text=label, exact=True)
                        if info["tag"] == "p"
                        else AdjacentControlStrategy(label=label, control_role=str(info["role"]))
                        if info["adjacent"]
                        else AccessibleRoleStrategy(
                            role=str(info["role"]),
                            name=label,
                        )
                    ],
                    cardinality=1,
                )
                entries[handle] = _ObservedEntry(spec=spec, locator=locator)
                controls.append(
                    ObservedControl(
                        target_handle=handle,
                        role=str(info["role"]),
                        label=label,
                        enabled=bool(info["enabled"]),
                        visible=True,
                        filled_by_automation=bool(info["filledByAutomation"]),
                        has_value=bool(info["hasValue"]),
                    )
                )
        self._observed = {observation_id: entries}
        self._latest_observation_id = observation_id
        vendor, app_version = await self._visible_identity()
        screen = await self._recognized_screen()
        visible_text = "\n".join(
            [await frame.locator("body").inner_text() for frame, _ in self._visible_frames()]
        )
        page_fingerprint = hashlib.sha256(
            self._fingerprint_salt + visible_text.encode("utf-8")
        ).hexdigest()
        return Observation(
            observation_id=observation_id,
            vendor=vendor,
            app_version=app_version,
            surface_features=["web", "frames", "accessible_roles", "table_relationships"],
            controls=controls,
            state={
                "screen": screen or "unknown",
                "frame_count": len(self._page.frames),
                "page_fingerprint": page_fingerprint,
                "dialog_present": any(
                    [
                        await frame.get_by_role("dialog").count() > 0
                        for frame, _ in self._visible_frames()
                    ]
                ),
            },
        )

    async def act(self, step: Step, args: PaymentQuery, variables: dict[str, object]) -> None:
        self._blocked_request = False
        try:
            if isinstance(step, NavigateStep):
                value = resolve_value(step.url, args, variables)
                if not isinstance(value, str):
                    raise RunStopped(
                        FailureCode.INVALID_INPUT,
                        step.step_id,
                        "string navigation URL",
                        "invalid typed URL",
                    )
                url = self._policy.absolute_url(value, self._page.url)
                self._policy.authorize_navigation(url, step_id=step.step_id)
                await self._page.goto(url, wait_until="load")
            elif isinstance(step, FillStep):
                target = await self._resolve(step.target_id, args, variables)
                self._policy.authorize(step, target.metadata)
                value = resolve_value(step.value, args, variables)
                rendered = value.isoformat() if isinstance(value, date) else str(value)
                tag = await target.locator.evaluate("el => el.tagName.toLowerCase()")
                operation = (
                    (lambda: target.locator.select_option(rendered))
                    if tag == "select"
                    else (lambda: target.locator.fill(rendered))
                )
                await self._approved_action(target.locator, operation)
                await target.locator.evaluate("el => { el.dataset.casetraceFilled = 'true'; }")
            elif isinstance(step, ClickStep):
                target = await self._resolve(step.target_id, args, variables)
                self._policy.authorize(step, target.metadata)
                await self._approved_action(
                    target.locator,
                    target.locator.click,
                    navigates=bool(target.metadata.form_action),
                )
            else:
                raise RunStopped(
                    FailureCode.POLICY_DENIED,
                    step.step_id,
                    "navigate, fill, or click action",
                    "unsupported mutation",
                )
            await self._validate_open_surfaces(step.step_id)
            for predicate in step.checks:
                if not await self.check(predicate, args, variables):
                    raise RunStopped(
                        FailureCode.CHECKPOINT_FAILED,
                        step.step_id,
                        "declared postcondition",
                        "condition false",
                    )
        except RunStopped:
            raise
        except PlaywrightTimeoutError as error:
            code = FailureCode.POLICY_DENIED if self._blocked_request else FailureCode.TIMEOUT
            raise RunStopped(
                code, step.step_id, "action before deadline", "action stopped"
            ) from error
        except Exception as error:
            code = FailureCode.POLICY_DENIED if self._blocked_request else FailureCode.UNKNOWN_STATE
            raise RunStopped(
                code, step.step_id, "supported browser state", "action stopped"
            ) from error

    async def read(self, step: Step, args: PaymentQuery, variables: dict[str, object]) -> object:
        if not isinstance(step, ReadStep):
            raise TypeError("read requires a ReadStep")
        target = await self._resolve(step.target_id, args, variables)
        self._policy.authorize(step, target.metadata)
        if step.parser is ReadParser.TABLE_ROWS:
            rows = await target.locator.evaluate(_TABLE_ROWS_JS)
            return [self._normalize_row(row) for row in rows]
        if step.parser is ReadParser.FIELDS:
            pairs = await target.locator.evaluate("""(table) => {
              const result = {};
              for (const header of table.querySelectorAll('th')) {
                const cell = header.nextElementSibling;
                if (cell && cell.tagName === 'TD') {
                  result[header.innerText.trim()] = cell.innerText.trim();
                }
              }
              return result;
            }""")
            result = self._normalize_row(pairs)
            if not result:
                text = await target.locator.evaluate("""el => {
                    const paragraph = el.closest('p');
                    return (paragraph || el).innerText.trim();
                }""")
                for label, value in re.findall(
                    r"(Member ID|Start date|End date|Amount|Currency|Direction|Reference)"
                    r"\s*:?\s*([^;]+)",
                    text,
                ):
                    result[_HEADER_KEYS[label.lower()]] = (
                        "" if value.strip() == "Any" else value.strip()
                    )
            if "source" in result:
                result["source"] = result["source"].lower()
            result["observation_id"] = self._latest_observation_id or "observation-unrecorded"
            return result
        text = (await target.locator.inner_text()).strip()
        if step.parser is ReadParser.TEXT:
            return text
        if step.parser is ReadParser.EXACT_MONEY:
            try:
                money = Decimal(text.replace(",", "").removeprefix("$"))
            except InvalidOperation as error:
                raise RunStopped(
                    FailureCode.CHECKPOINT_FAILED,
                    step.step_id,
                    "exact money",
                    "unparseable value",
                ) from error
            return f"{money:.2f}"
        if step.parser is ReadParser.DATE:
            try:
                return date.fromisoformat(text)
            except ValueError as error:
                raise RunStopped(
                    FailureCode.CHECKPOINT_FAILED,
                    step.step_id,
                    "ISO transaction date",
                    "unparseable value",
                ) from error
        if step.parser is ReadParser.ENUM:
            return text.upper()
        raise AssertionError("unreachable read parser")

    async def _approved_action(
        self,
        locator: Locator,
        operation: Callable[[], Awaitable[object]],
        *,
        navigates: bool = False,
    ) -> None:
        handle = await locator.element_handle()
        if handle is None:
            raise RunStopped(
                FailureCode.CHECKPOINT_FAILED, None, "attached target", "target detached"
            )
        try:
            await handle.evaluate("el => { el.__casetraceApprovedAction = true; }")
            frame = await handle.owner_frame()
            if navigates and frame is not None:
                async with frame.expect_navigation(
                    wait_until="load", timeout=self.action_timeout_ms
                ):
                    await operation()
            else:
                await operation()
        finally:
            try:
                await handle.evaluate("el => { delete el.__casetraceApprovedAction; }")
            except Exception:
                pass  # A successful navigation discards the old document and its gate.
            await handle.dispose()

    async def read_text(
        self, target_id: str, args: PaymentQuery, variables: dict[str, object]
    ) -> str:
        target = await self._resolve(target_id, args, variables)
        if not self._policy.request_allowed(target.metadata.page_url, "GET", "document"):
            raise RunStopped(
                FailureCode.POLICY_DENIED,
                None,
                "reviewed page route",
                "page route denied",
            )
        return (await target.locator.inner_text()).strip()

    async def check(
        self, predicate: Predicate, args: PaymentQuery, variables: dict[str, object]
    ) -> bool:
        if isinstance(predicate, (VisiblePredicate, AbsentPredicate, CountPredicate)):
            count = await self._target_count(predicate.target_id, args, variables)
            if isinstance(predicate, VisiblePredicate):
                return count > 0
            if isinstance(predicate, AbsentPredicate):
                return count == 0
            operators = {
                CountOperator.EQ: lambda a, b: a == b,
                CountOperator.NE: lambda a, b: a != b,
                CountOperator.LT: lambda a, b: a < b,
                CountOperator.LTE: lambda a, b: a <= b,
                CountOperator.GT: lambda a, b: a > b,
                CountOperator.GTE: lambda a, b: a >= b,
            }
            return operators[predicate.operator](count, predicate.expected)
        if isinstance(predicate, EqualsPredicate):
            return resolve_value(predicate.left, args, variables) == resolve_value(
                predicate.right, args, variables
            )
        if isinstance(predicate, AllPredicate):
            return all([await self.check(item, args, variables) for item in predicate.predicates])
        if isinstance(predicate, AnyPredicate):
            return any([await self.check(item, args, variables) for item in predicate.predicates])
        raise TypeError("unsupported predicate")

    async def capture(self) -> dict[str, object]:
        observation = await self.observe()
        screen = str(observation.state["screen"])
        snapshot = observation.model_dump(mode="json") | {
            "frames": [self._safe_route(frame.url) for frame, _ in self._visible_frames()],
            "screen": screen,
        }
        if screen in {"member_search", "member_summary", "account_activity", "transaction_detail"}:
            workspace = self._page.locator('iframe[title="Bank workspace"]')
            if await workspace.count() == 1 and await workspace.is_visible():
                image = await self._page.screenshot(mask=[workspace], animations="disabled")
                return {
                    "snapshot": snapshot,
                    "masked_screenshot": image,
                    "fully_masked": True,
                }
        return {"snapshot": snapshot, "fully_masked": False}

    async def set_human_control(self, enabled: bool) -> None:
        async with self._ownership_lock:
            self._human_control = enabled
            self._ownership_generation += 1
            try:
                async with asyncio.timeout(self.action_timeout_ms / 1000):
                    for frame in self._page.frames:
                        await frame.evaluate(
                            "state => globalThis.__casetraceSetOwnership(state)",
                            self._ownership_state(),
                        )
            except BaseException:
                # Losing the document while changing ownership cannot leave a live open gate.
                self._human_control = False
                self._ownership_generation += 1
                await self._context.close()
                raise

    def _ownership_state(self) -> dict[str, object]:
        return {"enabled": self._human_control, "generation": self._ownership_generation}

    async def verify_resume(
        self,
        *,
        browser_id: str,
        context_id: str,
        page_id: str,
        vendor: str,
        app_version: str,
    ) -> tuple[bool, str]:
        """Validate the visible restart checkpoint without navigating or reading cookies."""
        if (
            (browser_id, context_id, page_id) != (self.browser_id, self.context_id, self.page_id)
            or self._original_session != (self._browser, self._context, self._page)
            or not self._browser.is_connected()
            or self._context.browser is not self._browser
            or self._page.context is not self._context
            or self._page.is_closed()
        ):
            return False, "original browser session unavailable"
        if self._human_control:
            return False, "manual input must be quiesced before verification"
        if self._native_dialog_present:
            return False, "dialog requires intervention"
        try:
            await self._validate_open_surfaces()
            if await self._visible_identity() != (vendor, app_version):
                return False, "unsupported application identity"
            frames = self._visible_frames()
            for frame, _ in frames:
                if (
                    await frame.get_by_role("dialog").count()
                    or await frame.get_by_role("alertdialog").count()
                ):
                    return False, "dialog requires intervention"
            if await self._recognized_screen() != "member_search":
                return False, "return to authenticated member search"
            workspace = self._page.locator('iframe[title="Bank workspace"]')
            if await workspace.count() != 1 or not await workspace.is_visible():
                return False, "recognized bank workspace required"
            if len(frames) != 2:
                return False, "unexpected browser frame"
            frame = next(frame for frame, _ in frames if frame is not self._page.main_frame)
            if urlsplit(frame.url).path != "/bank/members":
                return False, "return to member search root"
            required = [
                frame.get_by_text("Signed in as: demo-operator", exact=True),
                frame.get_by_role("heading", name="Member search", exact=True),
                frame.get_by_role("textbox", name="Member ID", exact=True),
                frame.get_by_role("button", name="Search", exact=True),
            ]
            if not all([await item.count() == 1 and await item.is_visible() for item in required]):
                return False, "authenticated member search required"
            return True, "authenticated member search verified"
        except Exception:
            return False, "browser checkpoint unavailable"

    def manual_events(self) -> list[dict[str, str | bool]]:
        return list(self._manual_events)

    async def _resolve(
        self, target_id: str, args: PaymentQuery, variables: dict[str, object]
    ) -> _BrowserTarget:
        if self._latest_observation_id is not None:
            observed = self._observed.get(self._latest_observation_id, {}).get(target_id)
            if observed is not None:
                if not await observed.locator.is_visible():
                    raise RunStopped(
                        FailureCode.CHECKPOINT_FAILED,
                        None,
                        "fresh observed target",
                        "stale target handle",
                    )
                return await self._with_metadata(target_id, observed.locator, 1)
        try:
            spec = self._targets[target_id]
        except KeyError as error:
            raise RunStopped(
                FailureCode.CHECKPOINT_FAILED,
                None,
                "declared target",
                "unknown target",
            ) from error
        candidates = await self._strategy_candidates(spec, args, variables)
        unique = await self._agreeing_unique(candidates)
        if unique is None:
            count = max([await locator.count() for locator in candidates], default=0)
            code = FailureCode.LOCATOR_AMBIGUOUS if count > 1 else FailureCode.CHECKPOINT_FAILED
            raise RunStopped(code, None, "unique target", "target count mismatch")
        return await self._with_metadata(target_id, unique, 1)

    async def _target_count(
        self, target_id: str, args: PaymentQuery, variables: dict[str, object]
    ) -> int:
        try:
            spec = self._targets[target_id]
        except KeyError:
            if self._latest_observation_id is None:
                return 0
            observed = self._observed.get(self._latest_observation_id, {}).get(target_id)
            return int(observed is not None and await observed.locator.is_visible())
        candidates = await self._strategy_candidates(spec, args, variables)
        return max([await candidate.count() for candidate in candidates], default=0)

    async def _strategy_candidates(
        self, spec: TargetSpec, args: PaymentQuery, variables: dict[str, object]
    ) -> list[Locator]:
        scope: Page | FrameLocator | Locator = self._page
        for frame_name in spec.frame_path:
            scope = scope.frame_locator(
                f'iframe[title="{self._css_escape(frame_name)}"], '
                f'iframe[name="{self._css_escape(frame_name)}"]'
            )
        if spec.container:
            scope = scope.get_by_role("table", name=spec.container, exact=True)
        candidates: list[Locator] = []
        for strategy in spec.strategies:
            if isinstance(strategy, AccessibleRoleStrategy):
                name = self._resolve_text(strategy.name, args, variables)
                candidates.append(scope.get_by_role(strategy.role, name=name, exact=True))
            elif isinstance(strategy, VisibleTextStrategy):
                text = self._resolve_text(strategy.text, args, variables)
                candidates.append(scope.get_by_text(text, exact=strategy.exact))
            elif isinstance(strategy, AdjacentControlStrategy):
                label = self._resolve_text(strategy.label, args, variables)
                candidates.append(scope.get_by_label(label, exact=True))
                selector = {
                    "textbox": "input:not([type=hidden]),textarea",
                    "combobox": "select",
                    "button": "button,input[type=submit]",
                }.get(strategy.control_role, "[role=none]")
                candidates.append(
                    scope.locator("th")
                    .filter(has_text=re.compile(r"^\s*" + re.escape(label) + r"\s*$"))
                    .locator("xpath=following-sibling::td[1]")
                    .locator(selector)
                )
            elif isinstance(strategy, TableRelationStrategy):
                row_value = self._resolve_text(strategy.row_value, args, variables)
                table = scope
                if not spec.container:
                    table = scope.get_by_role("table").filter(
                        has=scope.get_by_role("columnheader", name=strategy.header, exact=True)
                    )
                row = table.get_by_role("row").filter(has_text=row_value)
                candidates.append(
                    row.get_by_role(strategy.control_role) if strategy.control_role else row
                )
        return candidates

    @staticmethod
    async def _agreeing_unique(candidates: list[Locator]) -> Locator | None:
        unique: list[Locator] = []
        for locator in candidates:
            count = await locator.count()
            if count > 1:
                return None
            if count == 1:
                unique.append(locator)
        if not unique:
            return None
        first_handle = await unique[0].element_handle()
        if first_handle is None:
            return None
        try:
            for locator in unique[1:]:
                other = await locator.element_handle()
                if other is None:
                    return None
                try:
                    if not await unique[0].evaluate("(el, other) => el === other", other):
                        return None
                finally:
                    await other.dispose()
            return unique[0]
        finally:
            await first_handle.dispose()

    async def _with_metadata(self, target_id: str, locator: Locator, count: int) -> _BrowserTarget:
        info = await locator.evaluate(_CONTROL_INFO_JS)
        handle = await locator.element_handle()
        frame = await handle.owner_frame() if handle else None
        if handle:
            await handle.dispose()
        if frame is None:
            raise RunStopped(
                FailureCode.CHECKPOINT_FAILED, None, "attached target", "target detached"
            )
        page_url = frame.url
        destination = info.get("href") or info.get("formAction")
        method = "GET" if info.get("href") else info.get("formMethod")
        meaning = self._control_meaning(page_url, info)
        return _BrowserTarget(
            locator=locator,
            metadata=ResolvedTarget(
                target_id=target_id,
                count=count,
                control_meaning=meaning,
                page_url=page_url,
                form_action=destination,
                form_method=method,
            ),
        )

    def _control_meaning(self, page_url: str, info: Mapping[str, Any]) -> str:
        path = urlsplit(page_url).path
        name = str(info.get("name") or "")
        label = str(info.get("label") or "").strip().lower()
        destination = str(info.get("href") or info.get("formAction") or "")
        if path == "/bank/members":
            if name == "member_id":
                return "member.search.member_id"
            if label == "search" and urlsplit(destination).path == "/bank/members/search":
                return "member.search.submit"
        if _MEMBER_ROUTE.fullmatch(path):
            if label == "transfer funds" or urlsplit(destination).path.endswith("/transfer"):
                return "financial.transfer"
            if label == "open" and urlsplit(destination).path.endswith("/activity"):
                return "member.account.open"
        if _ACTIVITY_ROUTE.fullmatch(path):
            if name in {"date_from", "date_to", "amount", "currency", "direction", "reference"}:
                return f"activity.filter.{name}"
            if name == "source" or label in {"history", "pending"}:
                return "activity.source"
            if label == "apply filters" and urlsplit(destination).path == path:
                return "activity.filter.submit"
            if label in {"next", "previous"}:
                return f"activity.pagination.{label}"
            if label == "view" and _DETAIL_ROUTE.fullmatch(urlsplit(destination).path):
                return "activity.transaction.view"
            if label == "back to member":
                return "navigation.back"
        if _DETAIL_ROUTE.fullmatch(path) and label == "back to activity":
            return "navigation.back"
        if path == "/bank/login":
            if name == "username":
                return "authentication.username"
            if name == "password":
                return "authentication.password"
            if label in {"sign in", "login"}:
                return "authentication.submit"
        return "unknown"

    async def _guard_request(self, route: Route) -> None:
        request = route.request
        if self._policy.request_allowed(request.url, request.method, request.resource_type):
            await route.continue_()
        else:
            self._blocked_request = True
            await route.abort("blockedbyclient")

    async def _reject_popup(self, popup: Page) -> None:
        self._blocked_request = True
        await popup.close()

    async def _validate_open_surfaces(self, step_id: str | None = None) -> None:
        if self._blocked_request:
            raise RunStopped(
                FailureCode.POLICY_DENIED, step_id, "reviewed requests", "request denied"
            )
        if len(self._context.pages) != 1 or self._page.is_closed():
            raise RunStopped(
                FailureCode.POLICY_DENIED,
                step_id,
                "single controlled page",
                "popup or page loss detected",
            )
        for frame in self._page.frames:
            if frame.url == "about:blank":
                continue
            if not self._policy.request_allowed(frame.url, "GET", "document"):
                raise RunStopped(
                    FailureCode.POLICY_DENIED,
                    step_id,
                    "reviewed frame origin and route",
                    "frame denied",
                )

    def _visible_frames(self) -> list[tuple[Frame, list[str]]]:
        result: list[tuple[Frame, list[str]]] = [(self._page.main_frame, [])]
        for frame in self._page.frames:
            if frame == self._page.main_frame:
                continue
            result.append((frame, [frame.name or "Bank workspace"]))
        return result

    async def _visible_identity(self) -> tuple[str | None, str | None]:
        text = await self._page.locator("body").inner_text()
        vendor_match = re.search(r"Vendor:\s*([^\r\n]+)", text)
        version_match = re.search(r"Application version:\s*([^\r\n]+)", text)
        if vendor_match is None:
            title = (await self._page.title()).strip()
            vendor = title or None
        else:
            vendor = vendor_match.group(1).strip()
        return vendor, version_match.group(1).strip() if version_match else None

    async def _recognized_screen(self) -> str | None:
        headings: set[str] = set()
        for frame, _ in self._visible_frames():
            values = await frame.get_by_role("heading", level=1).all_inner_texts()
            headings.update(value.strip() for value in values)
        if "Member search" in headings:
            frame = next(
                (item for item, _ in self._visible_frames() if item != self._page.main_frame), None
            )
            if frame and await frame.get_by_text("Deposit accounts", exact=True).count():
                return "member_summary"
            return "member_search"
        if "Account activity" in headings:
            return "account_activity"
        if "Transaction details" in headings:
            return "transaction_detail"
        if "Sign in" in headings or "Demo sign in" in headings:
            return "authentication"
        return None

    async def _record_manual_event(self, metadata: object) -> None:
        if not isinstance(metadata, Mapping):
            return
        event = {
            "event_type": self._sanitize_text(str(metadata.get("event_type", "unknown"))),
            "role": self._sanitize_text(str(metadata.get("role", "unknown"))),
            "label": self._sanitize_text(str(metadata.get("label", ""))),
            "blocked": bool(metadata.get("blocked", False)),
        }
        self._manual_events.append(event)
        if self._manual_event_handler is not None:
            result = self._manual_event_handler(event)
            if asyncio.iscoroutine(result):
                await result

    def _sanitize_text(self, value: str) -> str:
        safe = value
        for sensitive in sorted(self._sensitive_values, key=len, reverse=True):
            safe = safe.replace(sensitive, "[REDACTED]")
        for pattern in _SENSITIVE_TEXT:
            safe = pattern.sub("[REDACTED]", safe)
        return safe[:200]

    @staticmethod
    def _safe_route(url: str) -> str:
        path = urlsplit(url).path
        if path == "/":
            return "shell"
        if path == "/bank/members":
            return "member_search"
        if _MEMBER_ROUTE.fullmatch(path):
            return "member_summary"
        if _ACTIVITY_ROUTE.fullmatch(path):
            return "account_activity"
        if _DETAIL_ROUTE.fullmatch(path):
            return "transaction_detail"
        if path == "/bank/login":
            return "authentication"
        return "unknown"

    @staticmethod
    def _resolve_text(value: object, args: PaymentQuery, variables: dict[str, object]) -> str:
        resolved = resolve_value(value, args, variables) if not isinstance(value, str) else value
        return resolved.isoformat() if isinstance(resolved, date) else str(resolved)

    @staticmethod
    def _normalize_row(row: Mapping[str, object]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for header, value in row.items():
            key = _HEADER_KEYS.get(header.strip().lower())
            if key is None:
                key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
            normalized[key] = str(value)
        return normalized

    @staticmethod
    def _css_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
