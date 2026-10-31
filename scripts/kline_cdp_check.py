"""Inspect the local dashboard through Chrome DevTools Protocol."""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket


class CdpClient:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(url, timeout=20)
        self.sequence = 0
        self.events: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if message.get("method") in {
                "Runtime.consoleAPICalled",
                "Runtime.exceptionThrown",
                "Log.entryAdded",
            }:
                self.events.append(message)

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        exception = result.get("exceptionDetails")
        if exception:
            description = exception.get("exception", {}).get("description")
            raise RuntimeError(description or exception.get("text") or json.dumps(exception))
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


def get_page(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    for target in targets:
        if target.get("type") == "page" and target.get("url") == "http://127.0.0.1:8000/":
            return target
    raise RuntimeError("Dashboard page target not found")


def wait_for_chart(client: CdpClient, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.evaluate(
            """(() => {
              const container = document.querySelector('#kline-container');
              const runButton = document.querySelector('.run-button');
              return {
                hasContainer: Boolean(container),
                canvasCount: container?.querySelectorAll('canvas').length || 0,
                runText: runButton?.textContent?.trim() || '',
                bodyText: document.body.innerText,
              };
            })()"""
        )
        if state["hasContainer"] and state["canvasCount"] > 0:
            return
        if "回放" not in state["runText"] and "validation" in state["runText"]:
            client.evaluate("document.querySelector('.run-button')?.click()")
        time.sleep(0.4)
    raise TimeoutError("K-line canvas did not appear")


def inspect_state(client: CdpClient) -> dict[str, Any]:
    return client.evaluate(
        """(() => {
          const container = document.querySelector('#kline-container');
          const chartWrap = document.querySelector('.chart-wrap');
          const findChartComponent = (vnode) => {
            if (!vnode || typeof vnode !== 'object') return null;
            if (vnode.component?.exposed?.getViewState) return vnode.component;
            if (vnode.component) {
              const child = findChartComponent(vnode.component.subTree);
              if (child) return child;
            }
            const children = Array.isArray(vnode.children) ? vnode.children : [];
            for (const child of children) {
              const found = findChartComponent(child);
              if (found) return found;
            }
            return null;
          };
          const rootElement = document.querySelector('#app');
          const app = rootElement?.__vue_app__;
          const chartComponent = findChartComponent(rootElement?._vnode || app?._instance?.subTree);
          const rect = element => {
            const value = element?.getBoundingClientRect();
            return value ? {
              x: value.x, y: value.y, width: value.width, height: value.height,
            } : null;
          };
          return {
            viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
            container: rect(container),
            chartWrap: rect(chartWrap),
            canvases: [...(container?.querySelectorAll('canvas') || [])].map(canvas => ({
              width: canvas.width,
              height: canvas.height,
              cssWidth: canvas.clientWidth,
              cssHeight: canvas.clientHeight,
            })),
            viewState: chartComponent?.exposed?.getViewState?.() || null,
            barsLabel: document.querySelector('.chart-toolbar .panel-note')?.textContent?.trim(),
            timeline: document.querySelector('.timeline-meta')?.textContent?.trim(),
            title: document.querySelector('.panel-title')?.textContent?.trim(),
          };
        })()"""
    )


def capture(client: CdpClient, output: Path) -> None:
    rect = client.evaluate(
        """(() => {
          const wrap = document.querySelector('.chart-wrap');
          const value = wrap.getBoundingClientRect();
          return {
            x: Math.max(0, value.x),
            y: value.y + scrollY,
            width: value.width,
            height: value.height,
            scale: 1,
          };
        })()"""
    )
    screenshot = client.call(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": rect,
        },
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(screenshot["data"]))


def wait_for_value(
    client: CdpClient,
    expression: str,
    predicate: Any,
    timeout: float = 10,
) -> Any:
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = client.evaluate(expression)
        if predicate(last_value):
            return last_value
        time.sleep(0.15)
    raise TimeoutError(f"Timed out waiting for {expression}; last value={last_value!r}")


def get_view_state(client: CdpClient) -> dict[str, Any]:
    return client.evaluate(
        """(() => {
          const findChartComponent = vnode => {
            if (!vnode || typeof vnode !== 'object') return null;
            if (vnode.component?.exposed?.getViewState) return vnode.component;
            if (vnode.component) {
              const nested = findChartComponent(vnode.component.subTree);
              if (nested) return nested;
            }
            for (const child of Array.isArray(vnode.children) ? vnode.children : []) {
              const found = findChartComponent(child);
              if (found) return found;
            }
            return null;
          };
          const component = findChartComponent(document.querySelector('#app')?._vnode);
          return component?.exposed?.getViewState?.() || null;
        })()"""
    )


def click_chart_button(client: CdpClient, title: str) -> None:
    clicked = client.evaluate(
        f"""(() => {{
          const button = [...document.querySelectorAll('.chart-zoom-group button')]
            .find(item => item.title === {json.dumps(title, ensure_ascii=False)});
          if (!button) return false;
          button.click();
          return true;
        }})()"""
    )
    if not clicked:
        raise RuntimeError(f"Chart button not found: {title}")
    time.sleep(0.25)


def select_code(client: CdpClient, code: str) -> None:
    opened = client.evaluate(
        """(() => {
          const select = document.querySelector('.chart-toolbar .el-select__wrapper');
          if (!select) return false;
          select.click();
          return true;
        })()"""
    )
    if not opened:
        raise RuntimeError("ETF select was not found")
    time.sleep(0.25)
    selected = client.evaluate(
        f"""(() => {{
          const options = [...document.querySelectorAll('.el-select-dropdown__item')]
            .filter(item => item.offsetParent !== null);
          const option = options.find(item => item.textContent.includes({json.dumps(code)}));
          if (!option) return false;
          option.click();
          return true;
        }})()"""
    )
    if not selected:
        raise RuntimeError(f"ETF option not found: {code}")
    wait_for_value(
        client,
        f"""(() => {{
          const title = document.querySelector('.panel-title')?.textContent || '';
          return {{ title, hasCode: title.includes({json.dumps(code)}) }};
        }})()""",
        lambda value: value and value["hasCode"],
    )
    wait_for_value(
        client,
        """(() => {
          const label = document.querySelector('.chart-toolbar .panel-note')?.textContent || '';
          return Number.parseInt(label, 10) || 0;
        })()""",
        lambda value: value > 0,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = get_view_state(client)
        if state and state["activeCode"] == code and state["dataCount"] > 0:
            return
        time.sleep(0.15)
    raise TimeoutError(f"Chart did not switch to ETF {code}")


def set_timeline(client: CdpClient, index: int | None = None) -> str:
    target = client.evaluate(
        f"""(() => {{
          const slider = document.querySelector('.timeline-range');
          if (!slider) return null;
          const value = {index if index is not None else 'Number(slider.max)'};
          const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype, 'value'
          ).set;
          setter.call(slider, String(value));
          slider.dispatchEvent(new Event('input', {{ bubbles: true }}));
          return document.querySelector('.timeline-meta span')?.textContent || '';
        }})()"""
    )
    if target is None:
        raise RuntimeError("Timeline slider was not found")
    wait_for_value(
        client,
        """(() => ({
          label: document.querySelector('.timeline-meta span')?.textContent || '',
          state: (() => {
            const find = vnode => {
              if (!vnode || typeof vnode !== 'object') return null;
              if (vnode.component?.exposed?.getViewState) return vnode.component;
              if (vnode.component) {
                const nested = find(vnode.component.subTree);
                if (nested) return nested;
              }
              for (const child of Array.isArray(vnode.children) ? vnode.children : []) {
                const found = find(child);
                if (found) return found;
              }
              return null;
            };
            return find(document.querySelector('#app')?._vnode)?.exposed?.getViewState?.() || null;
          })(),
        }))()""",
        lambda value: value["state"] and value["state"]["lastDate"] == value["label"],
    )
    return client.evaluate(
        "document.querySelector('.timeline-meta span')?.textContent || ''"
    )


def wheel_zoom(client: CdpClient, delta: int) -> None:
    rect = client.evaluate(
        """(() => {
          const value = document.querySelector('#kline-container').getBoundingClientRect();
          return { x: value.x + value.width / 2, y: value.y + value.height / 2 };
        })()"""
    )
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]},
    )
    client.call(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseWheel",
            "x": rect["x"],
            "y": rect["y"],
            "deltaX": 0,
            "deltaY": delta,
        },
    )
    time.sleep(0.35)


def drag_chart(client: CdpClient, distance: int) -> None:
    rect = client.evaluate(
        """(() => {
          const value = document.querySelector('#kline-container').getBoundingClientRect();
          return {
            x: value.x + value.width / 2,
            y: value.y + value.height / 2,
          };
        })()"""
    )
    x, y = rect["x"], rect["y"]
    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": x + distance, "y": y, "button": "left"},
    )
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x + distance, "y": y, "button": "left"},
    )
    time.sleep(0.35)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9224)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("M2/reports/figures/dashboard_kline_replay.png"),
    )
    parser.add_argument("--no-run", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--full-check", action="store_true")
    args = parser.parse_args()

    page = get_page(args.port)
    client = CdpClient(page["webSocketDebuggerUrl"])
    try:
        client.call("Runtime.enable")
        client.call("Log.enable")
        client.call("Page.enable")
        if args.reload:
            client.call("Page.reload", {"ignoreCache": True})
            time.sleep(1.5)
            client.events = []
        if not args.no_run:
            client.evaluate("document.querySelector('.run-button')?.click()")
        wait_for_chart(client)
        time.sleep(1)
        state = inspect_state(client)
        checks: dict[str, Any] = {}
        measurements: dict[str, Any] = {}
        if args.full_check:
            initial = get_view_state(client)
            if not initial:
                raise RuntimeError("Could not read chart state")

            click_chart_button(client, "缩小")
            zoomed_out = get_view_state(client)
            click_chart_button(client, "放大")
            zoomed_in = get_view_state(client)
            wheel_zoom(client, -180)
            wheel_state = get_view_state(client)
            before_drag = wheel_state["visibleRange"]["from"]
            drag_chart(client, 150)
            dragged = get_view_state(client)

            timeline_target = set_timeline(client, 150)
            timeline_state = get_view_state(client)
            client.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1200,
                    "height": 1000,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            time.sleep(0.5)
            resized = get_view_state(client)
            client.call("Emulation.clearDeviceMetricsOverride")
            time.sleep(0.5)

            select_code(client, "511360")
            set_timeline(client)
            zero_trade_state = get_view_state(client)

            select_code(client, "515230")
            set_timeline(client)
            many_trade_state = get_view_state(client)

            select_code(client, "511220")
            set_timeline(client)
            click_chart_button(client, "复位缩放")
            final_state = get_view_state(client)
            state = inspect_state(client)

            checks = {
                "buttonZoomOut": zoomed_out["barSpace"] < initial["barSpace"],
                "buttonZoomIn": zoomed_in["barSpace"] > zoomed_out["barSpace"],
                "wheelZoom": wheel_state["barSpace"] != zoomed_in["barSpace"],
                "dragPan": dragged["visibleRange"]["from"] != before_drag,
                "timelineApplied": timeline_state["lastDate"] == timeline_target,
                "timelineKeptZoom": abs(timeline_state["barSpace"] - wheel_state["barSpace"]) < 0.01,
                "resizeAdjustedCanvas": resized["size"]["width"] != wheel_state["size"]["width"],
                "etfSwitched": "511220" in state["title"],
                "zeroTradeOrdersFiltered": zero_trade_state["overlayCount"] == 0,
                "manyRealTradesShown": many_trade_state["overlayCount"] == 11,
                "markersPresent": final_state["overlayCount"] == len(final_state["markerLabels"]) > 0,
                "markersUseCurrentEtf": (
                    final_state["activeCode"] == "511220"
                    and all(label in {"B", "S", "X"} for label in final_state["markerLabels"])
                ),
                "dataComplete": final_state["dataCount"] == 302,
                "resetZoom": abs(final_state["barSpace"] - 8) < 0.01,
            }
            measurements = {
                "initial": initial,
                "afterButtonZoomOut": zoomed_out,
                "afterButtonZoomIn": zoomed_in,
                "afterWheelZoom": wheel_state,
                "afterDrag": dragged,
                "afterTimeline": timeline_state,
                "afterResize": resized,
                "zeroTradeEtf": zero_trade_state,
                "manyTradeEtf": many_trade_state,
                "final": final_state,
            }
            if not all(checks.values()):
                raise AssertionError(json.dumps(checks, ensure_ascii=False, indent=2))

        capture(client, args.output)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        if checks:
            print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
        if measurements:
            print(json.dumps({"measurements": measurements}, ensure_ascii=False, indent=2))
        print(f"screenshot={args.output.resolve()}")
        print(f"eventCount={len(client.events)}")
        for event in client.events:
            print(json.dumps(event, ensure_ascii=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
