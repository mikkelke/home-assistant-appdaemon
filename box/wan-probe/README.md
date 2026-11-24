# wan-probe

Permanent probe for the box. When the internet drops it records, to the second, which layer stopped answering, and keeps mtr traces from inside the outage.

Every second it pings six targets. Every 5 s it sends a UDP DNS query (A record for `example.com`, 2 s timeout) to two resolvers. A target is **down** after 3 consecutive losses and **up** again after 3 consecutive replies.

| Target | Address | Layer |
| --- | --- | --- |
| `udm` | 10.21.0.1 | router, over the LAN |
| `gw4` | 185.10.221.65 | ISP IPv4 gateway |
| `gw6` | 2a03:5440:2000:2a::1 | ISP IPv6 gateway |
| `cf4` | 1.1.1.1 | internet |
| `g4` | 8.8.8.8 | internet |
| `cf6` | 2606:4700:4700::1111 | internet |
| `dns_udm` | DNS via 10.21.0.1 | router resolver |
| `dns_cf` | DNS via 1.1.1.1 | internet resolver |

## Install

```
sudo install -m 0755 wan-probe /usr/local/bin/wan-probe
sudo install -m 0644 wan-probe.service /etc/systemd/system/wan-probe.service
sudo install -m 0644 wan-probe.logrotate /etc/logrotate.d/wan-probe
sudo systemctl daemon-reload
sudo systemctl enable --now wan-probe
```

Python 3 standard library only; needs `ping` (iputils), `mtr` and `ip`, and runs as root. `events.jsonl` rotates monthly and keeps 12 months. Incident directories are not rotated.

## Reading the logs

`/var/log/wan-probe/events.jsonl` has one JSON object per line. `ts` is ISO 8601 with milliseconds and UTC offset. Successful probes are not logged.

| `ev` | Meaning |
| --- | --- |
| `loss` | An ICMP probe got no reply. `sent` is when it left; the row is written about 1 s later, when ping gives up on it. `streak` counts consecutive losses. |
| `icmp_error` | A router answered a probe with an ICMP error. `src` is who sent it: the box's own address means the neighbour lookup failed on the LAN, the router's address means the router could not route it, any other address is a hop beyond the router. |
| `late_reply` | A reply arrived after the probe was already counted lost. |
| `dns_fail` | A DNS query failed: `reason` is `timeout`, an `rcode`, or the socket error. |
| `state` | A target went `down` or `up`. `since` is when the first lost probe left (down) or the first reply arrived (up); `ts` is when the 3rd confirmed it. `down_s` is on `up` rows. |
| `summary` | Once a minute on the minute: `sent`, `lost`, `avg_ms`, `max_ms` per target. |
| `incident_start`, `incident_run`, `incident_end` | See below. |
| `probe_start`, `probe_stop`, `error` | Service start and stop, and internal errors. |

```
grep '"ev":"summary"' /var/log/wan-probe/events.jsonl | tail -3
grep -E '"ev":"(state|incident_)' /var/log/wan-probe/events.jsonl
jq -c 'select(.ev=="incident_end")' events.jsonl
jq -r 'select(.target=="cf4" and .ev=="loss") | .ts' events.jsonl
```

## Incidents

When any target except `udm` goes down, an incident opens: `incident_start` is logged and, in the background, mtr (`1.1.1.1`, `8.8.8.8`, and `-6` to `2606:4700:4700::1111`) plus `ip route`, `ip -6 route` and `ip neigh` are saved to `/var/log/wan-probe/incidents/<start>/` as `runNN-*.txt`. The set repeats every 30 s, at most 10 times. IPv4 mtr runs go one after another because routers rate-limit ICMP errors per destination; concurrent runs would show phantom loss on the first hops. Loss on a middle hop that does not continue to the last hop is that rate limiting, not real loss.

The incident closes when every target has been up for 10 s. `incident_end` has `outage_start`, `outage_end`, `duration_s`, `down_s` per target, and a classification:

| `classification` | Meaning |
| --- | --- |
| `lan` | The router (`udm`) was down too. |
| `isp_gateway` | Router fine; IPv4 and IPv6 both failed and a gateway was among the failures. |
| `upstream` | IPv4 and IPv6 both failed beyond the gateways, which kept answering. |
| `ipv4_only`, `ipv6_only` | Only one address family failed. |
| `dns_only` | Every ping was fine; only DNS failed. |

DNS runs every 5 s, so a DNS target takes 15 s to go down and 15 s to come back up. The router answers `example.com` from its cache until the record's TTL runs out, so `dns_udm` shows the router's resolver is alive and usually stays up through a short WAN outage; `dns_cf` is the check that needs the internet.

A target that is already unreachable when the probe starts goes down after 3 losses like any other, so an incident right after `probe_start` can be the network still coming up at boot.

## Self test

```
python3 /usr/local/bin/wan-probe --selftest
```

Checks the ping parser, the classifier and the state machine, then runs a live incident against a fake target (192.0.2.1, never answers) in a temporary directory: it must open, write the mtr and snapshot files, rerun after 30 s, close 10 s after the fake target comes back (pointed at loopback), and be classified. Takes about 70 s, touches nothing on the real log directory, and deletes its output (`--keep` keeps it).
