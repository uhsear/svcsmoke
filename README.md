# svcsmoke

Sample each ArcGIS Server service repeatedly and state how likely the sampling was to miss a
fault. Pass a service only when it returned a feature or the bytes of an image.

A service reads STARTED. Nothing alerts. For hours, every `exportImage` call on an
imagery service fails. The service description answers. The admin page is green. Everything a
check can read says the service is up, because the status says what state the service is in, and
it does not say whether a request returns anything. Esri's service status operation returns two
fields, `configuredState` and `realTimeState`, and neither one is the result of an export
([Service status][status]).

The first fix is a sweep: ask every layer once, and run the sweep after each restart. It finds
nothing. Say the fault sits on 2 of the 8 instances in the pool. One request is answered by one
instance. A request that lands on a good instance passes. The sweep says the service is fine,
and a quarter of the people using it still get the error.

The second fix is to repeat the sweep. Eight rounds feels like plenty. It is not. If each request
lands on a random instance, eight rounds all land on the 6 good instances with probability
(6/8) to the power 8, which is 10 percent. One run in ten passes a service that is broken for a
quarter of its users. A sweep does not print that figure. This tool prints it, and it refuses to
call a run healthy when the number of rounds does not support the claim.

The third trap is the pass condition. Esri says a request that completes with HTTP status 200
"has been processed", and that the client needs "to check for the presence of an error property"
in the body. A check that reads the status passes an error. So this tool passes a probe only on
evidence: a feature in the body of a query, or image bytes in the body of an export. The status is
recorded on the first failure of each probe and on a failed service root, and it never decides.

```
$ python svcsmoke.py --self-test
svcsmoke self-test: loopback stub only, no network, no credentials
--------------------------------------------------------------------
PASS  2/8 and 1/4 are the same quarter of the instances
...
PASS  eight rounds against a fault on 2 of 8 instances miss it 10 percent of the time  <-- pinned defect
PASS  eight rounds do not meet a 10 percent limit, nine do  <-- pinned defect
...
PASS  HTTP 200 with an error body fails, and the 200 is recorded  <-- pinned defect
PASS  a count with no feature is not a pass  <-- pinned defect
...
PASS  a token is never sent over plain http to another host  <-- pinned defect
PASS  a redirect is a failed probe, is not followed, and its query is dropped  <-- pinned defect
PASS  a body shorter than its Content-Length fails, so a half-sent image cannot pass  <-- pinned defect
PASS  a service whose only layer is a group probed nothing, exit 4  <-- pinned defect
PASS  a clean run with too few rounds is UNDERSAMPLED, not a pass  <-- pinned defect
...
PASS  a prefix of a flag is refused, not read as the flag: --ap  <-- pinned defect
PASS  a token the server echoes back is removed from the screen and from the report  <-- pinned defect
PASS  a 251 character token echoed in an error message is removed whole, not cut to a prefix first  <-- pinned defect
PASS  a map service without Data is probed through export as image bytes, never through a layer query  <-- pinned defect
PASS  and it never probes the tier being restarted or any tier after it  <-- pinned defect
PASS  a gate does not open on an undersampled pass  <-- pinned defect
...
PASS  a URL that holds only a user name is refused  <-- pinned defect
PASS  a FeatureServer is queried layer by layer and never exported  <-- pinned defect
PASS  a redirect to an address with a password does not print it  <-- pinned defect
PASS  a token echoed in the Content-Type header is removed before the type is cut  <-- pinned defect
...
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
269 assertions, 0 failed
```

The full run prints all 269 assertions. Each `...` line above is where this block is cut.

## What already exists

Monitoring a service is a solved problem, and the products below do it well. This tool adds a
few things that the pages I read do not describe, and it does not replace any of them.

- **Esri Health Check.** `/rest/info/healthcheck` reports "whether the responding machine in the
  ArcGIS Server site is able to receive and process requests"
  ([Health Check][health]). It is the right probe for a load balancer. It speaks for the machine,
  and it cannot say that a given service draws an image.
- **ArcGIS Monitor.** Esri's monitoring product raises alerts and calculates availability from
  critical alerts ([ArcGIS Monitor 10.8, data analysis][monitor]). It can read a response body.
  The 10.8 HTTP counter has optional "Find String" and "Find String Not" fields for text that
  should or should not appear in the response ([Add an HTTP counter][httpcounter]). Current
  Monitor documents a Check - Service observer for ArcGIS Server services. Its optional metrics
  are Check Response Code, Check Response Time, Check Response Size and Check Response Matches,
  "the total number of matches returned by the regular expression defined in the observer"
  ([Metrics reference, 2025.0][metrics]). I read the last two on 2026-10-15. They do not
  describe repeated sampling of one URL, a stated miss probability or an ordered-restart gate,
  and I make no claim about what Monitor does beyond them. Use Monitor for the alerts, the
  history and the body match. Use this tool when you want the number of rounds to come from a
  stated miss probability.
- **con terra Service Monitor.** A commercial monitor that lists MapServer, FeatureServer and
  ImageServer among its service types. Each check "is based on a request to the service and the subsequent
  verification of the response" ([Registering a new service][conterra], the "latest" page, read
  on 2026-10-15). It tests reachability, status code, an optional response time and the
  content. That is the same idea as this tool's pass condition. The page I read does not
  describe repeating the request or stating a miss probability.
- **Open-source scripts.** On 2026-10-13, before this repository was public, `gh search repos`
  found no repository named `svcsmoke`. That search now returns this repository, so it cannot be
  repeated. The queries `arcgis health`, `arcgis monitor` and `arcgis service check` returned
  results, and the repositories below exist (I confirmed each with `gh api` on 2026-10-13). Among
  them are
  [diaznoed/Service-Check-Scripts](https://github.com/diaznoed/Service-Check-Scripts) (monitors
  the health of an ArcGIS Server, validates a datastore, checks for stopped services),
  [OsmanKozen/python-arcgis-service-check](https://github.com/OsmanKozen/python-arcgis-service-check),
  [WestonSF/ArcGISServerMonitor](https://github.com/WestonSF/ArcGISServerMonitor) and
  [kapitigis/ArcGISMonitoringDashboard](https://github.com/kapitigis/ArcGISMonitoringDashboard).
  I read the README of the first and the last, and only the descriptions of the others. I did not
  run any of them. Search results change from day to day, so this list shows what I found and is
  not a survey.

What this tool adds is narrow. The number of rounds comes from a stated miss probability, and the
tool prints that probability. A run with too few rounds is not a pass. And the ordered-restart
gate (below) reuses the same proof to decide whether the next tier may restart. A probe that
passes only on returned data, never on a status, is the same idea as the body checks above.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package. The tool sends plain
`GET` requests with `urllib`.

```
git clone https://github.com/uhsear/svcsmoke.git
```

The self-test starts small HTTP servers on `127.0.0.1` and sends them requests. It needs no
network and no credentials. It takes about 9 seconds on Windows (Python 3.13) and 3 seconds on
Linux (Python 3.12), because a few cases wait on a timeout. It prints the same 269
assertions on Windows (Python 3.13.2), on Python 3.9.25 and on Ubuntu (Python 3.12.3). All three
outputs are identical line for line, apart from the line endings.
`coverage run --branch svcsmoke.py --self-test` reports 100 percent of lines and branches.

## Usage

Give one URL for each machine, or each address, that you want sampled. The URL ends in
`MapServer`, `FeatureServer` or `ImageServer`.

```
python svcsmoke.py --self-test
python svcsmoke.py https://gis.example.com/arcgis/rest/services/Roads/MapServer
python svcsmoke.py URL1 URL2 URL3
python svcsmoke.py URL --faulty 2/8 --max-miss 0.01
python svcsmoke.py URL --rounds 8
python svcsmoke.py URL --layers 0,3
python svcsmoke.py URL --out report.csv --apply
python svcsmoke.py --tiers tiers.txt --before app --wait 600
```

| Flag | Default | What it does |
|---|---|---|
| `URL` | none | A service URL. Give one per machine. Required unless `--self-test` or `--tiers`. |
| `--faulty` | `1/4` | The smallest share of instances with a fault that you want to catch, as K/M. `2/8` is the same share. |
| `--max-miss` | `0.01` | The largest chance that a run passes when that share is faulty. |
| `--rounds` | the fewest that meet `--max-miss` | Rounds per probe. A clean run with fewer rounds ends in UNDERSAMPLED. |
| `--layers` | every layer that is not a group | Comma-separated layer ids to query. Use it to leave out a layer that is meant to be empty. An ImageServer, and a map service probed through its export, ignore it. An empty value is a usage error. |
| `--timeout` | `30` | Seconds to wait for one response. At most 86400. |
| `--min-image-bytes` | `1000` | An image smaller than this counts as blank. A guess; see Limits. |
| `--bust-cache` | off | Add a different throwaway parameter to each round. Without it a PASS says that no cache was ruled out. |
| `--max-requests` | `2000` | Refuse a plan larger than this, before sending any sample. |
| `--tiers` | none | A file of tiers in dependency order. Needs `--before`. |
| `--before` | none | The tier about to restart. The gate opens only when every tier above it passes. |
| `--wait` | `0` | Poll the gate for up to this many seconds. At most 86400. |
| `--interval` | `10` | Seconds between polls. At most 86400. |
| `--out` | off | Path for the report CSV. Needs `--apply` to be written. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

The token, when a service needs one, comes from the environment variable `SVCSMOKE_TOKEN`. There
is no flag for it, because a flag shows in the process list and in shell history.

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, `--ou` is refused instead of `--out`, and
`--round` is refused instead of `--rounds`.

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer --ap
usage: svcsmoke.py [-h] [--faulty FAULTY] [--max-miss MAX_MISS]
                   [--rounds ROUNDS] [--layers LAYERS] [--timeout TIMEOUT]
                   [--min-image-bytes MIN_IMAGE_BYTES] [--bust-cache]
                   [--max-requests MAX_REQUESTS] [--tiers TIERS]
                   [--before BEFORE] [--wait WAIT] [--interval INTERVAL]
                   [--out OUT] [--apply] [--self-test]
                   [URL ...]
svcsmoke.py: error: unrecognized arguments: --ap
$ echo $?
64
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Every probe passed, and the rounds support the miss probability. For the gate: open. |
| 1 | At least one probe failed. For the gate: closed. |
| 2 | A file could not be read or written. |
| 3 | Nothing failed, but too few rounds ran to call the service healthy (UNDERSAMPLED). |
| 4 | Nothing was probed: a service listed no layer or no usable extent. |
| 5 | The gate was still closed when `--wait` ran out. |
| 64 | Usage error. Nothing is printed to stdout. The gate's "gate closed" progress lines go to stderr, so this holds after a gate has polled. |
| 70 | An unexpected crash. This is a fault in the tool, never a finding. |

When targets disagree, the worst wins in this order: 1, then 4, then 3.

## How many rounds

A service runs as a pool of instances. Esri documents a minimum and a maximum number of instances
for each machine ([Configure service instance settings][instances]). A request is answered by one
of them, and a REST request has no parameter that picks one. I found none in the documentation.
So this tool cannot ask each instance. It asks the same URL again and again, and it works out
how often that reaches a faulty instance.

Take a share `f` of the instances that carry the fault. Take `n` rounds, with each request landing
on a random instance, independent of the one before. A round reaches only good instances with
probability `1 - f`. All `n` rounds do, and so miss the fault, with probability `(1 - f) ** n`.

The fewest rounds that keep that under a limit are `ceil(ln(limit) / ln(1 - f))`. The tool
checks the result by calculation and does not trust the logarithm. At an exact boundary the
logarithm rounds up. For `f = 0.25` and a limit of `0.75 ** 3` it gives 4, and the answer is 3.

| Faulty share | limit 0.10 | limit 0.05 | limit 0.01 | limit 0.001 |
|---|---|---|---|---|
| 1/2 | 4 | 5 | 7 | 10 |
| 1/4 | 9 | 11 | 17 | 25 |
| 1/8 | 18 | 23 | 35 | 52 |
| 1/16 | 36 | 47 | 72 | 108 |

The row for 1/4 is the case in the story. Eight rounds miss it 10.01 percent of the time, and
the tool says 9 rounds are the fewest for a 10 percent limit. The self-test checks both numbers.
It also simulates a pool of 8 with 2 bad. In 20,000 trials, 8 rounds miss the fault within one
point of 10.01 percent of the time, and 17 rounds miss it under 1.2 percent of the time.

The default is 17 rounds. Each round asks every probe once, so a service with 20 layers costs
341 requests, including one to read the service root. The `--max-requests` limit refuses a
larger plan before it sends any sample:

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer --max-requests 10
error: the plan is 35 request(s) (2 probe(s) over 17 round(s), plus 1 to read the service roots), over --max-requests 10. Nothing was sampled. Name fewer layers with --layers, or raise the limit.
$ echo $?
64
```

**The assumption.** The model needs each request to land on a random instance. I could not find
documentation that says how a request is assigned to an instance. A dispatcher that rotates through
the instances needs fewer rounds than the table says. A dispatcher that sends one client to the
same instance every time makes repeated sampling from one client prove nothing. The printed
probability is exact under the model and only as good as the model. The model also takes a bad
instance to fail every request it answers. A fault that fails only some of the requests on a bad
instance needs more rounds than the table gives.

Ask for fewer rounds than the limit needs and the tool says so:

```
$ python svcsmoke.py http://127.0.0.1:8802/arcgis/rest/services/Pool/MapServer --rounds 8
svcsmoke: 1 target(s), 8 round(s) per probe (set by --rounds)
sampling: 8 round(s) miss a fault on 1/4 of the instances with probability 0.1001 (limit 0.0100), if each request lands on a random instance
1. UNDERSAMPLED http://127.0.0.1:8802/arcgis/rest/services/Pool/MapServer  2 probe(s), 17 request(s)

VERDICT: UNDERSAMPLED. Every request passed, but 8 round(s) leave a 0.1001 chance of missing a fault on 1/4 of the instances, over the limit of 0.0100. Run at least 17.
$ echo $?
3
```

That run is real. The stub (see "Try it without a server") had 2 of 8 instances failing layer 1, and 8 rounds did not
reach one. The 8-round run passed every request and the tool still refused to call it healthy.
The same stub with the default 17 rounds:

```
$ python svcsmoke.py http://127.0.0.1:8803/arcgis/rest/services/Pool/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. FAIL         http://127.0.0.1:8803/arcgis/rest/services/Pool/MapServer  2 probe(s), 35 request(s)
     layer 1: 2 of 17 failed (11.8%): SERVER_ERROR x2. First: error property: Unable to complete operation. (code 500) (HTTP 200, recorded only)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
$ echo $?
1
```

Two of 17 requests failed, and the tool names the layer. The stub's rate was one in four, and a
small sample shows 2 of 17. The count is a sample, not a measurement of the pool.

## What counts as a pass

A probe passes only when the response holds the thing the service exists to return.

**A MapServer or FeatureServer layer.** The tool asks for one feature: `where=1=1`,
`outFields=*`, `returnGeometry=false`, `resultRecordCount=1` ([Query (Map Service/Layer)][querymap],
[Query (Feature Service/Layer)][queryfeature]). The body must be a JSON object with no `error`
property and a `features` array that holds at least one feature. A count alone does not pass,
because a count says a table has rows and not that a feature reads. A layer that is empty on
purpose fails; leave it out with `--layers`.

Esri says `resultRecordCount` "only applies if `supportsPagination` is `true`". I infer that a
layer without pagination ignores it and returns up to its `maxRecordCount` features for each
probe. Esri does not say so, and no stub here exercises it. A body over 16 MB fails with the kind
`TOO_LARGE`.

Group layers hold no rows, so the tool skips any layer with sub-layers. It reads the layer list
from the service root ([Map Service][mapservice]). If the root lists no layer to query, the target
is NOTHING and the exit code is 4:

```
$ python svcsmoke.py http://127.0.0.1:8805/arcgis/rest/services/Empty/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. NOTHING      http://127.0.0.1:8805/arcgis/rest/services/Empty/MapServer  0 probe(s), 1 request(s)
     nothing probed: the service lists no layer to query

VERDICT: NOTHING PROBED. At least one target had no layer or view to ask, so a clean exit would mean nothing.
$ echo $?
4
```

**A map service without the Data capability.** Esri lists `Data` as the capability that Query
(Map Service/Layer) requires, and `Map` as the capability that Export Map requires ([Query (Map
Service/Layer)][querymap], [Export Map][exportmap]). By those pages, a map service published with
`Map` alone draws maps and should refuse every layer query. A layer probe would call it failed. So when the root's
`capabilities` string leaves out `Data` and the root has an extent, the tool asks `export` for the
same two views as for an ImageServer (`format=png`, because `jpgpng` is an `exportImage` format) and
judges them on image bytes. The root lists one layer here, so 2 probes means the two views:

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Draw/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8809/arcgis/rest/services/Draw/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
$ echo $?
0
```

The self-test checks the requests that this sends and that no layer query is sent. It cannot
check Esri's own error text for a missing `Data` capability, which I did not verify. The stub
answers with an invented error body. Another server may list a map service as `Map,Query`, as
restfake does, so the tool switches to `export` only when the root also gives an extent. With no
extent it queries the layers as for any other map service.

**An ImageServer.** The tool asks `exportImage` for a 400 by 400 image in two views. One view is
the whole extent of the service. The other is a window 1/100 as wide at its centre, so a fault
that shows only at a fine scale is sampled too. The extent and its spatial reference come from the
service root ([Image Service][imageservice], [Export Image][exportimage]).

The response must pass three tests. Its content type starts with `image/`. Its first bytes are the
signature of PNG, JPEG, GIF, BMP or TIFF ([PNG signature][png], [other
signatures][signatures]). It is at least `--min-image-bytes` long. An HTML page that arrives with
HTTP 200 fails and is named:

```
$ python svcsmoke.py http://127.0.0.1:8804/arcgis/rest/services/Imagery/ImageServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. FAIL         http://127.0.0.1:8804/arcgis/rest/services/Imagery/ImageServer  2 probe(s), 35 request(s)
     view overview: 7 of 17 failed (41.2%): NOT_IMAGE x7. First: content type 'text/html', page titled 'Service Error' (HTTP 200, recorded only)
     view detail: 4 of 17 failed (23.5%): NOT_IMAGE x4. First: content type 'text/html', page titled 'Service Error' (HTTP 200, recorded only)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
$ echo $?
1
```

**The status.** A good body under a status other than 200 also fails. Esri says any status other
than 200 "indicates that there has been a problem"
([Error handling with sync operations][errors]). The status never passes a probe by itself.

**Other failures.** A refused connection, a timeout, a hang-up and a body shorter than its
`Content-Length` are `TRANSPORT`, and the line gives the exception name and the operating system's
error number. The number differs between systems, for example 10061 on Windows and 111 on Linux
for a refused connection. A redirect is `REDIRECT` and is not followed, even when its `Location`
header is malformed. An HTML page where JSON was expected is `NOT_JSON`. A JSON `error` property
is `SERVER_ERROR`, and the report quotes its message. A JSON body that starts with a byte order
mark is read as JSON.

```
$ python svcsmoke.py http://127.0.0.1:1/arcgis/rest/services/Gone/MapServer --rounds 1
svcsmoke: 1 target(s), 1 round(s) per probe (set by --rounds)
sampling: 1 round(s) miss a fault on 1/4 of the instances with probability 0.7500 (limit 0.0100), if each request lands on a random instance
1. FAIL         http://127.0.0.1:1/arcgis/rest/services/Gone/MapServer  0 probe(s), 1 request(s)
     root: TRANSPORT, no response: ConnectionRefusedError (errno 10061)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
$ echo $?
1
```

## The token

A service that needs a token gets it from `SVCSMOKE_TOKEN`, in the `X-Esri-Authorization` header.
Esri recommends the header over a query parameter. The header "prevents intermediaries on the
network, such as proxies, gateways or load-balancers from being able to obtain the token"
([Accessing ArcGIS token secured web services][tokenheader]).

The stub's `Secure` service answers a token error until it sees that header:

```
$ python svcsmoke.py http://127.0.0.1:8806/arcgis/rest/services/Secure/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. FAIL         http://127.0.0.1:8806/arcgis/rest/services/Secure/MapServer  0 probe(s), 1 request(s)
     root: SERVER_ERROR, error property: Token Required (code 499) (HTTP 200, recorded only)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
$ echo $?
1
```

With the variable set, the same URL passes:

```
$ export SVCSMOKE_TOKEN=demo-token-0001
$ python svcsmoke.py http://127.0.0.1:8806/arcgis/rest/services/Secure/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8806/arcgis/rest/services/Secure/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
$ echo $?
0
```

The tool takes these steps so that the token reaches the server and nobody else:

- A URL may carry no user name, password, query string or fragment. A URL with a space, a control
  character or a non-ASCII character is refused too, and you percent-encode it. The refusal for a
  credential, a query string or a bad character does not quote the URL. Other refusals quote at
  most its first 60 characters, with any `token=` value hidden. A URL that holds an `@` is never
  quoted, so `user:password@host/...` typed without a scheme does not leak the password.
- With a token set, a plain `http` URL to a host other than this machine is refused, because the
  token would cross the network in the clear.
- Redirects are never followed, so the header cannot be sent on to the host that `Location`
  names. The self-test points one stub at another and checks that the second sees no request.
  A `Location` that holds `user:password@` is shown without the user name and password.
- No proxy is used. The tool passes `urllib` an empty proxy table, which the Python documentation
  gives as the way to disable a detected proxy ([urllib.request][urllib]). The self-test sets
  `http_proxy` to a dead port and checks that the request still arrives.
- The token value and any `token=` parameter are removed from every line the tool prints and
  from the report. The tool removes them from the whole reply body before it cuts the text to a
  length, so a token longer than the cut does not survive as a prefix. The self-test has servers
  that echo a 251 character token back in an error message and in an HTML page, and greps the
  screen and the report for it. A token that a server echoes in a `Content-Type` header is
  removed before the type is cut to 40 characters. An unexpected error and a usage error are
  scrubbed the same way.
- A trailing newline or carriage return on the token, as from a token file, is stripped. A token
  with a space, a control character or a non-ASCII character is refused at the start, and the
  message does not quote it.

## The ordered-restart gate

Restarting a tier before the tier it depends on is healthy leaves servers holding connections to
machines that are going away. A fixed `sleep` does not help, because it does not know when the
tier answers.

`--tiers` names a file with one tier per line, in dependency order. The first line is the tier
the others depend on most. `--before` names the tier you are about to restart.

```
db: http://127.0.0.1:8808/arcgis/rest/services/Healthy/MapServer
app: http://127.0.0.1:8808/arcgis/rest/services/Pool/MapServer
web: http://127.0.0.1:8808/arcgis/rest/services/Healthy/MapServer
```

A tier can name several URLs on one line, separated by spaces. Give one URL for each machine in
the tier. The gate then probes each of them:

```
db: https://db1.example.com/arcgis/rest/services/Roads/MapServer https://db2.example.com/arcgis/rest/services/Roads/MapServer
app: https://app1.example.com/arcgis/rest/services/Roads/MapServer
```

The self-test uses this form: its `db` tier has two targets, and the gate for `app` opens only
when both pass.

The gate probes every tier above the one named and nothing else. It never probes the tier being
restarted or a tier after it. It opens, with exit 0, only when every target in those tiers
passes at the full number of rounds. A gate that passed on too few rounds would be the weak
evidence this tool exists to refuse, so UNDERSAMPLED closes it.

```
$ python svcsmoke.py --tiers tiers.txt --before app
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         [db] http://127.0.0.1:8808/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
GATE: OPEN. Tier 'app' may restart.
$ echo $?
0
```

The `app` tier here is a pool with a fault, so the gate for `web` stays closed:

```
$ python svcsmoke.py --tiers tiers.txt --before web
svcsmoke: 2 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         [db] http://127.0.0.1:8808/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)
2. FAIL         [app] http://127.0.0.1:8808/arcgis/rest/services/Pool/MapServer  2 probe(s), 9 request(s)
     layer 1: 1 of 4 failed (25.0%): SERVER_ERROR x1. First: error property: Unable to complete operation. (code 500) (HTTP 200, recorded only)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
GATE: CLOSED. Do not restart 'web' yet; the tiers before it are not proven healthy.
$ echo $?
1
```

In a gate, a target stops sampling at the end of the round where a failure appears. The gate is
already closed, and there is no value in sending the rest of the rounds to a tier that is coming
back up.

The first tier has no tier above it. A gate for it would open without checking anything, so the
tool refuses:

```
$ python svcsmoke.py --tiers tiers.txt --before db
error: tier 'db' is the first, so no tier comes before it and the gate would open without checking anything
$ echo $?
64
```

`--wait` polls until the gate opens. `Flaky` here fails its first three requests, as a tier does
while it restarts:

```
$ python svcsmoke.py --tiers tiers2.txt --before app --wait 60 --interval 1
gate closed on attempt 1 (exit 1). Trying again in 1 s.
gate closed on attempt 2 (exit 1). Trying again in 1 s.
gate closed on attempt 3 (exit 1). Trying again in 1 s.
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         [db] http://127.0.0.1:8809/arcgis/rest/services/Flaky/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
GATE: OPEN. Tier 'app' may restart.
$ echo $?
0
```

If the gate is still closed when the wait runs out, the exit code is 5, and the gate line says
the tiers were not proven healthy. A `--wait` shorter than `--interval` is one look and then
exit 5, because there is no time left for a second poll. A restart script can use the exit code
as a guard:

```
python svcsmoke.py --tiers tiers.txt --before app --wait 900 && restart-app
```

The progress lines ("gate closed on attempt N") go to stderr. The report and the `GATE:` line go
to stdout. A run without `--tiers` never prints a `GATE:` line.

When `--out` and `--apply` are given and the report cannot be written, the gate is closed even
if every tier passed, and the exit code is 2. A gate must not read OPEN when the evidence for it
was lost:

```
$ python svcsmoke.py --tiers tiers3.txt --before app --out nodir/r.csv --apply
error: could not write nodir/r.csv: [Errno 2] No such file or directory: 'nodir/r.csv'
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         [db] http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
GATE: CLOSED. Do not restart 'app': the report could not be written.
$ echo $?
2
```

(The `error:` line comes first only because stderr is not buffered. The same run on a terminal
shows it last.)

This tool restarts nothing. It reads and it reports.

## The report file

Nothing is written without `--apply`. A run with `--out` alone says so:

```
$ python svcsmoke.py http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer --out r.csv
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.

Check only. r.csv was not written. Re-run with --apply.
$ echo $?
0
```

With `--apply` it writes one row for each probe, and one row for a target that could not be
probed:

```
$ python svcsmoke.py http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer --out r.csv --apply
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.

wrote r.csv
$ echo $?
0
$ cat r.csv
tier,target,verdict,probe,requests,failed,kinds,first_failure
,http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer,PASS,layer 0,17,0,,
,http://127.0.0.1:8807/arcgis/rest/services/Healthy/MapServer,PASS,layer 1,17,0,,
```

The text in the `first_failure` column always starts with words the tool chose, never with text
the server sent, so a spreadsheet will not read it as a formula. The `tier` column is the tier
name as you typed it in the tiers file, so a tier named `=1+1` opens as a formula in a
spreadsheet. Name tiers with letters. The target URL is whatever you typed. A report holds service URLs, so `.gitignore` lists `*.csv`. A report with another name is
not covered.

`--apply` overwrites an existing file named by `--out`, and the tool does not ask. In a gate it
refuses a file that is the `--tiers` file. It compares file identity, so a hard link or a symbolic
link to that file is refused too. The self-test makes a hard link only, because Windows cannot
make a symbolic link without a privilege. I checked the symbolic-link case by hand on Linux
(Python 3.12), where the run exits 64. It does not refuse any other existing file, including the tool
itself.

## Other flags

`--faulty` and `--max-miss` change the rounds:

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer --faulty 1/8 --max-miss 0.05
svcsmoke: 1 target(s), 23 round(s) per probe (the fewest that meet --max-miss)
sampling: 23 round(s) miss a fault on 1/8 of the instances with probability 0.0464 (limit 0.0500), if each request lands on a random instance
1. PASS         http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer  2 probe(s), 47 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 23 round(s). That bounds the chance of missing a fault on 1/8 of the instances at 0.0464. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
$ echo $?
0
```

`--layers` queries the named layers and nothing else:

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer --layers 0 --rounds 17
svcsmoke: 1 target(s), 17 round(s) per probe (set by --rounds)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer  1 probe(s), 18 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
$ echo $?
0
```

`--bust-cache` appends `_svcsmoke=<round>`, from 0, to each request. The flag is for a cache that
sits in front of the server, such as a reverse proxy, which would answer a repeated URL itself.
The self-test checks that the parameter changes every round and is absent without the flag. I did
not test it against a real server, and I did not check that a server ignores an unknown parameter.

Without the flag the tool cannot tell a cached answer from a fresh one. A cache that answers every
round from one reply makes a PASS meaningless, and the printed miss probability does not apply.
So a PASS without the flag ends with a sentence that says so:

```
$ python svcsmoke.py http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer --bust-cache
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8809/arcgis/rest/services/Healthy/MapServer  2 probe(s), 35 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share.
$ echo $?
0
```

The run above has the flag, so it has no such sentence. Every other PASS in this file shows it.

## Try it without a server

`svcsmoke.py` holds the small HTTP server that its self-test uses, as the class `Stub`. Save
this file next to `svcsmoke.py` as `stub.py`. It serves pretend services on 8 pretend instances.
Run it as `python stub.py 8803 33`, where the second number seeds which requests hit a bad
instance. The `Pool` service has 2 of 8 instances failing layer 1. `Imagery` has 2 of 8 returning
an HTML page for `exportImage`. `Draw` is a map service published with the `Map` capability
only.

Each output in this README came from a stub started for that example. Examples that ran one after
another against the same port shared a stub, and ran in the order they appear. The gate examples
under port 8808 are one such series.

```
# stub.py: serves pretend ArcGIS Server services on 127.0.0.1 for trying svcsmoke.
# python stub.py PORT SEED
import json
import random
import sys
import time

import svcsmoke

port, seed = int(sys.argv[1]), int(sys.argv[2])
rng = random.Random(seed)
GOOD = json.dumps({"features": [{"attributes": {"OBJECTID": 1}}]}).encode()
BAD = json.dumps({"error": {"code": 500, "details": [],
                            "message": "Unable to complete operation."}}).encode()
PAGE = b"<html><head><title>Service Error</title></head></html>"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
EXTENT = {"xmin": -1000.0, "ymin": -1000.0, "xmax": 1000.0, "ymax": 1000.0,
          "spatialReference": {"wkid": 3857}}
LAYERS = [{"id": 0, "name": "roads", "subLayerIds": None},
          {"id": 1, "name": "parcels", "subLayerIds": None}]
roots = {"count": 0}


def script(path, params, headers):
    parts = path.strip("/").split("/")
    name = parts[3]          # arcgis/rest/services/<name>/<Kind>[/<id>/query]
    instance = int(rng.random() * 8)      # which pretend instance answered
    if path.endswith("/ImageServer"):
        return {"body": json.dumps({"extent": EXTENT}).encode()}
    if path.endswith("/export"):        # Export Map, on the Draw service
        return {"body": PNG, "ctype": "image/png"}
    if path.endswith("exportImage"):
        if name == "Imagery" and instance < 2:
            return {"body": PAGE, "ctype": "text/html"}
        return {"body": PNG, "ctype": "image/png"}
    if path.endswith("/MapServer"):
        if name == "Flaky":
            roots["count"] += 1
            if roots["count"] < 4:
                return {"status": 503, "body": PAGE, "ctype": "text/html"}
        if name == "Secure" and "X-Esri-Authorization" not in headers:
            return {"body": json.dumps({"error": {"code": 499, "details": [],
                    "message": "Token Required"}}).encode()}
        if name == "Draw":      # published with the Map capability only
            return {"body": json.dumps({"capabilities": "Map",
                                        "fullExtent": EXTENT,
                                        "layers": LAYERS[:1]}).encode()}
        if name == "Empty":
            return {"body": json.dumps({"layers": [
                {"id": 0, "name": "group", "subLayerIds": [1]}]}).encode()}
        return {"body": json.dumps({"layers": LAYERS}).encode()}
    layer = int(parts[-2])
    if name == "Draw":      # no Data capability, so a layer query is refused
        return {"body": BAD}
    return {"body": BAD if name == "Pool" and layer == 1 and instance < 2
            else GOOD}


stub = svcsmoke.Stub(script, port)
print("serving on %d" % stub.port, flush=True)
time.sleep(3600)
```

The seed was 33 for the runs on ports 8802, 8803 and 8808, and 5 for all the others. Seed 33 is
the one where 8 rounds miss the fault on a fresh `Pool` and 17 rounds catch it.

## Also run against restfake

[restfake](https://github.com/uhsear/restfake) is a mock ArcGIS REST server that fails the way
ArcGIS fails. It is a second, separately written server, so the tool was run against it too. With
`--export-image-fault` the ImageServer description reads healthy and every `exportImage` returns
a 200 with an error envelope:

```
$ python svcsmoke.py http://127.0.0.1:8871/rest/services/Elevation/ImageServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. FAIL         http://127.0.0.1:8871/rest/services/Elevation/ImageServer  2 probe(s), 35 request(s)
     view overview: 17 of 17 failed (100.0%): NOT_IMAGE x17. First: content type 'application/json; charset=utf-8', body starts '{"error": {"code": 500, "message": "Unable to complete o... (HTTP 200, recorded only)
     view detail: 17 of 17 failed (100.0%): NOT_IMAGE x17. First: content type 'application/json; charset=utf-8', body starts '{"error": {"code": 500, "message": "Unable to complete o... (HTTP 200, recorded only)

VERDICT: FAILED. 1 target(s) returned no feature or image on at least one request. They are not healthy, whatever their state says.
$ echo $?
1
```

## Limits

- **No real ArcGIS Server.** This was built and tested without access to a live ArcGIS Server.
  Every network path ran against the loopback stub and against restfake. The request shapes come
  from the Esri pages cited here.
- **It cannot address an instance.** It samples a URL. The miss probability rests on the
  assumption stated under "How many rounds". One URL per machine is the tool's idea of an
  "instance" to sample.
- **A count fault is not seen.** The probe asks for a feature, not a count. restfake
  `--count-fault 1` makes layer 1 fail `returnCountOnly` while its features still read. The tool
  passes that service, and a client that calls `returnCountOnly` still fails on it. The evidence
  follows the list.

- **Blank images.** The 1000 byte minimum is a guess that stands for "a blank export is small".
  A real raster with no data at the centre of the extent can draw a blank detail view and fail.
  A transparent blank 400 by 400 PNG built with zlib is about 700 bytes, so an empty detail view
  of a healthy service fails under the default. Measure an export of your own imagery, and lower
  `--min-image-bytes` if a real tile is smaller. The tool does not decode an image. A blank PNG
  of at least the minimum passes. So does an image with a valid header and truncated pixel data,
  when the server sends no `Content-Length` to compare with. A solid-colour 400 by 400 JPEG is
  about 3 KB, and a solid opaque white PNG of that size is about 1.4 KB (measured with Pillow
  12.3.0), so a blank opaque export passes the byte minimum. Only a transparent blank PNG, about
  700 bytes, falls under it.
- **Timeouts.** `--timeout` goes to the socket, so it is a limit on each blocking operation and
  not on the whole response ([urllib.request][urllib]). A server that sends one byte every few
  seconds can outlast it.
- **TLS.** The default certificate checks apply, and no flag turns them off. A failed handshake
  is a `TRANSPORT` failure. A private certificate authority is a matter of Python's and
  OpenSSL's own configuration, and I did not test one.
- **Sequential.** Requests go out one at a time. A plan of 17 rounds over 20 layers takes as long
  as 341 round trips.
- **The report file** is written in place, not through a temporary file. A failed write can leave
  a partial file, and the exit code is 2 even when the services failed. Read the printed
  `VERDICT`, which is above the error.
- **A features list** passes when it is not empty, even if its entries are `null` or `{}`. The
  tool checks that a feature came back and not that its content is valid.
- **A healthy layer with no features** fails its probe, by design. Leave it out with `--layers`.
- **Stand-alone tables** in the root `tables` array are never probed. A service that holds only
  tables ends as NOTHING with exit 4.
- **The root lists no layer type.** A layer that cannot be queried, such as a raster layer, fails
  its probe until you leave it out with `--layers`. I did not check this against a live server.
- **Map services without Data.** The tool probes such a service through `export`, which shows
  that it draws and does not show that its data reads. `--layers` has no effect on it. The
  switch rests on the two Esri pages cited above, and I did not test it against a live server.
- **No cache control by default.** Without `--bust-cache` a cache or CDN in front of the server
  can answer every round from one cached reply. The printed miss probability then does not apply.
- **The miss probability** holds only for a fault that fails every request on a bad instance.
- **Compressed bodies.** A body that a proxy gzip-compressed, although the tool sent no
  `Accept-Encoding` header, is read as `NOT_JSON`. A healthy service behind such a proxy shows as
  FAIL.
- **Token forms.** The tool removes the token in its literal form. It also removes a token
  that a JSON body echoes with `/` sent as `\/`, because the text is scrubbed again after the
  body is parsed. A server that echoes the token URL-encoded (`/` as `%2F`) is not scrubbed. The
  self-test shows both.
- **Proxies.** A service that is reachable only through a mandatory HTTP proxy fails every probe
  with `TRANSPORT`, because the tool ignores the proxy environment variables on purpose.
- **Report timing.** The report file is opened only after all sampling ends, so a path that
  cannot be written is reported after the run and not before it.
- **The self-test writes** tier and report files into a temporary directory and removes it at the
  end. That is the one write outside `--apply`.
- **One connection for every request.** The tool opens a new connection for each request. On
  Windows each closed connection keeps its port in TIME_WAIT for a few minutes. A burst of runs
  can use every ephemeral port, and a request then shows `OSError (errno 10048)` as a false
  TRANSPORT failure. The self-test sends about 500 requests and makes the same demand. Wait a
  minute and run again. A self-test that fails for this reason prints FAIL lines and exits 1.
- **CI.** Read the last line of the self-test (`N assertions, 0 failed`) as well as the exit
  code. A CI step must require the text `0 failed`, because a self-test that printed nothing
  would also exit 0. A red self-test exits 1, the same code as a failed service.
- **Mutation sweep.** I ran 760 mutants of the non-test code of this version, on Linux with Python
  3.12.3. The sweep is a scratch script and is not shipped. It flips comparisons, boolean
  operators, constants, branches and return values. The self-test killed 735 and 25 survived.
  Eleven survivors change only how long a quoted text is cut. Two change a read cap and a split
  limit with no visible effect. Four are equivalent mutants. Three sit in the starting guess of
  `rounds_needed`. Five replace the return value of `main()` or the `__name__` guard, so the
  self-test never runs, and that is why a CI step must require `0 failed`. The same sweep run on
  Windows, while other loopback jobs ran, gave false kills from port exhaustion (errno 10048).
  Only the Linux result is valid.
- **The estimate in `rounds_needed`.** The logarithm only sets where the search starts. The loop
  that follows decides the answer.
- **Huge numbers.** A `--rounds` above about 1e308 ends in exit 70 against a target with no
  plan. So does a root extent with an integer of more than about 308 digits. Both fail closed,
  and neither is a finding.
- **Bad host names.** A host name with an empty label, or a label over 63 characters, ends the
  run with exit 64 and an `idna` message. It is not reported as a failed service.
- **Map services with Data.** A map service that has the Data capability is probed with layer
  queries only. Its export is never requested, so a map service that draws badly while its
  queries work reads PASS.
- **Redirects to https.** A server that redirects `http` to `https` makes a healthy service FAIL
  with `REDIRECT`. Give the `https` URL.
- **One token for every tier.** The same token goes to every URL in a `--tiers` file, including
  the `https` hosts you wrote there.
- **`--max-requests` is per survey.** With `--wait` the gate runs a new survey on each poll, so
  the total over the wait is not capped.
- **Custom coordinate systems.** An ImageServer root whose spatial reference has only `wkt`, with
  no `wkid` or `latestWkid`, ends as NOTHING with exit 4, so a healthy service in a custom
  coordinate system cannot be probed.
- **No network.** A workstation that has lost its network sends every probe to TRANSPORT and
  exits 1, the same as a service outage. A gate closes in that case.
- **The gate line and the cache.** With `--bust-cache` off, a PASS and an open gate can come
  from a cache. The VERDICT line says so, and the `GATE: OPEN` line does not repeat it.
- **A group layer** is detected by its `subLayerIds`. The Map Service page does not say how a
  group layer is identified, and the examples suggest sub-layers. Name layers with `--layers` if a
  server lists them differently.
- **Unreachable lines:** none. The branch coverage is 100 percent, with the stub's own
  server code included.

The count fault, run against `restfake.py --port 8873 --image-server --layers 3
--count-fault 1 --apply`. The tool passes the service. A count request on layer 1 fails, and a feature request on the same
layer reads:

```
$ python svcsmoke.py http://127.0.0.1:8873/rest/services/Basemap/MapServer
svcsmoke: 1 target(s), 17 round(s) per probe (the fewest that meet --max-miss)
sampling: 17 round(s) miss a fault on 1/4 of the instances with probability 0.0075 (limit 0.0100), if each request lands on a random instance
1. PASS         http://127.0.0.1:8873/rest/services/Basemap/MapServer  3 probe(s), 52 request(s)

VERDICT: PASS. Every probe returned a feature or image bytes on all 17 round(s). That bounds the chance of missing a fault on 1/4 of the instances at 0.0075. It says nothing about a smaller share. Cache busting was off, so a cache in front of the server could have answered repeated requests.
$ echo $?
0

$ curl -s "http://127.0.0.1:8873/rest/services/Basemap/MapServer/1/query?where=1%3D1&returnCountOnly=true&f=json"
{"error": {"code": 500, "message": "Unable to complete operation.", "details": ["Error performing query operation", "restfake --count-fault 1"]}}
$ curl -s "http://127.0.0.1:8873/rest/services/Basemap/MapServer/1/query?where=1%3D1&resultRecordCount=1&returnGeometry=false&outFields=*&f=json" | python -c "import sys, json; print(len(json.load(sys.stdin)[\"features\"]), \"feature(s)\")"
1 feature(s)
```

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [restfake](https://github.com/uhsear/restfake) - a mock ArcGIS REST server that fails the way
  ArcGIS fails: an `exportImage` that answers 200 with an error, a layer whose count fails, a
  dropped connection. This tool was run against it.
- [svcguard](https://github.com/uhsear/svcguard) - guarantee a service restarts around risky
  maintenance, even on failure. It restarts services. Run this tool after it, to prove the
  restarted service returns data.
- [svcsource](https://github.com/uhsear/svcsource) - report the database and dataset behind every
  service. When this tool names a failing service, that tool says what it reads.
- [svcdrift](https://github.com/uhsear/svcdrift) - diff a published service against the dataset
  it came from. It checks the schema, and this tool checks that a request returns data.
- [ghostsvc](https://github.com/uhsear/ghostsvc) - reconcile portal items against federated
  services.

[health]: https://developers.arcgis.com/rest/services-reference/enterprise/health-check/
[monitor]: https://enterprise.arcgis.com/en/monitor/10.8/reports/data-analysis.htm
[httpcounter]: https://enterprise.arcgis.com/en/monitor/10.8/administration/add-an-http-counter.htm
[metrics]: https://doc.arcgis.com/en/monitor/2025.0/get-started/windows/metrics-reference.htm
[conterra]: https://docs.conterra.de/en/servicemonitor/latest/monitoring/service-job/new.html
[instances]: https://enterprise.arcgis.com/en/server/12.0/administer/linux/configure-service-instance-settings.htm
[exportmap]: https://developers.arcgis.com/rest/services-reference/enterprise/export-map/
[querymap]: https://developers.arcgis.com/rest/services-reference/enterprise/query-map-service-layer/
[queryfeature]: https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/
[mapservice]: https://developers.arcgis.com/rest/services-reference/enterprise/map-service/
[imageservice]: https://developers.arcgis.com/rest/services-reference/enterprise/image-service/
[exportimage]: https://developers.arcgis.com/rest/services-reference/enterprise/export-image/
[errors]: https://developers.arcgis.com/rest/services-reference/enterprise/error-handling-with-sync-operations/
[tokenheader]: https://enterprise.arcgis.com/en/server/12.0/administer/linux/accessing-arcgis-token-secured-web-services.htm
[status]: https://developers.arcgis.com/rest/enterprise-administration/server/service-status/
[png]: https://www.w3.org/TR/png-3/
[signatures]: https://en.wikipedia.org/wiki/List_of_file_signatures
[urllib]: https://docs.python.org/3.9/library/urllib.request.html
