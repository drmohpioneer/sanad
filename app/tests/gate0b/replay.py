"""Deterministic dashboard replay and screenshot capture for Gate 0B.

This module deliberately lives in the test tree.  It reads, but never edits,
``app/web/dashboard.html`` and serves its real JavaScript against the committed
per-beat API goldens.  The only response-time addition is a small replay
bootstrap which fixes time and selects the useful view for each beat.

Run from the repository root::

    PYTHONPATH=app .venv/bin/python -m tests.gate0b.replay serve
    PYTHONPATH=app .venv/bin/python -m tests.gate0b.replay capture

The capture command uses an installed Chromium/Google Chrome binary directly;
it does not need Playwright, a user profile, a signed-in browser, or network.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import html
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
from threading import Lock, Thread
import time
from typing import Any, Final
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import urlopen

from .artifacts import write_json


HERE: Final = Path(__file__).resolve().parent
APP_DIR: Final = HERE.parents[1]
DASHBOARD_HTML: Final = APP_DIR / "web" / "dashboard.html"
BEATS_DIR: Final = HERE / "goldens" / "beats"
SCREENSHOTS_DIR: Final = HERE / "goldens" / "screenshots"
TOKEN_PREFIX: Final = "gate0b--"
TIMEZONE: Final = "Africa/Cairo"
LOCALE: Final = "en-US"

VIEWPORTS: Final = ((375, 812), (1440, 1000))
ATTESTATION_WIDTH: Final = 16
ATTESTATION_HEIGHT: Final = 8
ATTESTATION_INSET: Final = 2
CAPTURE_TOOL: Final = (
    "Chrome DevTools Protocol device emulation via tests.gate0b.replay"
)

# Response policy is intentionally strict.  In particular, the three Google
# Fonts links in the unchanged dashboard cannot make a request during replay.
CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'none'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)


@dataclass(frozen=True)
class Selection:
    """The real dashboard state that makes one beat legible in a viewport."""

    view: str
    patient_name: str | None = None
    scroll_selector: str | None = None
    scroll_text: str | None = None
    note: str = ""


# Every screenshot is intentional rather than nine copies of the board.  The
# selected text is asserted by the browser bootstrap before it marks ready.
SELECTIONS: Final[dict[str, Selection]] = {
    "beat-01-contract": Selection(
        "patient",
        patient_name="Ahmed Ali",
        note="The confirmed four-loop care contract.",
    ),
    "beat-02-durable-future": Selection(
        "patient",
        patient_name="Ahmed Ali",
        scroll_selector=".ncard.white",
        scroll_text="unreachable",
        note="The future ladder exhausted without losing the loops.",
    ),
    "beat-03-cost-barrier": Selection(
        "patient",
        patient_name="Ahmed Ali",
        scroll_selector=".bubble",
        scroll_text="hospital lab is free",
        note="The doctor's answer resumes the cost-blocked loop.",
    ),
    "beat-04-incomplete-evidence": Selection(
        "patient",
        patient_name="Ahmed Ali",
        scroll_selector=".bubble",
        scroll_text="Triglycerides, HDL is missing",
        note="Incomplete evidence stays open and asks only for what is missing.",
    ),
    "beat-05-complete-evidence": Selection(
        "patient",
        patient_name="Ahmed Ali",
        scroll_selector=".ncard.red",
        scroll_text="needs your review",
        note="Complete evidence advances the loop to pending review.",
    ),
    "beat-06-critical-potassium": Selection(
        "inbox",
        scroll_selector=".ncard.red",
        scroll_text="CRITICAL LAB",
        note="The critical potassium result is held at the top of the inbox.",
    ),
    "beat-07-contact-guard": Selection(
        "patient",
        patient_name="Amany Roushdy",
        scroll_selector=".ncard.yellow",
        scroll_text="Barrier needs you",
        note="The six-contact guard escalates without inventing another schedule.",
    ),
    "beat-08-doctor-review": Selection(
        "patient",
        patient_name="Ahmed Ali",
        scroll_selector=".ncard.green",
        scroll_text="Lipid panel",
        note="Doctor review closes the evidence loop.",
    ),
    "beat-09-end-of-day": Selection(
        "board",
        note="The final away-board totals and attention count.",
    ),
}


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _load_fixture(slug: str) -> dict[str, Any]:
    if slug not in SELECTIONS:
        raise KeyError(f"unknown Gate 0B beat: {slug}")
    path = BEATS_DIR / f"{slug}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _dashboard_monitor_patient_id(board: dict[str, Any]) -> str | None:
    """Mirror dashboard.html refreshMonitor's first-candidate rule."""
    patients = board.get("patients") or []
    candidates = [
        patient for patient in patients
        if any(
            str(loop.get("type") or "").upper() == "MONITOR"
            and loop.get("state") != "done"
            for loop in patient.get("loops") or []
        )
    ]
    pick = candidates[0] if candidates else next(
        (
            patient for patient in patients
            if any(
                str(loop.get("type") or "").upper() == "MONITOR"
                for loop in patient.get("loops") or []
            )
        ),
        None,
    )
    return str(pick.get("id")) if pick and pick.get("id") else None


def _qr_bytes(fixture: dict[str, Any]) -> tuple[str, bytes, str] | None:
    qr_link = fixture["api"]["board"].get("qr")
    if not qr_link:
        return None
    qr = fixture["api"].get("qr")
    if not isinstance(qr, dict):
        raise ValueError(
            f"{fixture['label']}: board renders {qr_link.get('url')!r}, but the QR "
            "response is absent from the API golden; regenerate Gate 0B JSON"
        )
    raw = base64.b64decode(str(qr.get("base64") or ""), validate=True)
    path = str(qr.get("path") or "")
    content_type = str(qr.get("content_type") or "")
    if path != qr_link.get("url"):
        raise ValueError(f"{fixture['label']}: QR path disagrees with board fixture")
    if content_type != "image/png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{fixture['label']}: QR fixture is not an image/png")
    if len(raw) != qr.get("bytes") or sha256(raw).hexdigest() != qr.get("sha256"):
        raise ValueError(f"{fixture['label']}: QR fixture bytes/hash do not verify")
    return path, raw, content_type


def _validate_fixture(slug: str, fixture: dict[str, Any]) -> None:
    if fixture.get("label") != slug:
        raise ValueError(f"{slug}: fixture label drifted to {fixture.get('label')!r}")
    monitor_id = _dashboard_monitor_patient_id(fixture["api"]["board"])
    if monitor_id and monitor_id not in fixture["api"].get("patients", {}):
        raise ValueError(
            f"{slug}: dashboard boot requires monitor patient {monitor_id!r}, but its "
            "detail response is absent from api.patients; regenerate Gate 0B JSON"
        )
    _qr_bytes(fixture)


_CAPTURE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _capture_id(value: str | None) -> str | None:
    return value if value and _CAPTURE_ID.fullmatch(value) else None


def _attestation_tag(session_nonce: str, capture_id: str, slug: str) -> str:
    return sha256(
        f"sanad-gate0b-attestation/v2\0{session_nonce}\0{capture_id}\0{slug}".encode()
    ).hexdigest()[:32]


def _fixed_time_ms(fixture: dict[str, Any]) -> int:
    captured_at = str(fixture["captured_at"])
    return int(datetime.fromisoformat(captured_at).timestamp() * 1000)


def _patient_id(fixture: dict[str, Any], name: str | None) -> str | None:
    if name is None:
        return None
    matches = [
        patient_id
        for patient_id, payload in fixture["api"]["patients"].items()
        if payload.get("patient", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one patient named {name!r}, got {matches!r}")
    return matches[0]


def _bootstrap_javascript(
    slug: str,
    fixture: dict[str, Any],
    capture_id: str,
    session_nonce: str,
) -> bytes:
    selection = SELECTIONS[slug]
    config = {
        "beat": slug,
        "fixed_time_ms": _fixed_time_ms(fixture),
        "timezone": TIMEZONE,
        "locale": LOCALE,
        "capture_id": capture_id,
        "attestation_tag": _attestation_tag(session_nonce, capture_id, slug),
        "view": selection.view,
        "patient_id": _patient_id(fixture, selection.patient_name),
        "patient_name": selection.patient_name,
        "scroll_selector": selection.scroll_selector,
        "scroll_text": selection.scroll_text,
        "expected_images": 1 if fixture["api"].get("qr") is not None else 0,
        "note": selection.note,
    }
    config_json = json.dumps(config, ensure_ascii=True, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    # This script is loaded before the dashboard's own classic script.  Its
    # load handler runs afterwards, when the dashboard's global lexical state
    # and functions exist.  Real setTimeout remains available; only the app's
    # recurring two-second poll is suppressed.
    script = f"""
"use strict";
(function gate0bBootstrap() {{
  const config = {config_json};
  const NativeDate = window.Date;
  const nativeSetTimeout = window.setTimeout.bind(window);
  const nativeMatchMedia = window.matchMedia ? window.matchMedia.bind(window) : null;

  class Gate0BDate extends NativeDate {{
    constructor(...args) {{
      if (args.length === 1 && typeof args[0] === "string" &&
          /^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}$/.test(args[0])) {{
        // The dashboard has one intentionally-local parser in fmtDayShort().
        // Make its Cairo interpretation independent of the browser host zone.
        super(args[0] + "+03:00");
      }} else {{
        super(...(args.length ? args : [config.fixed_time_ms]));
      }}
    }}
    static now() {{ return config.fixed_time_ms; }}
    static parse(value) {{ return NativeDate.parse(value); }}
    static UTC(...args) {{ return NativeDate.UTC(...args); }}
    _cairo() {{ return new NativeDate(this.getTime() + 3 * 60 * 60 * 1000); }}
    getHours() {{ return this._cairo().getUTCHours(); }}
    getMinutes() {{ return this._cairo().getUTCMinutes(); }}
    getDay() {{ return this._cairo().getUTCDay(); }}
    getDate() {{ return this._cairo().getUTCDate(); }}
    getMonth() {{ return this._cairo().getUTCMonth(); }}
    getFullYear() {{ return this._cairo().getUTCFullYear(); }}
    getTimezoneOffset() {{ return -180; }}
    setHours(hour, minute, second, millisecond) {{
      const shifted = this._cairo();
      shifted.setUTCHours(
        hour,
        minute === undefined ? shifted.getUTCMinutes() : minute,
        second === undefined ? shifted.getUTCSeconds() : second,
        millisecond === undefined ? shifted.getUTCMilliseconds() : millisecond
      );
      return this.setTime(shifted.getTime() - 3 * 60 * 60 * 1000);
    }}
    toDateString() {{
      const cairo = this._cairo();
      const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
      const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      return days[cairo.getUTCDay()] + " " + months[cairo.getUTCMonth()] + " " +
        String(cairo.getUTCDate()).padStart(2, "0") + " " + cairo.getUTCFullYear();
    }}
  }}
  Object.setPrototypeOf(Gate0BDate, NativeDate);
  window.Date = Gate0BDate;
  window.setInterval = function gate0bNoRecurringPoll() {{ return 0; }};
  window.clearInterval = function gate0bClearRecurringPoll() {{}};
  window.matchMedia = function gate0bMatchMedia(query) {{
    const original = nativeMatchMedia ? nativeMatchMedia(query) : null;
    const reduce = String(query).includes("prefers-reduced-motion");
    const dark = String(query).includes("prefers-color-scheme") && String(query).includes("dark");
    if (!reduce && !dark && original) return original;
    return {{
      matches: reduce,
      media: String(query),
      onchange: null,
      addListener: function() {{}},
      removeListener: function() {{}},
      addEventListener: function() {{}},
      removeEventListener: function() {{}},
      dispatchEvent: function() {{ return false; }}
    }};
  }};
  const motionStyle = document.createElement("style");
  motionStyle.id = "gate0b-reduced-motion";
  motionStyle.textContent = "*,*::before,*::after{{animation:none!important;transition:none!important;scroll-behavior:auto!important;caret-color:transparent!important}}";
  document.head.appendChild(motionStyle);
  document.documentElement.style.colorScheme = "light";
  try {{ window.localStorage.clear(); }} catch (error) {{}}
  window.__SANAD_GATE0B_REPLAY__ = Object.freeze(config);

  function normalized(value) {{
    return String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
  }}

  function findScrollTarget() {{
    if (!config.scroll_selector) return null;
    const wanted = normalized(config.scroll_text);
    return Array.from(document.querySelectorAll(config.scroll_selector)).find(function(node) {{
      return !wanted || normalized(node.textContent).includes(wanted);
    }}) || null;
  }}

  function rendered(node) {{
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility === "visible" &&
      Number(style.opacity) > 0;
  }}

  function renderedAncestorChain(node, stop) {{
    let current = node;
    while (current) {{
      const style = window.getComputedStyle(current);
      if (current.hidden || style.display === "none" ||
          style.visibility !== "visible" || Number(style.opacity) <= 0 ||
          style.contentVisibility === "hidden") {{
        return false;
      }}
      if (current === stop) return true;
      current = current.parentElement;
    }}
    return false;
  }}

  function observeView(target) {{
    const viewport = window.visualViewport || {{
      width: window.innerWidth,
      height: window.innerHeight,
      offsetLeft: 0,
      offsetTop: 0
    }};
    const topbar = document.querySelector(".topbar");
    const topbarStyle = topbar ? window.getComputedStyle(topbar) : null;
    const sticky = topbarStyle &&
      (topbarStyle.position === "sticky" || topbarStyle.position === "fixed");
    const occlusionBottom = Math.max(
      0, Math.ceil(sticky ? topbar.getBoundingClientRect().bottom : 0)
    );
    const sections = Array.from(document.querySelectorAll("main > section.view"));
    const visibleViews = sections.filter(rendered).map(function(node) {{
      return String(node.id || "").replace(/^view-/, "");
    }});
    const expected = document.getElementById("view-" + config.view);
    const currentNavs = Array.from(
      document.querySelectorAll('[data-nav][aria-current="page"]')
    ).map(function(node) {{ return String(node.dataset.nav || ""); }});
    const navCurrent = config.view === "patient"
      ? currentNavs.length === 0
      : currentNavs.length === 1 && currentNavs[0] === config.view;
    const stateView = typeof S !== "undefined" ? String(S.view || "") : "";
    const patientId = config.view === "patient" && typeof S !== "undefined"
      ? String(S.patientId || "") : "";
    const patientName = config.view === "patient" && typeof S !== "undefined"
      ? String((S.patient && S.patient.patient && S.patient.patient.name) || "") : "";
    const patientHeading = config.view === "patient" && expected
      ? Array.from(expected.querySelectorAll("h1")).find(function(node) {{
          return normalized(node.textContent) === normalized(config.patient_name);
        }}) || null
      : null;
    const patientHeadingMatches = config.view !== "patient" || Boolean(patientHeading);
    const anchor = target
      ? (evidenceNode(target) || target)
      : config.view === "patient"
        ? patientHeading
        : expected && expected.querySelector("#h-ex");
    const anchorKind = target
      ? "selected-evidence"
      : config.view === "patient" ? "patient-heading" : "board-heading";
    const rect = anchor ? anchor.getBoundingClientRect() : null;
    const style = anchor ? window.getComputedStyle(anchor) : null;
    const x = rect ? rect.left + rect.width / 2 : -1;
    const y = rect ? rect.top + rect.height / 2 : -1;
    const hit = rect ? document.elementFromPoint(x, y) : null;
    const unobscured = Boolean(
      anchor && hit && (hit === anchor || anchor.contains(hit))
    );
    const ancestorChainVisible = Boolean(
      expected && anchor && expected.contains(anchor) &&
      renderedAncestorChain(anchor, document.documentElement)
    );
    const anchorVisible = Boolean(
      expected && anchor && expected.contains(anchor) && rect && style &&
      rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility === "visible" &&
      Number(style.opacity) > 0 &&
      rect.left >= viewport.offsetLeft &&
      rect.right <= viewport.offsetLeft + viewport.width &&
      rect.top >= occlusionBottom + 1 && rect.bottom <= viewport.height &&
      unobscured && ancestorChainVisible
    );
    const proof = {{
      observed_view: visibleViews.length === 1 ? visibleViews[0] : "",
      visible_views: visibleViews,
      state_view: stateView,
      nav_current: navCurrent,
      patient_id: patientId,
      patient_name: patientName,
      patient_heading_matches: patientHeadingMatches,
      anchor_ancestor_chain_visible: ancestorChainVisible,
      anchor_kind: anchorKind,
      anchor_text: normalized(anchor && anchor.textContent),
      anchor_geometry: {{
        top: rect ? Math.round(rect.top) : -1,
        bottom: rect ? Math.round(rect.bottom) : -1,
        left: rect ? Math.round(rect.left) : -1,
        right: rect ? Math.round(rect.right) : -1,
        occlusion_bottom: occlusionBottom,
        visual_width: Math.round(viewport.width),
        visual_height: Math.round(viewport.height),
        unobscured: unobscured
      }}
    }};
    const expectedAnchorText = normalized(
      config.scroll_text || config.patient_name || "Exception line"
    );
    const valid = visibleViews.length === 1 &&
      proof.observed_view === config.view && stateView === config.view &&
      navCurrent && patientHeadingMatches && anchorVisible &&
      (!expectedAnchorText || proof.anchor_text.includes(expectedAnchorText)) &&
      (config.view !== "patient" || (
        patientId === String(config.patient_id || "") &&
        normalized(patientName) === normalized(config.patient_name)
      ));
    if (!valid) {{
      throw new Error("Gate 0B observed view mismatch: " + JSON.stringify(proof));
    }}
    return proof;
  }}

  function markReady(geometry, viewProof) {{
    const root = document.documentElement;
    root.dataset.gate0bReady = "true";
    root.dataset.gate0bBeat = config.beat;
    root.dataset.gate0bView = viewProof.observed_view;
    root.dataset.gate0bPatient = viewProof.patient_name;
    root.dataset.gate0bFixedTime = String(config.fixed_time_ms);
    root.dataset.gate0bTimezone = config.timezone;
    root.dataset.gate0bLocale = config.locale;
    root.dataset.gate0bCapture = config.capture_id;
    root.dataset.gate0bColorScheme = "light";
    root.dataset.gate0bMotion = "reduced";
    root.dataset.gate0bTarget = geometry.visibility;
    const marker = document.createElement("span");
    marker.id = "gate0b-replay-ready";
    marker.hidden = true;
    marker.textContent = config.beat;
    document.body.appendChild(marker);
    document.title = "GATE0B_READY " + config.beat + " · " + document.title;
  }}

  async function waitForImages() {{
    const images = Array.from(document.images);
    if (images.length !== config.expected_images) {{
      throw new Error(
        "Gate 0B image count mismatch: " + images.length +
        " observed, " + config.expected_images + " expected"
      );
    }}
    await Promise.all(images.map(async function(image) {{
      if (!image.complete) {{
        await new Promise(function(resolve, reject) {{
          image.addEventListener("load", resolve, {{once: true}});
          image.addEventListener("error", function() {{
            reject(new Error("Gate 0B image failed: " + (image.currentSrc || image.src)));
          }}, {{once: true}});
        }});
      }}
      if (!image.complete || image.naturalWidth <= 0 || image.naturalHeight <= 0) {{
        throw new Error("Gate 0B image decoded empty: " + (image.currentSrc || image.src));
      }}
      if (typeof image.decode === "function") {{
        try {{
          await image.decode();
        }} catch (error) {{
          throw new Error("Gate 0B image decode failed: " + (image.currentSrc || image.src));
        }}
      }}
    }}));
    return {{
      expected: config.expected_images,
      observed: images.length,
      decoded: images.length,
      failures: 0
    }};
  }}

  function evidenceNode(target) {{
    if (!target || !config.scroll_text) return target;
    const wanted = normalized(config.scroll_text);
    const candidates = [target].concat(Array.from(target.querySelectorAll("*"))).filter(function(node) {{
      return normalized(node.textContent).includes(wanted);
    }});
    candidates.sort(function(left, right) {{
      return normalized(left.textContent).length - normalized(right.textContent).length;
    }});
    return candidates[0] || null;
  }}

  function targetGeometry(target) {{
    const topbar = document.querySelector(".topbar");
    const topbarStyle = topbar ? window.getComputedStyle(topbar) : null;
    const sticky = topbarStyle && (topbarStyle.position === "sticky" || topbarStyle.position === "fixed");
    const occlusionBottom = Math.max(
      0,
      Math.ceil(sticky ? topbar.getBoundingClientRect().bottom : 0)
    );
    const viewport = window.visualViewport || {{
      width: window.innerWidth,
      height: window.innerHeight,
      offsetLeft: 0,
      offsetTop: 0
    }};
    if (!target) {{
      return {{
        visibility: "none",
        top: -1, bottom: -1, left: -1, right: -1,
        evidence_top: -1, evidence_bottom: -1,
        evidence_left: -1, evidence_right: -1,
        occlusion_bottom: occlusionBottom,
        visual_width: Math.round(viewport.width),
        visual_height: Math.round(viewport.height),
        unobscured: true
      }};
    }}
    const rect = target.getBoundingClientRect();
    const evidence = evidenceNode(target);
    const evidenceRect = evidence ? evidence.getBoundingClientRect() : null;
    const style = window.getComputedStyle(target);
    const x = evidenceRect ? evidenceRect.left + evidenceRect.width / 2 : -1;
    const y = evidenceRect ? evidenceRect.top + evidenceRect.height / 2 : -1;
    const hit = document.elementFromPoint(x, y);
    const unobscured = Boolean(
      evidence && hit && (hit === evidence || evidence.contains(hit))
    );
    const visibleHeight = Math.max(
      0,
      Math.min(rect.bottom, viewport.height) - Math.max(rect.top, occlusionBottom)
    );
    const visible = (
      rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility === "visible" && Number(style.opacity) > 0 &&
      rect.left >= viewport.offsetLeft && rect.right <= viewport.offsetLeft + viewport.width &&
      rect.top >= occlusionBottom + 8 &&
      visibleHeight >= Math.min(rect.height, 48) &&
      evidenceRect !== null && evidenceRect.width > 0 && evidenceRect.height > 0 &&
      evidenceRect.left >= viewport.offsetLeft &&
      evidenceRect.right <= viewport.offsetLeft + viewport.width &&
      evidenceRect.top >= occlusionBottom + 1 && evidenceRect.bottom <= viewport.height &&
      unobscured
    );
    return {{
      visibility: visible ? "visible" : "hidden",
      top: Math.round(rect.top),
      bottom: Math.round(rect.bottom),
      left: Math.round(rect.left),
      right: Math.round(rect.right),
      evidence_top: evidenceRect ? Math.round(evidenceRect.top) : -1,
      evidence_bottom: evidenceRect ? Math.round(evidenceRect.bottom) : -1,
      evidence_left: evidenceRect ? Math.round(evidenceRect.left) : -1,
      evidence_right: evidenceRect ? Math.round(evidenceRect.right) : -1,
      occlusion_bottom: occlusionBottom,
      visual_width: Math.round(viewport.width),
      visual_height: Math.round(viewport.height),
      unobscured: unobscured
    }};
  }}

  async function assertCleanReplay(geometry, images, viewProof) {{
    const statusResponse = await fetch(
      "/__gate0b__/status?capture_id=" + encodeURIComponent(config.capture_id),
      {{headers: {{"Accept": "application/json"}}}}
    );
    if (!statusResponse.ok) throw new Error("Gate 0B status endpoint failed");
    const status = await statusResponse.json();
    if ((status.failures || []).length) {{
      throw new Error("Gate 0B replay route failure: " + JSON.stringify(status.failures));
    }}
    const ready = new URLSearchParams({{
      capture_id: config.capture_id,
      beat: config.beat,
      view: viewProof.observed_view,
      visible_views: viewProof.visible_views.join(","),
      state_view: viewProof.state_view,
      nav_current: viewProof.nav_current ? "true" : "false",
      patient_id: viewProof.patient_id,
      patient_name: viewProof.patient_name,
      patient_heading_matches: viewProof.patient_heading_matches ? "true" : "false",
      anchor_ancestor_chain_visible: viewProof.anchor_ancestor_chain_visible ? "true" : "false",
      anchor_kind: viewProof.anchor_kind,
      anchor_text: viewProof.anchor_text,
      anchor_top: String(viewProof.anchor_geometry.top),
      anchor_bottom: String(viewProof.anchor_geometry.bottom),
      anchor_left: String(viewProof.anchor_geometry.left),
      anchor_right: String(viewProof.anchor_geometry.right),
      anchor_occlusion_bottom: String(viewProof.anchor_geometry.occlusion_bottom),
      anchor_visual_width: String(viewProof.anchor_geometry.visual_width),
      anchor_visual_height: String(viewProof.anchor_geometry.visual_height),
      anchor_unobscured: viewProof.anchor_geometry.unobscured ? "true" : "false",
      target: geometry.visibility,
      target_top: String(geometry.top),
      target_bottom: String(geometry.bottom),
      target_left: String(geometry.left),
      target_right: String(geometry.right),
      evidence_top: String(geometry.evidence_top),
      evidence_bottom: String(geometry.evidence_bottom),
      evidence_left: String(geometry.evidence_left),
      evidence_right: String(geometry.evidence_right),
      occlusion_bottom: String(geometry.occlusion_bottom),
      visual_width: String(geometry.visual_width),
      visual_height: String(geometry.visual_height),
      unobscured: geometry.unobscured ? "true" : "false",
      images_expected: String(images.expected),
      images_observed: String(images.observed),
      images_decoded: String(images.decoded),
      image_failures: String(images.failures),
      width: String(window.innerWidth),
      height: String(window.innerHeight),
      dpr: String(window.devicePixelRatio),
      attestation: config.attestation_tag
    }});
    const readyResponse = await fetch("/__gate0b__/ready?" + ready.toString());
    if (!readyResponse.ok) throw new Error("Gate 0B readiness callback failed");
  }}

  function installAttestation() {{
    const marker = document.createElement("canvas");
    marker.id = "gate0b-capture-attestation";
    marker.width = {ATTESTATION_WIDTH};
    marker.height = {ATTESTATION_HEIGHT};
    marker.setAttribute("aria-hidden", "true");
    marker.style.cssText = [
      "position:fixed", "right:{ATTESTATION_INSET}px", "bottom:{ATTESTATION_INSET}px",
      "width:{ATTESTATION_WIDTH}px", "height:{ATTESTATION_HEIGHT}px",
      "z-index:2147483647", "pointer-events:none",
      "image-rendering:pixelated"
    ].join(";");
    const context = marker.getContext("2d", {{alpha: false}});
    const bits = Array.from(config.attestation_tag).flatMap(function(nibble) {{
      const value = parseInt(nibble, 16);
      return [3, 2, 1, 0].map(function(shift) {{ return (value >> shift) & 1; }});
    }});
    bits.forEach(function(bit, index) {{
      context.fillStyle = bit ? "#fff" : "#000";
      context.fillRect(index % {ATTESTATION_WIDTH}, Math.floor(index / {ATTESTATION_WIDTH}), 1, 1);
    }});
    document.body.appendChild(marker);
  }}

  async function selectCaptureState() {{
    for (let attempt = 0; attempt < 240; attempt += 1) {{
      try {{
        if (typeof S !== "undefined" && S.board && S.settings && S.summary && S.syncedAt && !S.polling) {{
          if (config.view === "patient") {{
            await openPatient(config.patient_id);
          }} else {{
            setView(config.view);
          }}
          render();
          const images = await waitForImages();
          const target = findScrollTarget();
          if (config.scroll_selector && !target) {{
            throw new Error("Gate 0B scroll target missing: " + config.scroll_text);
          }}
          if (target) {{
            target.scrollIntoView({{behavior: "auto", block: "start", inline: "nearest"}});
            const topbar = document.querySelector(".topbar");
            const clearance = Math.ceil(
              topbar ? topbar.getBoundingClientRect().bottom : 0
            ) + 12;
            window.scrollBy(0, -clearance);
          }} else {{
            window.scrollTo({{top: 0, behavior: "auto"}});
          }}
          await new Promise(function(resolve) {{
            window.requestAnimationFrame(function() {{ window.requestAnimationFrame(resolve); }});
          }});
          const geometry = targetGeometry(target);
          if (config.scroll_selector && geometry.visibility !== "visible") {{
            throw new Error(
              "Gate 0B target is not visibly below the sticky header: " +
              JSON.stringify(geometry)
            );
          }}
          const viewProof = observeView(target);
          installAttestation();
          await new Promise(function(resolve) {{ nativeSetTimeout(resolve, 50); }});
          await assertCleanReplay(geometry, images, viewProof);
          markReady(geometry, viewProof);
          return;
        }}
      }} catch (error) {{
        document.documentElement.dataset.gate0bError = String(error && error.message || error);
        if (String(error && error.message || error).startsWith("Gate 0B")) return;
      }}
      await new Promise(function(resolve) {{ nativeSetTimeout(resolve, 25); }});
    }}
    document.documentElement.dataset.gate0bError = "dashboard did not become ready";
  }}

  window.addEventListener("load", function() {{ void selectCaptureState(); }}, {{once: true}});
}})();
"""
    return script.encode("utf-8")


def _dashboard_with_bootstrap(bootstrap_src: str) -> tuple[bytes, str]:
    source = DASHBOARD_HTML.read_bytes()
    digest = sha256(source).hexdigest()
    marker = b'<script>\n"use strict";'
    if source.count(marker) != 1:
        raise RuntimeError("dashboard bootstrap insertion point drifted")
    safe_src = html.escape(bootstrap_src, quote=True).encode("ascii")
    injected = b'<script src="' + safe_src + b'"></script>\n' + marker
    return source.replace(marker, injected, 1), digest


class ReplayHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying immutable fixtures and request observations."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], receipt_path: Path | None = None) -> None:
        fixtures = {slug: _load_fixture(slug) for slug in SELECTIONS}
        for slug, fixture in fixtures.items():
            _validate_fixture(slug, fixture)
        dashboard_source = DASHBOARD_HTML.read_bytes()
        super().__init__(address, ReplayHandler)
        self.fixtures = fixtures
        self.dashboard_sha256 = sha256(dashboard_source).hexdigest()
        self.replay_helper_sha256 = sha256(Path(__file__).read_bytes()).hexdigest()
        self.request_paths: list[dict[str, Any]] = []
        self.failures: dict[str, list[dict[str, Any]]] = {}
        self.ready: dict[str, dict[str, Any]] = {}
        self.capture_session_nonce = secrets.token_hex(32)
        self.observation_lock = Lock()
        self.receipt_path = receipt_path
        with self.observation_lock:
            self._persist_receipts_locked()

    def _persist_receipts_locked(self) -> None:
        if self.receipt_path is None:
            return
        write_json(
            self.receipt_path,
            {
                "schema": "sanad-gate0b-browser-receipts/v3",
                "synthetic": True,
                "dashboard_sha256": self.dashboard_sha256,
                "replay_helper_sha256": self.replay_helper_sha256,
                "capture_session_nonce": self.capture_session_nonce,
                "ready": dict(sorted(self.ready.items())),
                "failures": {
                    key: value for key, value in sorted(self.failures.items())
                },
            },
        )

    def observe(self, capture_id: str | None, path: str, status: int) -> None:
        row = {"capture_id": capture_id, "path": path, "status": status}
        with self.observation_lock:
            self.request_paths.append(row)
            if capture_id and status >= 400:
                self.failures.setdefault(capture_id, []).append(row)
                self._persist_receipts_locked()

    def capture_status(self, capture_id: str) -> dict[str, Any]:
        with self.observation_lock:
            return {
                "capture_id": capture_id,
                "failures": list(self.failures.get(capture_id, [])),
                "ready": dict(self.ready[capture_id]) if capture_id in self.ready else None,
            }

    def mark_ready(self, capture_id: str, row: dict[str, Any]) -> None:
        with self.observation_lock:
            if capture_id in self.ready:
                raise ValueError(f"duplicate readiness callback for {capture_id}")
            self.ready[capture_id] = dict(row)
            self._persist_receipts_locked()

    def mark_captured(
        self,
        capture_id: str,
        metrics: dict[str, Any],
        png_sha256: str,
        capture_identity: dict[str, str],
    ) -> None:
        with self.observation_lock:
            if capture_id not in self.ready:
                raise ValueError(f"capture completed before readiness for {capture_id}")
            if any(
                field in self.ready[capture_id]
                for field in ("capture_metrics", "png_sha256", "capture_identity")
            ):
                raise ValueError(f"duplicate screenshot capture for {capture_id}")
            if re.fullmatch(r"[0-9a-f]{64}", png_sha256) is None:
                raise ValueError(f"invalid screenshot SHA-256 for {capture_id}")
            self.ready[capture_id]["capture_metrics"] = dict(metrics)
            self.ready[capture_id]["png_sha256"] = png_sha256
            self.ready[capture_id]["capture_identity"] = dict(capture_identity)
            self._persist_receipts_locked()


class ReplayHandler(BaseHTTPRequestHandler):
    server: ReplayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        # Capture output is a checksum ledger, not a stream of HTTP noise.
        return

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        capture_id = self._request_capture_id()
        self.server.observe(capture_id, self.path, int(status))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, _canonical_bytes(payload), "application/json; charset=utf-8")

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def _cookie_context(self) -> tuple[str | None, str | None]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("gate0b_context")
        if not morsel or "~" not in morsel.value:
            return None, None
        slug, capture_id = morsel.value.split("~", 1)
        return self._known_slug(slug), _capture_id(capture_id)

    def _request_capture_id(self) -> str | None:
        queried = self._query().get("capture_id", [None])[0]
        return _capture_id(queried) or self._cookie_context()[1]

    @staticmethod
    def _token_slug(parts: list[str]) -> str | None:
        if len(parts) < 3 or parts[1] != "c" or not parts[2].startswith(TOKEN_PREFIX):
            return None
        return parts[2][len(TOKEN_PREFIX) :]

    def _known_slug(self, candidate: str | None) -> str | None:
        return candidate if candidate in self.server.fixtures else None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = parse_qs(split.query, keep_blank_values=True)
        parts = path.split("/")
        token_slug = self._known_slug(self._token_slug(parts))
        cookie_slug, cookie_capture = self._cookie_context()

        if path == "/__gate0b__/bootstrap.js":
            slug = self._known_slug(query.get("beat", [None])[0]) or cookie_slug
            capture_id = _capture_id(query.get("capture_id", [None])[0]) or cookie_capture
            if slug is None or capture_id is None:
                self._json(
                    {"error": "missing Gate 0B bootstrap context"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send(
                HTTPStatus.OK,
                _bootstrap_javascript(
                    slug,
                    self.server.fixtures[slug],
                    capture_id,
                    self.server.capture_session_nonce,
                ),
                "text/javascript; charset=utf-8",
            )
            return

        if path == "/__gate0b__/status":
            capture_id = _capture_id(query.get("capture_id", [None])[0])
            if capture_id is None:
                self._json({"error": "invalid capture_id"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(self.server.capture_status(capture_id))
            return

        if path == "/__gate0b__/ready":
            capture_id = _capture_id(query.get("capture_id", [None])[0])
            slug = self._known_slug(query.get("beat", [None])[0])
            view = query.get("view", [""])[0]
            visible_views = [
                name for name in query.get("visible_views", [""])[0].split(",")
                if name
            ]
            state_view = query.get("state_view", [""])[0]
            nav_current = query.get("nav_current", [""])[0] == "true"
            patient_id = query.get("patient_id", [""])[0]
            patient_name = query.get("patient_name", [""])[0]
            patient_heading_matches = (
                query.get("patient_heading_matches", [""])[0] == "true"
            )
            anchor_ancestor_chain_visible = (
                query.get("anchor_ancestor_chain_visible", [""])[0] == "true"
            )
            anchor_kind = query.get("anchor_kind", [""])[0]
            anchor_text = query.get("anchor_text", [""])[0]
            target = query.get("target", [""])[0]
            attestation = query.get("attestation", [""])[0]
            try:
                viewport = (
                    int(query.get("width", [""])[0]),
                    int(query.get("height", [""])[0]),
                )
                dpr = float(query.get("dpr", [""])[0])
                target_top = int(query.get("target_top", [""])[0])
                target_bottom = int(query.get("target_bottom", [""])[0])
                target_left = int(query.get("target_left", [""])[0])
                target_right = int(query.get("target_right", [""])[0])
                evidence_top = int(query.get("evidence_top", [""])[0])
                evidence_bottom = int(query.get("evidence_bottom", [""])[0])
                evidence_left = int(query.get("evidence_left", [""])[0])
                evidence_right = int(query.get("evidence_right", [""])[0])
                occlusion_bottom = int(query.get("occlusion_bottom", [""])[0])
                visual_viewport = (
                    int(query.get("visual_width", [""])[0]),
                    int(query.get("visual_height", [""])[0]),
                )
                unobscured = query.get("unobscured", [""])[0] == "true"
                anchor_geometry = {
                    "top": int(query.get("anchor_top", [""])[0]),
                    "bottom": int(query.get("anchor_bottom", [""])[0]),
                    "left": int(query.get("anchor_left", [""])[0]),
                    "right": int(query.get("anchor_right", [""])[0]),
                    "occlusion_bottom": int(
                        query.get("anchor_occlusion_bottom", [""])[0]
                    ),
                    "visual_width": int(
                        query.get("anchor_visual_width", [""])[0]
                    ),
                    "visual_height": int(
                        query.get("anchor_visual_height", [""])[0]
                    ),
                    "unobscured": (
                        query.get("anchor_unobscured", [""])[0] == "true"
                    ),
                }
                image_report = {
                    "expected": int(query.get("images_expected", [""])[0]),
                    "observed": int(query.get("images_observed", [""])[0]),
                    "decoded": int(query.get("images_decoded", [""])[0]),
                    "failures": int(query.get("image_failures", [""])[0]),
                }
            except ValueError:
                viewport, dpr = (0, 0), 0.0
                target_top, target_bottom, occlusion_bottom = 0, 0, -1
                target_left, target_right = 0, 0
                evidence_top, evidence_bottom = 0, 0
                evidence_left, evidence_right = 0, 0
                visual_viewport = (0, 0)
                unobscured = False
                anchor_geometry = {
                    "top": 0, "bottom": 0, "left": 0, "right": 0,
                    "occlusion_bottom": -1,
                    "visual_width": 0, "visual_height": 0,
                    "unobscured": False,
                }
                image_report = {"expected": -1, "observed": -1, "decoded": -1, "failures": -1}
            if capture_id is None or slug is None:
                self._json({"error": "invalid readiness context"}, HTTPStatus.BAD_REQUEST)
                return
            selection = SELECTIONS[slug]
            expected_target = "visible" if selection.scroll_selector else "none"
            expected_attestation = _attestation_tag(
                self.server.capture_session_nonce, capture_id, slug
            )
            expected_images = 1 if _qr_bytes(self.server.fixtures[slug]) is not None else 0
            expected_patient_id = _patient_id(
                self.server.fixtures[slug], selection.patient_name
            ) or ""
            expected_patient_name = selection.patient_name or ""
            expected_anchor_kind = (
                "selected-evidence" if selection.scroll_selector
                else "patient-heading" if selection.view == "patient"
                else "board-heading"
            )
            expected_anchor_text = _normalized_text(
                selection.scroll_text or selection.patient_name or "Exception line"
            )
            anchor_matches = (
                anchor_geometry["visual_width"] == viewport[0]
                and anchor_geometry["visual_height"] == viewport[1]
                and 0 <= anchor_geometry["occlusion_bottom"] < viewport[1]
                and anchor_geometry["top"]
                    >= anchor_geometry["occlusion_bottom"] + 1
                and anchor_geometry["bottom"] <= viewport[1]
                and anchor_geometry["bottom"] > anchor_geometry["top"]
                and anchor_geometry["left"] >= 0
                and anchor_geometry["right"] <= viewport[0]
                and anchor_geometry["right"] > anchor_geometry["left"]
                and anchor_geometry["unobscured"] is True
            )
            view_matches = (
                view == selection.view
                and visible_views == [selection.view]
                and state_view == selection.view
                and nav_current
                and patient_id == expected_patient_id
                and _normalized_text(patient_name)
                    == _normalized_text(expected_patient_name)
                and patient_heading_matches
                and anchor_ancestor_chain_visible
                and anchor_kind == expected_anchor_kind
                and expected_anchor_text in _normalized_text(anchor_text)
                and anchor_matches
            )
            geometry_matches = (
                target == "visible"
                and 0 <= occlusion_bottom < viewport[1]
                and target_top >= occlusion_bottom + 8
                and target_left >= 0
                and target_right <= viewport[0]
                and target_right > target_left
                and min(target_bottom, viewport[1]) - max(target_top, occlusion_bottom)
                    >= min(target_bottom - target_top, 48)
                and evidence_top >= occlusion_bottom + 1
                and evidence_bottom <= viewport[1]
                and evidence_bottom > evidence_top
                and evidence_left >= 0
                and evidence_right <= viewport[0]
                and evidence_right > evidence_left
                and unobscured
            ) if selection.scroll_selector else (
                target == "none"
                and target_top == -1
                and target_bottom == -1
                and target_left == -1
                and target_right == -1
                and evidence_top == -1
                and evidence_bottom == -1
                and evidence_left == -1
                and evidence_right == -1
                and 0 <= occlusion_bottom < viewport[1]
                and unobscured
            )
            if (
                not view_matches
                or target != expected_target
                or not geometry_matches
                or viewport not in VIEWPORTS
                or visual_viewport != viewport
                or dpr != 1.0
                or attestation != expected_attestation
                or image_report != {
                    "expected": expected_images,
                    "observed": expected_images,
                    "decoded": expected_images,
                    "failures": 0,
                }
            ):
                self._json(
                    {
                        "error": "readiness selection mismatch",
                        "expected": {
                            "view": selection.view,
                            "visible_views": [selection.view],
                            "state_view": selection.view,
                            "patient_id": expected_patient_id,
                            "patient_name": expected_patient_name,
                            "anchor_kind": expected_anchor_kind,
                            "anchor_text_contains": expected_anchor_text,
                            "target": expected_target,
                            "viewports": VIEWPORTS,
                            "dpr": 1,
                            "attestation": expected_attestation,
                            "visual_viewport": viewport,
                            "images": {
                                "expected": expected_images,
                                "observed": expected_images,
                                "decoded": expected_images,
                                "failures": 0,
                            },
                            "target_geometry": (
                                "visible, below sticky occlusion, and inside viewport"
                                if selection.scroll_selector
                                else "no selected target"
                            ),
                        },
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if self.server.capture_status(capture_id)["failures"]:
                self._json(
                    {"error": "replay had failed requests before readiness"},
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                self.server.mark_ready(
                    capture_id,
                    {
                        "beat": slug,
                        "view": view,
                        "view_proof": {
                            "observed_view": view,
                            "visible_views": visible_views,
                            "state_view": state_view,
                            "nav_current": nav_current,
                            "patient_id": patient_id,
                            "patient_name": patient_name,
                            "patient_heading_matches": patient_heading_matches,
                            "anchor_ancestor_chain_visible": (
                                anchor_ancestor_chain_visible
                            ),
                            "anchor_kind": anchor_kind,
                            "anchor_text": anchor_text,
                            "anchor_geometry": anchor_geometry,
                        },
                        "target": target,
                        "target_geometry": {
                            "top": target_top,
                            "bottom": target_bottom,
                            "left": target_left,
                            "right": target_right,
                            "evidence_top": evidence_top,
                            "evidence_bottom": evidence_bottom,
                            "evidence_left": evidence_left,
                            "evidence_right": evidence_right,
                            "occlusion_bottom": occlusion_bottom,
                            "visual_width": visual_viewport[0],
                            "visual_height": visual_viewport[1],
                            "unobscured": unobscured,
                        },
                        "images": image_report,
                        "width": viewport[0],
                        "height": viewport[1],
                        "dpr": dpr,
                        "attestation_tag": expected_attestation,
                    },
                )
            except ValueError as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
            return

        if token_slug is not None and len(parts) == 4 and parts[3] == "app":
            requested_capture = _capture_id(query.get("capture_id", [None])[0])
            capture_id = requested_capture or secrets.token_hex(12)
            bootstrap_src = (
                "/__gate0b__/bootstrap.js?beat=" + quote(token_slug, safe="")
                + "&capture_id=" + quote(capture_id, safe="")
            )
            dashboard, digest = _dashboard_with_bootstrap(bootstrap_src)
            if digest != self.server.dashboard_sha256:
                raise RuntimeError("dashboard changed while replay server was running")
            self._send(
                HTTPStatus.OK,
                dashboard,
                "text/html; charset=utf-8",
                headers={
                    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
                    "Set-Cookie": (
                        f"gate0b_context={token_slug}~{capture_id}; "
                        "Path=/; SameSite=Strict"
                    ),
                    "X-Gate0B-Dashboard-SHA256": self.server.dashboard_sha256,
                    "X-Gate0B-Beat": token_slug,
                    "X-Gate0B-Capture": capture_id,
                },
            )
            return

        slug = token_slug or cookie_slug
        if slug is None:
            self._json({"error": "unknown or missing Gate 0B beat"}, HTTPStatus.NOT_FOUND)
            return
        api = self.server.fixtures[slug]["api"]

        if path == "/health":
            self._json(api["health"])
            return
        qr = _qr_bytes(self.server.fixtures[slug])
        if qr is not None and path == qr[0]:
            self._send(HTTPStatus.OK, qr[1], qr[2])
            return
        if token_slug is not None and len(parts) >= 4:
            route = parts[3]
            if route in {"board", "cards", "feed", "reports", "settings", "summary"}:
                self._json(api[route])
                return
            if route == "patient" and len(parts) == 5:
                patient_id = parts[4]
                patient = api["patients"].get(patient_id)
                if patient is not None:
                    self._json(patient)
                    return
        self._json({"error": "route is outside the read-only replay"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._json(
            {"error": "Gate 0B replay is read-only"},
            HTTPStatus.METHOD_NOT_ALLOWED,
        )


def _server(port: int, receipt_path: Path | None = None) -> ReplayHTTPServer:
    return ReplayHTTPServer(("127.0.0.1", port), receipt_path=receipt_path)


def _serve_forever(port: int, receipt_path: Path | None) -> None:
    server = _server(port, receipt_path)
    host, actual_port = server.server_address
    print(f"Gate 0B replay: http://{host}:{actual_port}/c/{TOKEN_PREFIX}beat-01-contract/app")
    print(
        f"Readiness receipts: {receipt_path}"
        if receipt_path is not None
        else "Readiness receipts: disabled for inspection"
    )
    print("Ctrl-C stops the local, read-only fixture server.")
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _find_chrome(explicit: str | None) -> Path:
    candidates = [
        explicit,
        os.environ.get("SANAD_GATE0B_CHROME"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError(
        "Chrome/Chromium was not found; pass --chrome or SANAD_GATE0B_CHROME"
    )


def _chrome_common(
    chrome: Path,
    profile: Path,
) -> list[str]:
    return [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--metrics-recording-only",
        "--hide-scrollbars",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        f"--lang={LOCALE}",
        "--force-device-scale-factor=1",
        "--force-color-profile=srgb",
        "--force-prefers-reduced-motion",
        "--run-all-compositor-stages-before-draw",
        "--js-flags=--random-seed=230",
        "--window-size=1440,1000",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "about:blank",
    ]


def _run_chrome(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment["TZ"] = TIMEZONE
    environment["LANG"] = "en_US.UTF-8"
    environment["LC_ALL"] = "en_US.UTF-8"
    return subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=environment,
    )


class _ChromeDevTools:
    """Small, local-only CDP client for exact CSS viewport screenshot capture."""

    def __init__(self, chrome: Path, profile: Path) -> None:
        environment = dict(os.environ)
        environment["TZ"] = TIMEZONE
        environment["LANG"] = "en_US.UTF-8"
        environment["LC_ALL"] = "en_US.UTF-8"
        self._stderr = tempfile.TemporaryFile()
        self._process = subprocess.Popen(
            _chrome_common(chrome, profile),
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            env=environment,
        )
        self._connection: Any = None
        self._next_id = 0
        active_port = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if active_port.is_file():
                break
            if self._process.poll() is not None:
                details = self._stderr_tail()
                self.close()
                raise RuntimeError(
                    f"Chrome exited before CDP became ready: {details}"
                )
            time.sleep(0.05)
        else:
            self.close()
            raise TimeoutError("Chrome did not publish DevToolsActivePort within 15s")
        lines = active_port.read_text(encoding="utf-8").splitlines()
        if len(lines) != 2 or not lines[0].isdigit() or not lines[1].startswith("/"):
            self.close()
            raise RuntimeError(f"invalid Chrome DevToolsActivePort: {lines!r}")
        from websockets.sync.client import connect

        websocket_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        try:
            self._connection = connect(
                websocket_url,
                open_timeout=5,
                close_timeout=2,
                max_size=25_000_000,
            )
        except Exception:
            self.close()
            raise

    def _stderr_tail(self) -> str:
        try:
            self._stderr.flush()
            end = self._stderr.seek(0, os.SEEK_END)
            self._stderr.seek(max(0, end - 4000))
            return self._stderr.read().decode("utf-8", errors="replace")
        except Exception:
            return "<Chrome stderr unavailable>"

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        if self._connection is None:
            raise RuntimeError("Chrome CDP connection is closed")
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        self._connection.send(json.dumps(message, separators=(",", ":")))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            reply = json.loads(self._connection.recv(timeout=remaining))
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise RuntimeError(f"CDP {method} failed: {reply['error']!r}")
            return dict(reply.get("result") or {})
        raise TimeoutError(f"CDP {method} did not answer within {timeout}s")

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None
        if getattr(self, "_process", None) is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
        if getattr(self, "_stderr", None) is not None:
            self._stderr.close()

    def __enter__(self) -> "_ChromeDevTools":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _png_data_dimensions(data: bytes) -> tuple[int, int]:
    header = data[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Chrome did not produce PNG bytes")
    return struct.unpack(">II", header[16:24])


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        return _png_data_dimensions(path.read_bytes())
    except ValueError as error:
        raise ValueError(f"Chrome did not produce a PNG: {path}") from error


def _expected_screenshot_paths(output_root: Path) -> set[Path]:
    return {
        (output_root / f"{width}x{height}" / f"{slug}.png").resolve()
        for width, height in VIEWPORTS
        for slug in SELECTIONS
    }


def _assert_complete_screenshot_set(output_root: Path) -> set[Path]:
    expected = _expected_screenshot_paths(output_root)
    actual = {path.resolve() for path in output_root.glob("*/*.png")}
    extras = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extras or missing or len(actual) != 18:
        raise RuntimeError(
            "screenshot set must contain exactly 18 expected PNGs; "
            f"missing={missing}, extras={extras}, actual={len(actual)}"
        )
    for path in sorted(actual):
        viewport = path.parent.name
        expected_dimensions = next(
            dimensions
            for dimensions in VIEWPORTS
            if f"{dimensions[0]}x{dimensions[1]}" == viewport
        )
        if _png_dimensions(path) != expected_dimensions:
            raise ValueError(
                f"{path} is {_png_dimensions(path)}, expected {expected_dimensions}"
            )
    return actual


def _browser_version(chrome: Path) -> str:
    result = _run_chrome([str(chrome), "--version"], timeout=10)
    if result.returncode:
        raise RuntimeError("could not read Chrome/Chromium version")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _capture_identity(engine: dict[str, Any]) -> dict[str, str]:
    identity = {
        "capture_tool": CAPTURE_TOOL,
        "protocol_version": str(engine.get("protocolVersion") or ""),
        "product": str(engine.get("product") or ""),
        "revision": str(engine.get("revision") or ""),
        "user_agent": str(engine.get("userAgent") or ""),
        "js_version": str(engine.get("jsVersion") or ""),
    }
    if not all(identity.values()):
        raise ValueError(f"Chrome returned an incomplete capture identity: {identity!r}")
    return identity


def _assert_ready_receipt(
    receipt: dict[str, Any],
    *,
    session_nonce: str,
    capture_id: str,
    slug: str,
    width: int,
    height: int,
    png_sha256: str,
    capture_identity: dict[str, str],
) -> None:
    expected_keys = {
        "beat",
        "view",
        "view_proof",
        "target",
        "target_geometry",
        "images",
        "capture_metrics",
        "png_sha256",
        "capture_identity",
        "width",
        "height",
        "dpr",
        "attestation_tag",
    }
    if set(receipt) != expected_keys:
        raise ValueError(f"readiness receipt fields drifted for {capture_id}: {receipt!r}")
    expected_target = "visible" if SELECTIONS[slug].scroll_selector else "none"
    expected_base = {
        "beat": slug,
        "view": SELECTIONS[slug].view,
        "target": expected_target,
        "width": width,
        "height": height,
        "dpr": 1.0,
        "attestation_tag": _attestation_tag(session_nonce, capture_id, slug),
    }
    for field, expected in expected_base.items():
        if receipt.get(field) != expected:
            raise ValueError(
                f"readiness receipt {field} drifted for {capture_id}: "
                f"{receipt.get(field)!r} != {expected!r}"
            )
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("png_sha256") or "")) is None
        or receipt.get("png_sha256") != png_sha256
    ):
        raise ValueError(
            f"captured PNG hash drifted for {capture_id}: "
            f"{receipt.get('png_sha256')!r} != {png_sha256!r}"
        )
    identity_fields = {
        "capture_tool", "protocol_version", "product", "revision",
        "user_agent", "js_version",
    }
    if (
        not isinstance(receipt.get("capture_identity"), dict)
        or set(receipt["capture_identity"]) != identity_fields
        or not all(isinstance(value, str) and value for value in capture_identity.values())
        or receipt["capture_identity"] != capture_identity
    ):
        raise ValueError(
            f"browser capture identity drifted for {capture_id}: "
            f"{receipt.get('capture_identity')!r} != {capture_identity!r}"
        )
    selection = SELECTIONS[slug]
    expected_patient_id = _patient_id(
        _load_fixture(slug), selection.patient_name
    ) or ""
    expected_patient_name = selection.patient_name or ""
    expected_anchor_kind = (
        "selected-evidence" if selection.scroll_selector
        else "patient-heading" if selection.view == "patient"
        else "board-heading"
    )
    expected_anchor_text = _normalized_text(
        selection.scroll_text or selection.patient_name or "Exception line"
    )
    proof = receipt.get("view_proof")
    proof_fields = {
        "observed_view", "visible_views", "state_view", "nav_current",
        "patient_id", "patient_name", "patient_heading_matches",
        "anchor_ancestor_chain_visible", "anchor_kind", "anchor_text",
        "anchor_geometry",
    }
    if not isinstance(proof, dict) or set(proof) != proof_fields:
        raise ValueError(f"invalid observed view proof for {capture_id}: {proof!r}")
    proof_expected = {
        "observed_view": selection.view,
        "visible_views": [selection.view],
        "state_view": selection.view,
        "nav_current": True,
        "patient_id": expected_patient_id,
        "patient_name": expected_patient_name,
        "patient_heading_matches": True,
        "anchor_ancestor_chain_visible": True,
        "anchor_kind": expected_anchor_kind,
    }
    for field, expected in proof_expected.items():
        if proof.get(field) != expected:
            raise ValueError(
                f"observed view {field} drifted for {capture_id}: "
                f"{proof.get(field)!r} != {expected!r}"
            )
    observed_anchor_text = _normalized_text(proof.get("anchor_text"))
    if expected_anchor_text not in observed_anchor_text:
        raise ValueError(
            f"observed view anchor text drifted for {capture_id}: "
            f"{observed_anchor_text!r} lacks {expected_anchor_text!r}"
        )
    anchor_geometry = proof.get("anchor_geometry")
    anchor_fields = {
        "top", "bottom", "left", "right", "occlusion_bottom",
        "visual_width", "visual_height", "unobscured",
    }
    if not isinstance(anchor_geometry, dict) or set(anchor_geometry) != anchor_fields:
        raise ValueError(
            f"invalid observed view anchor geometry for {capture_id}: "
            f"{anchor_geometry!r}"
        )
    try:
        anchor_top = int(anchor_geometry["top"])
        anchor_bottom = int(anchor_geometry["bottom"])
        anchor_left = int(anchor_geometry["left"])
        anchor_right = int(anchor_geometry["right"])
        anchor_occlusion = int(anchor_geometry["occlusion_bottom"])
        anchor_visual_width = int(anchor_geometry["visual_width"])
        anchor_visual_height = int(anchor_geometry["visual_height"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"non-integer observed view anchor geometry for {capture_id}"
        ) from error
    if not (
        (anchor_visual_width, anchor_visual_height) == (width, height)
        and 0 <= anchor_occlusion < height
        and anchor_top >= anchor_occlusion + 1
        and anchor_bottom <= height
        and anchor_bottom > anchor_top
        and anchor_left >= 0
        and anchor_right <= width
        and anchor_right > anchor_left
        and anchor_geometry["unobscured"] is True
    ):
        raise ValueError(
            f"observed view anchor is not visibly unobscured for {capture_id}: "
            f"{anchor_geometry!r}"
        )
    geometry = receipt.get("target_geometry")
    geometry_fields = {
        "top", "bottom", "left", "right",
        "evidence_top", "evidence_bottom", "evidence_left", "evidence_right",
        "occlusion_bottom", "visual_width", "visual_height", "unobscured",
    }
    if not isinstance(geometry, dict) or set(geometry) != geometry_fields:
        raise ValueError(f"invalid target geometry for {capture_id}: {geometry!r}")
    try:
        top = int(geometry["top"])
        bottom = int(geometry["bottom"])
        left = int(geometry["left"])
        right = int(geometry["right"])
        evidence_top = int(geometry["evidence_top"])
        evidence_bottom = int(geometry["evidence_bottom"])
        evidence_left = int(geometry["evidence_left"])
        evidence_right = int(geometry["evidence_right"])
        occlusion = int(geometry["occlusion_bottom"])
        visual_width = int(geometry["visual_width"])
        visual_height = int(geometry["visual_height"])
    except (TypeError, ValueError) as error:
        raise ValueError(f"non-integer target geometry for {capture_id}") from error
    if (visual_width, visual_height) != (width, height):
        raise ValueError(f"visual viewport drifted for {capture_id}: {geometry!r}")
    if not 0 <= occlusion < height or geometry["unobscured"] is not True:
        raise ValueError(f"sticky-header geometry escaped viewport for {capture_id}")
    if expected_target == "visible":
        if not (
            top >= occlusion + 8
            and left >= 0
            and right <= width
            and right > left
            and min(bottom, height) - max(top, occlusion) >= min(bottom - top, 48)
            and evidence_top >= occlusion + 1
            and evidence_bottom <= height
            and evidence_bottom > evidence_top
            and evidence_left >= 0
            and evidence_right <= width
            and evidence_right > evidence_left
        ):
            raise ValueError(
                f"selected evidence is not visibly below the header for {capture_id}: "
                f"{geometry!r}"
            )
        if (
            anchor_top, anchor_bottom, anchor_left, anchor_right
        ) != (
            evidence_top, evidence_bottom, evidence_left, evidence_right
        ):
            raise ValueError(
                f"observed text anchor does not match selected evidence for {capture_id}: "
                f"{anchor_geometry!r} != {geometry!r}"
            )
    elif any(value != -1 for value in (
        top, bottom, left, right,
        evidence_top, evidence_bottom, evidence_left, evidence_right,
    )):
        raise ValueError(f"unexpected target geometry for {capture_id}: {geometry!r}")
    expected_images = 1 if _qr_bytes(_load_fixture(slug)) is not None else 0
    expected_image_report = {
        "expected": expected_images,
        "observed": expected_images,
        "decoded": expected_images,
        "failures": 0,
    }
    if receipt.get("images") != expected_image_report:
        raise ValueError(
            f"image readiness report drifted for {capture_id}: {receipt.get('images')!r}"
        )
    expected_metrics = {
        "source": "Chrome DevTools Protocol",
        "inner_width": width,
        "inner_height": height,
        "device_pixel_ratio": 1.0,
        "visual_width": width,
        "visual_height": height,
        "layout_client_width": width,
        "layout_client_height": height,
        "cdp_visual_client_width": width,
        "cdp_visual_client_height": height,
        "cdp_visual_scale": 1.0,
        "png_width": width,
        "png_height": height,
    }
    if receipt.get("capture_metrics") != expected_metrics:
        raise ValueError(
            f"CDP capture metrics drifted for {capture_id}: "
            f"{receipt.get('capture_metrics')!r}"
        )


def _decode_attestation(picture: Any) -> str:
    rgb = picture.convert("RGB")
    width, height = rgb.size
    left = width - ATTESTATION_INSET - ATTESTATION_WIDTH
    top = height - ATTESTATION_INSET - ATTESTATION_HEIGHT
    if left < 0 or top < 0:
        raise ValueError("screenshot is too small for the capture attestation")
    bits: list[int] = []
    for y in range(ATTESTATION_HEIGHT):
        for x in range(ATTESTATION_WIDTH):
            pixel = rgb.getpixel((left + x, top + y))
            if max(pixel) <= 32:
                bits.append(0)
            elif min(pixel) >= 223:
                bits.append(1)
            else:
                raise ValueError(
                    "capture attestation contains a non-binary pixel at "
                    f"{(left + x, top + y)}: {pixel!r}"
                )
    raw = bytearray()
    for offset in range(0, len(bits), 8):
        value = 0
        for bit in bits[offset:offset + 8]:
            value = (value << 1) | bit
        raw.append(value)
    return bytes(raw).hex()


def _write_screenshot_provenance(
    output_root: Path,
    *,
    browser_version: str,
    capture_identity: dict[str, str],
) -> Path:
    screenshots = _assert_complete_screenshot_set(output_root)
    receipt_path = output_root.parent / "screenshot-receipts.json"
    if not receipt_path.is_file():
        raise RuntimeError(
            "screenshot readiness receipts are missing; an arbitrary PNG set "
            "cannot be accepted by the provenance command"
        )
    receipts = json.loads(receipt_path.read_text(encoding="utf-8"))
    dashboard_sha = sha256(DASHBOARD_HTML.read_bytes()).hexdigest()
    helper_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    if receipts.get("schema") != "sanad-gate0b-browser-receipts/v3":
        raise ValueError("unknown screenshot receipt schema")
    if receipts.get("dashboard_sha256") != dashboard_sha:
        raise ValueError("receipt dashboard hash no longer matches the captured source")
    if receipts.get("replay_helper_sha256") != helper_sha:
        raise ValueError("receipt replay helper hash no longer matches this capture harness")
    if receipts.get("failures"):
        raise ValueError(
            f"browser receipt ledger contains route failures: {receipts['failures']!r}"
        )
    ready = receipts.get("ready") or {}
    if len(ready) != 18:
        raise ValueError(f"expected exactly 18 browser readiness receipts, got {len(ready)}")
    session_nonce = str(receipts.get("capture_session_nonce") or "")
    if re.fullmatch(r"[0-9a-f]{64}", session_nonce) is None:
        raise ValueError("screenshot receipts lack a fresh 256-bit capture session nonce")
    by_capture: dict[tuple[str, int, int], tuple[str, dict[str, Any]]] = {}
    for capture_id, receipt in ready.items():
        key = (
            str(receipt.get("beat")),
            int(receipt.get("width", 0)),
            int(receipt.get("height", 0)),
        )
        if key in by_capture:
            raise ValueError(f"duplicate readiness receipt for {key!r}")
        by_capture[key] = (capture_id, receipt)

    fixture_hashes = {
        f"beats/{slug}.json": sha256((BEATS_DIR / f"{slug}.json").read_bytes()).hexdigest()
        for slug in SELECTIONS
    }
    from PIL import Image

    screenshot_rows: dict[str, Any] = {}
    for path in sorted(screenshots):
        width, height = _png_dimensions(path)
        slug = path.stem
        key = (slug, width, height)
        if key not in by_capture:
            raise ValueError(f"no same-session readiness receipt for {path}")
        capture_id, receipt = by_capture[key]
        actual_png_sha256 = sha256(path.read_bytes()).hexdigest()
        _assert_ready_receipt(
            receipt,
            session_nonce=session_nonce,
            capture_id=capture_id,
            slug=slug,
            width=width,
            height=height,
            png_sha256=actual_png_sha256,
            capture_identity=capture_identity,
        )
        with Image.open(path) as picture:
            actual_attestation = _decode_attestation(picture)
        expected_attestation = _attestation_tag(session_nonce, capture_id, slug)
        if actual_attestation != expected_attestation:
            raise ValueError(
                f"{path} is not tied to readiness receipt {capture_id}: "
                f"attestation tag {actual_attestation!r} != {expected_attestation!r}"
            )
        screenshot_rows[path.relative_to(output_root.parent).as_posix()] = {
            "sha256": actual_png_sha256,
            "width": width,
            "height": height,
            "capture_id": capture_id,
            "readiness_receipt_sha256": sha256(_canonical_bytes(receipt)).hexdigest(),
            "attestation_tag": expected_attestation,
        }
    provenance = {
        "schema": "sanad-gate0b-screenshot-provenance/v3",
        "synthetic": True,
        "browser": {
            "version": browser_version,
            **capture_identity,
            "locale": LOCALE,
            "timezone": TIMEZONE,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "dashboard": {
            "path": "app/web/dashboard.html",
            "sha256": dashboard_sha,
            "source_file_modified_for_capture": False,
            "runtime_bootstrap": "test-only response injection from app/tests/gate0b/replay.py",
        },
        "replay_helper_sha256": helper_sha,
        "readiness_receipts": {
            "path": receipt_path.name,
            "sha256": sha256(receipt_path.read_bytes()).hexdigest(),
            "count": len(ready),
            "capture_session_nonce_sha256": sha256(session_nonce.encode()).hexdigest(),
        },
        "contract": {
            "viewports": [f"{width}x{height}" for width, height in VIEWPORTS],
            "device_pixel_ratio": 1,
            "color_scheme": "light",
            "reduced_motion": True,
            "fixed_date_source": "each fixture's captured_at",
            "font_policy": (
                "external fonts blocked; platform-local system fallback recorded above; "
                "pixels are not claimed portable across machines"
            ),
            "network_policy": (
                "dashboard CSP allows only same-origin reads; standalone Chrome also "
                "maps non-loopback DNS to NOTFOUND"
            ),
            "readiness_policy": (
                "each receipt binds the exact PNG SHA-256, Chrome/CDP identity, and "
                "fresh-session capture-ID attestation; callback occurs after all "
                "dashboard reads and images succeed, exactly one expected DOM view "
                "and patient identity are observed, and the selected evidence anchor "
                "plus its full ancestor chain are visibly unobscured"
            ),
        },
        "selections": {slug: asdict(selection) for slug, selection in SELECTIONS.items()},
        "fixture_sha256": fixture_hashes,
        "screenshots": screenshot_rows,
    }
    provenance_path = output_root.parent / "screenshot-provenance.json"
    write_json(provenance_path, provenance)

    # When writing the committed set, finish the manifest as the final step so
    # its checksum ledger includes all 18 images and this provenance record.
    if output_root.resolve() == SCREENSHOTS_DIR.resolve():
        manifest_path = output_root.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["screenshot_provenance"] = provenance_path.name
        artifact_hashes = {
            path.relative_to(output_root.parent).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in sorted(output_root.parent.rglob("*"))
            if path.is_file() and path != manifest_path
        }
        manifest["artifact_sha256"] = artifact_hashes
        write_json(manifest_path, manifest)
    return provenance_path


def _capture_one(
    server: ReplayHTTPServer,
    browser: _ChromeDevTools,
    capture_identity: dict[str, str],
    capture_id: str,
    slug: str,
    url: str,
    output: Path,
    width: int,
    height: int,
) -> str:
    context_id: str | None = None
    target_id: str | None = None
    try:
        context_id = str(browser.call("Target.createBrowserContext")["browserContextId"])
        target_id = str(browser.call(
            "Target.createTarget",
            {"url": "about:blank", "browserContextId": context_id},
        )["targetId"])
        session_id = str(browser.call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )["sessionId"])
        browser.call("Page.enable", session_id=session_id)
        browser.call("Runtime.enable", session_id=session_id)
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
            session_id=session_id,
        )
        browser.call(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-color-scheme", "value": "light"},
                    {"name": "prefers-reduced-motion", "value": "reduce"},
                ]
            },
            session_id=session_id,
        )
        browser.call(
            "Emulation.setTimezoneOverride",
            {"timezoneId": TIMEZONE},
            session_id=session_id,
        )
        browser.call(
            "Emulation.setLocaleOverride",
            {"locale": LOCALE},
            session_id=session_id,
        )
        browser.call(
            "Emulation.setScrollbarsHidden",
            {"hidden": True},
            session_id=session_id,
        )
        browser.call("Page.navigate", {"url": url}, session_id=session_id)

        state: dict[str, Any] = {}
        deadline = time.monotonic() + 20
        expression = (
            "JSON.stringify({"
            "ready:document.documentElement.dataset.gate0bReady||'',"
            "error:document.documentElement.dataset.gate0bError||'',"
            "inner_width:window.innerWidth,inner_height:window.innerHeight,"
            "dpr:window.devicePixelRatio,"
            "visual_width:window.visualViewport?window.visualViewport.width:window.innerWidth,"
            "visual_height:window.visualViewport?window.visualViewport.height:window.innerHeight"
            "})"
        )
        while time.monotonic() < deadline:
            try:
                evaluated = browser.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    session_id=session_id,
                    timeout=3,
                )
            except (RuntimeError, TimeoutError):
                time.sleep(0.05)
                continue
            if evaluated.get("exceptionDetails"):
                time.sleep(0.05)
                continue
            raw_state = (evaluated.get("result") or {}).get("value")
            if isinstance(raw_state, str):
                state = json.loads(raw_state)
            if state.get("error"):
                raise RuntimeError(f"dashboard replay rejected {capture_id}: {state['error']}")
            if state.get("ready") == "true":
                break
            time.sleep(0.05)
        else:
            raise TimeoutError(f"dashboard did not become capture-ready for {capture_id}")

        expected_runtime = {
            "ready": "true",
            "error": "",
            "inner_width": width,
            "inner_height": height,
            "dpr": 1,
            "visual_width": width,
            "visual_height": height,
        }
        if state != expected_runtime:
            raise ValueError(
                f"browser runtime viewport drifted for {capture_id}: "
                f"{state!r} != {expected_runtime!r}"
            )
        layout = browser.call("Page.getLayoutMetrics", session_id=session_id)
        css_layout = layout.get("cssLayoutViewport") or {}
        css_visual = layout.get("cssVisualViewport") or {}
        observed_layout = (
            float(css_layout.get("clientWidth", 0)),
            float(css_layout.get("clientHeight", 0)),
        )
        observed_visual = (
            float(css_visual.get("clientWidth", 0)),
            float(css_visual.get("clientHeight", 0)),
            float(css_visual.get("scale", 0)),
        )
        if observed_layout != (float(width), float(height)):
            raise ValueError(
                f"CDP layout viewport drifted for {capture_id}: {observed_layout!r}"
            )
        if observed_visual != (float(width), float(height), 1.0):
            raise ValueError(
                f"CDP visual viewport drifted for {capture_id}: {observed_visual!r}"
            )
        browser.call(
            "Runtime.evaluate",
            {
                "expression": (
                    "new Promise(function(resolve){requestAnimationFrame(function(){"
                    "requestAnimationFrame(function(){resolve(true);});});})"
                ),
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=session_id,
        )
        screenshot = browser.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            session_id=session_id,
        )
        png = base64.b64decode(str(screenshot.get("data") or ""), validate=True)
        dimensions = _png_data_dimensions(png)
        if dimensions != (width, height):
            raise ValueError(
                f"Chrome captured {dimensions!r} for {capture_id}, expected {(width, height)!r}"
            )
        from PIL import Image

        with Image.open(BytesIO(png)) as picture:
            picture.load()
            if picture.format != "PNG" or picture.size != (width, height):
                raise ValueError(f"Pillow rejected PNG geometry for {capture_id}")
            observed_attestation = _decode_attestation(picture)
        expected_attestation = _attestation_tag(
            server.capture_session_nonce, capture_id, slug
        )
        if observed_attestation != expected_attestation:
            raise ValueError(
                f"fresh-session attestation mismatch for {capture_id}: "
                f"{observed_attestation!r} != {expected_attestation!r}"
            )
        metrics = {
            "source": "Chrome DevTools Protocol",
            "inner_width": width,
            "inner_height": height,
            "device_pixel_ratio": 1.0,
            "visual_width": width,
            "visual_height": height,
            "layout_client_width": width,
            "layout_client_height": height,
            "cdp_visual_client_width": width,
            "cdp_visual_client_height": height,
            "cdp_visual_scale": 1.0,
            "png_width": width,
            "png_height": height,
        }
        png_sha256 = sha256(png).hexdigest()
        server.mark_captured(
            capture_id,
            metrics,
            png_sha256,
            capture_identity,
        )
        status = server.capture_status(capture_id)
        if status["failures"] or status["ready"] is None:
            raise RuntimeError(
                f"screenshot capture had failed routes for {capture_id}: {status!r}"
            )
        _assert_ready_receipt(
            status["ready"],
            session_nonce=server.capture_session_nonce,
            capture_id=capture_id,
            slug=slug,
            width=width,
            height=height,
            png_sha256=png_sha256,
            capture_identity=capture_identity,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return png_sha256
    finally:
        if target_id is not None:
            try:
                browser.call("Target.closeTarget", {"targetId": target_id}, timeout=3)
            except Exception:
                pass
        if context_id is not None:
            try:
                browser.call(
                    "Target.disposeBrowserContext",
                    {"browserContextId": context_id},
                    timeout=3,
                )
            except Exception:
                pass


def _capture(chrome_arg: str | None, output_root: Path) -> None:
    chrome = _find_chrome(chrome_arg)
    with tempfile.TemporaryDirectory(prefix="sanad-gate0b-capture-") as temp:
        staging = Path(temp)
        staged_output = staging / "goldens" / "screenshots"
        staged_receipts = staged_output.parent / "screenshot-receipts.json"
        profile = staging / "chrome-profile"
        profile.mkdir(parents=True)
        server = _server(0, staged_receipts)
        host, port = server.server_address
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        try:
            try:
                with urlopen(f"http://{host}:{port}/not-a-replay-route", timeout=3) as _response:
                    pass
            except Exception as error:
                # A 404 still proves the isolated server is accepting connections.
                if getattr(error, "code", None) != HTTPStatus.NOT_FOUND:
                    raise
            with _ChromeDevTools(chrome, profile) as browser:
                engine = browser.call("Browser.getVersion")
                capture_identity = _capture_identity(engine)
                for width, height in VIEWPORTS:
                    viewport_dir = staged_output / f"{width}x{height}"
                    for slug in SELECTIONS:
                        output = viewport_dir / f"{slug}.png"
                        token = TOKEN_PREFIX + slug
                        capture_id = f"chrome-{width}x{height}-{slug}"
                        url = (
                            f"http://{host}:{port}/c/{quote(token, safe='')}/app"
                            f"?capture_id={quote(capture_id, safe='')}"
                        )
                        digest = _capture_one(
                            server,
                            browser,
                            capture_identity,
                            capture_id,
                            slug,
                            url,
                            output,
                            width,
                            height,
                        )
                        print(
                            f"{output.relative_to(staged_output.parent)}  sha256={digest}"
                        )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        _assert_complete_screenshot_set(staged_output)
        output_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(_expected_screenshot_paths(staged_output)):
            relative = source.relative_to(staged_output.resolve())
            destination = output_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        shutil.copy2(staged_receipts, output_root.parent / staged_receipts.name)
        provenance = _write_screenshot_provenance(
            output_root,
            browser_version=_browser_version(chrome),
            capture_identity=capture_identity,
        )
        print(f"Gate 0B screenshot provenance written: {provenance}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the read-only dashboard replay")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--receipt-file",
        type=Path,
        default=None,
        help="optional durable receipt ledger (inspection is non-persistent by default)",
    )
    capture = sub.add_parser("capture", help="capture all 18 deterministic PNGs")
    capture.add_argument("--chrome", help="path to a Chrome/Chromium executable")
    capture.add_argument(
        "--output-root",
        type=Path,
        default=SCREENSHOTS_DIR,
        help="screenshot root (defaults to the committed Gate 0B goldens)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        receipt_path = args.receipt_file.resolve() if args.receipt_file else None
        _serve_forever(args.port, receipt_path)
    else:
        _capture(args.chrome, args.output_root.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
