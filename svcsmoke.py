#!/usr/bin/env python
"""Sample each ArcGIS Server service instance repeatedly and pass it only on features or image bytes.

A service can read STARTED while every exportImage call fails. A single sweep
asks one instance, so a fault that sits on 2 of 8 instances is missed on the
sweep, and eight rounds of the same sweep still miss it 10 percent of the time.
This tool states that miss probability, refuses to call a run healthy when the
rounds are too few to support the claim, and counts a probe as passed only when
the body holds a feature or the bytes of an image. The HTTP status is recorded
and never decides.

    python svcsmoke.py --self-test
    python svcsmoke.py URL [URL ...]
    python svcsmoke.py URL --faulty 2/8 --max-miss 0.01
    python svcsmoke.py URL --rounds 8
    python svcsmoke.py URL --layers 0,3
    python svcsmoke.py URL --out report.csv --apply
    python svcsmoke.py --tiers tiers.txt --before app --wait 600

URL is a MapServer, FeatureServer or ImageServer URL. Give one URL per machine
(or per address) that you want sampled. The token, if the service needs one, is
read from the environment variable SVCSMOKE_TOKEN and is sent in the
X-Esri-Authorization header, never in a URL. Redirects are never followed.

The ordered-restart gate: --tiers names a file with one tier per line, in
dependency order, and --before names the tier you are about to restart. The
gate opens (exit 0) only when every earlier tier passes. --wait polls until it
does. This tool restarts nothing and writes nothing without --apply.

Exit codes: 0 every probe passed and the rounds support the miss probability,
1 at least one probe failed, 2 a file could not be read or written, 3 nothing
failed but too few rounds ran to call the service healthy, 4 nothing was
probed, 5 the gate was still closed when --wait ran out, 64 usage error, 70 an
unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import http.client
import http.server
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The smallest fraction of instances carrying a fault that the sampling must be
# able to catch, as K/M. 1/4 is the 2-of-8 case this tool exists for. A smaller
# fraction needs more rounds: the number is printed, not hidden.
DEFAULT_FAULTY = "1/4"

# The largest probability of a clean run when that fraction of instances is
# faulty. 0.01 means one run in a hundred would miss it.
DEFAULT_MAX_MISS = 0.01

# Seconds to wait for one response before the probe counts as failed.
DEFAULT_TIMEOUT = 30.0

# An exported image smaller than this is treated as blank. This is a heuristic
# and a guess, not a rule: measure an export of your own imagery before
# trusting it, and lower it with --min-image-bytes if a real tile is smaller.
DEFAULT_MIN_IMAGE_BYTES = 1000

# One exportImage request is square and this many pixels on a side. 400 is the
# documented default size.
IMAGE_SIDE = 400

# The detail view covers 1/DETAIL_DIVISOR of the service extent's width, around
# its centre, so a fault that appears only at a fine scale is sampled too.
DETAIL_DIVISOR = 100

# A response body larger than this fails the probe, so that a layer which
# ignores resultRecordCount cannot fill memory.
MAX_BODY = 16 * 1024 * 1024

# The most requests one run may send. A plan above it is refused before any
# sampling starts, so the tool cannot flood a production server by accident.
DEFAULT_MAX_REQUESTS = 2000

# Seconds between polls when --wait is given.
DEFAULT_INTERVAL = 10.0

# The environment variable the token is read from. Never a flag, because a
# flag shows in the process list and in shell history.
TOKEN_ENV = "SVCSMOKE_TOKEN"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

MAP_SERVER = "MapServer"
FEATURE_SERVER = "FeatureServer"
IMAGE_SERVER = "ImageServer"
SERVICE_KINDS = (MAP_SERVER, FEATURE_SERVER, IMAGE_SERVER)

# What one probe can come to. OK is the only pass.
OK = "OK"
TRANSPORT = "TRANSPORT"        # no HTTP response, or a body cut short
REDIRECT = "REDIRECT"          # a 3xx. Never followed: it would carry the token
TOO_LARGE = "TOO_LARGE"        # body over MAX_BODY
NOT_JSON = "NOT_JSON"          # an HTML page, a login form, a cut-off body
SERVER_ERROR = "SERVER_ERROR"  # a JSON body with an error property
NO_FEATURES = "NO_FEATURES"    # JSON, no error, and no feature in it
NOT_IMAGE = "NOT_IMAGE"        # not an image content type or not image bytes
SMALL_IMAGE = "SMALL_IMAGE"    # image bytes, fewer than the minimum
HTTP_STATUS = "HTTP_STATUS"    # evidence in the body, but the status is not 200

# A target's verdict, and the order in which one run reports them.
PASS = "PASS"
FAIL = "FAIL"
UNDERSAMPLED = "UNDERSAMPLED"
NOTHING = "NOTHING"

# The first bytes of the formats exportImage can return as an image.
IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)

LOOPBACK = ("localhost", "127.0.0.1", "::1")

_TITLE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
_FAULTY = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_TOKEN_PARAM = re.compile(r"(?i)(token=)[^&\s\"']+")

# The columns --out writes: one row per probe, or one row for a target that
# could not be probed.
OUT_COLUMNS = ("tier", "target", "verdict", "probe", "requests", "failed",
               "kinds", "first_failure")


# ----------------------------------------------------------------- pure core

def check_fraction(fraction):
    """The share of instances that carry a fault. Above 0 and at most 1."""
    if not 0.0 < fraction <= 1.0:
        raise ValueError("the faulty fraction must be above 0 and at most 1, "
                         "not %r" % (fraction,))


def parse_faulty(text):
    """'2/8' -> 0.25. The smallest faulty share of instances to be caught."""
    match = _FAULTY.match(text or "")
    if not match:
        raise ValueError("--faulty must look like 2/8 (faulty instances over "
                         "all instances), not %r" % (text,))
    faulty, total = int(match.group(1)), int(match.group(2))
    if total < 1 or total > 10000 or faulty < 1 or faulty > total:
        raise ValueError("--faulty %s needs 1 <= K <= M <= 10000" % text)
    return faulty / float(total)


def miss_probability(fraction, rounds):
    """The chance that `rounds` requests all land on a healthy instance.

    Each request is taken to land on a uniformly random instance, independent
    of the one before. Under that model a fraction f of faulty instances is
    missed by n requests with probability (1 - f) ** n. Esri documents no rule
    for how a request is assigned to an instance, so the model is an
    assumption. A round-robin dispatcher would do better, a sticky one worse.
    """
    check_fraction(fraction)
    if rounds < 0:
        raise ValueError("rounds cannot be negative")
    return (1.0 - fraction) ** rounds


def rounds_needed(fraction, max_miss):
    """The fewest rounds whose miss probability is at most max_miss."""
    check_fraction(fraction)
    if not 0.0 < max_miss < 1.0:
        raise ValueError("the miss limit must be above 0 and below 1, not %r"
                         % (max_miss,))
    if fraction == 1.0:
        return 1
    estimate = math.log(max_miss) / math.log(1.0 - fraction)
    # Start one below the estimate and walk up, so float error at an exact
    # boundary (0.5 ** 3 == 0.125) can neither add a round nor drop one.
    rounds = max(1, int(math.ceil(estimate)) - 1)
    while miss_probability(fraction, rounds) > max_miss:
        rounds += 1
    return rounds


def tidy(text, limit=100):
    """One printable ASCII line: server text may hold anything."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = "".join(c if 32 <= ord(c) < 127 else "?" for c in text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def scrub(text, token):
    """Text safe to print: no token value and no token= parameter."""
    text = _TOKEN_PARAM.sub(r"\1[token]", text)
    if token:
        text = text.replace(token, "[token]")
    return text


def describe_body(body):
    """What a body that is not the expected thing looks like, in a line."""
    text = body[:4096].decode("utf-8", "replace")
    title = _TITLE.search(text)
    if title:
        return "page titled '%s'" % tidy(title.group(1), 80)
    return "body starts '%s'" % tidy(text, 60)


def parse_json(body):
    """(document, None) for a JSON object, else (None, (kind, detail))."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, (NOT_JSON, "not JSON, " + describe_body(body))
    if not isinstance(doc, dict):
        return None, (NOT_JSON, "JSON that is not an object")
    return doc, None


def error_text(doc):
    """The message of a REST error property, or a stand-in."""
    error = doc.get("error")
    if isinstance(error, dict) and error.get("message"):
        return "error property: %s (code %s)" % (
            tidy(str(error["message"]), 80), tidy(str(error.get("code")), 12))
    return "error property: %s" % tidy(str(error), 80)


def classify_features(body):
    """A feature query body -> (kind, detail). OK only when it holds a feature."""
    doc, failure = parse_json(body)
    if failure:
        return failure
    if "error" in doc:
        return SERVER_ERROR, error_text(doc)
    features = doc.get("features")
    if not isinstance(features, list):
        return NO_FEATURES, "JSON with no features array"
    if not features:
        return NO_FEATURES, "features array is empty"
    return OK, ""


def classify_image(ctype, body, min_bytes):
    """An export body -> (kind, detail). OK only for image bytes of some size."""
    if not (ctype or "").lower().startswith("image/"):
        return NOT_IMAGE, "content type '%s', %s" % (tidy(ctype, 40),
                                                      describe_body(body))
    if not any(body.startswith(sig) for sig in IMAGE_SIGNATURES):
        return NOT_IMAGE, "image content type, %s" % describe_body(body)
    if len(body) < min_bytes:
        return SMALL_IMAGE, "%d bytes, under the %d byte minimum" % (
            len(body), min_bytes)
    return OK, ""


class Reply(object):
    """One HTTP exchange, or the reason there was none."""

    def __init__(self, status, ctype, body, error=None, detail=""):
        self.status = status
        self.ctype = ctype
        self.body = body
        self.error = error
        self.detail = detail


def judge(service_kind, reply, min_bytes, token):
    """A reply -> (kind, detail, status).

    The body must hold the evidence: a feature, or image bytes. Esri says a
    status of 200 means the request was processed and any other status is a
    problem, so a good body under another status fails too. The status alone
    never passes a probe.
    """
    if reply.error:
        kind, detail = reply.error, reply.detail
    elif service_kind == IMAGE_SERVER:
        kind, detail = classify_image(reply.ctype, reply.body, min_bytes)
    else:
        kind, detail = classify_features(reply.body)
    if kind == OK and reply.status != 200:
        kind, detail = HTTP_STATUS, "HTTP %d with a good body" % reply.status
    return kind, scrub(detail, token), reply.status


def judge_root(reply, token):
    """A service root reply -> (document, None) or (None, (kind, detail, status))."""
    if reply.error:
        return None, (reply.error, scrub(reply.detail, token), reply.status)
    doc, failure = parse_json(reply.body)
    if failure:
        return None, (failure[0], scrub(failure[1], token), reply.status)
    if "error" in doc:
        return None, (SERVER_ERROR, scrub(error_text(doc), token),
                      reply.status)
    if reply.status != 200:
        return None, (HTTP_STATUS, "HTTP %d with a good body" % reply.status,
                      reply.status)
    return doc, None


class Target(object):
    """One service URL to sample, and the tier it belongs to if any."""

    def __init__(self, url, kind, tier=None):
        self.url = url
        self.kind = kind
        self.tier = tier


def check_target(url, token):
    """A service URL -> (url without a trailing slash, kind). ValueError if unusable.

    Refuses anything that would put a credential where it can leak: a user
    name or password in the URL, a query string, or a token sent over plain
    HTTP to a host that is not this machine.
    """
    parts = urllib.parse.urlsplit(url.strip())
    shown = scrub(tidy(url, 60), None)
    # Credentials are refused before the URL is ever quoted in a message.
    if parts.username or parts.password:
        raise ValueError("a URL may not carry a user name or password; put "
                         "the token in %s" % TOKEN_ENV)
    if parts.query or parts.fragment:
        raise ValueError("a URL may not carry a query string or fragment, so "
                         "a token cannot ride in one. Put it in %s"
                         % TOKEN_ENV)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("%s is not an http or https URL" % shown)
    parts.port  # a bad port raises ValueError here
    kind = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if kind not in SERVICE_KINDS:
        raise ValueError("%s does not end in %s" % (shown,
                                                    ", ".join(SERVICE_KINDS)))
    if token and parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise ValueError("%s is plain http and %s is set. The token would "
                         "cross the network in the clear" % (shown, TOKEN_ENV))
    return url.strip().rstrip("/"), kind


class Probe(object):
    """One request shape: a layer query, or an image export view."""

    def __init__(self, name, layer=None, bbox=None, wkid=None):
        self.name = name
        self.layer = layer
        self.bbox = bbox
        self.wkid = wkid


def pick_layers(doc, wanted):
    """The layer ids to query. Group layers hold no rows and are left out."""
    if wanted:
        return list(wanted)
    entries = doc.get("layers")
    if not isinstance(entries, list):
        return []
    ids = []
    for entry in entries:
        if not isinstance(entry, dict) or not _is_int(entry.get("id")):
            continue
        if entry.get("subLayerIds"):
            continue
        ids.append(entry["id"])
    return ids


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def views_from_root(doc):
    """(views, reason) from an image service root: the whole extent, then a detail."""
    extent = doc.get("extent")
    if not isinstance(extent, dict):
        extent = doc.get("fullExtent")
    if not isinstance(extent, dict):
        return [], "the service root has no extent"
    ref = extent.get("spatialReference")
    if not isinstance(ref, dict):
        ref = doc.get("spatialReference")
    wkid = None
    if isinstance(ref, dict):
        wkid = ref.get("wkid") or ref.get("latestWkid")
    if not _is_int(wkid):
        return [], "the extent has no well-known spatial reference id"
    corners = [extent.get(k) for k in ("xmin", "ymin", "xmax", "ymax")]
    if not all(_number(c) for c in corners):
        return [], "the extent has no usable corners"
    xmin, ymin, xmax, ymax = corners
    if xmax <= xmin or ymax <= ymin:
        return [], "the extent is empty"
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    half = max(xmax - xmin, ymax - ymin) / 2.0
    views = []
    for name, reach in (("overview", half), ("detail", half / DETAIL_DIVISOR)):
        views.append(Probe("view " + name, bbox=(cx - reach, cy - reach,
                                                 cx + reach, cy + reach),
                           wkid=wkid))
    return views, ""


def build_plan(service_kind, doc, wanted):
    """(probes, reason). An empty plan carries the reason nothing can be probed."""
    if service_kind == IMAGE_SERVER:
        return views_from_root(doc)
    ids = pick_layers(doc, wanted)
    if not ids:
        return [], "the service lists no layer to query"
    return [Probe("layer %d" % i, layer=i) for i in ids], ""


def probe_url(base, probe, nonce=None):
    """The request for one probe. `nonce` varies the URL for each round."""
    if probe.layer is not None:
        url = "%s/%d/query" % (base, probe.layer)
        params = [("where", "1=1"), ("outFields", "*"),
                  ("returnGeometry", "false"), ("resultRecordCount", "1"),
                  ("f", "json")]
    else:
        url = base + "/exportImage"
        params = [("bbox", "%.6f,%.6f,%.6f,%.6f" % probe.bbox),
                  ("bboxSR", str(probe.wkid)), ("imageSR", str(probe.wkid)),
                  ("size", "%d,%d" % (IMAGE_SIDE, IMAGE_SIDE)),
                  ("format", "jpgpng"), ("f", "image")]
    if nonce is not None:
        params.append(("_svcsmoke", str(nonce)))
    return url + "?" + urllib.parse.urlencode(params)


class ProbeStat(object):
    """How one probe fared over the rounds."""

    def __init__(self, name):
        self.name = name
        self.requests = 0
        self.failed = 0
        self.kinds = {}
        self.first = None

    def add(self, outcome):
        self.requests += 1
        if outcome[0] != OK:
            self.failed += 1
            self.kinds[outcome[0]] = self.kinds.get(outcome[0], 0) + 1
            if self.first is None:
                self.first = outcome


class TargetResult(object):
    """What sampling one target found."""

    def __init__(self, target):
        self.target = target
        self.root = None      # (kind, detail, status) when the root failed
        self.reason = ""      # why nothing could be probed
        self.plan = []
        self.stats = []
        self.requests = 0
        self.verdict = None


def target_verdict(result, rounds, needed):
    """FAIL, NOTHING, UNDERSAMPLED or PASS, in that order of precedence."""
    if result.root or any(s.failed for s in result.stats):
        return FAIL
    if not result.plan:
        return NOTHING
    if rounds < needed:
        return UNDERSAMPLED
    return PASS


def exit_code(results):
    """The worst verdict decides: FAIL 1, NOTHING 4, UNDERSAMPLED 3, else 0."""
    verdicts = [r.verdict for r in results]
    if not verdicts:
        return 4
    if FAIL in verdicts:
        return 1
    if NOTHING in verdicts:
        return 4
    if UNDERSAMPLED in verdicts:
        return 3
    return 0


def parse_tiers(text):
    """Tier file text -> [(name, [urls])], in the order written.

    One tier per line as 'name: url url'. Blank lines and lines starting with
    '#' are skipped. Earlier lines are the tiers that later lines depend on.
    """
    tiers = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, rest = line.partition(":")
        name, urls = name.strip(), rest.split()
        # Splitting on the first colon is safe: a name has none, a URL does.
        if not sep or not name or not urls or re.search(r"\s", name):
            raise ValueError("tiers line %d must look like 'name: url url'"
                             % number)
        if name in [t[0] for t in tiers]:
            raise ValueError("tiers line %d repeats the tier name '%s'"
                             % (number, name))
        tiers.append((name, urls))
    if not tiers:
        raise ValueError("the tiers file names no tier")
    return tiers


def tiers_before(tiers, name):
    """The tiers that must be healthy before `name` may restart."""
    names = [t[0] for t in tiers]
    if name not in names:
        raise ValueError("no tier named '%s'. The file has: %s"
                         % (name, ", ".join(names)))
    index = names.index(name)
    if index == 0:
        raise ValueError("tier '%s' is the first, so no tier comes before "
                         "it and the gate would open without checking "
                         "anything" % name)
    return tiers[:index]


def run_gate(survey, wait, interval, clock, sleep, say):
    """Call survey() until it passes or `wait` seconds run out.

    Returns (results, code). With wait 0 there is one look and the code is the
    survey's own. With a wait that ran out the code is 5.
    """
    deadline = clock() + wait
    attempt = 0
    while True:
        attempt += 1
        results = survey()
        code = exit_code(results)
        if code == 0:
            return results, 0
        if wait <= 0 or clock() + interval > deadline:
            return results, (code if wait <= 0 else 5)
        say("gate closed on attempt %d (exit %d). Trying again in %g s."
            % (attempt, code, interval))
        sleep(interval)


def describe(results, context):
    """The report as lines. `context` carries the rounds and the miss maths."""
    rounds, fraction = context["rounds"], context["fraction"]
    limit, needed = context["max_miss"], context["needed"]
    miss = miss_probability(fraction, rounds)
    lines = ["svcsmoke: %d target(s), %d round(s) per probe%s"
             % (len(results), rounds, context["rounds_note"])]
    lines.append("sampling: %d round(s) miss a fault on %s of the instances "
                 "with probability %.4f (limit %.4f), if each request lands "
                 "on a random instance" % (rounds, context["faulty"], miss,
                                           limit))
    for number, result in enumerate(results, 1):
        tier = "" if result.target.tier is None else (
            "[%s] " % result.target.tier)
        lines.append("%d. %-12s %s%s  %d probe(s), %d request(s)" % (
            number, result.verdict, tier, result.target.url,
            len(result.plan), result.requests))
        if result.root:
            lines.append("     root: %s, %s" % (result.root[0],
                                                result.root[1]))
        elif not result.plan:
            lines.append("     nothing probed: %s" % result.reason)
        for stat in result.stats:
            if stat.failed:
                kinds = ", ".join("%s x%d" % (k, v)
                                  for k, v in sorted(stat.kinds.items()))
                lines.append("     %s: %d of %d failed (%.1f%%): %s. First: "
                             "%s%s" % (stat.name, stat.failed, stat.requests,
                                       100.0 * stat.failed / stat.requests,
                                       kinds, stat.first[1],
                                       status_note(stat.first[2])))
    lines.append("")
    verdicts = [r.verdict for r in results]
    if FAIL in verdicts:
        lines.append("VERDICT: FAILED. %d target(s) returned no feature or "
                     "image on at least one request. They are not healthy, "
                     "whatever their state says." % verdicts.count(FAIL))
    elif NOTHING in verdicts or not results:
        lines.append("VERDICT: NOTHING PROBED. At least one target had no "
                     "layer or view to ask, so a clean exit would mean "
                     "nothing.")
    elif UNDERSAMPLED in verdicts:
        lines.append("VERDICT: UNDERSAMPLED. Every request passed, but %d "
                     "round(s) leave a %.4f chance of missing a fault on %s "
                     "of the instances, over the limit of %.4f. Run at least "
                     "%d." % (rounds, miss, context["faulty"], limit, needed))
    else:
        lines.append("VERDICT: PASS. Every probe returned a feature or image "
                     "bytes on all %d round(s). That bounds the chance of "
                     "missing a fault on %s of the instances at %.4f. It says "
                     "nothing about a smaller share." % (rounds,
                                                         context["faulty"],
                                                         miss))
    return lines


def status_note(status):
    """', HTTP 200' for a reply that had a status, else nothing."""
    return "" if status is None else " (HTTP %d, recorded only)" % status


def report_rows(results):
    """The --out rows: one per probe, or one for a target with no probe."""
    rows = []
    for result in results:
        base = {"tier": result.target.tier or "", "target": result.target.url,
                "verdict": result.verdict}
        if not result.stats:
            row = dict(base, probe="", requests=result.requests, failed=0,
                       kinds="", first_failure="")
            if result.root:
                row.update(failed=1, kinds=result.root[0],
                           first_failure=result.root[1])
            else:
                row["first_failure"] = result.reason
            rows.append(row)
        for stat in result.stats:
            rows.append(dict(
                base, probe=stat.name, requests=stat.requests,
                failed=stat.failed,
                kinds=" ".join("%s:%d" % kv
                               for kv in sorted(stat.kinds.items())),
                first_failure=stat.first[1] if stat.first else ""))
    return rows


# ------------------------------------------------------------------------ i/o

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. urllib would send the token header on to
    wherever the Location says, which may be another host. A redirect is
    reported as a failed probe instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reply(resp, max_body):
    """Read one response into a Reply. Never follows, never trusts the status."""
    status = resp.getcode()
    ctype = resp.headers.get("Content-Type", "")
    if 300 <= status < 400:
        where = urllib.parse.urlsplit(resp.headers.get("Location", ""))
        return Reply(status, ctype, b"", REDIRECT,
                     "redirected to %s" % tidy(where.netloc + where.path, 80))
    body = resp.read(max_body + 1)
    if len(body) > max_body:
        return Reply(status, ctype, b"", TOO_LARGE,
                     "body over %d bytes" % max_body)
    declared = resp.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) != len(body):
        return Reply(status, ctype, b"", TRANSPORT,
                     "body cut short: %d of %s bytes" % (len(body), declared))
    return Reply(status, ctype, body)


def make_fetcher(token, timeout, max_body=MAX_BODY):
    """fetch(url) -> Reply. One GET; any failure becomes a Reply, not a raise.

    No proxy is used, so the token header goes only to the host named. The
    token goes in X-Esri-Authorization, which Esri recommends over a query
    parameter because proxies, gateways and load balancers can read the latter.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         _NoRedirect)
    headers = {"User-Agent": "svcsmoke"}
    if token:
        headers["X-Esri-Authorization"] = "Bearer " + token

    def exchange(url):
        request = urllib.request.Request(url, headers=headers)
        try:
            resp = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            resp = exc
        with contextlib.closing(resp):
            return _reply(resp, max_body)

    def fetch(url):
        try:
            return exchange(url)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            name = ("timed out" if isinstance(reason, socket.timeout)
                    else type(reason).__name__)
            return Reply(None, "", b"", TRANSPORT, "no response: " + name)

    return fetch


def sample_target(result, rounds, fetch, bust, min_bytes, token, stop_on_fail):
    """Ask every probe once per round. With stop_on_fail, end the round a
    failure appears in, because a gate that is already closed gains nothing
    from hammering a tier that is coming back up."""
    base = result.target.url
    result.stats = [ProbeStat(p.name) for p in result.plan]
    for number in range(rounds):
        for probe, stat in zip(result.plan, result.stats):
            reply = fetch(probe_url(base, probe, number if bust else None))
            result.requests += 1
            stat.add(judge(result.target.kind, reply, min_bytes, token))
        if stop_on_fail and any(s.failed for s in result.stats):
            break


def survey(targets, options, fetch, stop_on_fail=False):
    """Discover every target, check the request budget, then sample each.

    Raises ValueError when the plan is over the budget, before any sampling.
    """
    results = []
    for target in targets:
        result = TargetResult(target)
        reply = fetch(target.url + "?f=json")
        result.requests += 1
        doc, failure = judge_root(reply, options["token"])
        if failure:
            result.root = failure
        else:
            result.plan, result.reason = build_plan(target.kind, doc,
                                                    options["layers"])
        results.append(result)
    planned = sum(len(r.plan) * options["rounds"] for r in results)
    sent = sum(r.requests for r in results)
    if planned + sent > options["max_requests"]:
        raise ValueError(
            "the plan is %d request(s) (%d probe(s) over %d round(s), plus "
            "%d to read the service roots), over --max-requests %d. Nothing "
            "was sampled. Name fewer layers with --layers, or raise the limit."
            % (planned + sent, sum(len(r.plan) for r in results),
               options["rounds"], sent, options["max_requests"]))
    for result in results:
        if result.plan:
            sample_target(result, options["rounds"], fetch, options["bust"],
                          options["min_bytes"], options["token"], stop_on_fail)
        result.verdict = target_verdict(result, options["rounds"],
                                        options["needed"])
    return results


def read_text(path):
    """A small text file as str. UTF-8, with or without a byte order mark."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def write_report(path, results):
    """Write the --out CSV. Only --apply calls this."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUT_COLUMNS))
        writer.writeheader()
        for row in report_rows(results):
            writer.writerow(row)


# ------------------------------------------------------------------ self-test

class Stub(object):
    """A local HTTP server whose answers a script decides, on 127.0.0.1.

    script(path, params, headers) returns a dict: body, status, ctype,
    headers, length (a Content-Length that lies), delay (seconds), close (send
    nothing at all). Every request is kept in .requests.
    """

    def __init__(self, script, port=0):
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                params = dict(urllib.parse.parse_qsl(parts.query))
                stub.requests.append((parts.path, params, dict(self.headers)))
                answer = script(parts.path, params, self.headers)
                if answer.get("delay"):
                    time.sleep(answer["delay"])
                if answer.get("close"):
                    self.close_connection = True
                    return
                body = answer.get("body", b"")
                self.send_response(answer.get("status", 200))
                self.send_header("Content-Type",
                                 answer.get("ctype", "application/json"))
                self.send_header("Content-Length",
                                 str(answer.get("length", len(body))))
                for name, value in answer.get("headers", {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                      Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02})
        self.thread.daemon = True
        self.thread.start()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves svcsmoke-selftest-* behind."""
    tmp = tempfile.mkdtemp(prefix="svcsmoke-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core, then over the network paths against
    a local stub. No credentials, no host but 127.0.0.1."""
    passed = [0]
    failed = []
    stubs = []
    saved_token = os.environ.pop(TOKEN_ENV, None)
    try:
        return _assertions(tmp, passed, failed, stubs)
    finally:
        for stub in stubs:
            stub.stop()
        _restore_env(TOKEN_ENV, saved_token)


def _restore_env(name, saved):
    """Put an environment variable back as it was, or remove it."""
    if saved is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = saved


def _assertions(tmp, passed, failed, stubs):
    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def serve(script):
        stub = Stub(script)
        stubs.append(stub)
        return stub

    def run(argv, token=None):
        """main() with its streams captured. Returns (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        if token is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = token
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
        finally:
            os.environ.pop(TOKEN_ENV, None)
        return code, out.getvalue(), err.getvalue()

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
    JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000
    GOOD = json.dumps({"features": [{"attributes": {"OBJECTID": 1}}]}).encode()
    ERR = json.dumps({"error": {"code": 500, "details": [],
                                "message": "Unable to complete operation."}}
                     ).encode()
    HTML = b"<html><head><title>Sign In</title></head><body>x</body></html>"
    EXTENT = {"xmin": -1000.0, "ymin": -1000.0, "xmax": 1000.0,
              "ymax": 1000.0, "spatialReference": {"wkid": 3857}}
    IMAGE_ROOT = json.dumps({"extent": EXTENT}).encode()

    def map_root(layers=((0, None),)):
        return json.dumps({"layers": [
            {"id": i, "name": "L%d" % i, "parentLayerId": -1,
             "subLayerIds": subs} for i, subs in layers]}).encode()

    def pool(m, bad, rng, layers=((0, None),), bad_layers=(0,)):
        """A MapServer whose requests land on a random one of m instances, of
        which the first `bad` fail the layers in bad_layers."""
        root = map_root(layers)

        def script(path, params, headers):
            if path.endswith("/MapServer"):
                return {"body": root}
            layer = int(path.split("/")[-2])
            if int(rng.random() * m) < bad and layer in bad_layers:
                return {"body": ERR}
            return {"body": GOOD}
        return script

    def image_pool(m, bad, rng, image=PNG):
        def script(path, params, headers):
            if path.endswith("/ImageServer"):
                return {"body": IMAGE_ROOT}
            if int(rng.random() * m) < bad:
                return {"body": HTML, "ctype": "text/html"}
            return {"body": image, "ctype": "image/png"}
        return script

    def opts(rounds=3, needed=1, layers=(), bust=False, min_bytes=1000,
             max_requests=100000, token=None):
        return {"rounds": rounds, "needed": needed, "layers": list(layers),
                "bust": bust, "min_bytes": min_bytes,
                "max_requests": max_requests, "token": token}

    def target(stub, path, kind=MAP_SERVER, tier=None):
        return Target(stub.url(path), kind, tier)

    print("svcsmoke self-test: loopback stub only, no network, no credentials")
    print("-" * 68)

    # ---- how likely a sweep is to miss a fault on some of the instances
    check(parse_faulty("2/8") == 0.25 and parse_faulty(" 1 / 4 ") == 0.25,
          "2/8 and 1/4 are the same quarter of the instances")
    for text in ("0/8", "9/8", "1/0", "abc", "0.25", "", "1/10001", "-1/4"):
        raises(lambda t=text: parse_faulty(t),
               "--faulty %r is refused" % text)
    raises(lambda: parse_faulty(None), "--faulty with no value is refused")
    check(round(miss_probability(0.25, 8), 4) == 0.1001,
          "eight rounds against a fault on 2 of 8 instances miss it 10 "
          "percent of the time  <-- pinned defect")
    check(miss_probability(0.25, 0) == 1.0, "no rounds always miss")
    check(miss_probability(1.0, 3) == 0.0,
          "a fault on every instance is never missed")
    check(miss_probability(0.5, 3) == 0.125, "(1 - 0.5) ** 3 is 0.125")
    for fraction, rounds in ((0.0, 1), (1.5, 1), (0.5, -1),
                             (float("nan"), 1)):
        raises(lambda f=fraction, r=rounds: miss_probability(f, r),
               "miss_probability(%r, %r) is refused" % (fraction, rounds))
    check(rounds_needed(0.25, 0.01) == 17
          and miss_probability(0.25, 17) < 0.01 <= miss_probability(0.25, 16),
          "17 rounds is the fewest that put the miss chance under 1 percent")
    check(rounds_needed(0.25, 0.10) == 9,
          "eight rounds do not meet a 10 percent limit, nine do"
          "  <-- pinned defect")
    check(rounds_needed(0.25, 0.75 ** 3) == 3
          and math.ceil(math.log(0.75 ** 3) / math.log(0.75)) == 4,
          "an exact boundary (0.75 ** 3) needs 3 rounds although the "
          "logarithm rounds up to 4  <-- pinned defect")
    edge = miss_probability(0.25, 16) * (1.0 - 1e-10)
    check(rounds_needed(0.25, edge) == 17,
          "a limit a hair under the 16 round chance needs 17"
          "  <-- pinned defect")
    check(rounds_needed(1.0, 0.5) == 1 and rounds_needed(0.9999, 0.5) == 1,
          "a fault on every instance needs one round, never zero")
    grid = [(f, m) for f in (0.01, 0.05, 0.125, 0.25, 1 / 3.0, 0.5, 0.9)
            for m in (0.5, 0.1, 0.05, 0.01, 0.001)]
    check(all(miss_probability(f, rounds_needed(f, m)) <= m
              and (rounds_needed(f, m) == 1
                   or miss_probability(f, rounds_needed(f, m) - 1) > m)
              for f, m in grid),
          "for %d shares and limits the answer is the fewest rounds that meet "
          "the limit" % len(grid))
    for limit in (0.0, 1.0, 1.5):
        raises(lambda m=limit: rounds_needed(0.25, m),
               "a miss limit of %r is refused" % limit)
    raises(lambda: rounds_needed(0.0, 0.5), "a faulty share of 0 is refused")

    rng = random.Random(7)
    misses = {8: 0, 17: 0}
    trials = 20000
    for _ in range(trials):
        draws = [int(rng.random() * 8) >= 2 for _ in range(17)]
        misses[8] += all(draws[:8])
        misses[17] += all(draws)
    check(abs(misses[8] / float(trials) - 0.1001) < 0.01,
          "a simulated pool of 8 with 2 bad, sampled 8 rounds, goes unseen "
          "within a point of 10.01 percent")
    check(misses[17] / float(trials) < 0.012,
          "and sampled 17 rounds it goes unseen under 1.2 percent of the time")

    # ---- what counts as a pass
    check(classify_features(GOOD)[0] == OK, "a feature in the body is a pass")
    kind, detail = classify_features(ERR)
    check(kind == SERVER_ERROR and detail.startswith(
        "error property: Unable to complete operation. (code 500)"),
        "an error property in a 200 body is a failure, with the message"
        "  <-- pinned defect")
    check(classify_features(b'{"features": []}')[0] == NO_FEATURES,
          "an empty features array is not a pass")
    check(classify_features(b'{"count": 48}')[0] == NO_FEATURES,
          "a count with no feature is not a pass  <-- pinned defect")
    kind, detail = classify_features(HTML)
    check(kind == NOT_JSON and "page titled 'Sign In'" in detail,
          "a login page is NOT_JSON and says what it was titled")
    check(classify_features(b"[1, 2]")[0] == NOT_JSON
          and classify_features(b'{"features": [')[0] == NOT_JSON
          and classify_features(b"\xff\xfe")[0] == NOT_JSON
          and classify_features(b"[" * 100000)[0] == NOT_JSON,
          "a list, a cut-off body, bad UTF-8 and a deep nest are all NOT_JSON")
    check(classify_features(b'{"error": "boom"}')[1] == "error property: boom"
          and classify_features(b'{"error": {"code": 500}}')[1]
          == "error property: {'code': 500}",
          "an error that is not the documented shape is still quoted")
    check(classify_features(b'{"features": "x"}')[0] == NO_FEATURES,
          "features that is not a list is not a pass")
    check(classify_features(
        json.dumps({"error": {"message": "=cmd|' /C calc'!A0"}}).encode()
    )[1].startswith("error property:"),
        "a report cell never starts with text the server chose"
        "  <-- pinned defect")
    check(classify_image("image/png", PNG, 1000)[0] == OK
          and classify_image("IMAGE/JPEG", JPG, 1000)[0] == OK,
          "PNG and JPEG bytes under an image content type are a pass")
    check(all(classify_image("image/x", sig + b"\x00" * 1200, 1000)[0] == OK
              for sig in IMAGE_SIGNATURES),
          "all %d image signatures are accepted" % len(IMAGE_SIGNATURES))
    kind, detail = classify_image("text/html", HTML, 1000)
    check(kind == NOT_IMAGE and "content type 'text/html'" in detail
          and "Sign In" in detail,
          "an HTML page where an image was asked for fails and is named"
          "  <-- pinned defect")
    check(classify_image("image/png", HTML, 1000)[0] == NOT_IMAGE
          and classify_image(None, PNG, 1000)[0] == NOT_IMAGE
          and classify_image("", PNG, 1000)[0] == NOT_IMAGE,
          "an image content type over HTML bytes, and no content type, fail")
    kind, detail = classify_image("image/png", PNG[:100], 1000)
    check(kind == SMALL_IMAGE and "100 bytes" in detail
          and classify_image("image/png", PNG[:100], 0)[0] == OK,
          "an image under the minimum is SMALL_IMAGE, and 0 switches it off")
    check(describe_body(b"plain words") == "body starts 'plain words'",
          "a body with no title is quoted from its start")
    check(tidy("a\n\t b\u00e9" + "x" * 200, 20) == "a b?xxxxxxxxxxxxx..."
          and tidy(None) == "" and len(tidy("y" * 500)) == 100,
          "tidy keeps one ASCII line of a bounded length")
    check(scrub("see token=abc123&x=1 and SECRET here", "SECRET")
          == "see token=[token]&x=1 and [token] here"
          and scrub("plain", None) == "plain",
          "scrub removes the token value and any token= parameter")

    reply = Reply(200, "application/json", GOOD)
    check(judge(MAP_SERVER, reply, 1000, None) == (OK, "", 200),
          "a good feature reply under 200 is OK")
    check(judge(MAP_SERVER, Reply(503, "application/json", GOOD), 1000, None)
          [0] == HTTP_STATUS,
          "a feature body under HTTP 503 still fails, Esri calls it a problem")
    kind, detail, status = judge(MAP_SERVER, Reply(200, "", ERR), 1000, None)
    check(kind == SERVER_ERROR and status == 200,
          "HTTP 200 with an error body fails, and the 200 is recorded"
          "  <-- pinned defect")
    check(judge(IMAGE_SERVER, Reply(200, "image/png", PNG), 1000, None)
          == (OK, "", 200)
          and judge(IMAGE_SERVER, Reply(200, "text/html", HTML), 1000, None)
          [0] == NOT_IMAGE,
          "an image service is judged on image bytes")
    check(judge(MAP_SERVER, Reply(None, "", b"", TRANSPORT, "gone T0K"), 1,
                "T0K") == (TRANSPORT, "gone [token]", None),
          "a transport failure carries its detail, scrubbed, with no status")
    doc, failure = judge_root(Reply(200, "", map_root()), None)
    check(failure is None and "layers" in doc, "a good root reply is a document")
    check(judge_root(Reply(None, "", b"", TRANSPORT, "x T0K"), "T0K")[1]
          == (TRANSPORT, "x [token]", None)
          and judge_root(Reply(200, "", HTML), None)[1][0] == NOT_JSON
          and judge_root(Reply(200, "", ERR), None)[1][0] == SERVER_ERROR
          and judge_root(Reply(500, "", map_root()), None)[1][0]
          == HTTP_STATUS,
          "a root that fails, is not JSON, is an error or is not 200 is a "
          "failure")

    # ---- which URLs are accepted
    check(check_target(" http://127.0.0.1:6080/a/b/MapServer/ ", None)
          == ("http://127.0.0.1:6080/a/b/MapServer", MAP_SERVER)
          and check_target("https://h.example/x/ImageServer", "t")[1]
          == IMAGE_SERVER
          and check_target("http://127.0.0.1/x/FeatureServer", "t")[1]
          == FEATURE_SERVER,
          "service kind comes from the last path segment, a slash is dropped")
    check(check_target("http://h.example/x/MapServer", None)[1] == MAP_SERVER
          and check_target("http://localhost/x/MapServer", "t")[1]
          == MAP_SERVER and check_target("http://[::1]:80/x/MapServer",
                                         "t")[1] == MAP_SERVER,
          "plain http is allowed with no token, and to loopback with one")
    raises(lambda: check_target("http://h.example/x/MapServer", "t"),
           "a token is never sent over plain http to another host"
           "  <-- pinned defect")
    for bad in ("https://u:p@h.example/x/MapServer",
                "ftp://u:hunter2@h.example/x/MapServer",
                "https://h.example/x/MapServer?token=abc",
                "https://h.example/x/MapServer#frag",
                "file:///x/MapServer", "https:///x/MapServer",
                "https://h.example/x/Other", "https://h.example:99999/x/"
                "MapServer", "http://[::1/MapServer", ""):
        raises(lambda b=bad: check_target(b, None),
               "the URL %r is refused" % bad)
    try:
        check_target("ftp://u:hunter2@h.example/x/MapServer?token=abc", None)
    except ValueError as exc:
        check("hunter2" not in str(exc) and "abc" not in str(exc),
              "a refusal never repeats a password or token from the URL"
              "  <-- pinned defect")

    # ---- which layers and views are probed
    doc = {"layers": [{"id": 0, "subLayerIds": None},
                      {"id": 1, "subLayerIds": [2, 3]},
                      {"id": 2, "subLayerIds": []}, {"id": 3},
                      "junk", {"id": "4"}, {"id": True}]}
    check(pick_layers(doc, []) == [0, 2, 3]
          and pick_layers(doc, [7, 1]) == [7, 1]
          and pick_layers({"layers": "x"}, []) == []
          and pick_layers({}, []) == [],
          "group layers and junk entries are left out, --layers overrides")
    views, why = views_from_root({"extent": EXTENT})
    check([v.name for v in views] == ["view overview", "view detail"]
          and views[0].bbox == (-1000.0, -1000.0, 1000.0, 1000.0)
          and views[1].bbox == (-10.0, -10.0, 10.0, 10.0)
          and views[0].wkid == 3857 and why == "",
          "an image service gets the whole extent and a 1/100 detail view")
    wide = {"extent": {"xmin": 0, "ymin": 0, "xmax": 400, "ymax": 100,
                       "spatialReference": {"latestWkid": 4326}}}
    views, why = views_from_root(wide)
    check(views[0].bbox == (0.0, -150.0, 400.0, 250.0)
          and views[0].wkid == 4326,
          "a wide extent becomes a square view, and latestWkid is used")
    views, why = views_from_root({"fullExtent": {
        "xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
        "spatialReference": {"wkid": 26917}})
    check(len(views) == 2 and views[0].wkid == 26917,
          "fullExtent and a top-level spatial reference are the fallbacks")

    def broken(**change):
        ext = dict(EXTENT)
        ext.update(change)
        return {"extent": ext}
    reasons = [views_from_root(d)[1] for d in (
        {}, {"extent": {"xmin": 0}}, broken(spatialReference=None),
        broken(spatialReference={"wkid": "x"}),
        broken(xmin="a"), broken(xmax=True), broken(ymin=float("nan")),
        broken(xmax=-1000.0), broken(ymax=-1000.0))]
    check(reasons[0] == "the service root has no extent"
          and reasons[1] == "the extent has no well-known spatial reference "
          "id" and reasons[2] == reasons[1] and reasons[3] == reasons[1]
          and set(reasons[4:7]) == {"the extent has no usable corners"}
          and set(reasons[7:]) == {"the extent is empty"},
          "an extent that cannot be probed says why")
    check(build_plan(IMAGE_SERVER, {"extent": EXTENT}, [])[0][0].name
          == "view overview"
          and build_plan(MAP_SERVER, doc, [])[0][1].name == "layer 2"
          and build_plan(MAP_SERVER, {"layers": []}, [])
          == ([], "the service lists no layer to query"),
          "the plan is views for images and layers otherwise")

    layer_url = probe_url("http://h/x/MapServer", Probe("layer 3", layer=3))
    query = dict(urllib.parse.parse_qsl(layer_url.split("?", 1)[1]))
    check(layer_url.startswith("http://h/x/MapServer/3/query?")
          and query == {"where": "1=1", "outFields": "*",
                        "returnGeometry": "false", "resultRecordCount": "1",
                        "f": "json"},
          "a layer probe asks for one feature, no geometry, as JSON")
    image_url = probe_url("http://h/x/ImageServer", Probe(
        "view overview", bbox=(1.0, 2.0, 3.0, 4.0), wkid=3857), 0)
    query = dict(urllib.parse.parse_qsl(image_url.split("?", 1)[1]))
    check(image_url.startswith("http://h/x/ImageServer/exportImage?")
          and query["f"] == "image" and query["size"] == "400,400"
          and query["bbox"] == "1.000000,2.000000,3.000000,4.000000"
          and query["bboxSR"] == "3857" and query["imageSR"] == "3857"
          and query["_svcsmoke"] == "0",
          "an image probe asks for image bytes, and round 0 still gets a "
          "nonce  <-- pinned defect")

    # ---- verdicts and exit codes
    def result(plan=1, root=None, failed=0):
        res = TargetResult(Target("http://h/x/MapServer", MAP_SERVER))
        res.plan = [Probe("layer %d" % i, layer=i) for i in range(plan)]
        res.root = root
        res.stats = [ProbeStat(p.name) for p in res.plan]
        if failed:
            res.stats[0].add((NOT_JSON, "d", 200))
        return res
    check(target_verdict(result(), 17, 17) == PASS
          and target_verdict(result(), 16, 17) == UNDERSAMPLED
          and target_verdict(result(0), 17, 17) == NOTHING
          and target_verdict(result(failed=1), 1, 17) == FAIL
          and target_verdict(result(0, root=(NOT_JSON, "d", 200)), 17, 17)
          == FAIL,
          "FAIL beats UNDERSAMPLED, and a root that fails is a FAIL")

    def coded(*verdicts):
        out = []
        for verdict in verdicts:
            res = result()
            res.verdict = verdict
            out.append(res)
        return exit_code(out)
    check(coded(PASS) == 0 and coded(PASS, UNDERSAMPLED) == 3
          and coded(UNDERSAMPLED, NOTHING) == 4
          and coded(NOTHING, FAIL) == 1 and exit_code([]) == 4,
          "exit codes: pass 0, fail 1, undersampled 3, nothing probed 4")

    # ---- the tier file and the gate
    tiers = parse_tiers("# order\n\ndb: http://h:1/a/MapServer http://h:2/a/"
                        "MapServer\napp : http://h:3/b/ImageServer\nweb: u\n")
    check(tiers == [("db", ["http://h:1/a/MapServer", "http://h:2/a/MapServer"
                            ]), ("app", ["http://h:3/b/ImageServer"]),
                    ("web", ["u"])],
          "a tier line is a name, a colon and its URLs, in file order")
    for text in ("db http://h/MapServer", ": u", "db:", "a b: u",
                 "a: u\na: v", "# only a comment\n", ""):
        raises(lambda t=text: parse_tiers(t),
               "the tier text %r is refused" % text)
    check([t[0] for t in tiers_before(tiers, "web")] == ["db", "app"]
          and [t[0] for t in tiers_before(tiers, "app")] == ["db"],
          "the gate for a tier covers the tiers above it, not itself")
    raises(lambda: tiers_before(tiers, "db"),
           "the first tier has nothing above it, so its gate is refused"
           "  <-- pinned defect")
    raises(lambda: tiers_before(tiers, "nope"), "an unknown tier is refused")

    def gate_of(codes, wait, interval=1.0):
        """run_gate over canned survey codes and a fake clock."""
        clock = [0.0]
        calls, said = [], []
        sequence = list(codes)

        def fake_survey():
            calls.append(clock[0])
            res = result()
            res.verdict = {0: PASS, 1: FAIL, 3: UNDERSAMPLED}[sequence.pop(0)]
            return [res]

        def sleep(seconds):
            clock[0] += seconds
        code = run_gate(fake_survey, wait, interval, lambda: clock[0], sleep,
                        said.append)[1]
        return code, calls, said
    code, calls, said = gate_of([0], 0)
    check(code == 0 and calls == [0.0] and said == [],
          "an open gate answers at once")
    code, calls, said = gate_of([1], 0)
    check(code == 1 and len(calls) == 1,
          "with no wait a closed gate is one look and the survey's own code")
    code, calls, said = gate_of([1, 3, 0], 10)
    check(code == 0 and calls == [0.0, 1.0, 2.0] and len(said) == 2
          and "attempt 1 (exit 1)" in said[0],
          "a gate polls until it opens and says why it is closed")
    code, calls, said = gate_of([1] * 20, 3)
    check(code == 5 and calls == [0.0, 1.0, 2.0, 3.0],
          "a gate still closed when the wait runs out exits 5, not the "
          "survey's code  <-- pinned defect")
    code, calls, said = gate_of([3, 3], 1.5, 1.0)
    check(code == 5 and len(calls) == 2,
          "a wait that ends before the next poll stops there")

    # ---- the report
    ctx = {"rounds": 17, "needed": 17, "fraction": 0.25, "faulty": "1/4",
           "max_miss": 0.01, "rounds_note": " (the fewest that meet "
           "--max-miss)"}
    good, bad, none, down = result(), result(failed=1), result(0), result(
        0, root=(TRANSPORT, "no response: timed out", None))
    for res, word in ((good, PASS), (bad, FAIL), (none, NOTHING),
                      (down, FAIL)):
        res.verdict = word
    none.reason = "the service lists no layer to query"
    bad.stats[0].requests = 17
    bad.target.tier = "db"
    text = "\n".join(describe([good, bad, none, down], ctx))
    check("layer 0: 1 of 17 failed (5.9%): NOT_JSON x1. First: d (HTTP 200, "
          "recorded only)" in text
          and "nothing probed: the service lists no layer" in text
          and "root: TRANSPORT, no response: timed out" in text
          and "[db] http://h/x/MapServer" in text
          and "probability 0.0075 (limit 0.0100)" in text
          and text.rstrip().endswith("whatever their state says."),
          "the report names each failure, its kind, its rate and the status")
    for results, word in (([good], "VERDICT: PASS. Every probe returned"),
                          ([none], "VERDICT: NOTHING PROBED"), ([], "NOTHING"),
                          ([good, bad], "VERDICT: FAILED. 1 target(s)")):
        check(word in "\n".join(describe(results, ctx)),
              "the verdict line for %s reads %r" % (
                  "/".join(r.verdict for r in results) or "no targets", word))
    under = result()
    under.verdict = UNDERSAMPLED
    ctx_short = dict(ctx, rounds=8)
    check("0.1001 chance of missing a fault on 1/4 of the instances, over the "
          "limit of 0.0100. Run at least 17." in "\n".join(
              describe([under], ctx_short)),
          "an undersampled run says the miss chance and the rounds to run")
    rows = report_rows([good, bad, none, down])
    check([(r["probe"], r["failed"]) for r in rows]
          == [("layer 0", 0), ("layer 0", 1), ("", 0), ("", 1)]
          and rows[1]["kinds"] == "NOT_JSON:1" and rows[1]["tier"] == "db"
          and rows[2]["first_failure"].startswith("the service lists")
          and rows[3]["kinds"] == TRANSPORT,
          "the report CSV has one row per probe, and one per unprobed target")

    # ---- the real fetcher, against a stub
    rng = random.Random(11)
    stub = serve(pool(1, 0, rng, layers=((0, None), (1, [2]), (2, None))))
    res = survey([target(stub, "/svc/MapServer")], opts(rounds=3), make_fetcher(
        None, 5))[0]
    queried = [r[0] for r in stub.requests]
    check(res.verdict == PASS and res.requests == 7
          and sorted(set(queried)) == ["/svc/MapServer", "/svc/MapServer/0/"
                                       "query", "/svc/MapServer/2/query"]
          and queried.count("/svc/MapServer/0/query") == 3,
          "a healthy service passes: one root, then each leaf layer per round"
          ", and the group layer is never queried")
    check(all("token" not in p and not any("T0K3N" in v for v in p.values())
              for _, p, _ in stub.requests), "no request carried a token")
    secret = "T0K3N-abc123"
    survey([target(stub, "/svc/MapServer")], opts(rounds=1, token=secret),
           make_fetcher(secret, 5))
    last = stub.requests[-1]
    check(last[2].get("X-Esri-Authorization") == "Bearer " + secret
          and all(secret not in str(r[0]) + str(r[1])
                  for r in stub.requests),
          "the token goes in X-Esri-Authorization and never in a URL"
          "  <-- pinned defect")

    stub = serve(lambda path, params, headers: {
        "body": map_root() if path.endswith("/MapServer") else ERR})
    res = survey([target(stub, "/svc/MapServer")], opts(rounds=2),
                 make_fetcher(None, 5))[0]
    check(res.verdict == FAIL and res.stats[0].failed == 2
          and res.stats[0].first[2] == 200
          and res.stats[0].kinds == {SERVER_ERROR: 2},
          "a service that answers 200 with an error every time is a FAIL "
          "with the 200 recorded  <-- pinned defect")

    stub = serve(lambda path, params, headers: {
        "body": map_root() if path.endswith("/MapServer") else HTML,
        "ctype": "text/html"} if path.endswith("query") else {
        "body": map_root()})
    res = survey([target(stub, "/svc/MapServer")], opts(rounds=1),
                 make_fetcher(None, 5))[0]
    check(res.stats[0].first[0] == NOT_JSON
          and "Sign In" in res.stats[0].first[1],
          "a login page where JSON was expected is a failed probe")

    landing = serve(lambda path, params, headers: {"body": GOOD})
    stub = serve(lambda path, params, headers: {
        "status": 302, "body": b"", "headers": {
            "Location": landing.url("/login?token=SECRETTOKEN")}})
    reply = make_fetcher("SECRETTOKEN", 5)(stub.url("/svc/MapServer"))
    check(reply.error == REDIRECT and landing.requests == []
          and "SECRETTOKEN" not in reply.detail and "?" not in reply.detail
          and reply.status == 302,
          "a redirect is a failed probe, is not followed, and its query is "
          "dropped  <-- pinned defect")

    probe_sock = socket.socket()
    probe_sock.bind(("127.0.0.1", 0))
    dead = probe_sock.getsockname()[1]
    probe_sock.close()
    reply = make_fetcher(None, 5)("http://127.0.0.1:%d/x/MapServer" % dead)
    check(reply.error == TRANSPORT and reply.status is None
          and reply.detail == "no response: ConnectionRefusedError",
          "a refused connection is a TRANSPORT failure with no status")
    reply = make_fetcher(None, 5)("https://127.0.0.1:%d/x/MapServer"
                                  % stubs[0].port)
    check(reply.error == TRANSPORT and reply.status is None
          and reply.detail.startswith("no response: "),
          "a failed TLS handshake is a TRANSPORT failure, never a pass")
    proxy_before = os.environ.get("http_proxy")
    os.environ["http_proxy"] = "http://127.0.0.1:%d" % dead
    try:
        reply = make_fetcher(None, 5)(stubs[0].url("/svc/MapServer"))
    finally:
        _restore_env("http_proxy", proxy_before)
    check(reply.error is None and reply.status == 200,
          "a proxy named in the environment is ignored, so it never sees the "
          "token header  <-- pinned defect")
    slow = serve(lambda path, params, headers: {"close": True,
                                                         "delay": 0.6})
    reply = make_fetcher(None, 0.15)(slow.url("/x"))
    check(reply.error == TRANSPORT and reply.detail == "no response: timed out",
          "a response slower than --timeout is a TRANSPORT failure")
    mute = serve(lambda path, params, headers: {"close": True})
    reply = make_fetcher(None, 5)(mute.url("/x"))
    check(reply.error == TRANSPORT and "RemoteDisconnected" in reply.detail,
          "a server that hangs up without answering is a TRANSPORT failure")
    cut = serve(lambda path, params, headers: {
        "body": b"0123456789", "length": 500, "ctype": "image/png"})
    reply = make_fetcher(None, 5)(cut.url("/x"))
    check(reply.error == TRANSPORT
          and reply.detail == "body cut short: 10 of 500 bytes",
          "a body shorter than its Content-Length fails, so a half-sent "
          "image cannot pass  <-- pinned defect")
    big = serve(lambda path, params, headers: {"body": b"x" * 100})
    reply = make_fetcher(None, 5, max_body=50)(big.url("/x"))
    check(reply.error == TOO_LARGE and reply.body == b"",
          "a body over the cap fails and is not kept")
    reply = make_fetcher(None, 5)(big.url("/x"))
    check(reply.error is None and reply.status == 200
          and len(reply.body) == 100,
          "a body under the cap is read whole")

    stub = serve(image_pool(1, 0, random.Random(1)))
    res = survey([target(stub, "/img/ImageServer", IMAGE_SERVER)],
                 opts(rounds=2), make_fetcher(None, 5))[0]
    shots = [r[1] for r in stub.requests if r[0].endswith("exportImage")]
    check(res.verdict == PASS and res.requests == 5
          and [s["bbox"] for s in shots[:2]] == [
              "-1000.000000,-1000.000000,1000.000000,1000.000000",
              "-10.000000,-10.000000,10.000000,10.000000"]
          and all(s["f"] == "image" and s["bboxSR"] == "3857" for s in shots),
          "an image service is asked for two views per round, as image bytes")
    stub = serve(image_pool(1, 1, random.Random(1)))
    res = survey([target(stub, "/img/ImageServer", IMAGE_SERVER)],
                 opts(rounds=2), make_fetcher(None, 5))[0]
    check(res.verdict == FAIL and res.stats[0].kinds == {NOT_IMAGE: 2}
          and res.stats[1].kinds == {NOT_IMAGE: 2},
          "an image service that answers 200 with a page fails both views"
          "  <-- pinned defect")
    stub = serve(image_pool(1, 0, random.Random(1), image=PNG[:300]))
    res = survey([target(stub, "/img/ImageServer", IMAGE_SERVER)],
                 opts(rounds=1), make_fetcher(None, 5))[0]
    check(res.stats[0].kinds == {SMALL_IMAGE: 1},
          "a tiny image is a failed probe")

    stub = serve(pool(1, 0, random.Random(1)))
    survey([target(stub, "/svc/MapServer")], opts(rounds=4, bust=True),
           make_fetcher(None, 5))
    nonces = [r[1].get("_svcsmoke") for r in stub.requests
              if r[0].endswith("query")]
    check(nonces == ["0", "1", "2", "3"],
          "--bust-cache sends a different nonce each round, starting at 0")
    stub = serve(pool(1, 0, random.Random(1)))
    survey([target(stub, "/svc/MapServer")], opts(rounds=2),
           make_fetcher(None, 5))
    check(all("_svcsmoke" not in r[1] for r in stub.requests),
          "without --bust-cache no extra parameter is sent")

    stub = serve(lambda path, params, headers: {
        "status": 503, "body": HTML, "ctype": "text/html"})
    res = survey([target(stub, "/svc/MapServer")], opts(),
                 make_fetcher(None, 5))[0]
    check(res.verdict == FAIL and res.root[0] == NOT_JSON
          and res.root[2] == 503 and res.requests == 1 and res.stats == [],
          "a root that fails is a FAIL with one request spent")
    stub = serve(lambda path, params, headers: {
        "body": json.dumps({"layers": [{"id": 0, "subLayerIds": [1]}]}
                           ).encode()})
    res = survey([target(stub, "/svc/MapServer")], opts(),
                 make_fetcher(None, 5))[0]
    check(res.verdict == NOTHING and exit_code([res]) == 4
          and res.reason == "the service lists no layer to query",
          "a service whose only layer is a group probed nothing, exit 4"
          "  <-- pinned defect")
    stub = serve(pool(1, 0, random.Random(1)))
    res = survey([target(stub, "/svc/MapServer")], opts(rounds=2, needed=17),
                 make_fetcher(None, 5))[0]
    check(res.verdict == UNDERSAMPLED and exit_code([res]) == 3,
          "a clean run with too few rounds is UNDERSAMPLED, not a pass"
          "  <-- pinned defect")
    stub = serve(pool(1, 1, random.Random(1)))
    res = survey([target(stub, "/svc/MapServer")], opts(rounds=5),
                 make_fetcher(None, 5), stop_on_fail=True)[0]
    full = survey([target(stub, "/svc/MapServer")], opts(rounds=5),
                  make_fetcher(None, 5))[0]
    check(res.requests == 2 and full.requests == 6,
          "a gate stops at the round a failure shows in, a plain run does not")
    stub = serve(pool(1, 0, random.Random(1)))
    raises(lambda: survey([target(stub, "/svc/MapServer")],
                          opts(rounds=50, max_requests=20),
                          make_fetcher(None, 5)),
           "a plan over --max-requests is refused")
    check([r[0] for r in stub.requests] == ["/svc/MapServer"],
          "and nothing but the root was requested before it was refused")
    stub = serve(pool(1, 0, random.Random(1)))
    survey([target(stub, "/svc/MapServer")], opts(rounds=1, layers=[5]),
           make_fetcher(None, 5))
    check([r[0] for r in stub.requests] == ["/svc/MapServer",
                                            "/svc/MapServer/5/query"],
          "--layers names the layers to query whatever the root lists")

    # ---- a pool where only some instances are bad, over the network
    stub = serve(pool(8, 2, random.Random(5)))
    seen = 0
    runs = 100
    for _ in range(runs):
        res = survey([target(stub, "/svc/MapServer")], opts(rounds=8),
                     make_fetcher(None, 5))[0]
        seen += res.verdict == FAIL
    check(60 <= seen <= 98,
          "a pool with 2 of 8 instances bad is caught by 8 rounds in most "
          "runs, and missed in the rest (caught %d of %d)" % (seen, runs))
    stub = serve(pool(8, 2, random.Random(5)))
    one = survey([target(stub, "/svc/MapServer")], opts(rounds=1),
                 make_fetcher(None, 5))[0]
    check(one.requests == 2, "a single pass is one root and one query")

    # ---- the command line
    stub = serve(pool(1, 0, random.Random(3)))
    code, out, err = run([stub.url("/svc/MapServer")])
    check(code == 0 and "VERDICT: PASS" in out
          and "17 round(s) per probe (the fewest that meet --max-miss)" in out
          and len(stub.requests) == 18 and err == "",
          "a healthy service passes at the 17 rounds the miss limit demands")
    code, out, err = run([stub.url("/svc/MapServer"), "--rounds", "8"])
    check(code == 3 and "VERDICT: UNDERSAMPLED" in out
          and "0.1001 chance" in out and "Run at least 17." in out
          and "(set by --rounds)" in out,
          "eight rounds exit 3 and say how many are needed  <-- pinned defect")
    code, out, err = run([stub.url("/svc/MapServer"), "--faulty", "1/2",
                          "--max-miss", "0.1"])
    check(code == 0 and "4 round(s) per probe" in out,
          "a bigger faulty share needs fewer rounds")
    bad_stub = serve(pool(1, 1, random.Random(3)))
    code, out, err = run([stub.url("/svc/MapServer"),
                          bad_stub.url("/svc/MapServer")])
    check(code == 1 and "VERDICT: FAILED. 1 target(s)" in out
          and "layer 0: 17 of 17 failed (100.0%): SERVER_ERROR x17" in out
          and "error property: Unable to complete operation." in out,
          "one failing target among two fails the run with its rate")
    code, out, err = run(["--layers", "4", stub.url("/svc/MapServer"),
                          "--rounds", "1"])
    check(code == 3 and stub.requests[-1][0] == "/svc/MapServer/4/query",
          "--layers 4 queries layer 4")
    for argv, why in (
            ([], "no URL and no tiers"),
            ([stub.url("/svc/MapServer"), "--apply"], "--apply without --out"),
            ([stub.url("/svc/MapServer"), "--wait", "5"],
             "--wait without a gate"),
            ([stub.url("/svc/MapServer"), "--tiers", "t"],
             "--tiers without --before"),
            ([stub.url("/svc/MapServer"), "--before", "t"],
             "--before without --tiers"),
            ([stub.url("/svc/MapServer"), "--tiers", "t", "--before", "b"],
             "URLs together with --tiers"),
            ([stub.url("/svc/MapServer"), "--rounds", "0"], "--rounds 0"),
            ([stub.url("/svc/MapServer"), "--timeout", "0"], "--timeout 0"),
            ([stub.url("/svc/MapServer"), "--interval", "0"], "--interval 0"),
            ([stub.url("/svc/MapServer"), "--max-requests", "0"],
             "--max-requests 0"),
            ([stub.url("/svc/MapServer"), "--min-image-bytes", "-1"],
             "--min-image-bytes -1"),
            ([stub.url("/svc/MapServer"), "--max-miss", "2"], "--max-miss 2"),
            ([stub.url("/svc/MapServer"), "--faulty", "x"], "--faulty x"),
            ([stub.url("/svc/MapServer"), "--layers", "a,1"], "--layers a,1"),
            (["http://127.0.0.1:1/x/Nope"], "a URL that is not a service")):
        code, out, err = run(argv)
        check(code == 64 and out == "" and err != "",
              "a usage error exits 64 and prints nothing to stdout: " + why)
    # Every prefix case is otherwise a valid run, so a parser that accepted
    # the prefix would run, write or exit 0 instead of exiting 64.
    prefixed = os.path.join(tmp, "prefixed.csv")
    base = [stub.url("/svc/MapServer")]
    for argv, why in (
            (base + ["--out", prefixed, "--ap"], "--ap"),
            (base + ["--out", prefixed, "--app"], "--app"),
            (base + ["--ou", prefixed, "--apply"], "--ou"),
            (base + ["--fault", "1/2"], "--fault"),
            (base + ["--round", "3"], "--round"),
            (base + ["--bust", "--rounds", "1"], "--bust")):
        code, out, err = run(argv)
        check(code == 64 and out == "" and not os.path.exists(prefixed),
              "a prefix of a flag is refused, not read as the flag: " + why
              + ("  <-- pinned defect" if why == "--ap" else ""))
    code, out, err = run([stub.url("/svc/MapServer"), "--max-requests", "5"])
    check(code == 64 and "Nothing was sampled" in err and out == "",
          "a plan over --max-requests exits 64 before sampling")
    code, out, err = run(["--tiers", "t", "--before", "b", "--wait", "-1"])
    check(code == 64 and "--wait cannot be negative" in err,
          "a negative wait is refused")
    code, out, err = run(["http://203.0.113.9/x/MapServer"], token="abc")
    check(code == 64 and "clear" in err and "abc" not in err,
          "a token with a plain http URL to another host exits 64 before any "
          "request  <-- pinned defect")
    check(_layer_ids("3,3, 4") == [3, 4], "--layers drops a repeated id")

    # ---- --apply is the only thing that writes
    out_path = os.path.join(tmp, "report.csv")
    stub = serve(pool(1, 0, random.Random(3)))
    argv = [stub.url("/svc/MapServer"), "--out", out_path, "--rounds", "17"]
    code, out, err = run(argv)
    check(code == 0 and not os.path.exists(out_path)
          and "Check only" in out and os.listdir(tmp) == [],
          "without --apply nothing is written  <-- pinned defect")
    code, out, err = run(argv + ["--apply"])
    with open(out_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    check(code == 0 and "wrote " + out_path in out and len(rows) == 1
          and rows[0]["probe"] == "layer 0" and rows[0]["requests"] == "17"
          and rows[0]["verdict"] == PASS,
          "--apply writes the report, one row per probe")
    code, out, err = run([stub.url("/svc/MapServer"), "--out",
                          os.path.join(tmp, "no", "dir", "r.csv"),
                          "--apply", "--rounds", "17"])
    check(code == 2 and "could not write" in err,
          "a report that cannot be written exits 2  <-- pinned defect")

    # ---- the token reaches nobody but the server
    secret = "TOKEN-9f3a77c1"
    leaky = serve(lambda path, params, headers: {
        "body": map_root()} if path.endswith("/MapServer") else {
        "body": json.dumps({"error": {"code": 498, "message":
                                      "invalid %s for 'token=%s'"
                                      % (secret, secret)}}).encode()})
    leak_csv = os.path.join(tmp, "leak.csv")
    code, out, err = run([leaky.url("/svc/MapServer"), "--out", leak_csv,
                          "--apply", "--rounds", "2"], token=secret)
    with open(leak_csv, encoding="utf-8") as handle:
        written = handle.read()
    check(code == 1 and secret not in out + err + written
          and "[token]" in out and "[token]" in written,
          "a token the server echoes back is removed from the screen and "
          "from the report  <-- pinned defect")

    # ---- the ordered-restart gate
    db1 = serve(pool(1, 0, random.Random(2)))
    db2 = serve(pool(1, 0, random.Random(2)))
    app = serve(pool(1, 0, random.Random(2)))
    web = serve(pool(1, 0, random.Random(2)))
    tier_path = os.path.join(tmp, "tiers.txt")
    with open(tier_path, "w", encoding="utf-8") as handle:
        handle.write("# dependency order\ndb: %s %s\napp: %s\nweb: %s\n" % (
            db1.url("/a/MapServer"), db2.url("/a/MapServer"),
            app.url("/a/MapServer"), web.url("/a/MapServer")))
    code, out, err = run(["--tiers", tier_path, "--before", "app"])
    check(code == 0 and "GATE: OPEN. Tier 'app' may restart." in out
          and "[db] " in out and len(db1.requests) > 0
          and len(db2.requests) > 0,
          "the gate for app opens when both db targets pass")
    check(app.requests == [] and web.requests == [],
          "and it never probes the tier being restarted or any tier after it"
          "  <-- pinned defect")
    code, out, err = run(["--tiers", tier_path, "--before", "db"])
    check(code == 64 and "no tier comes before it" in err,
          "the gate for the first tier is a usage error, not a quiet open")
    code, out, err = run(["--tiers", tier_path, "--before", "web",
                          "--rounds", "2"])
    check(code == 3 and "GATE: CLOSED" in out and "not proven healthy" in out,
          "a gate does not open on an undersampled pass  <-- pinned defect")
    down_stub = serve(pool(1, 1, random.Random(2)))
    down_path = os.path.join(tmp, "down.txt")
    with open(down_path, "w", encoding="utf-8") as handle:
        handle.write("db: %s\napp: u\n" % down_stub.url("/a/MapServer"))
    code, out, err = run(["--tiers", down_path, "--before", "app"])
    check(code == 1 and "GATE: CLOSED. Do not restart 'app'" in out
          and len(down_stub.requests) == 2,
          "a failing earlier tier closes the gate after one failed round")
    code, out, err = run(["--tiers", down_path, "--before", "app", "--wait",
                          "0.3", "--interval", "0.05"])
    check(code == 5 and "still not healthy" in out
          and "gate closed on attempt 1 (exit 1)" in out,
          "a gate that stays closed through --wait exits 5")
    recovering = {"n": 0}

    def flaky(path, params, headers):
        if path.endswith("/MapServer"):
            recovering["n"] += 1
            if recovering["n"] < 3:
                return {"status": 503, "body": HTML, "ctype": "text/html"}
            return {"body": map_root()}
        return {"body": GOOD}
    flaky_stub = serve(flaky)
    flaky_path = os.path.join(tmp, "flaky.txt")
    with open(flaky_path, "w", encoding="utf-8") as handle:
        handle.write("db: %s\napp: u\n" % flaky_stub.url("/a/MapServer"))
    code, out, err = run(["--tiers", flaky_path, "--before", "app", "--wait",
                          "30", "--interval", "0.05"])
    check(code == 0 and out.count("gate closed on attempt") == 2
          and "GATE: OPEN" in out,
          "a tier that comes back during --wait opens the gate")
    code, out, err = run(["--tiers", os.path.join(tmp, "absent.txt"),
                          "--before", "app"])
    check(code == 2 and out == "", "an unreadable tiers file exits 2")
    bad_tiers = os.path.join(tmp, "bad.txt")
    with open(bad_tiers, "w", encoding="utf-8") as handle:
        handle.write("no colon here\n")
    code, out, err = run(["--tiers", bad_tiers, "--before", "app"])
    check(code == 64 and "line 1" in err, "a malformed tiers file exits 64")
    code, out, err = run(["--tiers", tier_path, "--before", "app", "--out",
                          tier_path, "--apply"])
    check(code == 64 and "also the --tiers file" in err,
          "--out may not be the tiers file  <-- pinned defect")
    with open(tier_path, encoding="utf-8") as handle:
        check(handle.read().startswith("# dependency order"),
              "and the tiers file was left as it was")

    # ---- the harness, the crash exit and the import
    _restore_env("SVCSMOKE_SELFTEST_PROBE", "kept")
    kept = os.environ.get("SVCSMOKE_SELFTEST_PROBE")
    _restore_env("SVCSMOKE_SELFTEST_PROBE", None)
    check(kept == "kept" and "SVCSMOKE_SELFTEST_PROBE" not in os.environ,
          "the environment is put back as it was, set or unset")
    table = globals()
    original = table["survey"]

    def explode(*args, **kwargs):
        raise RuntimeError("boom")
    table["survey"] = explode
    try:
        code, out, err = run([stub.url("/svc/MapServer")])
    finally:
        table["survey"] = original
    check(code == 70 and "unexpected RuntimeError: boom" in err
          and "fault in the tool" in err,
          "an unexpected exception exits 70, not the 1 that means a service "
          "failed  <-- pinned defect")
    original = table["self_test"]
    table["self_test"] = lambda: 7
    try:
        code = run(["--self-test"])[0]
    finally:
        table["self_test"] = original
    check(code == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")

    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the ValueError it is looking for")

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    # The footer on its red path. A footer that could only print 0 failed
    # would hide every FAIL line above it from anyone who reads the last line.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    # Importing the module runs nothing and opens no socket.
    spec = importlib.util.spec_from_file_location(
        "svcsmoke_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.miss_probability(0.5, 1) == 0.5,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="svcsmoke.py",
        description="Sample each ArcGIS Server service instance repeatedly "
                    "and pass it only on features or image bytes.",
        epilog="The token is read from %s. Nothing is written without "
               "--apply." % TOKEN_ENV,
        allow_abbrev=False,
    )
    ap.add_argument("urls", nargs="*", metavar="URL",
                    help="a MapServer, FeatureServer or ImageServer URL. One "
                         "per machine to sample.")
    ap.add_argument("--faulty", default=DEFAULT_FAULTY,
                    help="the smallest share of instances with a fault to be "
                         "caught, as K/M (default %s)" % DEFAULT_FAULTY)
    ap.add_argument("--max-miss", dest="max_miss", type=float,
                    default=DEFAULT_MAX_MISS,
                    help="the largest chance of a clean run when that share "
                         "is faulty (default %g)" % DEFAULT_MAX_MISS)
    ap.add_argument("--rounds", type=int,
                    help="rounds per probe. Default: the fewest that meet "
                         "--max-miss. Fewer ends in UNDERSAMPLED, exit 3.")
    ap.add_argument("--layers",
                    help="comma-separated layer ids to query instead of "
                         "every layer. Use it to leave out a layer that is "
                         "meant to be empty. Ignored for an ImageServer.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="seconds to wait for one response (default %g)"
                         % DEFAULT_TIMEOUT)
    ap.add_argument("--min-image-bytes", dest="min_image_bytes", type=int,
                    default=DEFAULT_MIN_IMAGE_BYTES,
                    help="an image smaller than this counts as blank "
                         "(default %d)" % DEFAULT_MIN_IMAGE_BYTES)
    ap.add_argument("--bust-cache", dest="bust_cache", action="store_true",
                    help="add a different throwaway parameter to each round, "
                         "so a cache keyed on the URL cannot answer")
    ap.add_argument("--max-requests", dest="max_requests", type=int,
                    default=DEFAULT_MAX_REQUESTS,
                    help="refuse a plan larger than this (default %d)"
                         % DEFAULT_MAX_REQUESTS)
    ap.add_argument("--tiers", help="a file with one 'name: url url' line "
                                    "per tier, in dependency order")
    ap.add_argument("--before",
                    help="the tier about to restart. The gate opens only when "
                         "every tier above it in --tiers passes.")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="poll the gate for up to this many seconds. Exit 5 "
                         "if it is still closed.")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="seconds between polls (default %g)"
                         % DEFAULT_INTERVAL)
    ap.add_argument("--out", help="path for the report CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _layer_ids(text):
    """'0, 3,3' -> [0, 3]. ValueError on anything that is not an id."""
    ids = []
    for piece in text.split(","):
        piece = piece.strip()
        if not piece.isdigit():
            raise ValueError("--layers must be comma-separated layer ids, not "
                             "%r" % tidy(text, 40))
        if int(piece) not in ids:
            ids.append(int(piece))
    return ids


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _usage(message):
    print("error: %s" % message, file=sys.stderr)
    return 64


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a service failed'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the services." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read or written. A mistyped --ap is a usage error.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    gate = bool(args.tiers or args.before)
    if gate and not (args.tiers and args.before):
        return _usage("--tiers and --before go together.")
    if gate and args.urls:
        return _usage("name URLs or --tiers, not both.")
    if not gate and not args.urls:
        return _usage("give at least one service URL, or --tiers with "
                      "--before. Use --self-test to verify the tool without "
                      "a server.")
    if args.wait and not gate:
        return _usage("--wait needs --tiers and --before.")
    if args.wait < 0 or args.interval <= 0 or args.timeout <= 0:
        return _usage("--wait cannot be negative, and --interval and "
                      "--timeout must be above zero.")
    if args.rounds is not None and args.rounds < 1:
        return _usage("--rounds must be at least 1.")
    if args.max_requests < 1 or args.min_image_bytes < 0:
        return _usage("--max-requests must be at least 1 and "
                      "--min-image-bytes cannot be negative.")
    if args.apply and not args.out:
        return _usage("--apply needs --out.")
    if gate and args.out and _same_file(args.out, args.tiers):
        return _usage("--out %s is also the --tiers file. Name a new file."
                      % args.out)

    token = os.environ.get(TOKEN_ENV) or None
    try:
        fraction = parse_faulty(args.faulty)
        needed = rounds_needed(fraction, args.max_miss)
        layers = _layer_ids(args.layers) if args.layers else []
    except ValueError as exc:
        return _usage(str(exc))

    try:
        if gate:
            tiers = parse_tiers(read_text(args.tiers))
            earlier = tiers_before(tiers, args.before)
            pairs = [(name, url) for name, urls in earlier for url in urls]
        else:
            pairs = [(None, url) for url in args.urls]
        targets = []
        for tier, url in pairs:
            checked, kind = check_target(url, token)
            targets.append(Target(checked, kind, tier))
    except ValueError as exc:
        return _usage(str(exc))
    except (IOError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    rounds = args.rounds or needed
    options = {"rounds": rounds, "needed": needed, "layers": layers,
               "bust": args.bust_cache, "min_bytes": args.min_image_bytes,
               "max_requests": args.max_requests, "token": token}
    context = {"rounds": rounds, "needed": needed, "fraction": fraction,
               "faulty": args.faulty.replace(" ", ""),
               "max_miss": args.max_miss,
               "rounds_note": (" (set by --rounds)" if args.rounds else
                               " (the fewest that meet --max-miss)")}
    fetch = make_fetcher(token, args.timeout)

    try:
        if gate:
            results, code = run_gate(
                lambda: survey(targets, options, fetch, True), args.wait,
                args.interval, time.monotonic, time.sleep, print)
        else:
            results = survey(targets, options, fetch)
            code = exit_code(results)
    except ValueError as exc:
        return _usage(str(exc))

    for line in describe(results, context):
        print(line)
    if gate:
        print(_gate_line(args.before, code))

    if args.out:
        if args.apply:
            # A traceback here would exit 1, the code for "a service failed".
            try:
                write_report(args.out, results)
            except (IOError, OSError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    return code


def _gate_line(name, code):
    """The one line a restart script greps for."""
    if code == 0:
        return "GATE: OPEN. Tier '%s' may restart." % name
    if code == 5:
        return ("GATE: CLOSED. The tiers before '%s' were still not healthy "
                "when --wait ran out." % name)
    return ("GATE: CLOSED. Do not restart '%s' yet; the tiers before it are "
            "not proven healthy." % name)


if __name__ == "__main__":
    sys.exit(main())
