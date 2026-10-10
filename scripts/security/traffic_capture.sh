#!/usr/bin/env bash
# Team 1 (Security) — labeled, headers-only traffic captures on the ROBOT network (eno0).
#
#   traffic_capture.sh check                     # interface / scope sanity check
#   traffic_capture.sh run <condition> [secs]    # one labeled run, e.g. run idle 300
#   traffic_capture.sh session [secs] [reps]     # idle/talk alternating, prompts between runs
#   traffic_capture.sh report                    # combine all runs into one comparison table
#   traffic_capture.sh wipe-pcaps                # delete any kept .pcap files
#
# Output: data/traffic/<YYYY-MM-DD>/
#   runs.csv                 run_id,condition,start_utc,end_utc,secs,packets,payload_bytes
#   <run>_flows.csv          src,dst,proto,dport,packets,payload_bytes
#   <run>_timeline.csv       second,packets,payload_bytes   (for the dashboard)
#   <run>_dds_sources.csv    src,packets  (who announces on 239.255.0.1 — alert baseline)
#
# Rules of engagement (Security Lab): eno0 / 192.168.123.0/24 only, headers only (-s 96),
# no payloads kept. The .pcap is deleted after summarizing unless KEEP_PCAP=1.
set -euo pipefail

IFACE="${IFACE:-eno0}"
SNAP=96
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$REPO/data/traffic/$(date +%F)"
RUNS="$OUT/runs.csv"

die() { echo "error: $*" >&2; exit 1; }

guard_iface() {
  [[ "$IFACE" == "eno0" ]] || die "IFACE=$IFACE is out of scope; captures are allowed on eno0 only"
  ip -br addr show "$IFACE" | grep -q '192\.168\.123\.' \
    || die "$IFACE has no 192.168.123.x address — is the robot cable plugged in and the G1 powered on?"
}

cmd_check() {
  guard_iface
  echo "== $IFACE"; ip -br addr show "$IFACE"
  echo "== routes"; ip route
  echo "== ip_forward = $(sysctl -n net.ipv4.ip_forward)"
  echo "== neighbours on $IFACE"; ip neigh show dev "$IFACE"
  echo "== DDS announcers (10 s)"
  sudo timeout 10 tcpdump -i "$IFACE" -nn -q 'udp and dst host 239.255.0.1' 2>/dev/null \
    | awk '{split($3,s,"."); print s[1]"."s[2]"."s[3]"."s[4]}' | sort | uniq -c || true
}

# tcpdump -tt -nn -q line:  1790360000.123 IP 192.168.123.161.5555 > 239.255.0.1.7400: UDP, length 120
parse() {
  tcpdump -tt -nn -q -r "$1" 2>/dev/null | awk '$2=="IP" {
    n = split($3,s,"."); m = split($5,d,"."); sub(":","",d[m])
    src = s[1]"."s[2]"."s[3]"."s[4]; dst = d[1]"."d[2]"."d[3]"."d[4]
    proto = ($6 ~ /^UDP/) ? "udp" : ($6 ~ /^tcp/) ? "tcp" : tolower($6)
    sub(",","",proto)
    dport = (m == 5) ? d[5] : "-"
    len = ($NF ~ /^[0-9]+$/) ? $NF : 0
    printf "%d %s %s %s %s %d\n", int($1), src, dst, proto, dport, len }'
}

summarize() {  # $1=pcap $2=prefix  → prints "packets bytes"
  local rows; rows="$(mktemp)"; parse "$1" > "$rows"
  { echo "src,dst,proto,dport,packets,payload_bytes"
    awk '{k=$2","$3","$4","$5; n[k]++; b[k]+=$6} END {for (k in n) print k","n[k]","b[k]}' "$rows" \
      | sort -t, -k6,6nr; } > "$2_flows.csv"
  { echo "second,packets,payload_bytes"
    awk 'NR==1{t0=$1} {n[$1-t0]++; b[$1-t0]+=$6} END {for (t in n) print t","n[t]","b[t]}' "$rows" \
      | sort -t, -k1,1n; } > "$2_timeline.csv"
  { echo "src,packets"
    awk '$3=="239.255.0.1" {n[$2]++} END {for (k in n) print k","n[k]}' "$rows" | sort -t, -k2,2nr; } \
    > "$2_dds_sources.csv"
  awk '{p++; b+=$6} END {printf "%d %d\n", p, b}' "$rows"
  rm -f "$rows"
}

cmd_run() {
  local cond="${1:?condition label required, e.g. idle or talk}" secs="${2:-300}"
  [[ "$cond" =~ ^[a-z0-9_-]+$ ]] || die "condition must be lowercase letters/digits/_/-"
  guard_iface
  mkdir -p "$OUT"; chmod 700 "$OUT"
  [[ -f "$RUNS" ]] || echo "run_id,condition,start_utc,end_utc,secs,packets,payload_bytes" > "$RUNS"
  local id; id="$(printf 'r%02d' "$(( $(wc -l < "$RUNS") ))")_$cond"
  local pcap="$OUT/$id.pcap"
  local start; start="$(date -u +%FT%TZ)"
  echo ">> $id: capturing $secs s on $IFACE (start $start) — Ctrl+C ends early"
  sudo timeout "$secs" tcpdump -i "$IFACE" -nn -q -s "$SNAP" -w "$pcap" 2>/dev/null || true
  local end; end="$(date -u +%FT%TZ)"
  sudo chown "$USER" "$pcap"
  read -r pk by < <(summarize "$pcap" "$OUT/$id")
  echo "$id,$cond,$start,$end,$secs,$pk,$by" >> "$RUNS"
  [[ "${KEEP_PCAP:-0}" == 1 ]] || rm -f "$pcap"
  echo "<< $id: $pk packets, $by payload bytes → $OUT/${id}_*.csv"
}

cmd_session() {
  local secs="${1:-300}" reps="${2:-2}"
  for ((i = 1; i <= reps; i++)); do
    read -rp "[$i/$reps] IDLE: nobody speaks. Enter to start ${secs}s… "
    cmd_run idle "$secs"
    read -rp "[$i/$reps] TALK: officers repeat the 5 test phrases. Enter to start ${secs}s… "
    cmd_run talk "$secs"
  done
  cmd_report
}

cmd_report() {
  [[ -f "$RUNS" ]] || die "no runs yet in $OUT"
  echo "== runs ($RUNS)"; column -s, -t "$RUNS"
  echo; echo "== per condition (normalized per minute)"
  awk -F, 'NR>1 {s[$2]+=$5; p[$2]+=$6; b[$2]+=$7; r[$2]++}
    END {printf "%-10s %5s %12s %14s\n","condition","runs","pkts/min","bytes/min"
         for (c in r) printf "%-10s %5d %12.0f %14.0f\n", c, r[c], p[c]*60/s[c], b[c]*60/s[c]}' "$RUNS"
  echo; echo "== DDS announcers seen in any run (compare with the approved device list)"
  tail -q -n +2 "$OUT"/*_dds_sources.csv 2>/dev/null | cut -d, -f1 | sort -u
}

cmd_wipe() { rm -fv "$REPO"/data/traffic/*/*.pcap; }

case "${1:-}" in
  check)      cmd_check ;;
  run)        shift; cmd_run "$@" ;;
  session)    shift; cmd_session "$@" ;;
  report)     cmd_report ;;
  wipe-pcaps) cmd_wipe ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
